# Changelog

## 0.1.0a1 — 2026-09-22

This source-only alpha publishes the vNext core runtime update. Windows and Python 3.11 are the validation target; a standalone installer is not included.

- Separates runtime, model routing, safety, tools, services, and observability responsibilities.
- Adds browser automation workbench integration, an Agent CLI bridge, layered memory, checkpoints, and trace contracts.
- Expands regression coverage for cancellation, evidence, runtime completion, and Windows shell transport.
- Aligns package installation dependencies with the runtime requirements and checks import boundaries in CI.

Upgrade in a virtual environment with `python -m pip install -e .[dev]`. Keep existing local configuration and runtime data outside version control. Configure your own model credentials; live-provider behavior and the full desktop UI require separate acceptance testing.
