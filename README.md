# PawMate AI

PawMate AI is a Windows-first desktop AI assistant built with Python, PySide6, a Web/Qt chat UI, multi-provider LLM routing, local tools, browser automation, memory, and an animated desktop pet.

This repository is a sanitized public alpha package intended for portfolio review and reproducible local runs. It excludes private configuration, logs, browser profiles, conversation history, and API keys.

## Status

- Alpha quality, Windows-first.
- The desktop pet module and PNG frame assets are included so the app can run without broken animation references.
- Users must provide their own model provider API key.
- Packaging into a one-click Windows installer is planned but not part of this repository yet.

## Features

- Web-based chat surface embedded in a PySide6 desktop window.
- Multi-provider LLM support for OpenAI-compatible providers, Anthropic, DeepSeek, Qwen, Gemini, and MiniMax-style routing.
- Tool execution layer with confirmation gates and redaction.
- Browser automation facade for navigation, reading, extracting, and acting on web pages.
- Local conversation storage, long-term memory helpers, and runtime diagnostics.
- Animated desktop pet runtime with standing, sitting/work, and sleeping loops.
- Built-in skill templates for troubleshooting, code review, and weekly reports.

## Architecture

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

## Quick Start

```powershell
git clone https://github.com/your-name/pawmate-ai.git
cd pawmate-ai
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .[dev]
python -m pawmate
```

The virtual environment is intentionally not committed. A `.venv` folder contains machine-specific paths, platform binaries, package caches, and sometimes local metadata. Recreate it from `pyproject.toml` with the commands above after cloning.

For managed Playwright browser automation, install browser binaries after dependencies:

```powershell
python -m playwright install chromium
```

PawMate can also use an installed Edge/Chrome flow depending on configuration.

## Configuration

The repository includes [pawmate/config.json.example](pawmate/config.json.example). On first run, PawMate creates `pawmate/config.json` locally. That file is ignored by Git.

You can configure credentials in the settings UI, or use environment variables such as:

```powershell
$env:DEEPSEEK_API_KEY="..."
$env:OPENAI_API_KEY="..."
$env:ANTHROPIC_API_KEY="..."
$env:QWEN_API_KEY="..."
$env:GEMINI_API_KEY="..."
$env:MINIMAXI_API_KEY="..."
```

Do not commit API keys, tokens, logs, browser profiles, or chat history.

## Tests

```powershell
python -m pytest -q
```

The included tests focus on routing, parser holdback, browser facade behavior, cancellation safety, and desktop-pet motion logic.

## Security Model

- Tool calls are routed through a registry with confirmation levels.
- File and command operations are constrained by runtime security settings.
- Sensitive values are redacted before being logged or surfaced where possible.
- WebSocket access is disabled by default and requires a token when enabled.
- Runtime data lives outside the public source set and is ignored by Git.

## Desktop Pet Assets

The desktop pet code and frame assets are included because the runtime depends on exact frame names and alignment. See [ASSETS_LICENSE.md](ASSETS_LICENSE.md) for the asset boundary. The source code is Apache-2.0 licensed; visual assets have separate usage restrictions.

## Roadmap

- Stabilize first-run setup and provider diagnostics.
- Split the desktop pet into a dedicated installable package after the public baseline is proven.
- Add Windows installer packaging.
- Improve CI coverage for GUI-adjacent modules without requiring a display.
- Add screenshots and a short demo GIF.

## License

Source code is licensed under Apache-2.0. See [LICENSE](LICENSE).

Desktop pet images, icons, and visual assets are documented separately in [ASSETS_LICENSE.md](ASSETS_LICENSE.md).
