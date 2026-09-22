# PawMate browser_use deep bugfix notes

Scope: browser_use only. This patch is intentionally smaller than a full tools directory refactor.
It fixes confirmed hidden browser bugs without changing the public four-tool facade shape:

- `browser_goto`
- `browser_read`
- `browser_act`
- `browser_extract`

## Fixed

### P1: foreground/background/native session contamination

Before:

- `playwright_browser.py` kept one global `_current_session`.
- Background search/read could replace the foreground page.
- Native/CDP sessions could be overwritten by normal browser operations.

Now:

- Added `_browser_sessions: dict[str, BrowserSession]`.
- Sessions are separated into slots: `foreground`, `background`, `native`.
- `_current_session` remains only as a compatibility pointer to the most recently used session.

### P1: attached CDP session replaced by normal operations

Before:

- `browser_facade._ensure_session_with_settings()` could replace an attached CDP session when later operations did not require attached mode.

Now:

- Attached sessions are preserved when visibility matches.
- Native/login-state sessions live in the `native` slot.
- Normal foreground/background sessions do not evict native CDP state.

### P1/P2: arbitrary 9222 CDP attach in default auto mode

Before:

- `attach_mode=auto` tried to connect to whatever was listening on `127.0.0.1:9222`.
- This can attach to the wrong browser process if another tool/user launched a debugging browser.

Now:

- Default `auto` skips arbitrary existing CDP endpoints unless `cdp_url` is explicitly configured.
- It prefers launching PawMate's dedicated CDP browser profile.

### P2: browser goal leakage

Before:

- `get_router(goal)` only updated goal when non-empty, so an old goal could leak into later `browser_read/browser_act`.

Now:

- `get_router()` always resets the router goal, including to empty string.

### P2: browser_read / browser_act / browser_extract target ambiguity

Before:

- `browser_read()` had no visibility/session hint.
- `browser_act()` always used foreground internally.
- `browser_extract()` had no visibility hint.

Now:

- `browser_read(visibility="auto", goal="")`.
- `browser_act(..., visibility="foreground")`.
- `browser_extract(query="", visibility="auto")`.
- The router passes visibility down to browser runtime.

### P2: vision fallback screenshots wrong window

Before:

- Vision subagent always used native desktop browser screenshot.
- If current task was background/headless/CDP, vision could capture the wrong user-visible window.

Now:

- Vision screenshot first tries the current Playwright/CDP page screenshot.
- Native screenshot is only fallback.

### P2: native browser clipboard pollution

Before:

- `native_browser_open_url()` and `native_browser_type_text()` overwrote the user's clipboard.

Now:

- They save/restore prior Unicode clipboard text when possible.
- Result includes `clipboard_restored`.

Limit: this preserves Unicode text clipboard only, not arbitrary binary/image clipboard formats.

### P2: native screenshot path boundary

Before:

- `native_browser_screenshot(path=...)` wrote to arbitrary expanded path.

Now:

- Custom path goes through `SecurityService.check_path(..., mode="write")`.

### P2: blind last-tab selection

Before:

- `browser_select_page()` with no index/filter selected the last live page.

Now:

- If the current session has an active page, it keeps it.
- Otherwise it returns `ambiguous_page_selection` and asks for `index`, `url_contains`, or `title_contains`.

## Not fully solved in this patch

These require a larger browser refactor:

1. Stable DOM refs: current refs are still CSS selectors and can go stale after DOM mutation.
2. Full `BrowserSessionPool` object: this patch uses slot helpers, not a standalone class.
3. Full CDP process identity validation: default auto no longer blindly attaches, but explicit `cdp_url` still trusts the user-provided endpoint.
4. Full native clipboard preservation for non-text formats.
5. Unified browser download boundary: Playwright download and network download still have separate implementations.

## Static validation performed

```bash
python -m compileall -q pawmate/core/browser pawmate/tools/browser pawmate/tools/browser_facade.py pawmate/tools/playwright_browser.py pawmate/tools/native_browser.py pawmate/tools/browser_input.py pawmate/tools/cdp_attach.py pawmate/tools/browser_preflight.py
```

## Second-pass fixes in this package

The prior deepfix was directionally right but still failed the native/session contract. This package adds the missing closure points:

### P0: native session now threads through the full stack

- `BrowserAdapters.navigate/observe/extract` now accept `require_native`.
- `BrowserRouter.goto(use_my_login=true)` passes `require_native=True` into navigate and precheck observe.
- `BrowserRouter.read/act/extract(visibility="auto")` now reuse the last resolved route, including native.
- `playwright_browser._get_active_page(require_native=True)` only uses `_browser_sessions["native"]` and fail-closes with `BrowserDependencyError("native_session_disconnected")` if unavailable.
- `browser_input_action()` no longer checks native and then calls ordinary `_get_active_page(foreground)`; it calls `_get_active_page(..., require_native=True)` directly.

### P1: auto read/extract/action now follows the previous session slot

- `BrowserRouter` stores `_last_visibility` and `_last_require_native` separately from `_goal`.
- `get_router(goal="")` can clear stale prompt goal without losing the previous browser slot.
- `browser_goto(goal="搜索...", visibility="auto") -> browser_read(visibility="auto")` stays in background.

### P1: default foreground auto no longer becomes prefer

- `browser_facade.launch_managed()` no longer converts `attach_mode=auto` to `prefer`.
- Default auto CDP uses a PawMate-dedicated port range starting at `19222`.
- Native attach still uses `9222` unless the user explicitly configures `cdp_url`.

### P2: current-page form fill no longer navigates to an empty URL

- `browser_fill_form(url="", fields=...)` now fills the current page.
- `page.goto(url)` only happens when `url` is non-empty.

## Additional validation included

```bash
python docs/browser/run_deepfix_static_validation.py
python -m pytest tests/browser/test_browser_session_contract.py -q
```
