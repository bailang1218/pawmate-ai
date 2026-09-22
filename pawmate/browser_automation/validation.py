"""Strict validation for PawMate browser automation workflow JSON."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from .models import ValidationIssue, ValidationReport
from .policy import BrowserAutomationPolicy, PolicyViolation


MAX_WORKFLOW_BYTES = 256 * 1024
MAX_STEPS = 200
MAX_NESTING = 8
MAX_TIMEOUT_MS = 120_000
MAX_ITERATIONS = 100
ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
VAR_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,127}$")
TEMPLATE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")

ALLOWED_ACTIONS = {
    "browser.goto",
    "browser.read",
    "browser.click",
    "browser.fill",
    "browser.type",
    "browser.smart_type",
    "browser.key",
    "browser.hotkey",
    "browser.scroll",
    "browser.extract",
    "browser.wait",
    "browser.wait_for",
    "browser.assert",
    "data.set",
    "control.if",
    "control.foreach",
}

READ_ONLY_ACTIONS = {
    "browser.goto",
    "browser.read",
    "browser.extract",
    "browser.wait",
    "browser.wait_for",
    "browser.assert",
    "data.set",
    "control.if",
    "control.foreach",
}

ACTION_PARAM_KEYS: dict[str, set[str]] = {
    "browser.goto": {"url", "use_my_login", "goal", "visibility"},
    "browser.read": {"goal", "visibility"},
    "browser.click": {"ref", "intent", "visibility"},
    "browser.fill": {"ref", "intent", "field", "value", "clear", "visibility"},
    "browser.type": {"ref", "intent", "field", "value", "clear", "visibility"},
    "browser.smart_type": {"ref", "intent", "field", "text", "clear", "visibility"},
    "browser.key": {"key", "intent", "visibility"},
    "browser.hotkey": {"keys", "intent", "visibility"},
    "browser.scroll": {"deltaX", "deltaY", "intent", "visibility"},
    "browser.extract": {"query", "visibility"},
    "browser.wait": {"duration_ms"},
    "browser.wait_for": {"condition", "interval_ms", "goal", "visibility"},
    "browser.assert": {"condition", "observe", "message", "goal", "visibility"},
    "data.set": {"name", "value"},
    "control.if": {"condition", "then", "else"},
    "control.foreach": {"items", "item", "steps", "max_iterations"},
}

ALLOWED_KEYS = {
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "Delete",
    "Space",
    "PageUp",
    "PageDown",
    "Home",
    "End",
}

_SENSITIVE_INPUT_WORDS = {
    "password",
    "passcode",
    "otp",
    "verification code",
    "credit card",
    "card number",
    "cvv",
    "密码",
    "验证码",
    "银行卡",
}


class WorkflowValidator:
    """Validate the versioned workflow DSL without executing user content."""

    def validate(self, value: Any) -> ValidationReport:
        issues: list[ValidationIssue] = []
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return ValidationReport((self._issue("$", "not_json", f"Workflow must be JSON serializable: {exc}"),))
        if len(encoded) > MAX_WORKFLOW_BYTES:
            issues.append(self._issue("$", "payload_too_large", f"Workflow exceeds {MAX_WORKFLOW_BYTES} bytes"))
        if not isinstance(value, dict):
            issues.append(self._issue("$", "invalid_type", "Workflow must be an object"))
            return ValidationReport(tuple(issues))

        allowed_top = {"id", "name", "version", "description", "status", "inputs", "steps", "created_at", "updated_at"}
        self._unknown_keys(value, allowed_top, "$", issues)
        workflow_id = value.get("id")
        if workflow_id not in (None, "") and not ID_RE.fullmatch(str(workflow_id)):
            issues.append(self._issue("$.id", "invalid_id", "id must match ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$"))
        if not isinstance(value.get("name"), str) or not str(value.get("name") or "").strip():
            issues.append(self._issue("$.name", "required", "name is required"))
        elif len(str(value["name"])) > 120:
            issues.append(self._issue("$.name", "too_long", "name must be at most 120 characters"))
        version = value.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 1_000_000:
            issues.append(self._issue("$.version", "invalid_version", "version must be an integer from 1 to 1000000"))
        if value.get("status", "draft") not in {"draft", "published", "archived"}:
            issues.append(self._issue("$.status", "invalid_status", "status must be draft, published, or archived"))

        secret_inputs = self._validate_inputs(value.get("inputs", {}), issues)
        steps = value.get("steps")
        if not isinstance(steps, list) or not steps:
            issues.append(self._issue("$.steps", "required", "steps must be a non-empty array"))
            return ValidationReport(tuple(issues))

        seen: set[str] = set()
        counter = [0]
        self._validate_steps(steps, "$.steps", 1, seen, counter, issues, secret_inputs)
        if counter[0] > MAX_STEPS:
            issues.append(self._issue("$.steps", "too_many_steps", f"Workflow contains {counter[0]} steps; maximum is {MAX_STEPS}"))
        return ValidationReport(tuple(issues))

    def _validate_inputs(self, inputs: Any, issues: list[ValidationIssue]) -> set[str]:
        secret_inputs: set[str] = set()
        if not isinstance(inputs, dict):
            issues.append(self._issue("$.inputs", "invalid_type", "inputs must be an object"))
            return secret_inputs
        if len(inputs) > 64:
            issues.append(self._issue("$.inputs", "too_many_inputs", "At most 64 inputs are allowed"))
        for name, spec in inputs.items():
            path = f"$.inputs.{name}"
            if not VAR_RE.fullmatch(str(name)):
                issues.append(self._issue(path, "invalid_name", "Input name is invalid"))
            if not isinstance(spec, dict):
                issues.append(self._issue(path, "invalid_type", "Input specification must be an object"))
                continue
            self._unknown_keys(spec, {"type", "required", "default", "secret", "description"}, path, issues)
            input_type = spec.get("type", "string")
            if input_type not in {"string", "integer", "number", "boolean", "array", "object"}:
                issues.append(self._issue(path + ".type", "invalid_type_name", "Unsupported input type"))
            if "required" in spec and not isinstance(spec["required"], bool):
                issues.append(self._issue(path + ".required", "invalid_type", "required must be boolean"))
            if "secret" in spec and not isinstance(spec["secret"], bool):
                issues.append(self._issue(path + ".secret", "invalid_type", "secret must be boolean"))
            if spec.get("secret") is True:
                secret_inputs.add(str(name))
                if spec.get("default") is not None:
                    issues.append(
                        self._issue(
                            path + ".default",
                            "secret_default_forbidden",
                            "Secret inputs cannot define persisted default values",
                        )
                    )
            elif "default" in spec and spec.get("default") is not None:
                if not self._input_type_matches(spec.get("default"), str(input_type)):
                    issues.append(
                        self._issue(
                            path + ".default",
                            "invalid_default_type",
                            f"default must match input type {input_type}",
                        )
                    )
        return secret_inputs

    def _validate_steps(
        self,
        steps: Any,
        path: str,
        depth: int,
        seen: set[str],
        counter: list[int],
        issues: list[ValidationIssue],
        secret_inputs: set[str],
    ) -> None:
        if depth > MAX_NESTING:
            issues.append(self._issue(path, "nesting_too_deep", f"Maximum nesting depth is {MAX_NESTING}"))
            return
        if not isinstance(steps, list):
            issues.append(self._issue(path, "invalid_type", "Nested steps must be an array"))
            return
        for index, step in enumerate(steps):
            step_path = f"{path}[{index}]"
            counter[0] += 1
            if not isinstance(step, dict):
                issues.append(self._issue(step_path, "invalid_type", "Step must be an object"))
                continue
            self._unknown_keys(step, {"id", "action", "params", "timeout_ms", "retry", "on_error", "save_as"}, step_path, issues)
            step_id = str(step.get("id") or "")
            if not ID_RE.fullmatch(step_id):
                issues.append(self._issue(step_path + ".id", "invalid_id", "Step id is required and must be a safe identifier"))
            elif step_id in seen:
                issues.append(self._issue(step_path + ".id", "duplicate_id", f"Duplicate step id: {step_id}"))
            else:
                seen.add(step_id)
            action = str(step.get("action") or "")
            if action not in ALLOWED_ACTIONS:
                issues.append(self._issue(step_path + ".action", "unsupported_action", f"Unsupported action: {action or '(empty)'}"))
            timeout = step.get("timeout_ms", 30_000)
            if not isinstance(timeout, int) or isinstance(timeout, bool) or not 100 <= timeout <= MAX_TIMEOUT_MS:
                issues.append(self._issue(step_path + ".timeout_ms", "invalid_timeout", f"timeout_ms must be 100..{MAX_TIMEOUT_MS}"))
            if step.get("on_error", "stop") not in {"stop", "continue"}:
                issues.append(self._issue(step_path + ".on_error", "invalid_on_error", "on_error must be stop or continue"))
            save_as = step.get("save_as", "")
            if save_as and not VAR_RE.fullmatch(str(save_as)):
                issues.append(self._issue(step_path + ".save_as", "invalid_name", "save_as is invalid"))
            params = step.get("params", {})
            if not isinstance(params, dict):
                issues.append(self._issue(step_path + ".params", "invalid_type", "params must be an object"))
                continue
            self._validate_retry(step.get("retry"), action, step_path, issues)
            self._validate_action(
                action,
                params,
                step_path + ".params",
                depth,
                seen,
                counter,
                issues,
                secret_inputs,
            )

    def _validate_retry(self, retry: Any, action: str, path: str, issues: list[ValidationIssue]) -> None:
        if retry is None:
            return
        if not isinstance(retry, dict):
            issues.append(self._issue(path + ".retry", "invalid_type", "retry must be an object"))
            return
        self._unknown_keys(retry, {"max_attempts", "backoff_ms", "retry_on"}, path + ".retry", issues)
        attempts = retry.get("max_attempts", 1)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 3:
            issues.append(self._issue(path + ".retry.max_attempts", "invalid_attempts", "max_attempts must be 1..3"))
        elif attempts > 1 and action not in READ_ONLY_ACTIONS:
            issues.append(self._issue(path + ".retry.max_attempts", "unsafe_retry", "Mutating browser actions cannot be retried automatically"))
        backoff = retry.get("backoff_ms", 250)
        if not isinstance(backoff, int) or isinstance(backoff, bool) or not 0 <= backoff <= 10_000:
            issues.append(self._issue(path + ".retry.backoff_ms", "invalid_backoff", "backoff_ms must be 0..10000"))
        retry_on = retry.get("retry_on", [])
        if retry_on and (not isinstance(retry_on, list) or len(retry_on) > 16 or not all(isinstance(item, str) for item in retry_on)):
            issues.append(self._issue(path + ".retry.retry_on", "invalid_retry_on", "retry_on must be an array of at most 16 strings"))

    def _validate_action(
        self,
        action: str,
        params: dict[str, Any],
        path: str,
        depth: int,
        seen: set[str],
        counter: list[int],
        issues: list[ValidationIssue],
        secret_inputs: set[str],
    ) -> None:
        if action in ACTION_PARAM_KEYS:
            self._unknown_keys(params, ACTION_PARAM_KEYS[action], path, issues)
        if "visibility" in params and params.get("visibility") not in {"auto", "background", "foreground"}:
            issues.append(self._issue(path + ".visibility", "invalid_visibility", "visibility must be auto, background, or foreground"))
        if action == "browser.goto":
            self._required_string(params, "url", path, issues)
            url = params.get("url")
            if isinstance(url, str) and "{{" not in url:
                self._validate_url(url, path + ".url", issues)
        elif action in {"browser.click"}:
            if not self._nonempty_string(params.get("intent")):
                issues.append(self._issue(path + ".intent", "required", "click requires intent for risk classification"))
        elif action in {"browser.fill", "browser.type", "browser.smart_type"}:
            if not self._nonempty_string(params.get("ref")) and not self._nonempty_string(params.get("intent")):
                issues.append(self._issue(path, "missing_target", f"{action} requires ref or intent"))
            if not self._nonempty_string(params.get("intent")):
                issues.append(self._issue(path + ".intent", "required", f"{action} requires intent for risk classification"))
            value_key = "text" if action == "browser.smart_type" else "value"
            if value_key not in params:
                issues.append(self._issue(path + "." + value_key, "required", f"{value_key} is required"))
            semantic = f"{params.get('intent', '')} {params.get('field', '')}".lower()
            if any(word in semantic for word in _SENSITIVE_INPUT_WORDS):
                value = params.get(value_key)
                match = TEMPLATE_RE.fullmatch(str(value or ""))
                expression = match.group(1).strip() if match else ""
                if not expression.startswith("inputs.") or expression[7:] not in secret_inputs:
                    issues.append(
                        self._issue(
                            path + "." + value_key,
                            "secret_input_required",
                            "Sensitive fields must reference an input declared with secret=true",
                        )
                    )
        elif action == "browser.key":
            self._required_string(params, "key", path, issues)
            if params.get("key") not in ALLOWED_KEYS:
                issues.append(self._issue(path + ".key", "unsupported_key", "Unsupported browser key"))
        elif action == "browser.hotkey":
            keys = params.get("keys")
            if not isinstance(keys, list) or not keys or len(keys) > 4 or not all(isinstance(item, str) for item in keys):
                issues.append(self._issue(path + ".keys", "invalid_keys", "keys must contain 1..4 strings"))
        elif action == "browser.scroll":
            for key in ("deltaX", "deltaY"):
                if key in params and (not isinstance(params[key], int) or isinstance(params[key], bool) or abs(params[key]) > 100_000):
                    issues.append(self._issue(path + "." + key, "invalid_delta", f"{key} must be an integer within +/-100000"))
        elif action == "browser.wait":
            duration = params.get("duration_ms", 500)
            if not isinstance(duration, int) or isinstance(duration, bool) or not 0 <= duration <= 30_000:
                issues.append(self._issue(path + ".duration_ms", "invalid_duration", "duration_ms must be 0..30000"))
        elif action in {"browser.wait_for", "browser.assert"}:
            self._validate_condition(params.get("condition"), path + ".condition", issues)
            if action == "browser.wait_for":
                interval = params.get("interval_ms", 500)
                if not isinstance(interval, int) or isinstance(interval, bool) or not 100 <= interval <= 5000:
                    issues.append(self._issue(path + ".interval_ms", "invalid_interval", "interval_ms must be 100..5000"))
        elif action == "data.set":
            self._required_identifier(params, "name", path, issues)
            if "value" not in params:
                issues.append(self._issue(path + ".value", "required", "value is required"))
        elif action == "control.if":
            self._validate_condition(params.get("condition"), path + ".condition", issues)
            self._validate_steps(
                params.get("then", []), path + ".then", depth + 1, seen, counter, issues, secret_inputs
            )
            if "else" in params:
                self._validate_steps(
                    params.get("else"), path + ".else", depth + 1, seen, counter, issues, secret_inputs
                )
        elif action == "control.foreach":
            if "items" not in params:
                issues.append(self._issue(path + ".items", "required", "items is required"))
            self._required_identifier(params, "item", path, issues)
            max_iterations = params.get("max_iterations", 20)
            if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or not 1 <= max_iterations <= MAX_ITERATIONS:
                issues.append(self._issue(path + ".max_iterations", "invalid_iterations", f"max_iterations must be 1..{MAX_ITERATIONS}"))
            self._validate_steps(
                params.get("steps", []), path + ".steps", depth + 1, seen, counter, issues, secret_inputs
            )

        self._validate_templates(params, path, issues)

    def _validate_condition(self, condition: Any, path: str, issues: list[ValidationIssue]) -> None:
        if not isinstance(condition, dict):
            issues.append(self._issue(path, "invalid_condition", "condition must be an object"))
            return
        self._unknown_keys(condition, {"left", "op", "right"}, path, issues)
        if "left" not in condition:
            issues.append(self._issue(path + ".left", "required", "left is required"))
        op = condition.get("op", "truthy")
        if op not in {"eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte", "exists", "truthy", "falsy"}:
            issues.append(self._issue(path + ".op", "invalid_operator", "Unsupported condition operator"))
        if op not in {"exists", "truthy", "falsy"} and "right" not in condition:
            issues.append(self._issue(path + ".right", "required", "right is required for this operator"))

    def _validate_templates(self, value: Any, path: str, issues: list[ValidationIssue]) -> None:
        if isinstance(value, str):
            if len(value) > 32_000:
                issues.append(self._issue(path, "string_too_long", "String value exceeds 32000 characters"))
            for match in TEMPLATE_RE.finditer(value):
                if not VAR_RE.fullmatch(match.group(1).strip()):
                    issues.append(self._issue(path, "invalid_template", f"Invalid template variable: {match.group(1)}"))
        elif isinstance(value, dict):
            for key, item in value.items():
                self._validate_templates(item, f"{path}.{key}", issues)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._validate_templates(item, f"{path}[{index}]", issues)

    def _validate_url(self, value: str, path: str, issues: list[ValidationIssue]) -> None:
        parsed = urlparse(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            issues.append(self._issue(path, "unsafe_url", "Only absolute http:// and https:// URLs are allowed"))
        if parsed.username or parsed.password:
            issues.append(self._issue(path, "url_credentials", "Credentials must not be embedded in URLs"))
        try:
            BrowserAutomationPolicy().enforce_url(value)
        except PolicyViolation as exc:
            if not any(issue.path == path and issue.code == exc.code for issue in issues):
                issues.append(self._issue(path, exc.code, str(exc)))

    def _required_string(self, params: dict[str, Any], key: str, path: str, issues: list[ValidationIssue]) -> None:
        if not self._nonempty_string(params.get(key)):
            issues.append(self._issue(path + "." + key, "required", f"{key} is required"))

    def _required_identifier(self, params: dict[str, Any], key: str, path: str, issues: list[ValidationIssue]) -> None:
        if not VAR_RE.fullmatch(str(params.get(key) or "")):
            issues.append(self._issue(path + "." + key, "invalid_name", f"{key} must be a safe identifier"))

    @staticmethod
    def _nonempty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _input_type_matches(value: Any, expected: str) -> bool:
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "array":
            return isinstance(value, list)
        if expected == "object":
            return isinstance(value, dict)
        return False

    @staticmethod
    def _unknown_keys(value: dict[str, Any], allowed: set[str], path: str, issues: list[ValidationIssue]) -> None:
        for key in value:
            if key not in allowed:
                issues.append(ValidationIssue(f"{path}.{key}", "unknown_field", f"Unknown field: {key}"))

    @staticmethod
    def _issue(path: str, code: str, message: str) -> ValidationIssue:
        return ValidationIssue(path=path, code=code, message=message)
