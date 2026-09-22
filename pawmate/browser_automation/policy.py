"""Risk classification and fail-closed policy for browser automation."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

from pawmate.core.browser.boundary import BrowserRisk, classify_browser_action

from .models import RiskDecision, WorkflowStep


_SENSITIVE_WORDS = {
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


class PolicyViolation(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrowserAutomationPolicy:
    """Apply the same risk boundary to RPA and AI-produced steps."""

    def assess(self, step: WorkflowStep, params: dict[str, Any] | None = None) -> RiskDecision:
        resolved = params if params is not None else step.params
        action = step.action
        reasons: list[str] = []
        if action == "browser.goto":
            level = "high" if bool(resolved.get("use_my_login")) else "low"
            if bool(resolved.get("use_my_login")):
                reasons.append("uses_existing_login_session")
            return RiskDecision(level, level == "high", reasons=tuple(reasons))
        if action.startswith("browser.") and action not in {
            "browser.read",
            "browser.extract",
            "browser.wait",
            "browser.wait_for",
            "browser.assert",
        }:
            runtime_action = action.split(".", 1)[1]
            intent = str(resolved.get("intent") or "")
            text = str(resolved.get("text") or resolved.get("value") or "")
            key = str(resolved.get("key") or "+".join(str(item) for item in list(resolved.get("keys") or [])))
            _, risk = classify_browser_action(runtime_action, intent=intent, text=text, key=key)
            level = risk.value
            semantic_text = f"{intent} {str(resolved.get('field') or '')}".lower()
            if action in {"browser.fill", "browser.type", "browser.smart_type"} and any(
                word in semantic_text for word in _SENSITIVE_WORDS
            ):
                level = "high"
                reasons.append("sensitive_input")
            if risk in {BrowserRisk.HIGH, BrowserRisk.CRITICAL}:
                reasons.append("external_side_effect")
            return RiskDecision(
                level=level,
                requires_approval=level in {"high", "critical"},
                critical=level == "critical",
                reasons=tuple(dict.fromkeys(reasons)),
            )
        return RiskDecision("low", False)

    def enforce_url(self, url: str) -> None:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise PolicyViolation("unsafe_url", "Only absolute HTTP(S) URLs are allowed")
        if parsed.username or parsed.password:
            raise PolicyViolation("url_credentials", "Credentials embedded in URLs are forbidden")
        hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
        if hostname in {
            "metadata.google.internal",
            "metadata.azure.internal",
            "instance-data.ec2.internal",
        }:
            raise PolicyViolation("metadata_endpoint", "Cloud instance metadata endpoints are forbidden")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise PolicyViolation("unsafe_network_target", "Link-local, multicast, and unspecified network targets are forbidden")

    def authorize(self, decision: RiskDecision, *, allow_high_risk: bool, allow_critical: bool) -> None:
        if decision.critical and not allow_critical:
            raise PolicyViolation("critical_approval_required", "Critical browser action requires explicit allow_critical=true")
        if decision.requires_approval and not (allow_high_risk or allow_critical):
            raise PolicyViolation("approval_required", "High-risk browser action requires explicit approval")
