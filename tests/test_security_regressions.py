from __future__ import annotations

import pytest
import asyncio
import json
import os
import time
from types import SimpleNamespace

from pawmate.bridge.config_bridge import (
    _merge_config,
    _public_config,
    _validate_save_payload,
)


def test_public_config_never_exposes_secrets() -> None:
    source = {
        "llm": {"openai": {"api_key": "sk-secret", "model": "gpt"}},
        "websocket": {"auth_token": "token-value"},
    }
    public = _public_config(source)
    assert public["llm"]["openai"]["api_key"] == ""
    assert public["websocket"]["auth_token"] == ""
    assert public["llm"]["openai"]["model"] == "gpt"


def test_blank_secret_from_ui_preserves_existing_value() -> None:
    existing = {"llm": {"openai": {"api_key": "sk-existing", "model": "old"}}}
    update = {"llm": {"openai": {"api_key": "", "model": "new"}}}
    merged = _merge_config(existing, update)
    assert merged["llm"]["openai"]["api_key"] == "sk-existing"
    assert merged["llm"]["openai"]["model"] == "new"


@pytest.mark.parametrize(
    "api_key",
    [
        "这里不是密钥",
        "sk-key with spaces",
        "x" * 4097,
    ],
)
def test_config_rejects_api_key_values_that_cannot_be_http_headers(api_key: str) -> None:
    with pytest.raises(ValueError, match="API 密钥"):
        _validate_save_payload({
            "llm": {
                "provider": "deepseek",
                "deepseek": {"api_key": api_key, "model": "deepseek-chat"},
            }
        })


def test_llm_factory_rejects_non_ascii_api_key_before_client_creation(
    monkeypatch,
) -> None:
    from pawmate.core.model import llm_factory

    monkeypatch.setattr(
        llm_factory,
        "_load_config",
        lambda: {
            "llm": {
                "provider": "deepseek",
                "deepseek": {
                    "api_key": "误粘贴的整段文字",
                    "model": "deepseek-chat",
                },
            }
        },
    )

    with pytest.raises(RuntimeError, match="只能包含 ASCII"):
        llm_factory.get_llm_client(provider="deepseek")


def test_engine_startup_preserves_provider_configuration_error(monkeypatch) -> None:
    from pawmate.core.runtime.engine import AgentEngine
    from pawmate.main import PawMateApp

    async def fail_create(**_kwargs):
        raise RuntimeError("DEEPSEEK API 密钥格式无效")

    monkeypatch.setattr(AgentEngine, "create", fail_create)

    with pytest.raises(RuntimeError, match="API 密钥格式无效"):
        PawMateApp._create_engine_sync(
            SimpleNamespace(_history_store=None),
            {
                "llm": {
                    "provider": "deepseek",
                    "deepseek": {
                        "api_key": "invalid",
                        "model": "deepseek-chat",
                    },
                }
            },
        )


def test_invalid_non_ascii_key_does_not_count_as_configured() -> None:
    from pawmate.core.model.llm_router import get_available_llm_providers
    from pawmate.main import has_valid_api_key

    invalid_config = {
        "llm": {
            "mode": "auto",
            "provider": "deepseek",
            "deepseek": {
                "api_key": "这不是有效密钥",
                "model": "deepseek-chat",
            },
        }
    }
    assert has_valid_api_key(invalid_config) is False
    assert "deepseek" not in get_available_llm_providers(invalid_config)
    assert has_valid_api_key({
        "llm": {
            "mode": "auto",
            "provider": "deepseek",
            "deepseek": {
                "api_key": "sk-valid-transport-key",
                "model": "deepseek-chat",
            },
        }
    }) is True


def test_saving_valid_key_retries_engine_after_initial_failure(monkeypatch) -> None:
    from pawmate import main

    old_config = {
        "llm": {
            "provider": "deepseek",
            "deepseek": {
                "api_key": "这不是有效密钥",
                "model": "deepseek-chat",
            },
        }
    }
    new_config = {
        "llm": {
            "provider": "deepseek",
            "deepseek": {
                "api_key": "sk-valid-transport-key",
                "model": "deepseek-v4-pro",
            },
        }
    }
    calls = []
    app = SimpleNamespace(
        _cfg=old_config,
        _engine=None,
        _setup_required=False,
        _sync_desktop_pet_from_config=lambda _cfg: None,
        _start_engine_initialization=lambda: calls.append("start"),
    )
    monkeypatch.setattr(main, "ensure_config_file", lambda: new_config)

    main.PawMateApp._on_config_saved(app, "saved")

    assert app._cfg == new_config
    assert calls == ["start"]


