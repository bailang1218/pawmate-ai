"""Structured visual locate contract for browser automation."""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Optional

from pawmate.ocr_vision.models import LocateResult


_LOCATE_SYSTEM = (
    "你是定位工具。只根据图片内容回答。只返回 JSON："
    '{"ok": true|false, "target": string, "confidence": number, '
    '"bbox": {"x": number, "y": number, "w": number, "h": number}, '
    '"point": {"x": number, "y": number}, "reason": string}。'
    "坐标基于图片左上角原点；无法确定就 ok=false；禁止任何解释性散文、禁止 markdown。"
)


def build_locate_messages(image_data_url: str, target: str) -> tuple[str, list]:
    """Return the system prompt and multimodal messages for the vision client."""
    user = [
        {"type": "text", "text": f"定位目标：{target}。只返回上述 JSON。"},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]
    return _LOCATE_SYSTEM, [{"role": "user", "content": user}]


_FENCE_OPEN = re.compile(r"^\s*```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\s*```\s*$")
_FIRST_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _strip(text: str) -> str:
    text = _FENCE_OPEN.sub("", text.strip())
    text = _FENCE_CLOSE.sub("", text)
    return text.strip()


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _valid_point(point) -> bool:
    return isinstance(point, dict) and _num(point.get("x")) is not None and _num(point.get("y")) is not None


def _valid_bbox(bbox) -> bool:
    return isinstance(bbox, dict) and all(_num(bbox.get(key)) is not None for key in ("x", "y", "w", "h"))


def _norm_point(point) -> dict:
    return {"x": int(round(_num(point["x"]))), "y": int(round(_num(point["y"])))}


def _norm_bbox(bbox) -> Optional[dict]:
    if not _valid_bbox(bbox):
        return None
    return {key: int(round(_num(bbox[key]))) for key in ("x", "y", "w", "h")}


def _in_bounds(point: dict, image_size: tuple[int, int]) -> bool:
    width, height = image_size
    return 0 <= point["x"] < width and 0 <= point["y"] < height


def parse_locate_response(
    raw: str,
    *,
    image_size: Optional[tuple[int, int]] = None,
    min_confidence: float = 0.75,
) -> LocateResult:
    """Parse a vision model response into LocateResult. Never returns prose."""
    if not raw or not raw.strip():
        return LocateResult.fail("empty")

    cleaned = _strip(raw)
    obj = None
    try:
        obj = json.loads(cleaned)
    except Exception:
        match = _FIRST_OBJ.search(cleaned)
        if match:
            try:
                obj = json.loads(match.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return LocateResult.fail("unparseable")
    if "ok" not in obj:
        return LocateResult.fail("schema_missing_ok")

    confidence = _num(obj.get("confidence")) or 0.0
    if not bool(obj.get("ok")):
        return LocateResult.fail(str(obj.get("reason") or "model_reported_not_ok"), confidence=confidence)

    point = obj.get("point")
    norm_bbox = _norm_bbox(obj.get("bbox"))
    if not _valid_point(point):
        return LocateResult.fail("missing_point", confidence=confidence, bbox=norm_bbox)
    norm_point = _norm_point(point)
    if confidence < min_confidence:
        return LocateResult.fail(
            f"low_confidence(<{min_confidence})",
            confidence=confidence,
            point=norm_point,
            bbox=norm_bbox,
        )
    if image_size and not _in_bounds(norm_point, image_size):
        return LocateResult.fail("point_out_of_bounds", confidence=confidence)
    if norm_bbox is not None:
        if norm_bbox["w"] <= 0 or norm_bbox["h"] <= 0:
            return LocateResult.fail("invalid_bbox", confidence=confidence)
        if image_size:
            width, height = image_size
            if norm_bbox["x"] < 0 or norm_bbox["y"] < 0 or norm_bbox["x"] + norm_bbox["w"] > width or norm_bbox["y"] + norm_bbox["h"] > height:
                return LocateResult.fail("bbox_out_of_bounds", confidence=confidence)
        if not (norm_bbox["x"] <= norm_point["x"] <= norm_bbox["x"] + norm_bbox["w"] and norm_bbox["y"] <= norm_point["y"] <= norm_bbox["y"] + norm_bbox["h"]):
            return LocateResult.fail("point_outside_bbox", confidence=confidence)
    return LocateResult(ok=True, point=norm_point, bbox=norm_bbox, confidence=confidence, reason="ok")


VisionCall = Callable[[str, list, int], Awaitable[str]]


async def locate(
    image_data_url: str,
    target: str,
    *,
    vision_call: VisionCall,
    image_size: Optional[tuple[int, int]] = None,
    min_confidence: float = 0.75,
    max_tokens: int = 512,
) -> LocateResult:
    """Locate target through the injected vision client call."""
    system, messages = build_locate_messages(image_data_url, target)
    try:
        raw = await vision_call(system, messages, max_tokens)
    except Exception as exc:
        return LocateResult.fail(f"vision_call_error:{exc}")
    return parse_locate_response(raw, image_size=image_size, min_confidence=min_confidence)
