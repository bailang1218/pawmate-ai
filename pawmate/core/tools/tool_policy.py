"""Runtime tool policy decisions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


HIGH_RISK_VALUES = {"high", "critical"}
DANGEROUS_SIDE_EFFECTS = {"external_write", "destructive", "privileged"}
AUTO_APPROVE_BLOCKED_RISKS = {"critical"}
AUTO_APPROVE_BLOCKED_SIDE_EFFECTS = {"destructive", "privileged"}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    requires_confirm: bool = False
    risk: str = "low"
    side_effect: str = "read_only"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_tool_result(self, tool_name: str) -> str:
        return f"[policy_denied] {tool_name}: {self.reason}"


def evaluate_tool_policy(tool_def: Any, input_data: dict[str, Any]) -> PolicyDecision:
    risk = str(getattr(tool_def, "risk", "low") or "low")
    side_effect = str(getattr(tool_def, "side_effect", "read_only") or "read_only")
    approval = str(getattr(tool_def, "approval", "auto") or "auto")
    runtime_policy = getattr(tool_def, "runtime_policy", None)
    if callable(runtime_policy):
        overrides = runtime_policy(dict(input_data or {}))
        if not isinstance(overrides, dict):
            return PolicyDecision(
                allowed=False,
                reason="runtime policy resolver returned invalid metadata",
                risk=risk,
                side_effect=side_effect,
            )
        risk = str(overrides.get("risk") or risk)
        side_effect = str(overrides.get("side_effect") or side_effect)
        approval = str(overrides.get("approval") or approval)
    requires_confirm = bool(getattr(tool_def, "requires_confirm", False) or approval == "confirm")
    if approval != "confirm":
        requires_confirm = False

    if getattr(tool_def, "model_visible", True) is False and not getattr(tool_def, "allow_hidden_execute", False):
        return PolicyDecision(
            allowed=False,
            reason="hidden tool is not executable through runtime policy",
            risk=risk,
            side_effect=side_effect,
        )

    if (risk in HIGH_RISK_VALUES or side_effect in DANGEROUS_SIDE_EFFECTS) and approval != "confirm":
        return PolicyDecision(
            allowed=False,
            reason="high-risk side-effect tool requires confirm approval",
            requires_confirm=True,
            risk=risk,
            side_effect=side_effect,
        )

    return PolicyDecision(
        allowed=True,
        reason="requires_confirmation" if requires_confirm else "allowed",
        requires_confirm=requires_confirm,
        risk=risk,
        side_effect=side_effect,
        metadata={
            "approval": approval,
            "argument_keys": sorted(str(key) for key in input_data.keys()),
        },
    )


def can_auto_approve(tool_def: Any) -> bool:
    risk = str(getattr(tool_def, "risk", "low") or "low")
    side_effect = str(getattr(tool_def, "side_effect", "read_only") or "read_only")
    return risk not in HIGH_RISK_VALUES and side_effect not in DANGEROUS_SIDE_EFFECTS


def can_auto_approve_request(tool_def: Any, input_data: dict[str, Any]) -> bool:
    """Honor auto-approve using the effective policy for this invocation.

    Auto-approve intentionally covers ordinary external interaction such as
    browser clicks and typing. Critical, destructive, and privileged actions
    remain confirmation-only even when the global switch is enabled.
    """
    decision = evaluate_tool_policy(tool_def, input_data)
    return (
        decision.allowed
        and decision.risk not in AUTO_APPROVE_BLOCKED_RISKS
        and decision.side_effect not in AUTO_APPROVE_BLOCKED_SIDE_EFFECTS
    )
