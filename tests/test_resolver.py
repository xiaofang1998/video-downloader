"""解析层测试。联网部分只测离线可判定的逻辑。"""

from __future__ import annotations

import pytest

from xydl.config import Config
from xydl.models import LinkCandidate
from xydl.resolver import Resolver, is_auth_wall
from xydl.search import SearchResult, StaticProvider, SearchRouter, rank


def make_resolver(config: Config, provider=None) -> Resolver:
    if provider is None:
        router = SearchRouter([], config)
    else:
        router = SearchRouter([provider], config)
    return Resolver(config, router=router)


# ── 复判候选 ──────────────────────────────────────────────────────────
def test_recandidate_reclassifies_expanded_url(config: Config):
    """短链展开后必须重新判平台/类型，不能沿用旧值。"""
    resolver = make_resolver(config)
    cand = resolver._recandidate("https://www.douyin.com/video/7123456", "expanded",
                                 base=0.92)
    assert cand.platform == "douyin"
    assert cand.kind == "page"        # 展开后已经不是短链了
    assert cand.source == "expanded"
    assert cand.confidence == 0.92


def test_recandidate_recognises_direct_media(config: Config):
    resolver = make_resolver(config)
    cand = resolver._recandidate("https://cdn.x.com/a.mp4", "expanded")
    assert cand.kind == "direct"


# ── DRM 短路 ──────────────────────────────────────────────────────────
def test_drm_only_candidates_short_circuit(config: Config):
    """全是受保护平台时不该浪费下载配额，应立刻判定需人工。"""
    resolver = make_resolver(config)
    candidates = [LinkCandidate(url="https://v.qq.com/x/cover/a.html",
                                platform="tencent_video", platform_label="腾讯视频",
                                kind="drm", note="有版权保护")]
    outcome = resolver.resolve(candidates, "某部电影")
    assert outcome.drm_blocked
    assert outcome.needs_manual
    assert not outcome.ok
    assert "版权" in outcome.note


def test_direct_link_uses_no_network(config: Config):
    resolver = make_resolver(config)
    candidates = [LinkCandidate(url="http://127.0.0.1:1/a.mp4", platform="unknown",
                                kind="direct", source="message", confidence=0.7)]
    outcome = resolver.resolve(candidates, "")
    assert outcome.ok
    assert outcome.chosen.url == "http://127.0.0.1:1/a.mp4"


# ── 落源 ──────────────────────────────────────────────────────────────
def test_title_search_used_when_no_link(config: Config):
    provider = StaticProvider(config, [
        SearchResult(title="猫咪打呼噜合集_哔哩哔哩", url="http://127.0.0.1:1/v/1",
                     rank=0, provider="static"),
    ])
    resolver = make_resolver(config, provider)
    outcome = resolver.resolve([], "猫咪打呼噜合集")
    assert outcome.ok
    assert outcome.chosen.source == "search"
    assert provider.calls                       # 确实发起了搜索


def test_title_search_skipped_when_disabled(config: Config):
    config.set("resolve.allow_title_search", False)
    provider = StaticProvider(config, [
        SearchResult(title="x", url="http://127.0.0.1:1/v/1", rank=0),
    ])
    resolver = make_resolver(config, provider)
    outcome = resolver.resolve([], "猫咪打呼噜合集")
    assert not outcome.ok
    assert outcome.needs_manual
    assert not provider.calls


def test_no_search_provider_degrades_gracefully(config: Config):
    resolver = make_resolver(config)             # 空 router
    outcome = resolver.resolve([], "猫咪打呼噜合集")
    assert outcome.needs_manual
    assert any("搜索源" in e for e in outcome.errors)


def test_empty_message_needs_manual(config: Config):
    resolver = make_resolver(config)
    outcome = resolver.resolve([], "")
    assert outcome.needs_manual
    assert not outcome.ok


def test_drm_results_from_search_are_skipped(config: Config):
    provider = StaticProvider(config, [
        SearchResult(title="正版电影", url="https://v.qq.com/x/cover/a.html", rank=0),
    ])
    resolver = make_resolver(config, provider)
    outcome = resolver.resolve([], "某电影")
    assert not outcome.ok                   # 只搜到 DRM 源，不该当成可下载


def test_expand_failure_is_reported_and_falls_through(config: Config):
    """短链展开失败时应继续尝试下一个候选，而不是整单失败。"""
    resolver = make_resolver(config, StaticProvider(config, error="搜索引擎挂了"))
    bad = LinkCandidate(url="http://127.0.0.1:1/短链", platform="douyin",
                        platform_label="抖音", kind="short", confidence=0.9)
    good = LinkCandidate(url="http://127.0.0.1:1/ok.mp4", platform="unknown",
                         kind="direct", confidence=0.7)
    outcome = resolver.resolve([bad, good], "")
    assert outcome.ok
    assert outcome.chosen.url == "http://127.0.0.1:1/ok.mp4"
    assert outcome.errors                     # 失败原因被记录下来


