import os
import subprocess
import requests
import logging

logger = logging.getLogger(__name__)


class GitHubService:
    def __init__(self, repo_owner: str = "Archit-d300", repo_name: str = "Agentic-RCA"):
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.base_url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}"
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def create_pull_request(
        self,
        incident_id: str,
        patch_str: str,
        forensic_evidence: str,
        verification_summary: str,
        repo_path: str = "."
    ) -> dict:
        """
        Completes the full GitHub workflow:
        1. Creates a new git feature branch locally.
        2. Applies the patch and commits changes.
        3. Pushes the branch to remote repository.
        4. Checks for existing open PRs (prevents duplicate PRs).
        5. Opens a GitHub Pull Request with mandatory human review (no auto-merge).
        """
        branch_name = f"fix/{incident_id}"
        
        try:
            # Step A: Local Git Workflow - Create branch, commit, push
            # 1. Checkout new branch
            subprocess.run(["git", "checkout", "-b", branch_name], cwd=repo_path, check=True, capture_output=True)
            
            # 2. Write patch to temporary file and apply
            patch_file = os.path.join(repo_path, "temp_patch.patch")
            with open(patch_file, "w", encoding="utf-8") as f:
                f.write(patch_str)
            
            subprocess.run(["git", "apply", "--ignore-whitespace", "temp_patch.patch"], cwd=repo_path, check=True, capture_output=True)
            if os.path.exists(patch_file):
                os.remove(patch_file)

            # 3. Stage and commit
            subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
            commit_msg = f"fix(rca): automated resolution for incident {incident_id}"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_path, check=True, capture_output=True)

            # 4. Push branch to remote
            push_res = subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            if push_res.returncode != 0 and "already exists" not in push_res.stderr:
                logger.warning(f"Git push warning: {push_res.stderr}")

        except Exception as exc:
            logger.error(f"Git operations failed during PR preparation: {exc}")
            # Continue to API flow if branch was already pushed or handled externally

        # Step B: Duplicate PR Prevention Check
        if self.token:
            try:
                existing_prs = requests.get(
                    f"{self.base_url}/pulls?state=open&head={self.repo_owner}:{branch_name}",
                    headers=self.headers,
                    timeout=10
                )
                if existing_prs.status_code == 200 and len(existing_prs.json()) > 0:
                    pr_data = existing_prs.json()[0]
                    return {
                        "status": "exists",
                        "pr_url": pr_data.get("html_url"),
                        "message": "Pull Request already exists for this branch."
                    }
            except requests.RequestException as req_err:
                logger.warning(f"Failed to check existing PRs: {req_err}")

        # Step C: Create Pull Request via GitHub REST API
        pr_title = f"fix: automated patch resolution for incident {incident_id}"
        pr_body = (
            f"## Automated RCA Fix Request\n\n"
            f"**Incident ID:** `{incident_id}`\n\n"
            f"### Forensic Evidence & Root Cause\n{forensic_evidence}\n\n"
            f"### Verification Summary\n{verification_summary}\n\n"
            f"> **Note:** This Pull Request requires mandatory human review. Auto-merge is disabled."
        )

        payload = {
            "title": pr_title,
            "head": branch_name,
            "base": "main",
            "body": pr_body,
            "draft": False
        }

        try:
            response = requests.post(
                f"{self.base_url}/pulls",
                json=payload,
                headers=self.headers,
                timeout=15
            )

            if response.status_code in [200, 201]:
                res_json = response.json()
                return {
                    "status": "success",
                    "pr_url": res_json.get("html_url"),
                    "pr_number": res_json.get("number")
                }
            else:
                return {
                    "status": "failed",
                    "reason": f"GitHub API Error ({response.status_code}): {response.text}"
                }

        except requests.RequestException as err:
            return {
                "status": "error",
                "reason": f"GitHub API Request failed: {str(err)}"
            }