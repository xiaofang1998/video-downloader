"""回话模板测试。"""

from __future__ import annotations

from xydl.config import Config
from xydl.models import Order, OrderStatus
from xydl.replies import Replies


def make_order(**kw) -> Order:
    base = dict(id=7, conversation_id="buyer1", title="猫咪打呼噜合集",
                file_path="C:/dl/猫咪打呼噜合集.mp4", file_size=12_345_678,
                status=OrderStatus.DONE.value, chosen_url="https://v.douyin.com/x/")
    base.update(kw)
    return Order(**base)


def test_done_template_includes_file_and_size(config: Config):
    text = Replies(config).done(make_order())
    assert "猫咪打呼噜合集.mp4" in text
    assert "11.8 MB" in text


def test_all_statuses_render_non_empty(config: Config):
    replies = Replies(config)
    for status in (OrderStatus.RECEIVED.value, OrderStatus.DOWNLOADING.value,
                   OrderStatus.DONE.value, OrderStatus.FAILED.value,
                   OrderStatus.NEED_MANUAL.value):
        order = make_order(status=status, error="模拟错误")
        assert replies.reply_for(order).strip()


def test_missing_placeholder_is_kept_not_raised(config: Config):
    """模板里写了不存在的占位符，应该原样保留而不是炸掉整条流水线。"""
    config.set("replies.done", "标题是 {title}，未知字段 {nope}")
    text = Replies(config).done(make_order())
    assert "{nope}" in text
    assert "猫咪打呼噜合集" in text


def test_broken_template_does_not_raise(config: Config):
    config.set("replies.done", "孤立的左括号 { 会让 format 失败")
    assert Replies(config).done(make_order())      # 不抛异常即通过


def test_empty_template_returns_empty(config: Config):
    config.set("replies.done", "")
    assert Replies(config).done(make_order()) == ""


def test_none_values_render_as_empty(config: Config):
    text = Replies(config).done(make_order(file_path="", file_size=0))
    assert "None" not in text


def test_progress_throttling(config: Config):
    config.set("replies.progress_step", 25)
    replies = Replies(config)
    assert replies.should_report_progress(30, 0) is True
    assert replies.should_report_progress(20, 0) is False
    assert replies.should_report_progress(50, 30) is False
    assert replies.should_report_progress(55, 30) is True


def test_progress_step_has_a_sane_floor(config: Config):
    """有人把 step 配成 1 会让买家被刷屏，代码层面兜住。"""
    config.set("replies.progress_step", 1)
    assert Replies(config).step >= 5


def test_failed_reply_stays_short_and_human(config: Config):
    """给买家的话术里不该出现长技术说明（完整错误留在 order.error）。"""
    long_error = (
        "yt-dlp：该站点要求携带浏览器 cookie（抖音/小红书等的常规风控，不是登录问题）。"
        "处理：装一个浏览器并登录该站点后，把 config.json 的 download.cookies_from_browser "
        "填成 edge 或 chrome；或导出 cookies.txt，把路径填到 download.cookies_file"
    )
    text = Replies(config).failed(make_order(status="failed", error=long_error))
    assert len(text) < 120
    assert "cookies.txt" not in text          # 后半段技术细节被砍掉
    assert "下载失败" in text


def test_short_reason_takes_first_clause():
    from xydl.replies import short_reason

    assert short_reason("A；B；C") == "A"
    assert short_reason("A。B") == "A"
    assert short_reason("") == ""
    assert short_reason("没有分隔符的一句很长的话" * 10).endswith("…")


def test_drm_template_mentions_platform(config: Config):
    text = Replies(config).drm(make_order(), "腾讯视频")
    assert "腾讯视频" in text


def test_need_manual_hides_reason_by_default(config: Config):
    text = Replies(config).need_manual(make_order(), reason="内部技术细节")
    assert "内部技术细节" not in text


def test_need_manual_can_append_reason_when_enabled(config: Config):
    config.set("replies.append_reason", True)
    text = Replies(config).need_manual(make_order(), reason="技术细节")
    assert "技术细节" in text
