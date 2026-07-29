import asyncio
import json
from datetime import date

from pawmate.core.tools.tool_result_budget import budget_tool_result
from pawmate.tools.core.registry import APPROVAL_NOTIFY, ToolRegistry
from pawmate.tools.web_search import native


def test_native_web_search_falls_back_between_configured_providers(monkeypatch):
    async def failing_adapter(settings, options):
        raise native.NativeWebSearchError("boom", "first provider failed")

    async def succeeding_adapter(settings, options):
        return {
            "answer": "ok from qwen",
            "sources": [{"title": "Example", "url": "https://example.com", "snippet": ""}],
            "raw": {"usage": {"total_tokens": 12}},
        }

    monkeypatch.setattr(native, "_provider_order", lambda provider, allow_fallback=True: ["deepseek", "qwen"])
    monkeypatch.setattr(
        native,
        "_resolve_settings",
        lambda name: native.ProviderSettings(
            name=name,
            api_key="test-key",
            model=f"{name}-model",
            base_url="https://example.test",
            configured=True,
            source={},
        ),
    )
    monkeypatch.setitem(native._ADAPTERS, "deepseek", failing_adapter)
    monkeypatch.setitem(native._ADAPTERS, "qwen", succeeding_adapter)

    result = asyncio.run(native.native_web_search("杭州天气"))

    assert result["ok"] is True
    assert result["provider"] == "qwen"
    assert result["answer"] == "ok from qwen"
    assert result["attempts"][0]["provider"] == "deepseek"
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][1]["status"] == "succeeded"
    assert result["boundary"]["browser_session"] is False


def test_native_web_search_treats_unresolved_provider_tool_markup_as_failure(monkeypatch):
    async def unresolved_adapter(settings, options):
        return {
            "answer": '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="web_search"></｜｜DSML｜｜invoke>',
            "sources": [],
            "raw": {},
        }

    async def succeeding_adapter(settings, options):
        return {
            "answer": "final answer",
            "sources": [{"title": "Evidence", "url": "https://example.com/evidence", "snippet": ""}],
            "raw": {},
        }

    monkeypatch.setattr(native, "_provider_order", lambda provider, allow_fallback=True: ["deepseek", "qwen"])
    monkeypatch.setattr(
        native,
        "_resolve_settings",
        lambda name: native.ProviderSettings(
            name=name,
            api_key="test-key",
            model=f"{name}-model",
            base_url="https://example.test",
            configured=True,
            source={},
        ),
    )
    monkeypatch.setitem(native._ADAPTERS, "deepseek", unresolved_adapter)
    monkeypatch.setitem(native._ADAPTERS, "qwen", succeeding_adapter)

    result = asyncio.run(native.native_web_search("杭州天气"))

    assert result["ok"] is True
    assert result["provider"] == "qwen"
    assert result["attempts"][0]["error_type"] == "unresolved_tool_markup"


def test_native_web_search_skips_missing_keys_before_fallback(monkeypatch):
    async def succeeding_adapter(settings, options):
        return {
            "answer": "ok",
            "sources": [{"title": "Evidence", "url": "https://example.com/evidence", "snippet": ""}],
            "raw": {},
        }

    def resolve_settings(name):
        return native.ProviderSettings(
            name=name,
            api_key="" if name == "deepseek" else "test-key",
            model=f"{name}-model",
            base_url="https://example.test",
            configured=name != "deepseek",
            source={},
        )

    monkeypatch.setattr(native, "_provider_order", lambda provider, allow_fallback=True: ["deepseek", "qwen"])
    monkeypatch.setattr(native, "_resolve_settings", resolve_settings)
    monkeypatch.setitem(native._ADAPTERS, "qwen", succeeding_adapter)

    result = asyncio.run(native.native_web_search("台风最新路径"))

    assert result["ok"] is True
    assert result["provider"] == "qwen"
    assert result["attempts"][0]["status"] == "skipped"
    assert result["attempts"][0]["error_type"] == "not_configured"


def test_native_web_search_rejects_ungrounded_answer_and_falls_back(monkeypatch):
    async def ungrounded_adapter(settings, options):
        return {"answer": "plausible but unsupported", "sources": [], "raw": {}}

    async def grounded_adapter(settings, options):
        return {
            "answer": "supported answer",
            "sources": [{"title": "Official", "url": "https://official.example/report", "snippet": "report"}],
            "raw": {},
        }

    monkeypatch.setattr(native, "_provider_order", lambda provider, allow_fallback=True: ["deepseek", "qwen"])
    monkeypatch.setattr(
        native,
        "_resolve_settings",
        lambda name: native.ProviderSettings(
            name=name,
            api_key="test-key",
            model=f"{name}-model",
            base_url="https://example.test",
            configured=True,
            source={},
        ),
    )
    monkeypatch.setitem(native._ADAPTERS, "deepseek", ungrounded_adapter)
    monkeypatch.setitem(native._ADAPTERS, "qwen", grounded_adapter)

    result = asyncio.run(native.native_web_search("current fact"))

    assert result["ok"] is True
    assert result["provider"] == "qwen"
    assert result["grounded"] is True
    assert result["evidence_count"] == 1
    assert result["sources"][0]["source_id"] == "S1"
    assert result["attempts"][0]["error_type"] == "missing_evidence_sources"


