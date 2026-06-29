"""Vision service implementation.

This module keeps the feature isolated from the rest of the app:
- screenshot capture
- image analysis through the configured vision route
- runtime artifact storage and cleanup
"""

from __future__ import annotations

import base64
import datetime
import mimetypes
import os
import platform
import re
import shutil
from pathlib import Path
from threading import Thread

from .models import OCRResult, ScreenshotResult, VisionStorageConfig
from pawmate.core.llm_factory import get_llm_client
from pawmate.core.llm_router import resolve_llm_route_candidates

try:
    from PIL import ImageGrab
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False
    ImageGrab = None  # type: ignore[assignment]


def _default_root_dir() -> Path:
    env = os.environ.get("PAWMATE_VISION_DIR")
    if env:
        return Path(env).expanduser().resolve()

    home = Path.home()
    system = platform.system()
    if system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "PawMate" / "ocr_vision"
        return home / "AppData" / "Local" / "PawMate" / "ocr_vision"
    if system == "Darwin":
        return home / "Library" / "Application Support" / "PawMate" / "ocr_vision"

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / "pawmate" / "ocr_vision"
    return home / ".cache" / "pawmate" / "ocr_vision"


def _load_app_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        import json

        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _image_data_url(path: Path) -> str:
    mime_type, _encoding = mimetypes.guess_type(str(path))
    if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            mime_type = "image/jpeg"
        elif suffix == ".webp":
            mime_type = "image/webp"
        elif suffix == ".gif":
            mime_type = "image/gif"
        else:
            mime_type = "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


