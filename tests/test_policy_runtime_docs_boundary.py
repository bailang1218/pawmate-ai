from pathlib import Path


DOCS = [
    "architecture.md",
    "context_boundary.md",
    "message_roles.md",
    "tool_call_lifecycle.md",
    "observation_contract.md",
    "policy_engine.md",
    "browser_boundary.md",
    "memory_boundary.md",
    "trace_and_replay.md",
    "ui_event_boundary.md",
    "plugin_boundary.md",
]


def test_runtime_boundary_contract_docs_exist_and_have_required_sections():
    docs_dir = Path(__file__).resolve().parents[1] / "docs" / "runtime"

    for name in DOCS:
        text = (docs_dir / name).read_text(encoding="utf-8")
        assert "## Responsibility" in text
        assert "## Inputs" in text
        assert "## Outputs" in text
        assert "## Invariants" in text
        assert "## Forbidden" in text
        assert "## Tests" in text
        assert "## Code Paths" in text
