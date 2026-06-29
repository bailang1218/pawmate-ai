"""Parse textual tool-call protocols emitted as assistant text."""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, Optional


_CALL_RE = re.compile(r"functions\.(?P<name>[A-Za-z_][\w]*)\((?P<args>.*?)\)", re.DOTALL)

_DSML_BAR_RE = r"[\|｜]"
_DSML_PREFIX_RE = rf"(?:<\s*{_DSML_BAR_RE}\s*{_DSML_BAR_RE}\s*DSML\s*{_DSML_BAR_RE}\s*{_DSML_BAR_RE}\s*{{tag}}\b(?P<attrs>[^>]*)>)"
_DSML_CLOSE_RE = rf"(?:</\s*{_DSML_BAR_RE}\s*{_DSML_BAR_RE}\s*DSML\s*{_DSML_BAR_RE}\s*{_DSML_BAR_RE}\s*{{tag}}\s*>)"
_DSML_TOOL_CALLS_RE = re.compile(
    _DSML_PREFIX_RE.format(tag="tool_calls")
    + r"(?P<body>.*?)"
    + _DSML_CLOSE_RE.format(tag="tool_calls"),
    re.DOTALL | re.IGNORECASE,
)
_DSML_INVOKE_RE = re.compile(
    _DSML_PREFIX_RE.format(tag="invoke")
    + r"(?P<body>.*?)"
    + _DSML_CLOSE_RE.format(tag="invoke"),
    re.DOTALL | re.IGNORECASE,
)
_DSML_PARAMETER_RE = re.compile(
    _DSML_PREFIX_RE.format(tag="parameter")
    + r"(?P<value>.*?)"
    + _DSML_CLOSE_RE.format(tag="parameter"),
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r"([A-Za-z_][\w:-]*)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
_TOOL_ALIASES = {
    "fs_read_directory": ("list_directory", "list_dir"),
    "fs_list_directory": ("list_directory", "list_dir"),
    "fs_read_file": ("read_text_file", "read_file"),
    "fs_write_file": ("write_file",),
    "fs_create_directory": ("create_directory",),
    "fs_move_path": ("move_path",),
    "shell": ("run_shell_command", "run_command"),
}


def parse_textual_tool_call(text: str, tool_registry) -> Optional[Dict[str, Any]]:
    """Extract a single tool call from assistant text.

    Returns a dict with at least ``name`` and ``input``. When the tool call is
    embedded in surrounding prose, ``call_text`` contains the protocol block to
    remove and ``visible_text`` contains the remaining ordinary assistant text.
    """
    if not text:
        return None

    return _parse_dsml_tool_call(text, tool_registry) or _parse_functions_tool_call(text, tool_registry)


def strip_textual_tool_call_protocol(text: str, tool_registry) -> str:
    """Return user-visible text with any supported textual tool protocol removed."""
    parsed = parse_textual_tool_call(text, tool_registry)
    if parsed is None:
        return text
    return str(parsed.get("visible_text", ""))


def strip_any_textual_tool_call_protocol(text: str) -> str:
    """Remove textual tool protocol even when the tool is unavailable or not exposed."""
    if not text:
        return text

    stripped = _DSML_TOOL_CALLS_RE.sub("\n\n", text)
    stripped = _CALL_RE.sub("", stripped)
    protocol_start = _find_protocol_start(stripped)
    if protocol_start is not None:
        stripped = stripped[:protocol_start]
    return stripped.strip()


def visible_text_before_textual_tool_protocol(text: str, tool_registry) -> str:
    """Return the currently visible prefix while a textual protocol may stream in."""
    parsed = parse_textual_tool_call(text, tool_registry)
    if parsed is not None:
        return _hold_trailing_partial_marker(_remove_protocol_gap(text, str(parsed.get("call_text") or "")))

    protocol_start = _find_protocol_start(text)
    if protocol_start is None:
        return _hold_trailing_partial_marker(text)
    return _hold_trailing_partial_marker(text[:protocol_start])


def visible_text_before_any_textual_tool_protocol(text: str) -> str:
    """Return visible stream text before any textual protocol marker."""
    protocol_start = _find_protocol_start(text)
    if protocol_start is None:
        return _hold_trailing_partial_marker(text)
    return _hold_trailing_partial_marker(text[:protocol_start])


def has_textual_tool_call_protocol(text: str) -> bool:
    """Fast protocol marker check for stream filtering."""
    return _find_protocol_start(text) is not None


# A trailing run that *could* grow into the DSML opener "<||...". While streaming
# we must not emit it yet, otherwise a lone "<" (or "<|") leaks to the UI before
# the disambiguating second bar arrives. Matches "<" + spaces/bars at end of text.
_TRAILING_DSML_OPENER_RE = re.compile(r"<[\s\|｜]*$")
# A "strong" dangling opener already contains at least one bar (clearly DSML),
# used to distinguish a real truncated tool-call from a literal "<" in prose.
_STRONG_TRAILING_DSML_RE = re.compile(r"<\s*[\|｜][\s\|｜]*$")


def _trailing_partial_marker_start(text: str) -> int | None:
    """Index where an incomplete (still-streaming) DSML opener begins, else None."""
    match = _TRAILING_DSML_OPENER_RE.search(text)
    return match.start() if match else None


def _hold_trailing_partial_marker(text: str) -> str:
    """Withhold a trailing run that may be the start of a textual tool protocol.

    Complete markers are already removed by the callers, so any remaining trailing
    "<..." run is necessarily an incomplete opener that should stay buffered until
    the next stream chunk proves whether it is protocol or ordinary prose.
    """
    if not text:
        return text
    start = _trailing_partial_marker_start(text)
    if start is None:
        return text
    return text[:start]


def is_truncated_tool_protocol(text: str, tool_registry) -> bool:
    """True when the assistant text looks like a tool call cut off mid-stream.

    Conservative on purpose: a bare trailing "<" inside a long prose/code answer is
    NOT flagged (it is probably literal). We only flag (a) a message that is *only*
    a dangling opener like "<" or "<|", or (b) text where a protocol marker has
    started but no complete call can be parsed.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    # Whole answer is just a dangling opener -> the "<" + truncation symptom.
    if _TRAILING_DSML_OPENER_RE.fullmatch(stripped):
        return True
    # A protocol marker appeared but the call never completed -> mid-call cut.
    if _find_protocol_start(text) is not None and parse_textual_tool_call(text, tool_registry) is None:
        return True
    # A strong trailing opener (already has a bar) with no parseable call.
    if _STRONG_TRAILING_DSML_RE.search(text) and parse_textual_tool_call(text, tool_registry) is None:
        return True
    return False


def _parse_functions_tool_call(text: str, tool_registry) -> Optional[Dict[str, Any]]:
    match = _CALL_RE.search(text)
    if not match:
        return None

    tool_name = _resolve_tool_name(match.group("name"), tool_registry)
    if not tool_name:
        return None

    raw_arguments = _strip_markdown_fence(match.group("args").strip())
    tool_input = _parse_argument_value(raw_arguments) if raw_arguments else {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    return {
        "name": tool_name,
        "input": tool_input,
        "call_text": match.group(0),
        "visible_text": _join_visible_text(text[: match.start()], text[match.end() :]),
    }


def _parse_dsml_tool_call(text: str, tool_registry) -> Optional[Dict[str, Any]]:
    tool_calls_match = _DSML_TOOL_CALLS_RE.search(text)
    if not tool_calls_match:
        return None

    invoke_match = _DSML_INVOKE_RE.search(tool_calls_match.group("body"))
    if not invoke_match:
        return None

    invoke_attrs = _parse_attrs(invoke_match.group("attrs") or "")
    tool_name = _resolve_tool_name(str(invoke_attrs.get("name") or ""), tool_registry)
    if not tool_name:
        return None

    tool_input: dict[str, Any] = {}
    for parameter_match in _DSML_PARAMETER_RE.finditer(invoke_match.group("body")):
        attrs = _parse_attrs(parameter_match.group("attrs") or "")
        name = str(attrs.get("name") or "")
        if not name:
            continue
        value = parameter_match.group("value").strip()
        tool_input[name] = _parse_dsml_parameter(value, attrs)

    return {
        "name": tool_name,
        "input": tool_input,
        "call_text": tool_calls_match.group(0),
        "visible_text": _join_visible_text(text[: tool_calls_match.start()], text[tool_calls_match.end() :]),
    }


def _parse_dsml_parameter(value: str, attrs: dict[str, str]) -> Any:
    if attrs.get("string", "").lower() == "true":
        return value
    if attrs.get("json", "").lower() == "true" or attrs.get("object", "").lower() == "true":
        return _parse_argument_value(value)
    return _parse_argument_value(value)


def _resolve_tool_name(raw_name: str, tool_registry) -> str:
    tool_name = str(raw_name or "")
    if not tool_name:
        return ""
    if tool_registry.has_tool(tool_name):
        return tool_name
    for alias in _TOOL_ALIASES.get(tool_name, ()):
        if tool_registry.has_tool(alias):
            return alias
    return ""


def _parse_argument_value(raw_arguments: str) -> Any:
    raw_arguments = _strip_markdown_fence(raw_arguments)
    try:
        return json.loads(raw_arguments)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(raw_arguments)
        except Exception:
            return raw_arguments


def _parse_attrs(raw_attrs: str) -> dict[str, str]:
    return {match.group(1): match.group(3) for match in _ATTR_RE.finditer(raw_attrs)}


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    return stripped


def _join_visible_text(before: str, after: str) -> str:
    return "\n\n".join(part.strip() for part in (before, after) if part.strip())


def _remove_protocol_gap(text: str, call_text: str) -> str:
    if not call_text or call_text not in text:
        return text
    before, after = text.split(call_text, 1)
    if after.strip():
        return _join_visible_text(before, after)
    return before.rstrip("\r\n")


def _find_protocol_start(text: str) -> int | None:
    starts = []
    dsml_match = re.search(r"<\s*[\|｜]\s*[\|｜]", text)
    if dsml_match:
        starts.append(dsml_match.start())
    functions_match = re.search(r"functions\.", text)
    if functions_match:
        starts.append(functions_match.start())
    return min(starts) if starts else None
