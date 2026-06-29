# Contributing

Thanks for taking a look at PawMate AI. This project is currently an alpha portfolio project, so contributions should be small, focused, and easy to review.

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .[dev]
python -m pytest -q
```

## Pull Requests

- Keep changes focused on one behavior or module.
- Do not commit local `pawmate/config.json`, API keys, tokens, logs, browser profiles, downloaded files, or conversation history.
- Update tests when changing routing, tool execution, security checks, storage behavior, or desktop-pet motion logic.
- For UI changes, include before/after screenshots when possible.

## Security Issues

Please do not open a public issue for sensitive vulnerabilities or leaked credentials. Contact the maintainer privately first, or use GitHub private vulnerability reporting if it is enabled.
