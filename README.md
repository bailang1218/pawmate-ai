# PawMate AI

> 中文 / English

PawMate AI 是一个 Windows 优先的桌面 AI 助手，使用 Python、PySide6、Web/Qt 聊天界面、多模型供应商路由、本地工具、浏览器自动化、记忆模块和动画桌宠组成。

PawMate AI is a Windows-first desktop AI assistant built with Python, PySide6, a Web/Qt chat UI, multi-provider LLM routing, local tools, browser automation, memory, and an animated desktop pet.

这是一个脱敏后的公开 Alpha 包，主要用于作品集展示和可复现的本地运行。仓库不包含私有配置、日志、浏览器配置、聊天记录或 API keys。

This is a sanitized public alpha package for portfolio review and reproducible local runs. It excludes private configuration, logs, browser profiles, conversation history, and API keys.

## 项目状态 / Status

- Alpha 阶段，当前以 Windows 为主要目标平台。
- Alpha quality, currently Windows-first.
- 已包含桌宠模块和 PNG 帧资源，避免 clone 后动画引用缺失。
- Desktop pet runtime and PNG frame assets are included so the app can run without broken animation references.
- 用户需要自行配置模型供应商 API key。
- Users must provide their own model provider API key.
- 一键 Windows 安装包/EXE 是计划项，但还不是这个公开包的一部分。
- One-click Windows installer/EXE packaging is planned, but not included in this public package yet.

## 功能 / Features

- 嵌入 PySide6 桌面窗口的 Web 聊天界面。
- Web-based chat surface embedded in a PySide6 desktop window.
- 支持 OpenAI-compatible、Anthropic、DeepSeek、Qwen、Gemini、MiniMax 风格的多供应商路由。
- Multi-provider LLM support for OpenAI-compatible providers, Anthropic, DeepSeek, Qwen, Gemini, and MiniMax-style routing.
- 带确认门和脱敏处理的工具执行层。
- Tool execution layer with confirmation gates and redaction.
- 浏览器自动化封装，支持导航、读取、提取和网页操作。
- Browser automation facade for navigation, reading, extracting, and acting on web pages.
- 本地会话存储、长期记忆辅助和运行时诊断。
- Local conversation storage, long-term memory helpers, and runtime diagnostics.
- 动画桌宠运行时，包含站立、坐下/工作、睡眠等循环。
- Animated desktop pet runtime with standing, sitting/work, and sleeping loops.
- 内置故障排查、代码审查、周报等技能模板。
- Built-in skill templates for troubleshooting, code review, and weekly reports.

## 结构 / Architecture

```text
pawmate/
  main.py                 Application bootstrap and runtime wiring
  app/                    Window and service wiring
  bridge/                 Qt/Web bridge contracts and websocket bridge
  core/                   Agent engine, LLM routing, tools, prompts, security
  tools/                  Built-in tools, browser adapters, MCP bridge
  memory/                 Memory stores and summarization helpers
  storage/                Local config, history, and conversation stores
  ui/                     PySide6 shell and Web UI assets
  desktop_pet/            Pet runtime, motion graph, loop configs, PNG assets
  skills/                 Built-in skill templates and import/install support
tests/                    Logic and routing tests
```

## 快速开始 / Quick Start

```powershell
git clone https://github.com/your-name/pawmate-ai.git
cd pawmate-ai
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .[dev]
python -m pawmate
```

虚拟环境不会提交到仓库。`.venv` 通常包含本机路径、平台相关二进制文件、包缓存和本地元数据；clone 后请根据 `pyproject.toml` 用上面的命令重新创建。

The virtual environment is intentionally not committed. A `.venv` folder usually contains machine-specific paths, platform binaries, package caches, and local metadata. Recreate it from `pyproject.toml` with the commands above after cloning.

如果需要 Playwright 管理的浏览器自动化，请在安装依赖后安装 Chromium：

For managed Playwright browser automation, install Chromium after dependencies:

```powershell
python -m playwright install chromium
```

根据配置，PawMate 也可以使用本机已安装的 Edge/Chrome 流程。

Depending on configuration, PawMate can also use an installed Edge/Chrome flow.

## 配置 / Configuration

仓库包含 [pawmate/config.json.example](pawmate/config.json.example)。首次运行时，PawMate 会在本地创建 `pawmate/config.json`，该文件已被 Git 忽略。

The repository includes [pawmate/config.json.example](pawmate/config.json.example). On first run, PawMate creates `pawmate/config.json` locally. That file is ignored by Git.

你可以在设置界面配置凭据，也可以使用环境变量：

You can configure credentials in the settings UI, or use environment variables:

```powershell
$env:DEEPSEEK_API_KEY="..."
$env:OPENAI_API_KEY="..."
$env:ANTHROPIC_API_KEY="..."
$env:QWEN_API_KEY="..."
$env:GEMINI_API_KEY="..."
$env:MINIMAXI_API_KEY="..."
$env:MINIMAXI_GROUP_ID="..."
```

不要提交 API keys、tokens、日志、浏览器 profiles 或聊天记录。

Do not commit API keys, tokens, logs, browser profiles, or chat history.

## 测试 / Tests

```powershell
python -m pytest -q
```

当前测试主要覆盖路由、工具解析 holdback、浏览器封装、取消安全和桌宠动作逻辑。

The included tests focus on routing, parser holdback, browser facade behavior, cancellation safety, and desktop-pet motion logic.

## 安全模型 / Security Model

- 工具调用通过注册表路由，并带有确认级别。
- Tool calls are routed through a registry with confirmation levels.
- 文件和命令操作受运行时安全设置约束。
- File and command operations are constrained by runtime security settings.
- 敏感值会尽可能在日志或界面展示前脱敏。
- Sensitive values are redacted before being logged or surfaced where possible.
- WebSocket 默认关闭，启用时需要 token。
- WebSocket access is disabled by default and requires a token when enabled.
- 运行时数据不属于公开源码集合，并被 Git 忽略。
- Runtime data lives outside the public source set and is ignored by Git.

## 桌宠素材 / Desktop Pet Assets

桌宠代码和帧资源包含在仓库中，因为运行时依赖精确的帧名称和对齐方式。源码采用 Apache-2.0 许可；图片、头像、图标和其他视觉资产有单独边界，请查看 [ASSETS_LICENSE.md](ASSETS_LICENSE.md)。

The desktop pet code and frame assets are included because the runtime depends on exact frame names and alignment. Source code is licensed under Apache-2.0; images, avatars, icons, and other visual assets have separate boundaries documented in [ASSETS_LICENSE.md](ASSETS_LICENSE.md).

## 路线图 / Roadmap

- 稳定首次运行流程和供应商诊断。
- Stabilize first-run setup and provider diagnostics.
- 在公开基线验证后，将桌宠拆成独立可安装包。
- Split the desktop pet into a dedicated installable package after the public baseline is proven.
- 添加 Windows 安装器/EXE 打包。
- Add Windows installer/EXE packaging.
- 提升 GUI 邻近模块的 CI 覆盖。
- Improve CI coverage for GUI-adjacent modules without requiring a display.
- 添加截图和短演示 GIF。
- Add screenshots and a short demo GIF.

## 许可证 / License

源码使用 Apache-2.0 许可，详见 [LICENSE](LICENSE)。

Source code is licensed under Apache-2.0. See [LICENSE](LICENSE).

桌宠图片、头像、图标和视觉资产在 [ASSETS_LICENSE.md](ASSETS_LICENSE.md) 中单独说明。

Desktop pet images, icons, and visual assets are documented separately in [ASSETS_LICENSE.md](ASSETS_LICENSE.md).
