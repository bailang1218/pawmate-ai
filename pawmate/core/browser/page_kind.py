"""Page kind classification and reachability precheck."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PageKindRule:
    kind: str
    url_substrings: tuple = ()
    text_signals: tuple = ()
    selector_signals: tuple = ()


PAGE_KIND_RULES: list = [
    PageKindRule(
        kind="paywalled_course",
        url_substrings=("/cheese/play", "/cheese/ss"),
        text_signals=("购买后观看", "领券购买", "试看中", "为TA充电后即可观看"),
        selector_signals=(".cheese-", ".ep-pay", "purchase"),
    ),
    PageKindRule(
        kind="login_wall",
        text_signals=("登录后查看", "请先登录", "扫码登录", "登录解锁"),
        selector_signals=(".login-wall", ".bili-mini-login"),
    ),
    PageKindRule(
        kind="not_found",
        url_substrings=("/404",),
        text_signals=("视频不存在", "啥都木有", "页面不存在", "稿件不可见"),
    ),
]


def classify_page_kind(url: str, observe_items: list) -> str:
    """Return a page kind string; defaults to normal."""
    url = url or ""
    texts = [str(item.get("text", "")) for item in observe_items]
    selectors = [str(item.get("selector", "")) for item in observe_items]
    for rule in PAGE_KIND_RULES:
        if any(part in url for part in rule.url_substrings):
            return rule.kind
        if rule.text_signals and any(signal in text for text in texts for signal in rule.text_signals):
            return rule.kind
        if rule.selector_signals and any(signal in selector for selector in selectors for signal in rule.selector_signals):
            return rule.kind
    return "normal"


@dataclass(frozen=True)
class GoalBlockRule:
    goal_pattern: re.Pattern
    blocked_by: frozenset
    suggestion: str


GOAL_BLOCK_RULES: list = [
    GoalBlockRule(
        goal_pattern=re.compile(r"(评论|简介|弹幕|description|comment)", re.IGNORECASE),
        blocked_by=frozenset({"paywalled_course"}),
        suggestion="这是付费课程，简介/评论区不可见；要我找免费替代视频吗？",
    ),
    GoalBlockRule(
        goal_pattern=re.compile(r".*"),
        blocked_by=frozenset({"login_wall", "not_found"}),
        suggestion="页面需要登录或不存在；要我换个公开来源吗？",
    ),
]


@dataclass
class Reachability:
    reachable: bool
    page_kind: str
    reason: str = ""
    suggestion: str = ""


def assess_reachability(goal_text: str, url: str, observe_items: list) -> Reachability:
    page_kind = classify_page_kind(url, observe_items)
    if page_kind == "normal":
        return Reachability(reachable=True, page_kind=page_kind)
    for rule in GOAL_BLOCK_RULES:
        if page_kind in rule.blocked_by and rule.goal_pattern.search(goal_text or ""):
            return Reachability(
                reachable=False,
                page_kind=page_kind,
                reason=f"page_kind={page_kind} 无法满足目标",
                suggestion=rule.suggestion,
            )
    return Reachability(reachable=True, page_kind=page_kind)

