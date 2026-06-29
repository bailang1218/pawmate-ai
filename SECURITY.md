# 安全策略 / Security Policy

PawMate AI 是一个本地桌面助手，可能与文件、命令、浏览器、模型供应商和本地运行状态交互。请把它当作敏感软件处理。

PawMate AI is a local desktop assistant that can interact with files, commands, browsers, model providers, and local runtime state. Treat it as sensitive software.

## 不要提交 / Never Commit

- API keys、tokens、密码或模型供应商凭据。
- API keys, tokens, passwords, or provider credentials.
- `pawmate/config.json`。
- `pawmate/config.json`.
- `data/`、`logs/`、`downloads/`、浏览器 profiles、截图、缓存文件或聊天记录。
- `data/`, `logs/`, `downloads/`, browser profiles, screenshots, cache files, or conversation history.
- `.env`、`*.env`、`*.local.json` 或机器特定设置。
- `.env`, `*.env`, `*.local.json`, or machine-specific settings.

## 报告方式 / Reporting

请私下向维护者报告安全问题。如果仓库启用了 GitHub private vulnerability reporting，请优先使用它；否则，请在公开细节前直接联系维护者。

Please report security issues privately to the maintainer. If this repository has GitHub private vulnerability reporting enabled, use that. Otherwise, contact the maintainer directly before publishing details.

## 运行时说明 / Runtime Notes

- WebSocket 默认关闭，启用时需要认证 token。
- WebSocket support is disabled by default and requires an auth token when enabled.
- 工具执行围绕确认门和脱敏设计，但用户仍应仔细检查工具动作。
- Tool execution is designed around confirmation gates and redaction, but users should still review tool actions carefully.
- 如果配置为使用已有浏览器 profile，浏览器自动化可能接触已登录会话。
- Browser automation may touch logged-in sessions if configured to use an existing browser profile.
