import subprocess

from backend.services.sandbox_service import SandboxVerificationService


def test_sandbox_rejects_missing_reproduction_test(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.services.sandbox_service.shutil.which", lambda _: "/usr/bin/docker")
    result = SandboxVerificationService().verify_patch(
        original_repo_path=str(tmp_path),
        patch_str="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+b=2\n",
        generated_test="",
    )
    assert result["verified"] is False
    assert result["status"] == "missing_reproduction_test"


def test_sandbox_fails_closed_when_docker_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.services.sandbox_service.shutil.which", lambda _: None)
    result = SandboxVerificationService().verify_patch(
        original_repo_path=str(tmp_path),
        patch_str="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+b=2\n",
        generated_test="def test_fix():\n    assert True\n",
    )
    assert result["verified"] is False
    assert result["status"] == "sandbox_unavailable"
    assert "not executed on the host" in result["reason"]


def test_sandbox_rejects_complete_source_instead_of_diff(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.services.sandbox_service.shutil.which", lambda _: "/usr/bin/docker")
    result = SandboxVerificationService().verify_patch(
        original_repo_path=str(tmp_path),
        patch_str="def fixed():\n    return True\n",
        generated_test="def test_fix():\n    assert True\n",
    )
    assert result["verified"] is False
    assert result["status"] == "invalid_patch_format"


def test_assertion_failure_classification_rejects_setup_errors():
    assert SandboxVerificationService._is_assertion_failure("FAILED tests/test_x.py::test_x - AssertionError")
    assert not SandboxVerificationService._is_assertion_failure("ERROR collecting tests/test_x.py\nModuleNotFoundError")
    assert not SandboxVerificationService._is_assertion_failure("collected 0 items")


def test_windows_docker_pipe_connection_error_is_sandbox_unavailable(tmp_path, monkeypatch):
    from backend.services import sandbox_service

    monkeypatch.setattr(sandbox_service.shutil, "which", lambda _: "/usr/bin/docker")
    output = (
        "failed to connect to the docker API at "
        "npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct "
        "and if the daemon is running: open //./pipe/dockerDesktopLinuxEngine: "
        "The system cannot find the file specified."
    )

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", output)

    monkeypatch.setattr(sandbox_service.subprocess, "run", run)
    workspace = tmp_path / "repo"
    workspace.mkdir()

    result = SandboxVerificationService()._run_pytest(workspace, None)

    assert result["status"] == "sandbox_unavailable"
    assert "dockerDesktopLinuxEngine" in result["output"]


def test_shared_sandbox_volume_mount_is_scoped_to_one_workspace(tmp_path, monkeypatch):
    from backend.services import sandbox_service

    volume_root = tmp_path / "shared-volume"
    sandbox_parent = volume_root / "rca_sandbox_example"
    workspace = sandbox_parent / "repo"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(sandbox_service, "SANDBOX_VOLUME", "agentic-rca-sandbox-workspaces")
    monkeypatch.setattr(sandbox_service, "SANDBOX_WORKDIR", str(volume_root))
    monkeypatch.setattr(sandbox_service.shutil, "which", lambda _: "/usr/bin/docker")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "1 passed", "")

    monkeypatch.setattr(sandbox_service.subprocess, "run", run)
    service = SandboxVerificationService()
    result = service._run_pytest(workspace, None)

    assert result["status"] == "completed"
    assert "--network" in commands[0]
    mount = commands[0][commands[0].index("--mount") + 1]
    assert "source=agentic-rca-sandbox-workspaces" in mount
    assert "volume-subpath=rca_sandbox_example" in mount
    assert commands[0][commands[0].index("-w") + 1] == "/sandbox-workspaces/repo"
