"""LLM 工厂：从 config.json 加载并创建提供商客户端。

配置优先级（高 → 低）：
  1. kwargs 显式传入值
  2. config.json / config_store（SettingsDrawer 保存值）
  3. os.getenv 环境变量 fallback
  4. DEFAULT_LLM_CONFIG fallback
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from pawmate.core import providers  # noqa: F401
from pawmate.core.llm_provider import LLMProviderFactory
from pawmate.core.model_catalog import (
    build_default_llm_config,
    normalize_llm_config_keys,
    normalize_provider_key,
)

_logger = logging.getLogger("pawmate")

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

DEFAULT_SYSTEM_PROMPT = (
    "你是 PawMate，一个运行在用户本地桌面端的 AI 助手。"
    "你可以帮助用户完成聊天、文件阅读、网页自动化、工具调用、配置管理和工作流辅助。"
    "你应该用自然、清晰、可靠的方式回答问题。"
    "\n\n"
    "首次和用户对话时，如果用户没有告诉你希望如何称呼你，"
    "你可以简短介绍自己，并询问用户希望怎么称呼你。"
    "例如：「你好，我是 PawMate。你可以把我当作本地桌面 AI 助手。你希望我怎么称呼你？」"
    "\n\n"
    "规则：\n"
    "1. 不要声称自己还只是 Web UI 原型；\n"
    "2. 不要说「还没有接入真实 AI」，除非系统确实处于 mock 模式；\n"
    "3. 不要暴露内部实现细节，除非用户询问；\n"
    "4. 如果正在处理工具调用或文件读取，用简洁方式说明进度；\n"
    "5. 如果用户只是普通聊天，直接自然回答；\n"
    "6. 默认使用中文回答，除非用户明确要求其他语言；\n"
    "7. 回答要务实、清楚，避免空泛。"
    "\n\n"
    "▎工具调用"
    "\n"
    "你可以读取文件、运行命令、搜索网页、填写表单、点击按钮——像真人一样操作电脑。"
    "\n\n"
    "浏览器系列工具只暴露 4 个意图级动词：\n"
    "• browser_goto(url, use_my_login?, goal?) — 打开网址；需要用户登录态时 use_my_login=true\n"
    "• browser_read() — 读取当前页可操作元素，返回 ref、文本、链接和页面类型\n"
    "• browser_act(ref?, action?, value?, intent?, key?, keys?, deltaX?, deltaY?, x?, y?, text?, clear?) — DOM 模式支持 click/type/fill/key/hotkey/scroll/click_xy/insert_text/smart_type；视觉模式用 intent 描述目标\n"
    "• browser_extract(query?) — 抽取当前页正文/文本\n"
    "\n"
    "Browser automation priority: use the four browser facade tools for browser, logged-in, Edge/Chrome, extraction, click, and form tasks. Do not call legacy low-level browser_* or native_browser_* tools; they are internal adapter functions only. "
    "If browser_goto/read/act/extract cannot attach, observe, locate, or operate, stop and explain the typed blocker to the user.\n"
    "浏览器操作规则（必须严格遵守）：\n"
    "- 需要操作浏览器时，一律调用 browser_goto / browser_read / browser_act / browser_extract。\n"
    "- 禁止用 run_shell_command 手动启动浏览器，例如 msedge --remote-debugging-port=9222。\n"
    "- 禁止要求用户关闭他正在使用的 Edge/Chrome，禁止要求用户手动加调试端口重启浏览器。\n"
    "- 用户明确需要已有登录态时，browser_goto 传 use_my_login=true；若挂载失败，停下说明原因，不要静默切到托管浏览器。\n"
    "- 若 browser_goto 报错，停下并把真实错误告诉用户，不要转去 run_shell_command 或坐标盲操。\n"
    "\n"
    "遇到链接/网页任务时，优先用 browser_goto 而不是 open_url。"
    "登录后台、客服系统、看/找视频等需要用户登录态的任务，在 browser_goto 中显式传 use_my_login=true。"
    "\n\n"
    "▎常用工作流：\n"
    "- 查资料：browser_goto → browser_read → browser_extract\n"
    "- 登录/客服/后台：browser_goto(use_my_login=true) → browser_read → browser_act\n"
    "- 页面点击：browser_goto → browser_read 获取 ref → browser_act；如果 facade 失败，停下说明原因\n"
    "\n"
    "▎重要约束：\n"
    "- 禁止伪调用，需要执行就发起真实工具调用\n"
    "- 下载文件用 download_file / download_with_metadata\n"
    "- 桌面应用用 launch_desktop_app\n"
    "- 不要主动创建 .bat / .ps1 / .cmd 安装脚本\n"
    "- 不要诱导用户双击未知脚本或可执行文件\n"
    "- 安装类操作只能通过 run_shell_command 在用户授权后执行\n"
)

DEFAULT_LLM_CONFIG: Dict[str, Any] = build_default_llm_config()

# ── helpers ──────────────────────────────────────────────────────


def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"llm": DEFAULT_LLM_CONFIG}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"llm": DEFAULT_LLM_CONFIG}


def _save_config(config: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    current_llm = config.get("llm", {})
    current_llm = normalize_llm_config_keys(current_llm if isinstance(current_llm, dict) else {})
    normalized = _merge_dict(DEFAULT_LLM_CONFIG, current_llm)
    result = dict(config)
    result["llm"] = normalized
    return result


def _is_invalid_key(value: str) -> bool:
    value = value.strip()
    if not value:
        return True
    lowered = value.lower()
    return ("placeholder" in lowered) or ("edit-me" in lowered)


# ── source-of-truth helpers ──────────────────────────────────────


def _non_empty(value: Optional[str]) -> Optional[str]:
    """Return value if it is non-empty and not a placeholder, else None."""
    if value and not _is_invalid_key(value.strip()):
        return value.strip()
    return None


def _source_label(source: str) -> str:
    return source  # "kwargs", "config", "env", "default"


# ── pure config resolution (testable without IO/API) ─────────────


def resolve_llm_config(
    provider: str,
    provider_config: dict,
    defaults: dict,
    kwargs: dict,
    env_getter=None,
) -> Dict[str, Any]:
    """Resolve LLM configuration fields with correct priority.

    Parameters
    ----------
    provider : str
        Lower-cased provider name (e.g. "deepseek").
    provider_config : dict
        Provider-specific section from config.json or config_store.
    defaults : dict
        DEFAULT_LLM_CONFIG[provider] fallback.
    kwargs : dict
        Override arguments passed to get_llm_client(**kwargs).
    env_getter : callable or None
        Function to get env var by name (default: os.getenv).

    Returns
    -------
    dict with keys: api_key, model, base_url, extra_kwargs, sources
        sources is a dict: {"api_key": str, "model": str, "base_url": str}
    """
    if env_getter is None:
        env_getter = os.getenv

    env_prefix = provider.upper()
    sources: Dict[str, str] = {}
    result: Dict[str, Any] = {}

    # ── api_key ──────────────────────────────────────────────
    # Priority: kwargs > config (non-empty) > env > default
    api_key = _non_empty(kwargs.get("api_key"))
    sources["api_key"] = "kwargs" if api_key else None

    if api_key is None:
        api_key = _non_empty(provider_config.get("api_key"))
        sources["api_key"] = "config" if api_key else None

    if api_key is None:
        api_key = _non_empty(env_getter(f"{env_prefix}_API_KEY"))
        sources["api_key"] = "env" if api_key else None

    if api_key is None:
        api_key = _non_empty(defaults.get("api_key"))
        sources["api_key"] = "default" if api_key else None

    result["api_key"] = api_key or ""
    result["api_key_present"] = bool(api_key and not _is_invalid_key(api_key))

    # ── model ────────────────────────────────────────────────
    model = _non_empty(kwargs.get("model"))
    sources["model"] = "kwargs" if model else None

    if model is None:
        model = _non_empty(provider_config.get("model"))
        sources["model"] = "config" if model else None

    if model is None:
        model = _non_empty(env_getter(f"{env_prefix}_MODEL"))
        sources["model"] = "env" if model else None

    if model is None:
        model = _non_empty(defaults.get("model"))
        sources["model"] = "default" if model else None

    result["model"] = model or ""

    # ── base_url ─────────────────────────────────────────────
    base_url = _non_empty(kwargs.get("base_url"))
    sources["base_url"] = "kwargs" if base_url else None

    if base_url is None:
        base_url = _non_empty(provider_config.get("base_url"))
        sources["base_url"] = "config" if base_url else None

    if base_url is None:
        base_url = _non_empty(env_getter(f"{env_prefix}_BASE_URL"))
        sources["base_url"] = "env" if base_url else None

    if base_url is None:
        base_url = _non_empty(defaults.get("base_url"))
        sources["base_url"] = "default" if base_url else None

    result["base_url"] = base_url or ""

    # ── extra kwargs (everything except api_key, model) ──────
    extra = {}
    for k, v in provider_config.items():
        if k not in ("api_key", "model", "base_url"):
            extra[k] = v
    for k, v in kwargs.items():
        if k not in ("api_key", "model", "base_url"):
            extra[k] = v

    result["extra_kwargs"] = extra
    result["sources"] = sources

    return result


# ── public API ──────────────────────────────────────────────────


def get_llm_client(provider: Optional[str] = None, **kwargs):
    """
    获取 LLM 客户端。

    Priority: kwargs > config.json > env > default.

    Args:
        provider: 提供商名称
        **kwargs: 覆盖参数（api_key/model/base_url 或提供商专属参数）
    """
    raw_config = _load_config()
    config = _normalize_config(raw_config)
    if config != raw_config:
        _save_config(config)

    llm_config = config.get("llm", {})

    if provider is None or (isinstance(provider, str) and _is_invalid_key(provider.strip())):
        provider = str(llm_config.get("provider", "deepseek"))
    provider = normalize_provider_key(provider)

    provider_config = llm_config.get(provider)
    if not isinstance(provider_config, dict):
        raise ValueError(f"配置中不存在提供商: {provider}")

    defaults = DEFAULT_LLM_CONFIG.get(provider, {})

    resolved = resolve_llm_config(
        provider=provider,
        provider_config=provider_config,
        defaults=defaults,
        kwargs=kwargs,
    )

    api_key = resolved["api_key"]
    model = resolved["model"]
    base_url = resolved["base_url"]
    sources = resolved["sources"]
    extra_kwargs = dict(resolved["extra_kwargs"])

    # ── Non-sensitive config source log ──────────────────────
    api_log = "present" if resolved["api_key_present"] else "missing"
    _logger.info(
        "[LLMFactory] provider=%s source=config  model=%s source=%s  api_key=%s source=%s",
        provider,
        model,
        sources.get("model", "?"),
        api_log,
        sources.get("api_key", "?"),
    )
    if base_url:
        _logger.info(
            "[LLMFactory] base_url=%s source=%s",
            base_url[:40],
            sources.get("base_url", "?"),
        )

    # ── Validate ─────────────────────────────────────────────
    if not resolved["api_key_present"]:
        raise RuntimeError(
            f"{provider.upper()} API 密钥未配置，请在设置中填写真实 API Key。"
        )
    if not model:
        raise RuntimeError(
            f"{provider.upper()} 模型未配置，请在设置中选择模型。"
        )

    # ── Merge base_url into extra_kwargs if present ──────────
    if base_url:
        extra_kwargs["base_url"] = base_url

    # ── Special handling for minimaxi ────────────────────────
    if provider == "minimaxi":
        group_id = str(extra_kwargs.get("group_id", "")).strip() or os.getenv("MINIMAXI_GROUP_ID", "").strip() or os.getenv("MINIMAX_GROUP_ID", "").strip()
        if group_id:
            extra_kwargs["group_id"] = group_id

    return LLMProviderFactory.create(provider, api_key, model, **extra_kwargs)


def get_llm_client_with_fallback(
    primary_provider: Optional[str] = None,
    fallback_providers: Optional[List[str]] = None,
    **kwargs,
) -> tuple[str, "LLMProvider"]:
    """
    获取 LLM 客户端，带 fallback 链。

    按顺序尝试各提供商，第一个成功初始化的作为当前 LLM 客户端。
    如果全部失败，抛出最后一个异常。

    Args:
        primary_provider: 首选提供商（None 则从配置读取）
        fallback_providers: 备选提供商列表（None 则从 config.FALLBACK_PROVIDERS 读取）
        **kwargs: 额外参数（传给每个提供商的 get_llm_client）

    Returns:
        (provider_name, LLMProvider) 元组
    """
    from pawmate import config as pawmate_config

    if fallback_providers is None:
        fallback_providers = getattr(pawmate_config, "FALLBACK_PROVIDERS", [])
    fallback_providers = [normalize_provider_key(p) for p in fallback_providers if p.strip()]

    raw_config = _load_config()
    llm_cfg = raw_config.get("llm", {})

    if primary_provider is None or _is_invalid_key(primary_provider.strip()):
        primary_provider = normalize_provider_key(str(llm_cfg.get("provider", "deepseek")))
    else:
        primary_provider = normalize_provider_key(primary_provider)

    # 构建尝试链：主提供商 + fallback（去重）
    chain = [primary_provider]
    for p in fallback_providers:
        if p not in chain:
            chain.append(p)

    last_exception = None
    attempted = []

    for provider_name in chain:
        try:
            client = get_llm_client(provider=provider_name, **kwargs)
            _logger.info(
                "[LLMFactory] fallback 成功: %s (尝试链: %s)",
                provider_name,
                " → ".join(chain),
            )
            return provider_name, client
        except Exception as e:
            _logger.warning(
                "[LLMFactory] fallback %s 失败: %s",
                provider_name, e,
            )
            attempted.append(provider_name)
            last_exception = e
            continue

    raise RuntimeError(
        f"所有提供商均无法连接: {' → '.join(attempted)}"
        f"（最后错误: {last_exception}）"
    )
