"""链接识别测试 —— 这是整套系统最容易出错、也最值得测的地方。

用例全部取自真实的分享文案格式。改正则之前先跑这个。
"""

from __future__ import annotations

import pytest

from xydl import linkparse
from xydl.linkparse import (
    detect_platform,
    extract_links,
    guess_title,
    is_short_link,
    normalize_url,
    parse,
)

# ── 平台判定 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://v.douyin.com/iRNBho6/", "douyin"),
    ("https://www.douyin.com/video/7123456", "douyin"),
    ("https://www.iesdouyin.com/share/video/7123456/", "douyin"),
    ("https://v.kuaishou.com/abc", "kuaishou"),
    ("https://www.kuaishou.com/short-video/123", "kuaishou"),
    ("https://xhslink.com/a/xxxx", "xiaohongshu"),
    ("https://www.xiaohongshu.com/explore/123", "xiaohongshu"),
    ("https://b23.tv/BV1xx411c7mD", "bilibili"),
    ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/watch?v=x", "youtube"),
    ("https://x.com/u/status/1", "twitter"),
    ("https://v.qq.com/x/cover/a.html", "tencent_video"),
    ("https://m.tb.cn/h.5xYz", "xianyu"),
    ("https://example.com/x", None),
    # ── 新增平台：国内社区视频 ──────────────────────────────────
    ("https://www.acfun.cn/v/ac12345", "acfun"),
    ("https://www.acfun.com/v/ac12345", "acfun"),
    # ── 新增平台：海外社区视频 / 短视频 ─────────────────────────
    ("https://www.reddit.com/r/funny/comments/abc/", "reddit"),
    ("https://redd.it/abc", "reddit"),
    ("https://rumble.com/v1234.html", "rumble"),
    ("https://streamable.com/abc", "streamable"),
    ("https://www.nicovideo.jp/watch/sm12345", "niconico"),
    ("https://nico.ms/sm12345", "niconico"),
    ("https://9gag.com/gag/abc", "9gag"),
    ("https://coub.com/view/abc", "coub"),
    # ── 新增平台：音频 / 播客 / 音乐 ────────────────────────────
    ("https://soundcloud.com/user/track", "soundcloud"),
    ("https://bandcamp.com/track/abc", "bandcamp"),
    ("https://www.mixcloud.com/user/track/", "mixcloud"),
])
def test_detect_platform(url, expected):
    rule = detect_platform(url)
    assert (rule.key if rule else None) == expected


@pytest.mark.parametrize("url,is_short", [
    # 短链判定必须按**域名**，不能按平台：同一个平台两种链接形态
    ("https://v.douyin.com/iRNBho6/", True),
    ("https://www.douyin.com/video/123", False),
    ("https://b23.tv/abc", True),
    ("https://www.bilibili.com/video/BV1xx411c7mD", False),
    ("https://youtu.be/x", True),
    ("https://www.youtube.com/watch?v=x", False),
    ("https://xhslink.com/a/x", True),
    ("https://www.xiaohongshu.com/explore/1", False),
    ("https://example.com/x", False),
])
def test_short_link_is_per_domain(url, is_short):
    assert is_short_link(url) is is_short


def test_platform_detection_not_fooled_by_prefix():
    """evil-douyin.com 不该被认成抖音。"""
    assert detect_platform("https://evil-douyin.com/x") is None
    assert detect_platform("https://notyoutube.com.evil.cn/x") is None


# ── URL 抽取 ──────────────────────────────────────────────────────────
def test_extract_stops_at_chinese_chars():
    links = extract_links("看这个https://v.douyin.com/iRNBho6/好玩吧")
    assert [c.url for c in links] == ["https://v.douyin.com/iRNBho6/"]


def test_extract_strips_trailing_punctuation():
    for text in ("链接 https://v.douyin.com/abc/。",
                 "链接 https://v.douyin.com/abc/，",
                 "链接 https://v.douyin.com/abc/,",
                 "链接 https://v.douyin.com/abc/ "):
        assert [c.url for c in extract_links(text)] == ["https://v.douyin.com/abc/"]


def test_extract_multiple_links_in_order_of_confidence():
    links = extract_links(
        "先看 https://www.bilibili.com/video/BV1xx411c7mD 再看 https://v.douyin.com/abc/"
    )
    assert {c.platform for c in links} == {"bilibili", "douyin"}


