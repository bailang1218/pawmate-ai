# Security Policy

PawMate AI is a local desktop assistant that can interact with files, commands, browsers, model providers, and local runtime state. Treat it as sensitive software.

## Never Commit

- API keys, tokens, passwords, or provider credentials.
- `pawmate/config.json`.
- `data/`, `logs/`, `downloads/`, browser profiles, screenshots, cache files, or conversation history.
- `.env`, `*.env`, `*.local.json`, or machine-specific settings.

## Reporting

Please report security issues privately to the maintainer. If this repository has GitHub private vulnerability reporting enabled, use that. Otherwise, contact the maintainer directly before publishing details.

## Runtime Notes

- WebSocket support is disabled by default and requires an auth token when enabled.
- Tool execution is designed around confirmation gates and redaction, but users should still review tool actions carefully.
- Browser automation may touch logged-in sessions if configured to use an existing browser profile.
