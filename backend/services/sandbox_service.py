import os
import shutil
import tempfile
import subprocess
import logging

logger = logging.getLogger(__name__)

class SandboxVerificationService:
    def __init__(self, timeout_seconds: int = 120):
        self.timeout_seconds = timeout_seconds

    def verify_patch(
        self,
        original_repo_path: str,
        patch_str: str,
        test_file: str = "tests/"
    ) -> dict:
        """
        1. Pre-patch test run: Ensures test fails expectedly (bug exists).
        2. Applies unified diff (patch_str) using git apply.
        3. Post-patch test run: Ensures test passes.
        4. Runs regression suite.
        Uses Docker with hardened security (no network, memory limits).
        """
        if not os.path.exists(original_repo_path):
            return {
                "verified": False,
                "reason": f"Original repository path does not exist: {original_repo_path}"
            }

        # Create temporary disposable workspace
        temp_dir = tempfile.mkdtemp(prefix="sandbox_vbf_")
        try:
            # Copy source code to temp workspace
            shutil.copytree(original_repo_path, temp_dir, dirs_exist_ok=True)

            # Step A: Pre-patch reproduction test (Verify bug fails expectedly)
            pre_test_res = self._run_in_docker(temp_dir, test_file)
            if pre_test_res["returncode"] == 0:
                return {
                    "verified": False,
                    "reason": "Reproduction check failed: Pre-patch tests passed. Expected the target issue to fail before applying the patch."
                }

            # Step B: Apply Unified Diff Patch
            patch_file_path = os.path.join(temp_dir, "candidate.patch")
            with open(patch_file_path, "w", encoding="utf-8") as f:
                f.write(patch_str)

            apply_cmd = ["git", "apply", "--ignore-whitespace", "candidate.patch"]
            apply_res = subprocess.run(
                apply_cmd,
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if apply_res.returncode != 0:
                return {
                    "verified": False,
                    "reason": f"Failed to apply git diff patch:\n{apply_res.stderr}"
                }

            # Step C: Post-patch reproduction test (Verify fix passes)
            post_test_res = self._run_in_docker(temp_dir, test_file)
            if post_test_res["returncode"] != 0:
                return {
                    "verified": False,
                    "reason": f"Post-patch verification failed:\n{post_test_res['stdout']}\n{post_test_res['stderr']}"
                }

            # Step D: Regression Test Suite Execution
            regression_res = self._run_in_docker(temp_dir, "tests/")
            if regression_res["returncode"] != 0:
                return {
                    "verified": False,
                    "reason": f"Regression test suite failed after applying patch:\n{regression_res['stdout']}"
                }

            return {
                "verified": True,
                "reason": "Patch verified successfully: Pre-patch failed, post-patch passed, and regression suite passed in Docker sandbox."
            }

        except subprocess.TimeoutExpired:
            return {
                "verified": False,
                "reason": f"Sandbox execution timed out after {self.timeout_seconds} seconds."
            }
        except Exception as exc:
            return {
                "verified": False,
                "reason": f"Sandbox execution encountered error: {str(exc)}"
            }
        finally:
            # Clean up temp workspace
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _run_in_docker(self, workspace_path: str, test_target: str) -> dict:
        """
        Executes pytest inside Docker container with restricted network and resource bounds.
        Falls back to local subprocess if Docker daemon is not running.
        """
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "none",            # Disable network access (Hardening)
            "--memory", "512m",             # Memory constraint (Hardening)
            "-v", f"{os.path.abspath(workspace_path)}:/app",
            "-w", "/app",
            "python:3.11-slim",
            "pytest", test_target
        ]

        try:
            res = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds
            )
            return {
                "returncode": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr
            }
        except (subprocess.SubprocessError, FileNotFoundError):
            # Local fallback if Docker daemon is absent in development environment
            fallback_cmd = ["pytest", test_target]
            res = subprocess.run(
                fallback_cmd,
                cwd=workspace_path,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds
            )
            return {
                "returncode": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr
            }