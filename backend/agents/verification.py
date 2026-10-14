"""Stage 5: candidate validation and reflection-loop decision with Sandbox & GitHub Integration."""

import ast
import json
from backend.agents.state import AgentState
from backend.services.sandbox_service import SandboxVerificationService
from backend.services.github_service import GitHubService

MAX_REFLECTIONS = 1


def verification(state: AgentState) -> AgentState:
    patched = state.get("patched_code", "")
    patch_file = state.get("patch_file", "")
    diff_text = state.get("diff_text", "") or state.get("patch_diff", "")
    count = int(state.get("reflection_count", 0) or 0)
    
    repo_path = state.get("repo_path", ".")
    incident_id = state.get("incident_id", "demo-incident")
    test_file = state.get("test_file_name", "tests/")
    logs: list[str] = []

    # 1. Verification stop if no patch present
    if not patched:
        return {
            "verification_passed": None,
            "regression_passed": None,
            "verification_mode": "pre_isolation",
            "verification_feedback": "No candidate patch or diff was produced.",
            "needs_reflection": False,
            "current_stage": "verification",
            "stage_logs": ["verification stopped: no candidate patch"],
        }

    # 2. Static AST Syntax Check
    if patched and patch_file.endswith(".py"):
        try:
            ast.parse(patched)
            logs.append("candidate Python source parses successfully")
        except SyntaxError as exc:
            feedback = f"Candidate syntax validation failed: {exc}. Return a corrected complete file."
            retry = count < MAX_REFLECTIONS
            logs.append(feedback)
            return {
                "verification_passed": False,
                "regression_passed": None,
                "verification_mode": "static_validation",
                "verification_feedback": feedback,
                "reflection_count": count + 1 if retry else count,
                "needs_reflection": retry,
                "verification_log": "\n".join(logs),
                "current_stage": "verification",
                "stage_logs": logs,
            }

    # 3. Dynamic Isolation Check via Sandbox Service (Docker + Disposable Copy)
    patch_payload = diff_text if diff_text else patched
    sandbox = SandboxVerificationService()
    sandbox_res = sandbox.verify_patch(
        original_repo_path=repo_path,
        patch_str=patch_payload,
        test_file=test_file
    )

    test_passed = sandbox_res.get("verified", False)
    test_output = sandbox_res.get("reason", "")

    # 4. Handle Failure & Reflection Loop Decision
    if not test_passed:
        feedback = (
            "The isolated patch test did not pass in Docker sandbox. "
            "Revise the patch using this execution output:\n" + test_output[-5000:]
        )
        retry = count < MAX_REFLECTIONS
        logs.append(f"sandbox verification failed: {test_output}")
        logs.append("reflection retry requested" if retry else "reflection limit reached")
        
        return {
            "verification_passed": False,
            "regression_passed": False,
            "verification_mode": "sandbox",
            "verification_feedback": feedback,
            "reflection_count": count + 1 if retry else count,
            "needs_reflection": retry,
            "verification_log": "\n".join(logs),
            "current_stage": "verification",
            "stage_logs": logs,
        }

    # 5. On Successful Sandbox Verification: Create GitHub Pull Request
    logs.append("patch and regression checks passed in Docker sandbox")
    logs.append("Opening GitHub PR for human-gated approval...")

    github_svc = GitHubService()
    pr_res = github_svc.create_pull_request(
        incident_id=incident_id,
        patch_str=patch_payload,
        forensic_evidence=state.get("root_cause_explanation", "Forensic analysis completed."),
        verification_summary=test_output
    )

    pr_url = pr_res.get("pr_url")
    if pr_url:
        logs.append(f"GitHub Pull Request opened successfully: {pr_url}")

    return {
        "verification_passed": True,
        "regression_passed": True,
        "verification_mode": "sandbox",
        "verification_feedback": "",
        "needs_reflection": False,
        "pr_url": pr_url,
        "verification_log": "\n".join(logs),
        "current_stage": "verification",
        "stage_logs": logs,
    }