def test_native_web_search_fails_closed_when_all_answers_have_no_sources(monkeypatch):
    async def ungrounded_adapter(settings, options):
        return {"answer": "unsupported", "sources": [], "raw": {}}

    monkeypatch.setattr(native, "_provider_order", lambda provider, allow_fallback=True: ["deepseek"])
    monkeypatch.setattr(
        native,
        "_resolve_settings",
        lambda name: native.ProviderSettings(
            name=name,
            api_key="test-key",
            model=f"{name}-model",
            base_url="https://example.test",
            configured=True,
            source={},
        ),
    )
    monkeypatch.setitem(native._ADAPTERS, "deepseek", ungrounded_adapter)

    result = asyncio.run(native.native_web_search("current fact"))

    assert result["ok"] is False
    assert result["error_type"] == "all_providers_failed"
    assert result["attempts"][0]["error_type"] == "missing_evidence_sources"


def test_source_collector_handles_grounding_metadata():
    payload = {
        "candidates": [
            {
                "groundingMetadata": {
                    "groundingChunks": [
                        {"web": {"uri": "https://weather.example/news", "title": "Weather"}},
                    ]
                }
            }
        ]
    }

    sources = native._collect_sources(payload, limit=3)

    assert sources == [{"title": "Weather", "url": "https://weather.example/news", "snippet": ""}]


def test_query_prompt_carries_current_date_for_relative_time_queries():
    options = native.SearchOptions(
        query="杭州今天天气",
        freshness_days=7,
        max_sources=3,
        forced=True,
        allowed_domains=(),
        blocked_domains=(),
    )

    prompt = native._with_domain_prompt(options)

    assert date.today().isoformat() in prompt
    assert "Resolve relative dates" in prompt


def test_qwen_freshness_is_mapped_to_supported_buckets():
    one_day = native.SearchOptions(
        query="weather",
        freshness_days=1,
        max_sources=3,
        forced=True,
        allowed_domains=(),
        blocked_domains=(),
    )
    two_weeks = native.SearchOptions(
        query="news",
        freshness_days=14,
        max_sources=3,
        forced=True,
        allowed_domains=(),
        blocked_domains=(),
    )

    assert native._search_options_payload(one_day)["freshness"] == 7
    assert native._search_options_payload(two_weeks)["freshness"] == 30


def test_qwen_dashscope_response_extracts_answer_and_sources():
    payload = {
        "output": {
            "choices": [{"message": {"content": "answer text", "role": "assistant"}}],
            "search_info": {
                "search_results": [
                    {"title": "Weather", "url": "https://weather.example/today", "site_name": "WeatherSite"}
                ]
            },
        },
        "usage": {"total_tokens": 10},
    }

    assert native._extract_dashscope_text(payload) == "answer text"
    assert native._collect_sources(payload["output"]["search_info"], limit=2) == [
        {"title": "Weather", "url": "https://weather.example/today", "snippet": ""}
    ]


def test_qwen_dashscope_endpoint_converts_compatible_base_url():
    settings = native.ProviderSettings(
        name="qwen",
        api_key="test-key",
        model="qwen-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        configured=True,
        source={},
    )

    assert native._qwen_dashscope_endpoint(settings) == (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    )


def test_native_web_search_tool_registers_as_notify_readonly_network_tool():
    registry = ToolRegistry()

    native.register_native_web_search_tools(registry)

    tool = registry.get_tool("native_web_search")
    assert tool is not None
    assert registry.get_approval("native_web_search") == APPROVAL_NOTIFY
    assert tool.category == "network"
    assert tool.side_effect == "read_only"


def test_native_web_search_ui_projection_stays_structured_and_compact():
    result = {
        "ok": True,
        "operation": "native_web_search",
        "provider": "qwen",
        "model": "qwen-plus",
        "grounded": True,
        "evidence_count": 3,
        "citation_required": True,
        "query": "杭州今天实时天气",
        "answer": "杭州今天多云，体感较热。" * 50,
        "sources": [
            {"title": "杭州天气预报", "url": "https://www.nmc.cn/publish/forecast/AZJ/hangzhou.html", "snippet": "nmc"},
            {"title": "备用来源", "url": "https://example.com/weather/hangzhou", "snippet": "example"},
            {"title": "第三来源", "url": "https://example.com/third", "snippet": "third"},
        ],
        "attempts": [{"provider": "deepseek", "status": "failed", "message": "x" * 300}],
    }

    views = budget_tool_result("native_web_search", result)
    data = json.loads(views.ui)

    assert data["operation"] == "native_web_search"
    assert data["provider"] == "qwen"
    assert data["grounded"] is True
    assert data["evidence_count"] == 3
    assert len(data["answer"]) <= 240
    assert len(data["sources"]) == 2
    assert len(views.ui) <= 800