def test_bare_domain_without_scheme():
    """买家手打、没带 https:// 的情况。"""
    links = extract_links("v.douyin.com/iRNBho6/ 帮我下")
    assert len(links) == 1
    assert links[0].url == "https://v.douyin.com/iRNBho6/"


def test_bare_domain_is_restricted_to_known_platforms():
    """`www.` 开头的写法一定是 URL 形态，照收（冷门站点也可能是有效源）；
    真正裸露的域名（someblog.example.com/x）噪声太多，只认已知平台。"""
    assert len(extract_links("www.bilibili.com/video/BV1xx")) == 1
    assert extract_links("someblog.example.com/post") == []
    kept = extract_links("www.someblog.example.com/post")
    assert len(kept) == 1 and kept[0].platform == "unknown"


def test_url_fragment_and_query_preserved():
    links = extract_links("https://www.bilibili.com/video/BV1xx411c7mD?p=1&t=2")
    assert links[0].url == "https://www.bilibili.com/video/BV1xx411c7mD?p=1&t=2"


def test_normalize_url_idempotent():
    url = normalize_url("HTTPS://WWW.Example.COM:443/Path?a=1")
    assert url == "https://www.example.com/Path?a=1"
    assert normalize_url(url) == url


# ── 类型判定 ──────────────────────────────────────────────────────────
def test_direct_media_detected():
    assert extract_links("http://cdn.x.com/a/b.mp4")[0].kind == "direct"
    assert extract_links("http://cdn.x.com/a/b.m3u8")[0].kind == "direct"


def test_drm_platform_flagged():
    top = parse("https://v.qq.com/x/cover/abc.html").best
    assert top.kind == "drm"
    assert "DRM" in top.note
    assert parse("https://v.qq.com/x/cover/abc.html").note   # 整单解析也该给提示


# ── 标题猜测 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("看看【猫咪打呼噜合集】这个太逗了 https://v.douyin.com/abc/", "猫咪打呼噜合集"),
    ("http://xhslink.com/a/xxxxx 复制本条信息，打开【小红书】App查看", ""),
    ("帮我下这个", ""),
    ("没有链接", ""),
    ("在吗", ""),
    ("谢谢你", ""),
    ("就这个视频，猫咪打呼噜合集", "猫咪打呼噜合集"),
    ("我想下载周杰伦演唱会完整版", "周杰伦演唱会完整版"),
    ("老板帮我下这个 https://v.douyin.com/x/", ""),
    ("", ""),
])
def test_guess_title(text, expected):
    assert guess_title(text) == expected


def test_guess_title_strips_leading_demonstratives():
    """【@某人 的作品】没有信息量要跳过；开头的「这个视频」是填充词，
    去掉后剩下的「太搞笑了」作为搜索词更干净。"""
    title = guess_title("7.92 复制打开抖音，看看【@小明 的作品】这个视频太搞笑了 https://v.douyin.com/x/")
    assert title == "太搞笑了"


def test_guess_title_keeps_meaningful_part():
    title = guess_title("帮我看看【自制奶油蘑菇汤教程】这个做法")
    assert title == "自制奶油蘑菇汤教程"


# ── 真实分享文案的回归用例 ────────────────────────────────────────────
def test_real_douyin_share_text():
    """用户真实发来的一条抖音分享文案（原始形态，一字未改）。

    这里有两个曾经踩过的坑：
      1. 【钟世祺的作品】里是**作者名**，不是标题 —— 真正的标题在括号外面；
      2. 结尾 ":3pm p@d.aa YZz:/ 02/14" 是分享口令，而且 ":" "@" "/" 会在
         标点压缩时被吃掉，所以砍码必须在压缩**之前**做。
    """
    title = guess_title(
        "0.23 复制打开抖音，看看【钟世祺的作品】吹笛子建议收藏🌟这首拿去商演真的夯爆了 "
        "《韩湘子秘... https://v.douyin.com/N3Pc1QJiZSE/ :3pm p@d.aa YZz:/ 02/14"
    )
    assert "钟世祺" not in title          # 作者名不该出现在标题里
    assert title.startswith("吹笛子建议收藏")   # 真正的标题在括号外面
    assert "3pm" not in title and "YZz" not in title and "02/14" not in title


def test_english_title_is_not_eaten_by_share_code_rule():
    """分享码规则曾经把英文标题结尾的 "video" 当码删掉。"""
    text = "This is Rick Astley - Never Gonna Give You Up official video"
    assert guess_title(text).endswith("video")