# ── 搜索结果排序 ──────────────────────────────────────────────────────
def test_rank_prefers_configured_domains():
    results = [
        SearchResult(title="随机站", url="https://random.example/a", rank=0),
        SearchResult(title="B站", url="https://www.bilibili.com/video/BV1", rank=3),
    ]
    ranked = rank(results, prefer_domains=["bilibili.com"])
    assert ranked[0].domain == "www.bilibili.com"


def test_rank_drops_blocked_domains():
    results = [
        SearchResult(title="CSDN 水文", url="https://blog.csdn.net/x", rank=0),
        SearchResult(title="正常", url="https://www.bilibili.com/video/BV1", rank=1),
    ]
    ranked = rank(results, block_domains=["csdn.net"])
    assert all("csdn" not in r.url for r in ranked)
    assert len(ranked) == 1


def test_rank_score_is_monotonic_in_rank():
    results = [
        SearchResult(title="一", url="https://a.example/1", rank=0),
        SearchResult(title="二", url="https://b.example/2", rank=5),
    ]
    ranked = rank(results)
    assert ranked[0].score > ranked[1].score


def test_describe_is_human_readable(config: Config):
    resolver = make_resolver(config)
    from xydl import linkparse
    outcome = resolver.resolve(linkparse.extract_links("http://127.0.0.1:1/a.mp4"), "")
    assert "搜索落源" in outcome.describe() or "原始链接" in outcome.describe()
    assert resolver.resolve([], "").describe()


# ── 登录墙识别 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,auth", [
    ("https://www.xiaohongshu.com/login?redirectPath=http%3A%2F%2Fx.com%2Fa", True),
    ("https://x.com/signin", True),
    ("https://x.com/passport/login", True),
    ("https://x.com/a?redirect_uri=/b", True),
    ("https://x.com/a?next=/b", True),
    ("https://www.xiaohongshu.com/discovery/item/6ab896ff0000000018007f59", False),
    ("https://www.douyin.com/video/7689741577656339766", False),
    ("https://x.com/videos/123", False),
])
def test_is_auth_wall(url, auth):
    assert is_auth_wall(url) is auth


def test_pick_landing_skips_the_login_redirect():
    """实测踩过：小红书短链会 302 到笔记页，未登录时**再 302 到 login**。

    一路跟重定向到底只会拿到登录墙，真正的笔记地址反而被丢掉。
    """
    chain = [
        "https://xhslink.cn/o/3vcYqWbEzc6",
        "https://www.xiaohongshu.com/discovery/item/6ab896ff0000000018007f59?type=video",
        "https://www.xiaohongshu.com/login?redirectPath=http%3A%2F%2Fwww.xiaohongshu.com%2Fdiscovery",
    ]
    landing = Resolver._pick_landing(chain, chain[-1])
    assert landing == chain[1]
    assert "discovery/item" in landing


def test_pick_landing_uses_last_hop_when_no_auth_wall():
    chain = ["https://v.douyin.com/abc/", "https://www.douyin.com/video/123"]
    assert Resolver._pick_landing(chain, chain[-1]) == chain[-1]


def test_pick_landing_falls_back_on_empty_chain():
    assert Resolver._pick_landing([], "https://fallback/") == "https://fallback/"


# ── 未知域名自动展开 ──────────────────────────────────────────────────
def test_unknown_domain_gets_expansion_attempt(config: Config, monkeypatch):
    """平台新增短链域名时，未知域名也要试一次展开，不能直接当普通页下载。"""
    resolver = make_resolver(config)

    from xydl.resolver import ExpandResult

    def fake_expand(url):
        return ExpandResult(original=url,
                            final_url="https://www.douyin.com/video/7123456",
                            chain=[url, "https://www.douyin.com/video/7123456"],
                            status=200)

    monkeypatch.setattr(resolver, "expand", fake_expand)
    cand = LinkCandidate(url="https://brand-new-short.example/abc", platform="unknown",
                         kind="page", confidence=0.6)
    resolved, _ = resolver._try_candidate(cand, [])
    assert resolved is not None
    assert resolved.platform == "douyin"       # 展开后重新判定了平台
    assert resolved.url == "https://www.douyin.com/video/7123456"


def test_known_platform_page_is_not_expanded(config: Config, monkeypatch):
    """已知平台且是普通页面时不该多打一次请求。"""
    resolver = make_resolver(config)
    calls: list[str] = []
    monkeypatch.setattr(resolver, "expand",
                        lambda url: calls.append(url) or None)
    monkeypatch.setattr(resolver, "probe_title", lambda url: "")
    cand = LinkCandidate(url="https://www.douyin.com/video/123", platform="douyin",
                         kind="page", confidence=0.9)
    resolved, _ = resolver._try_candidate(cand, [])
    assert resolved is cand
    assert calls == []
