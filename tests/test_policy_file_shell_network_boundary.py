import pytest
import socket

from pawmate.core.safety.path_security import PathSecurityError, sanitize_path
from pawmate.core.runtime.runtime_config import normalize_security_config
from pawmate.core.safety.security_service import SecurityService
from pawmate.storage.app_paths import get_project_root
from pawmate.tools.gateway import builtin_gateway
from pawmate.tools.gateway.builtin_gateway import register_builtin_tools, run_shell_command
from pawmate.tools.downloads.download_tool import download_file
from pawmate.tools.core.registry import APPROVAL_CONFIRM, RiskLevel, SideEffectLevel, ToolCategory, ToolRegistry


def test_default_path_policy_uses_workspace_root():
    root = get_project_root()
    cfg = normalize_security_config({})

    assert cfg["allowed_roots"] == [str(root)]
    assert sanitize_path(str(root / "README.md"), cfg).startswith(str(root))
    with pytest.raises(PathSecurityError):
        sanitize_path(str(root.parent / "outside.txt"), cfg)


def test_legacy_home_root_is_treated_as_workspace_default():
    root = get_project_root()
    cfg = normalize_security_config({"allowed_roots": ["~"]})

    assert cfg["allowed_roots"] == [str(root)]


def test_file_read_boundary_rejects_secret_and_binary(monkeypatch, tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret", encoding="utf-8")
    binary = tmp_path / "image.png"
    binary.write_bytes(b"\x89PNG\x00\x00binary")

    service = SecurityService(lambda: {
        "path_access_mode": "strict",
        "allowed_roots": [str(tmp_path)],
    })
    old_service = builtin_gateway._security_service
    monkeypatch.setattr(builtin_gateway, "_security_service", service)
    try:
        assert builtin_gateway.read_text_file(str(secret)).startswith("[security]")
        assert builtin_gateway.read_text_file(str(binary)).startswith("[security]")
    finally:
        monkeypatch.setattr(builtin_gateway, "_security_service", old_service)


@pytest.mark.asyncio
async def test_shell_boundary_rejects_background_and_destructive_commands():
    background = await run_shell_command("Start-Process notepad")
    destructive = await run_shell_command("rm -rf ./build")

    assert background.startswith("[security]")
    assert destructive.startswith("[security]")


@pytest.mark.asyncio
async def test_download_boundary_rejects_internal_and_unconfirmed_executable(monkeypatch):
    def public_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]

    monkeypatch.setattr("pawmate.core.safety.network_boundary.socket.getaddrinfo", public_dns)

    internal = await download_file("http://127.0.0.1/file.txt")
    executable = await download_file("https://example.com/setup.exe")

    assert internal["error_type"] == "security"
    assert "internal IP" in internal["message"]
    assert executable["error_type"] == "approval_unavailable"
    assert executable["filename"] == "setup.exe"


def test_network_boundary_rejects_domains_resolving_to_internal_ip(monkeypatch):
    from pawmate.core.safety.network_boundary import NetworkBoundaryError, validate_external_http_url

    def private_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 80))]

    monkeypatch.setattr("pawmate.core.safety.network_boundary.socket.getaddrinfo", private_dns)

    with pytest.raises(NetworkBoundaryError, match="internal IP"):
        validate_external_http_url("https://example.com/file.txt")


def test_file_shell_network_tools_have_policy_metadata():
    registry = ToolRegistry()
    register_builtin_tools(registry)

    shell = registry.get_tool("run_shell_command")
    write_file = registry.get_tool("write_file")
    read_file = registry.get_tool("read_text_file")
    download = registry.get_tool("download_file")
    run_tests = registry.get_tool("run_tests")

    assert shell.category == ToolCategory.SHELL.value
    assert shell.risk == RiskLevel.HIGH.value
    assert shell.side_effect == SideEffectLevel.PRIVILEGED.value
    assert shell.approval == APPROVAL_CONFIRM

    assert write_file.category == ToolCategory.FILE.value
    assert write_file.risk == RiskLevel.HIGH.value
    assert write_file.side_effect == SideEffectLevel.LOCAL_WRITE.value
    assert write_file.approval == APPROVAL_CONFIRM

    assert read_file.category == ToolCategory.FILE.value
    assert read_file.side_effect == SideEffectLevel.READ_ONLY.value
    assert read_file.approval == APPROVAL_CONFIRM

    assert download.category == ToolCategory.NETWORK.value
    assert download.risk == RiskLevel.HIGH.value
    assert download.side_effect == SideEffectLevel.LOCAL_WRITE.value
    assert download.approval == APPROVAL_CONFIRM

    assert run_tests.category == ToolCategory.SHELL.value
    assert run_tests.approval == APPROVAL_CONFIRM


def test_compatibility_aliases_are_executable_but_hidden_from_model():
    registry = ToolRegistry()
    register_builtin_tools(registry)

    visible = {item["name"] for item in registry.list_tools()}

    assert {"run_command", "list_dir", "read_file"}.isdisjoint(visible)
    assert all(registry.has_tool(name) for name in ("run_command", "list_dir", "read_file"))
