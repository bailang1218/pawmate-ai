from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def assert_contains(rel: str, needle: str) -> None:
    text = read(rel)
    assert needle in text, f"missing {needle!r} in {rel}"


def main() -> None:
    router = read("pawmate/core/browser/router.py")
    facade = read("pawmate/tools/browser/facade.py")
    pw = read("pawmate/tools/browser/playwright_runtime.py")

    for rel in [
        "pawmate/core/browser/router.py",
        "pawmate/tools/browser/facade.py",
        "pawmate/tools/browser/playwright_runtime.py",
        "pawmate/tools/browser/native.py",
        "pawmate/tools/browser/input.py",
        "pawmate/tools/browser/cdp.py",
        "pawmate/tools/browser/preflight.py",
    ]:
        ast.parse(read(rel), filename=rel)

    assert "navigate: Callable[[str, str, str, bool]" in router
    assert "observe: Callable[[str, str, bool]" in router
    assert "extract: Callable[[str, str, str, bool]" in router
    assert "self._last_visibility" in router
    assert "self._last_require_native" in router
    assert "await self._a.navigate(url, resolved_visibility, self._goal, require_native)" in router
    assert "await self._a.observe(resolved_visibility, self._goal, require_native)" in router

    assert 'attach_mode = "prefer"' not in facade
    assert "return resolved_visibility" in facade
    assert "require_native=require_native" in facade
    assert 'visibility: str = "auto"' in facade

    assert "_DEFAULT_CDP_PORT = 19222" in pw
    assert "_DEFAULT_NATIVE_CDP_PORT = 9222" in pw
    assert "def _dedicated_auto_cdp_url" in pw
    assert "require_native: bool = False" in pw
    assert '_browser_sessions.get("native")' in pw
    assert "Never fall back to a" in pw
    assert "require_native=require_native" in pw
    assert "if url:\n        await page.goto(url" in pw

    print("deepfix_static_validation_ok")


if __name__ == "__main__":
    main()
