from pawmate.core.tools.tool_result_budget import budget_tool_result


def test_search_memory_ui_hides_data_only_memory_content():
    result = (
        "Found 1 data-only relevant memories:\n"
        "1. very private memory body (kind=EPISODIC_MEMORY; data_only=true)"
    )

    views = budget_tool_result("search_memory", result)

    assert "very private memory body" not in views.ui
    assert "已检索到 1 条相关记忆" in views.ui
    assert "very private memory body" in views.model
