# 贡献指南 / Contributing

感谢你关注 PawMate AI。当前项目是 Alpha 阶段的作品集项目，所以贡献最好保持小而集中，方便审查和回归测试。

Thanks for taking a look at PawMate AI. This project is currently an alpha portfolio project, so contributions should stay small, focused, and easy to review.

## 环境准备 / Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .[dev]
python -m pytest -q
```

不要提交 `.venv`。它是本地虚拟环境，应该由每个开发者在自己的机器上重新创建。

Do not commit `.venv`. It is a local virtual environment and should be recreated by each developer on their own machine.

## Pull Requests

- 每个 PR 尽量只改一个行为或一个模块。
- Keep each PR focused on one behavior or module.
- 不要提交本地 `pawmate/config.json`、API keys、tokens、日志、浏览器 profiles、下载文件或聊天记录。
- Do not commit local `pawmate/config.json`, API keys, tokens, logs, browser profiles, downloaded files, or conversation history.
- 修改路由、工具执行、安全检查、存储行为或桌宠动作逻辑时，请同步更新测试。
- Update tests when changing routing, tool execution, security checks, storage behavior, or desktop-pet motion logic.
- UI 改动尽量附带前后截图。
- For UI changes, include before/after screenshots when possible.

## 安全问题 / Security Issues

请不要用公开 issue 报告敏感漏洞或泄露凭据。优先私下联系维护者；如果 GitHub private vulnerability reporting 已启用，也可以使用它。

Please do not open a public issue for sensitive vulnerabilities or leaked credentials. Contact the maintainer privately first, or use GitHub private vulnerability reporting if it is enabled.