def test_share_code_is_removed():
    text = "8Xk2mQzP 复制打开抖音 看看【小明】旅行日记 https://v.douyin.com/x/"
    title = guess_title(text)
    assert "8Xk2mQzP" not in title
    assert "旅行日记" in title


def test_real_xiaohongshu_share_text():
    """用户真实发来的小红书分享文案。

    踩过两个坑：
      1. 短链域名是 `xhslink.**cn**`，早期只登记了 `.com`，认不出来；
      2. 「复制文本后进入」的清理规则会误伤 —— URL 结尾的数字被
         「7.92 复制打开抖音」那类口令前缀规则匹配掉，把后面的文案吃了。
    """
    text = ("当你开始注重 你将拥有紧致有型的身材！ https://xhslink.cn/o/3vcYqWbEzc6 "
            "复制文本后进入【小红书】，直接查看笔记。")
    result = parse(text)
    assert result.has_link
    top = result.best
    assert top.platform == "xiaohongshu"
    assert top.kind == "short"          # 必须认成短链，否则不会去展开
    assert top.url == "https://xhslink.cn/o/3vcYqWbEzc6"
    assert result.title_guess == "当你开始注重 你将拥有紧致有型的身材"
    assert "文本后" not in result.title_guess
    assert "查看笔记" not in result.title_guess


def test_share_code_prefix_does_not_eat_url_tail():
    """口令前缀规则不能匹配到 URL 尾部的数字（'...Ezc6 复制' 里的 '6 复制'）。"""
    from xydl.linkparse import clean_share_text

    cleaned = clean_share_text("看看这个 https://xhslink.cn/o/abc123 复制打开小红书")
    assert "abc123" not in cleaned
    assert "复制" not in cleaned


@pytest.mark.parametrize("url,expected", [
    ("https://xhslink.cn/o/3vcYqWbEzc6", "xiaohongshu"),
    ("https://xhslink.com/a/xxxxx", "xiaohongshu"),
    ("https://bili2233.cn/BV1xx", "bilibili"),
])
def test_additional_short_domains(url, expected):
    rule = detect_platform(url)
    assert rule is not None and rule.key == expected
    assert is_short_link(url) is True


# ── 新增平台：类型判定与短链 ────────────────────────────────────────
@pytest.mark.parametrize("url,kind", [
    # 新视频平台都是普通「页面」链接，走 yt-dlp 全流程，不该被判成 DRM 或误判短链
    ("https://www.acfun.cn/v/ac12345", "page"),
    ("https://www.reddit.com/r/funny/comments/abc/", "page"),
    ("https://rumble.com/v1234.html", "page"),
    ("https://streamable.com/abc", "page"),
    ("https://www.nicovideo.jp/watch/sm12345", "page"),
    ("https://9gag.com/gag/abc", "page"),
    ("https://coub.com/view/abc", "page"),
    ("https://soundcloud.com/user/track", "page"),
    ("https://bandcamp.com/track/abc", "page"),
    ("https://www.mixcloud.com/user/track/", "page"),
])
def test_new_platforms_are_page_not_drm(url, kind):
    top = extract_links(url)[0]
    assert top.kind == kind
    assert top.kind != "drm"


def test_nico_short_domain_expands():
    """nico.ms 是 Niconico 的短链域名，应判为 short 而非 page。"""
    assert is_short_link("https://nico.ms/sm12345") is True


# ── 端到端解析 ────────────────────────────────────────────────────────
def test_parse_real_xiaohongshu_share():
    r = parse("😆 8Xk2mQzP 😆 http://xhslink.com/a/xxxxx 复制本条信息，打开【小红书】App查看")
    assert r.has_link
    assert r.links[0].platform == "xiaohongshu"
    assert r.links[0].kind == "short"


def test_parse_message_without_link_but_with_title():
    r = parse("就这个视频，猫咪打呼噜合集")
    assert not r.has_link
    assert r.needs_search
    assert r.title_guess == "猫咪打呼噜合集"


def test_parse_completely_useless_message():
    r = parse("在吗")
    assert not r.has_link
    assert not r.needs_search
    assert "人工" in r.note


def test_parse_empty_string():
    r = parse("")
    assert not r.has_link
    assert r.title_guess == ""


def test_confidence_ordering_prefers_real_links():
    r = parse("https://v.douyin.com/abc/ 和 https://v.qq.com/x/cover/z.html")
    assert r.best.platform == "douyin"


def test_describe_does_not_crash():
    assert linkparse.describe([]) == "无链接"
    assert "抖音" in linkparse.describe(extract_links("https://v.douyin.com/abc/"))
