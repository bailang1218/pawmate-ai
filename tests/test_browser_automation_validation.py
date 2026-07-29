from pawmate.browser_automation.validation import WorkflowValidator


def _workflow(step):
    return {"name": "test", "version": 1, "steps": [step]}


def test_workflow_validator_accepts_safe_semantic_browser_steps():
    value = {
        "name": "read example",
        "version": 1,
        "inputs": {"url": {"type": "string", "required": True}},
        "steps": [
            {"id": "open", "action": "browser.goto", "params": {"url": "{{inputs.url}}"}},
            {"id": "read", "action": "browser.read", "params": {}, "retry": {"max_attempts": 2}},
            {"id": "extract", "action": "browser.extract", "params": {"query": "main content"}},
        ],
    }

    report = WorkflowValidator().validate(value)

    assert report.valid, report.to_dict()


def test_workflow_validator_rejects_unsafe_url_and_arbitrary_script():
    unsafe_url = WorkflowValidator().validate(
        _workflow({"id": "open", "action": "browser.goto", "params": {"url": "javascript:alert(1)"}})
    )
    script = WorkflowValidator().validate(
        _workflow({"id": "script", "action": "browser.evaluate", "params": {"script": "fetch('/secrets')"}})
    )

    assert not unsafe_url.valid
    assert any(issue.code == "unsafe_url" for issue in unsafe_url.issues)
    assert not script.valid
    assert any(issue.code == "unsupported_action" for issue in script.issues)


def test_workflow_validator_rejects_automatic_retry_for_write_action():
    report = WorkflowValidator().validate(
        _workflow(
            {
                "id": "click",
                "action": "browser.click",
                "params": {"intent": "continue"},
                "retry": {"max_attempts": 2},
            }
        )
    )

    assert not report.valid
    assert any(issue.code == "unsafe_retry" for issue in report.issues)


def test_workflow_validator_rejects_unknown_action_parameters():
    report = WorkflowValidator().validate(
        _workflow(
            {
                "id": "open",
                "action": "browser.goto",
                "params": {"url": "https://example.test", "javascript": "alert(1)"},
            }
        )
    )

    assert not report.valid
    assert any(issue.code == "unknown_field" and issue.path.endswith("javascript") for issue in report.issues)


def test_workflow_validator_requires_click_intent_even_when_ref_is_present():
    report = WorkflowValidator().validate(
        _workflow({"id": "click", "action": "browser.click", "params": {"ref": "ref_12"}})
    )

    assert not report.valid
    assert any(issue.path.endswith(".intent") and issue.code == "required" for issue in report.issues)


def test_workflow_validator_rejects_cloud_metadata_target():
    report = WorkflowValidator().validate(
        _workflow({"id": "open", "action": "browser.goto", "params": {"url": "http://169.254.169.254/latest/meta-data"}})
    )

    assert not report.valid
    assert any(issue.code == "unsafe_network_target" for issue in report.issues)


def test_secret_inputs_cannot_persist_defaults_or_literal_passwords():
    workflow = {
        "name": "login",
        "version": 1,
        "inputs": {
            "password": {"type": "string", "secret": True, "default": "do-not-store"},
        },
        "steps": [
            {
                "id": "password",
                "action": "browser.fill",
                "params": {"intent": "fill password", "value": "literal-secret"},
            }
        ],
    }

    report = WorkflowValidator().validate(workflow)
    codes = {issue.code for issue in report.issues}

    assert "secret_default_forbidden" in codes
    assert "secret_input_required" in codes


def test_sensitive_fields_accept_declared_secret_input_template():
    workflow = {
        "name": "login",
        "version": 1,
        "inputs": {"password": {"type": "string", "secret": True, "required": True}},
        "steps": [
            {
                "id": "password",
                "action": "browser.fill",
                "params": {"intent": "fill password", "value": "{{inputs.password}}"},
            }
        ],
    }

    assert WorkflowValidator().validate(workflow).valid is True


def test_input_default_must_match_declared_type():
    report = WorkflowValidator().validate(
        {
            "name": "typed",
            "version": 1,
            "inputs": {"count": {"type": "integer", "default": "not-an-integer"}},
            "steps": [{"id": "read", "action": "browser.read", "params": {}}],
        }
    )

    assert any(issue.code == "invalid_default_type" for issue in report.issues)
