# Browser deepfix verification

This patch closes the native/session routing bug that the previous deepfix still left open.

## Required contract

1. `browser_goto(use_my_login=true)` must attach native CDP and every following `navigate/read/act/extract` in the same router flow must use `require_native=True`.
2. `require_native=True` must only use `_browser_sessions["native"]`; it must never fall back to foreground/background managed sessions.
3. `browser_goto(goal="搜索...", visibility="auto") -> browser_read(visibility="auto")` must stay on the background slot.
4. `get_router(goal="")` may clear stale prompt goal, but must not clear the last resolved session route.
5. `browser_fill_form(url="")` must fill the current page and must not call `page.goto("")`.
6. Default foreground `attach_mode=auto` must not be converted to `prefer`; arbitrary existing CDP endpoints are only probed when the user explicitly sets `cdp_url`, `attach_mode=prefer`, or `attach_mode=attach`.

## Validation commands

```bash
python -m compileall -q pawmate/core/browser pawmate/tools/browser pawmate/tools/browser_facade.py pawmate/tools/playwright_browser.py pawmate/tools/native_browser.py pawmate/tools/browser_input.py pawmate/tools/cdp_attach.py pawmate/tools/browser_preflight.py
python docs/browser/run_deepfix_static_validation.py
python -m pytest tests/browser/test_browser_session_contract.py -q
```

## Mock Postman flows to run after migration

### A. Native login-state flow

```json
{"tool":"browser_goto","args":{"url":"https://admin.example.com","use_my_login":true,"goal":"登录后台"}}
{"tool":"browser_read","args":{"visibility":"auto"}}
{"tool":"browser_act","args":{"action":"click","ref":"#submit","visibility":"auto"}}
{"tool":"browser_extract","args":{"query":"页面状态","visibility":"auto"}}
```

Expected: every response session has `slot=native`, `mode=native`, `require_native=true` where present.

### B. Background search/read flow

```json
{"tool":"browser_goto","args":{"url":"https://www.baidu.com","goal":"搜索 AI agent","visibility":"auto"}}
{"tool":"browser_read","args":{"visibility":"auto"}}
{"tool":"browser_extract","args":{"query":"摘要","visibility":"auto"}}
```

Expected: all three operations use `visibility=background` and do not touch foreground/native.

### C. Native disconnected flow

Force close the CDP browser after native attach, then call:

```json
{"tool":"browser_act","args":{"action":"click","ref":"#x","visibility":"auto"}}
```

Expected: `ok=false`, `error_type=native_session_disconnected`; no managed fallback.

### D. Current-page form fill

```json
{"tool":"browser_act","args":{"action":"fill","ref":"#kw","value":"test","visibility":"auto"}}
```

Expected: no `page.goto("")`; fill happens on the current page.
