from pawmate.core.browser.escalation import EscalationLadder
from pawmate.core.browser.surface import BrowserSurface, Modality, Target


def native_dom():
    return BrowserSurface(Target.NATIVE, Modality.DOM)


def test_cdp_fail_with_login_does_not_downgrade():
    ladder = EscalationLadder()
    decision = ladder.on_cdp_attach_failed(use_my_login=True, current=native_dom())
    assert decision.action == "ask_user"
    assert decision.to_target is None
    assert ladder.events[-1].to_surface == "(halt)"
    assert ladder.events[-1].reason == "login_required"


def test_cdp_fail_without_login_downgrades_to_managed():
    ladder = EscalationLadder()
    decision = ladder.on_cdp_attach_failed(use_my_login=False, current=native_dom())
    assert decision.action == "switch"
    assert decision.to_target == Target.MANAGED and decision.to_modality == Modality.DOM


def test_dom_empty_escalates_to_vision_same_target():
    ladder = EscalationLadder()
    decision = ladder.on_dom_empty(current=native_dom())
    assert decision.action == "vision"
    assert decision.to_target == Target.NATIVE
    assert decision.to_modality == Modality.VISION


def test_vision_unlocated_retries_then_stops():
    ladder = EscalationLadder(vision_retry_budget=1)
    current = BrowserSurface(Target.NATIVE, Modality.VISION)
    first = ladder.on_vision_unlocated(current=current)
    second = ladder.on_vision_unlocated(current=current)
    assert first.action == "retry"
    assert second.action == "stop"


def test_every_transition_emits_typed_event():
    ladder = EscalationLadder()
    ladder.on_dom_empty(current=native_dom())
    event = ladder.events[-1]
    assert event.from_surface and event.to_surface and event.reason
    assert set(event.as_dict().keys()) == {"from", "to", "reason", "evidence"}
