"""Fail-closed Docker sandbox for candidate patch verification.

The candidate is applied only to a disposable copy. No host execution fallback is
permitted: if Docker or the configured image is unavailable, verification fails.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from dotenv import load_dotenv

load_dotenv()
from typing import Any

DEFAULT_IMAGE = os.getenv("RCA_SANDBOX_IMAGE", "agentic-rca-sandbox:py311")
SANDBOX_VOLUME = os.getenv("RCA_SANDBOX_VOLUME")
SANDBOX_WORKDIR = os.getenv("RCA_SANDBOX_WORKDIR")
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


class SandboxVerificationService:
    def __init__(self, timeout_seconds: int = 120, image: str | None = None):
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.image = image or DEFAULT_IMAGE
        self.sandbox_volume = SANDBOX_VOLUME
        self.sandbox_workdir = Path(SANDBOX_WORKDIR).resolve() if SANDBOX_WORKDIR else None

    def verify_patch(
        self,
        original_repo_path: str,
        patch_str: str,
        test_file: str = "tests/test_generated_reproduction.py",
        generated_test: str = "",
    ) -> dict[str, Any]:
        source = Path(original_repo_path).resolve()
        if not source.is_dir():
            return self._failure("invalid_repository", f"Repository path does not exist: {source}")
        if not patch_str or not patch_str.strip() or patch_str.strip() == "(no diff)":
            return self._failure("invalid_patch", "No unified diff was supplied.")
        if not generated_test.strip():
            return self._failure("missing_reproduction_test", "No generated reproduction test was supplied; refusing to claim verification.")

        safe_test_path = self._safe_test_path(test_file)
        if safe_test_path is None:
            return self._failure("invalid_test_path", "Generated test path must be a safe relative Python path.")
        if not self._is_unified_diff(patch_str):
            return self._failure("invalid_patch_format", "Expected a unified diff in diff_text, not complete source code.")
        if not shutil.which("docker"):
            return self._failure("sandbox_unavailable", "Docker executable is unavailable. Candidate code was not executed on the host.")
        if bool(self.sandbox_volume) != bool(self.sandbox_workdir):
            return self._failure(
                "sandbox_setup_error",
                "RCA_SANDBOX_VOLUME and RCA_SANDBOX_WORKDIR must be configured together.",
            )

        if self.sandbox_workdir:
            try:
                self.sandbox_workdir.mkdir(parents=True, exist_ok=True)
                temp_parent = Path(
                    tempfile.mkdtemp(prefix="rca_sandbox_", dir=self.sandbox_workdir)
                )
            except OSError as exc:
                return self._failure(
                    "sandbox_setup_error",
                    f"Could not prepare the shared sandbox workspace: {exc}",
                )
        else:
            temp_parent = Path(tempfile.mkdtemp(prefix="rca_sandbox_"))
        workspace = temp_parent / "repo"
        try:
            shutil.copytree(
                source,
                workspace,
                ignore=shutil.ignore_patterns(*EXCLUDED_DIRS, "*.pyc", ".env", ".env.*"),
                dirs_exist_ok=True,
            )
            # A fresh git repository lets `git apply` work even for uploaded ZIPs without .git.
            init = subprocess.run(["git", "init", "-q"], cwd=workspace, capture_output=True, text=True, timeout=10)
            if init.returncode != 0:
                return self._failure("git_unavailable", init.stderr or "Could not initialize disposable repository.")

            generated_path = workspace / safe_test_path
            generated_path.parent.mkdir(parents=True, exist_ok=True)
            generated_path.write_text(generated_test, encoding="utf-8")

            pre = self._run_pytest(workspace, safe_test_path)
            if pre["status"] != "completed":
                return self._failure(pre["status"], f"Pre-patch reproduction setup/execution failed; this does not establish the defect.\n{pre['output']}")
            if pre["returncode"] == 0:
                return self._failure("not_reproduced", "The generated reproduction test passed before the patch; the bug was not demonstrated.\n" + pre["output"])
            if not self._is_assertion_failure(pre["output"]):
                return self._failure("reproduction_setup_error", "The pre-patch test failed for a reason other than a normal test assertion.\n" + pre["output"])

            patch_path = temp_parent / "candidate.patch"
            patch_path.write_text(patch_str, encoding="utf-8")
            check = subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=workspace, capture_output=True, text=True, timeout=15)
            if check.returncode != 0:
                return self._failure("invalid_patch", f"Patch does not apply cleanly: {check.stderr[-4000:]}")
            apply = subprocess.run(["git", "apply", str(patch_path)], cwd=workspace, capture_output=True, text=True, timeout=15)
            if apply.returncode != 0:
                return self._failure("invalid_patch", f"Patch application failed: {apply.stderr[-4000:]}")

            post = self._run_pytest(workspace, safe_test_path)
            if post["status"] != "completed" or post["returncode"] != 0:
                return self._failure(post["status"] if post["status"] != "completed" else "reproduction_after_patch_failed", "Reproduction test did not pass after the patch.\n" + post["output"])

            regression = self._run_pytest(workspace, None)
            if regression["status"] != "completed" or regression["returncode"] != 0:
                return self._failure(regression["status"] if regression["status"] != "completed" else "regression_failed", "Full regression suite did not pass after the patch.\n" + regression["output"])

            return {
                "verified": True,
                "status": "verified",
                "error_category": None,
                "reproduction_before_patch": "failed_as_expected",
                "reproduction_after_patch": "passed",
                "regression_passed": True,
                "reason": "Verified in Docker: reproduction failed before the patch, passed after the patch, and the full regression suite passed.",
                "pre_patch_output": pre["output"][-5000:],
                "post_patch_output": post["output"][-5000:],
                "regression_output": regression["output"][-8000:],
            }
        except subprocess.TimeoutExpired as exc:
            return self._failure("timeout", f"Sandbox command timed out after {self.timeout_seconds}s: {exc.cmd}")
        except Exception as exc:
            return self._failure("sandbox_error", f"Sandbox verification failed safely: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(temp_parent, ignore_errors=True)

    def _run_pytest(self, workspace: Path, target: str | None) -> dict[str, Any]:
        if not workspace.exists():
            return {"status": "workspace_error", "returncode": -1, "output": "Sandbox workspace is missing."}
        uid = os.getuid() if hasattr(os, "getuid") else 65534
        gid = os.getgid() if hasattr(os, "getgid") else 65534
        # Never run candidate code as root, even when the API server itself runs as root.
        if uid == 0:
            uid, gid = 65534, 65534
        # Only the disposable copy is made writable for the unprivileged container user.
        for current, dirs, files in os.walk(workspace):
            try:
                os.chmod(current, 0o777)
            except OSError:
                pass
            for filename in files:
                try:
                    os.chmod(Path(current) / filename, 0o666)
                except OSError:
                    pass
        args = [
            "docker", "run", "--rm", "--network", "none",
            "--memory", "512m", "--cpus", "1.0", "--pids-limit", "128",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--user", f"{uid}:{gid}",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        ]
        if self.sandbox_volume and self.sandbox_workdir:
            relative_parent = workspace.parent.relative_to(self.sandbox_workdir).as_posix()
            container_workspace = PurePosixPath("/sandbox-workspaces") / workspace.name
            args.extend(
                [
                    "--mount",
                    (
                        "type=volume,"
                        f"source={self.sandbox_volume},"
                        "target=/sandbox-workspaces,"
                        f"volume-subpath={relative_parent}"
                    ),
                    "-w",
                    str(container_workspace),
                ]
            )
        else:
            args.extend(["-v", f"{workspace.resolve()}:/workspace:rw", "-w", "/workspace"])
        args.extend([self.image, "python", "-m", "pytest", "-q"])
        if target:
            args.append(target)
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout_seconds)
        except FileNotFoundError:
            return {"status": "sandbox_unavailable", "returncode": -1, "output": "Docker executable is unavailable."}
        except subprocess.TimeoutExpired as exc:
            return {"status": "timeout", "returncode": -1, "output": ((exc.stdout or "") + "\n" + (exc.stderr or "") + "\nSandbox execution timed out.")[-8000:]}
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if (
            "unable to find image" in output.lower()
            or "pull access denied" in output.lower()
            or SandboxVerificationService._is_docker_unavailable(output)
        ):
            return {"status": "sandbox_unavailable", "returncode": result.returncode, "output": output[-8000:]}
        if "No module named pytest" in output or "No module named 'pytest'" in output:
            return {"status": "sandbox_setup_error", "returncode": result.returncode, "output": output[-8000:]}
        return {"status": "completed", "returncode": result.returncode, "output": output[-12000:]}

    @staticmethod
    def _is_docker_unavailable(output: str) -> bool:
        lower = output.lower()
        return any(
            marker in lower
            for marker in (
                "cannot connect to the docker daemon",
                "permission denied while trying to connect to the docker daemon",
                "failed to connect to the docker api",
                "failed to connect to the docker daemon",
                "error during connect",
                "dockerdesktoplinuxengine",
            )
        )

    @staticmethod
    def _safe_test_path(value: str) -> str | None:
        # Generated test is always placed under tests/; ignore legacy placeholder names.
        candidate = PurePosixPath((value or "").replace("\\", "/"))
        if value.startswith("generated::") or not value:
            return "tests/test_generated_reproduction.py"
        if candidate.is_absolute() or ".." in candidate.parts or not str(candidate).endswith(".py"):
            return None
        return str(candidate) if str(candidate).startswith("tests/") else "tests/test_generated_reproduction.py"

    @staticmethod
    def _is_unified_diff(text: str) -> bool:
        return "--- a/" in text and "+++ b/" in text and "@@" in text

    @staticmethod
    def _is_assertion_failure(output: str) -> bool:
        lower = output.lower()
        if any(token in lower for token in ("error collecting", "errors during collection", "modulenotfounderror", "importerror", "no module named", "syntaxerror", "collected 0 items")):
            return False
        return " failed" in lower or " assertionerror" in lower or "assertionerror" in lower

    @staticmethod
    def _failure(category: str, reason: str) -> dict[str, Any]:
        return {
            "verified": False,
            "status": category,
            "error_category": category,
            "regression_passed": False if category in {"regression_failed", "timeout", "sandbox_unavailable", "sandbox_setup_error", "sandbox_error"} else None,
            "reason": reason[-12000:],
        }
