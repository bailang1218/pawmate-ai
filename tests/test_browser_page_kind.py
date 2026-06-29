from pawmate.core.browser.page_kind import assess_reachability, classify_page_kind


def test_cheese_url_is_paywalled():
    items = [{"text": "试看中，购买后观看完整视频", "selector": "div.cheese-player"}]
    assert classify_page_kind("https://www.bilibili.com/cheese/play/ss162073687", items) == "paywalled_course"


def test_normal_video_is_normal():
    items = [{"text": "AI Agent 教程", "selector": "a.video-title", "href": "/video/BV1xx"}]
    assert classify_page_kind("https://www.bilibili.com/video/BV1xx", items) == "normal"


def test_paywalled_blocks_comment_goal():
    items = [{"text": "领券购买", "selector": "div.cheese-pay"}]
    result = assess_reachability("根据视频简介在评论区写个概览", "https://www.bilibili.com/cheese/play/ss1", items)
    assert not result.reachable and result.page_kind == "paywalled_course" and result.suggestion


def test_normal_video_comment_goal_reachable():
    items = [{"text": "评论 1.2万", "selector": "div.reply-list", "href": ""}]
    result = assess_reachability("在评论区写个概览", "https://www.bilibili.com/video/BV1xx", items)
    assert result.reachable and result.page_kind == "normal"


def test_login_wall_blocks_any_goal():
    items = [{"text": "请先登录", "selector": ".bili-mini-login"}]
    result = assess_reachability("随便看看", "https://www.bilibili.com/x", items)
    assert not result.reachable and result.page_kind == "login_wall"