def test_reading_normalized_config_does_not_rewrite_secret_file(
    monkeypatch,
    tmp_path,
) -> None:
    from pawmate import main
    from pawmate.storage.secret_codec import protect_config

    config_path = tmp_path / "config.json"
    normalized = main._merge_dict(
        main.DEFAULT_CONFIG,
        {
            "llm": {
                "provider": "deepseek",
                "deepseek": {
                    "api_key": "sk-valid-transport-key",
                    "model": "deepseek-chat",
                },
            },
        },
    )
    config_path.write_text(
        json.dumps(protect_config(normalized), ensure_ascii=False),
        encoding="utf-8",
    )
    before = config_path.read_bytes()
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)

    loaded = main.ensure_config_file()

    assert loaded["llm"]["deepseek"]["api_key"] == "sk-valid-transport-key"
    assert config_path.read_bytes() == before


def test_llm_factory_decrypts_config_secrets(monkeypatch, tmp_path) -> None:
    from pawmate.core.model import llm_factory
    from pawmate.storage.secret_codec import protect_config

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            protect_config(
                {
                    "llm": {
                        "provider": "openai",
                        "openai": {"api_key": "audit-key", "model": "gpt-test"},
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_factory, "CONFIG_PATH", config_path)

    loaded = llm_factory._load_config()

    assert loaded["llm"]["openai"]["api_key"] == "audit-key"


def test_llm_factory_saves_secrets_protected_and_atomically(monkeypatch, tmp_path) -> None:
    from pawmate.core.model import llm_factory
    from pawmate.storage.secret_codec import PREFIX

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(llm_factory, "CONFIG_PATH", config_path)

    llm_factory._save_config(
        {"llm": {"provider": "openai", "openai": {"api_key": "audit-key"}}}
    )

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    stored_key = stored["llm"]["openai"]["api_key"]
    if os.name == "nt":
        assert stored_key.startswith(PREFIX)
    else:
        assert stored_key == "audit-key"
    assert not config_path.with_suffix(".json.tmp").exists()


def test_quiz_rejects_untrusted_model_endpoint(monkeypatch, tmp_path) -> None:
    app = pytest.importorskip(
        "quiz_app.app",
        reason="quiz_app is not part of the public PawMate core package",
    )

    monkeypatch.setattr(app, "SETTINGS_PATH", tmp_path / "settings.json")
    with pytest.raises(ValueError, match="Untrusted"):
        app.update_deepseek_config({"baseUrl": "https://attacker.invalid"})


def test_websocket_server_requires_token() -> None:
    from pawmate.bridge.ws_server import WsChatServer

    with pytest.raises(ValueError, match="auth_token"):
        WsChatServer(object(), auth_token="")


@pytest.mark.asyncio
async def test_sync_tool_handler_does_not_block_timeout() -> None:
    from pawmate.tools.core.registry import ToolDef, ToolRegistry

    registry = ToolRegistry()

    def blocking_handler():
        time.sleep(0.2)
        return "done"

    registry.register(ToolDef(
        name="blocking_read",
        description="blocking test",
        input_schema={"type": "object", "properties": {}},
        handler=blocking_handler,
        timeout=0.03,
    ))
    started = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError):
        await registry.execute("blocking_read", {})
    assert time.perf_counter() - started < 0.15


@pytest.mark.asyncio
async def test_side_effect_tool_is_never_automatically_retried() -> None:
    from pawmate.core.tools.tool_call_runner import ToolCallRunner
    from pawmate.tools.core.registry import SideEffectLevel, ToolDef, ToolRegistry

    registry = ToolRegistry()
    calls = 0

    async def ambiguous_write():
        nonlocal calls
        calls += 1
        raise ConnectionError("commit outcome unknown")

    registry.register(ToolDef(
        name="ambiguous_write",
        description="write test",
        input_schema={"type": "object", "properties": {}},
        handler=ambiguous_write,
        side_effect=SideEffectLevel.LOCAL_WRITE,
        timeout=1,
    ))
    outcome = await ToolCallRunner(registry, max_retries=3, retry_delay=0).run("ambiguous_write", {})
    assert outcome.success is False
    assert calls == 1


def test_external_model_visible_tools_are_forced_to_confirm() -> None:
    from pawmate.tools.core.registry import APPROVAL_AUTO, APPROVAL_CONFIRM, ToolDef

    tool = ToolDef(
        name="get_status",
        description="untrusted MCP description",
        input_schema={"type": "object", "properties": {}},
        handler=lambda: "ok",
        approval=APPROVAL_AUTO,
        source="mcp:untrusted",
    )
    assert tool.approval == APPROVAL_CONFIRM
    assert tool.requires_confirm is True


@pytest.mark.asyncio
async def test_tool_argument_size_limit() -> None:
    from pawmate.tools.core.registry import ToolDef, ToolRegistry

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="echo_limited",
        description="input limit test",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=lambda text: text,
    ))
    with pytest.raises(ValueError, match="64 KiB"):
        await registry.execute("echo_limited", {"text": "x" * (70 * 1024)})