class VisionService:
    """Encapsulates screenshot and model-backed vision behavior."""

    def __init__(self, config: VisionStorageConfig | None = None):
        root_dir = config.root_dir if config else _default_root_dir()
        self._config = config or VisionStorageConfig(root_dir=root_dir)

    @property
    def config(self) -> VisionStorageConfig:
        return self._config

    @property
    def storage_root(self) -> Path:
        return self._config.root_dir / self._config.temp_dir_name

    def capture_screenshot(self, path: str = "screenshot.png") -> ScreenshotResult:
        """Capture a desktop screenshot into the runtime tmp folder.

        Browser pages should use browser_screenshot/page.screenshot() instead;
        this desktop capture is only a non-DOM fallback.
        """
        storage_root = self.storage_root
        storage_root.mkdir(parents=True, exist_ok=True)

        given = Path(path)
        today = datetime.date.today().isoformat()
        if given.is_absolute():
            target = given
        elif given.parent and str(given.parent) != ".":
            target = storage_root / today / given
        else:
            target = storage_root / today / given.name

        target.parent.mkdir(parents=True, exist_ok=True)

        if not PIL_AVAILABLE:
            return ScreenshotResult(
                ok=False,
                path=None,
                message="Pillow not available: install pillow to enable screenshots",
                storage_root=storage_root,
            )

        try:
            image = ImageGrab.grab()
            image.save(str(target))
        except Exception as exc:
            return ScreenshotResult(
                ok=False,
                path=None,
                message=f"screenshot failed: {exc}",
                storage_root=storage_root,
            )

        Thread(target=self.cleanup_old_screenshots, daemon=True).start()
        return ScreenshotResult(
            ok=True,
            path=target,
            message=f"saved: {target}",
            storage_root=storage_root,
        )

    async def analyze_image(
        self,
        path: str,
        prompt: str = "",
        *,
        lang: str = "eng",
    ) -> OCRResult:
        """Analyze a saved image with the configured vision LLM route."""
        image_path = Path(path)
        if not image_path.exists():
            return OCRResult(
                ok=False,
                text="",
                path=image_path,
                language=lang,
                message=f"file not found: {image_path}",
            )

        user_prompt = (prompt or "").strip()
        if not user_prompt:
            user_prompt = (
                "请分析这张图片并返回结构化结果。优先提取可见文字、界面元素和位置关系；"
                "如果需要定位可点击目标，请返回 JSON，包含 target、confidence、"
                "bbox{x,y,w,h} 和 point{x,y}。无法可靠定位时明确返回 ok=false 和原因。"
                f"OCR 语言偏好: {lang}。"
            )
        elif _looks_like_grounding_request(user_prompt):
            user_prompt = (
                f"{user_prompt}\n\n"
                "请只返回定位结果 JSON："
                "{\"ok\": true|false, \"target\": string, \"confidence\": number, "
                "\"bbox\": {\"x\": number, \"y\": number, \"w\": number, \"h\": number}, "
                "\"point\": {\"x\": number, \"y\": number}, \"reason\": string}。"
                "坐标必须基于输入图片左上角；无法确定就 ok=false，不要猜。"
            )

        try:
            data_url = _image_data_url(image_path)
            config = _load_app_config()
            choices = resolve_llm_route_candidates("vision", config, user_input=user_prompt)
            if not choices:
                raise RuntimeError("没有配置可用的视觉模型服务商 API Key")

            last_error: Exception | None = None
            for choice in choices:
                try:
                    client = get_llm_client(provider=choice.provider, model=choice.model)
                    chunks: list[str] = []
                    messages = [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": user_prompt},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ]
                    async for event in client.stream(
                        messages=messages,
                        system=(
                            "你是 PawMate 的视觉理解工具。请只根据图片内容回答，"
                            "不确定的地方明确说不确定，不要模拟或猜造看不到的内容。"
                            "当任务是定位、点击、框选、找按钮或找输入框时，返回坐标 JSON；"
                            "无法感知或定位失败时说明原因，不要建议盲操。"
                        ),
                        tools=None,
                        max_tokens=2048,
                    ):
                        if event.get("type") == "text_delta":
                            chunks.append(str(event.get("text", "")))
                    text = "".join(chunks).strip()
                    if text:
                        return OCRResult(
                            ok=True,
                            text=text,
                            path=image_path,
                            language=lang,
                            message=f"ok: {choice.provider}/{choice.model or '(default)'}",
                        )
                    raise RuntimeError(f"{choice.provider} returned empty vision response")
                except Exception as exc:
                    last_error = exc
                    continue
            raise RuntimeError(str(last_error or "vision model call failed"))
        except Exception as exc:
            return OCRResult(
                ok=False,
                text="",
                path=image_path,
                language=lang,
                message=f"ocr failed: {exc}",
            )

    async def ocr_image(self, path: str, lang: str = "eng", prompt: str = "") -> OCRResult:
        """Compatibility wrapper: OCR now uses the configured vision route."""
        return await self.analyze_image(path, prompt=prompt, lang=lang)

    async def capture_and_ocr(self, screenshot_path: str = "screenshot.png", lang: str = "eng") -> tuple[ScreenshotResult, OCRResult | None]:
        """Convenience helper for capture then OCR."""
        screenshot = self.capture_screenshot(screenshot_path)
        if not screenshot.ok or screenshot.path is None:
            return screenshot, None
        return screenshot, await self.ocr_image(str(screenshot.path), lang=lang)

    def cleanup_old_screenshots(self) -> None:
        """Best-effort cleanup of date folders older than keep_days."""
        cutoff = datetime.date.today() - datetime.timedelta(days=self._config.keep_days)
        storage_root = self.storage_root
        try:
            if not storage_root.exists() or not storage_root.is_dir():
                return
            for child in storage_root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    folder_date = datetime.date.fromisoformat(child.name)
                except Exception:
                    continue
                if folder_date < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
        except Exception:
            pass


_GROUNDING_RE = re.compile(
    r"(坐标|位置|定位|点击|点一下|按钮|输入框|bbox|bounding|coordinate|click|target)",
    re.IGNORECASE,
)


def _looks_like_grounding_request(prompt: str) -> bool:
    return bool(_GROUNDING_RE.search(prompt or ""))


_DEFAULT_SERVICE = VisionService()


def get_default_service() -> VisionService:
    return _DEFAULT_SERVICE


def capture_screenshot(path: str = "screenshot.png") -> str:
    result = _DEFAULT_SERVICE.capture_screenshot(path)
    return result.message


async def ocr_image(path: str, lang: str = "eng", prompt: str = "") -> str:
    result = await _DEFAULT_SERVICE.ocr_image(path, lang=lang, prompt=prompt)
    return result.text if result.ok else f"[error] {result.message}"


async def vision_analyze(path: str, prompt: str = "", lang: str = "eng") -> str:
    result = await _DEFAULT_SERVICE.analyze_image(path, prompt=prompt, lang=lang)
    return result.text if result.ok else f"[error] {result.message}"
