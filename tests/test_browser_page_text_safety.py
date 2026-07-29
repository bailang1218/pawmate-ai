import pytest

from pawmate.tools.browser import playwright_runtime


class _RecordingPage:
    def __init__(self):
        self.script = ""

    async def evaluate(self, script):
        self.script = script
        return "  page   text  "


@pytest.mark.asyncio
async def test_extract_page_text_only_prunes_a_detached_clone():
    page = _RecordingPage()

    result = await playwright_runtime._extract_page_text(page)

    assert result == "page text"
    assert "source.cloneNode(true)" in page.script
    assert "clone.querySelectorAll(" in page.script
    assert "document.querySelectorAll(" not in page.script
    assert "return clone.textContent" in page.script
