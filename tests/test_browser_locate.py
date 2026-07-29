import asyncio

from pawmate.core.browser.locate import locate, parse_locate_response
from pawmate.ocr_vision.models import LocateResult


def test_valid_json():
    raw = '{"ok": true, "target": "btn", "confidence": 0.9, "point": {"x": 120, "y": 40}, "bbox": {"x": 100, "y": 30, "w": 60, "h": 24}, "reason": "ok"}'
    result = parse_locate_response(raw)
    assert result.ok and result.point == {"x": 120, "y": 40} and result.confidence == 0.9


def test_fenced_json():
    raw = '```json\n{"ok": true, "confidence": 0.8, "point": {"x": 5, "y": 6}}\n```'
    result = parse_locate_response(raw)
    assert result.ok and result.point == {"x": 5, "y": 6}


def test_prose_is_unparseable_not_string():
    result = parse_locate_response("图片左下角好像是 0，但实际应为未加载？右下角疑似 0:00……")
    assert isinstance(result, LocateResult) and not result.ok and result.reason == "unparseable"


def test_missing_ok():
    result = parse_locate_response('{"point": {"x": 1, "y": 2}}')
    assert not result.ok and result.reason == "schema_missing_ok"


def test_model_reported_not_ok():
    result = parse_locate_response('{"ok": false, "reason": "看不清", "confidence": 0.1}')
    assert not result.ok and result.reason == "看不清"


def test_low_confidence_demoted():
    raw = '{"ok": true, "confidence": 0.2, "point": {"x": 10, "y": 10}}'
    result = parse_locate_response(raw, min_confidence=0.4)
    assert not result.ok and "low_confidence" in result.reason and result.point == {"x": 10, "y": 10}


def test_out_of_bounds():
    raw = '{"ok": true, "confidence": 0.9, "point": {"x": 9999, "y": 10}}'
    result = parse_locate_response(raw, image_size=(800, 600))
    assert not result.ok and result.reason == "point_out_of_bounds"


def test_point_inside_bbox_when_present():
    raw = '{"ok": true, "confidence": 0.8, "point": {"x": 130, "y": 42}, "bbox": {"x": 100, "y": 30, "w": 60, "h": 24}}'
    result = parse_locate_response(raw)
    assert result.ok
    assert result.bbox["x"] <= result.point["x"] <= result.bbox["x"] + result.bbox["w"]
    assert result.bbox["y"] <= result.point["y"] <= result.bbox["y"] + result.bbox["h"]


def test_never_returns_string():
    for raw in ["", "garbage", "{bad json", '{"ok": true}', '{"ok": true, "point": {"x":"a"}}']:
        assert isinstance(parse_locate_response(raw), LocateResult)


def test_locate_wraps_vision_call_error():
    async def boom(system, messages, max_tokens):
        raise RuntimeError("timeout")

    result = asyncio.run(locate("data:image/png;base64,xx", "btn", vision_call=boom))
    assert not result.ok and result.reason.startswith("vision_call_error")


def test_locate_happy_path():
    async def fake(system, messages, max_tokens):
        assert "只返回 JSON" in system
        return '{"ok": true, "confidence": 0.9, "point": {"x": 3, "y": 4}}'

    result = asyncio.run(locate("data:image/png;base64,xx", "btn", vision_call=fake))
    assert result.ok and result.point == {"x": 3, "y": 4}
