"""多剧集展开 + 流水线批量下载的测试。

B 站那一层用**假的 yt-dlp 输出**（结构与真实 ``--flat-playlist -J`` 一致，
见调研记录）；流水线那一层用本地 HTTP 服务器，真下真打包、完全离线。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from xydl import series as series_mod
from xydl.config import Config
from xydl.models import OrderStatus
from xydl.pipeline import Pipeline
from xydl.store import Store
from conftest import wait_for_terminal

# ── 真实 yt-dlp --flat-playlist 输出的结构（已实测）───────────────────
COLLECTION_JSON = {
    "_type": "playlist",
    "title": "合集｜影视飓风",
    "entries": [
        {"id": "BV1be41127ft", "url": "https://www.bilibili.com/video/BV1be41127ft",
         "ie_key": "BiliBili", "title": None},
        {"id": "BV132421A7VN", "url": "https://www.bilibili.com/video/BV132421A7VN",
         "ie_key": "BiliBili", "title": None},
        {"id": "BV1fx421172L", "url": "https://www.bilibili.com/video/BV1fx421172L",
         "ie_key": "BiliBili", "title": None},
    ],
}
MULTIP_JSON = {
    "_type": "playlist",
    "title": "多分P视频",
    "entries": [
        {"id": None, "url": "https://www.bilibili.com/video/BV1PTHS6MEC5?p=1",
         "ie_key": "BiliBili"},
        {"id": None, "url": "https://www.bilibili.com/video/BV1PTHS6MEC5?p=2",
         "ie_key": "BiliBili"},
    ],
}
SINGLE_JSON = {
    "_type": "video",
    "title": "一个单集视频",
    "entries": [],
}


class FakeEngine:
    """假的 yt-dlp 引擎：只回答 ``probe_playlist``。"""

    def __init__(self, payload, available=True):
        self.payload = payload
        self.available = available
        self.calls: list[tuple[str, int]] = []

    def probe_playlist(self, url, limit=50):
        self.calls.append((url, limit))
        return self.payload or {}


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config({"paths": {"data_dir": str(tmp_path / "d")},
                   "net": {"proxy": "none"}})


# ── 集数区间 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("spec,total,expected", [
    ("", 3, [1, 2, 3]),
    ("1-3,5", 10, [1, 2, 3, 5]),
    ("9-99", 12, [9, 10, 11, 12]),
    ("3,1,2", 5, [1, 2, 3]),
    ("0-2", 5, [1, 2]),
    ("99", 5, []),
    ("5-1", 5, [1, 2, 3, 4, 5]),
])
def test_parse_range(spec, total, expected):
    assert series_mod.parse_range(spec, total) == expected


def test_parse_range_zero_total():
    assert series_mod.parse_range("1-3", 0) == []


# ── 文件名 / 打包 ─────────────────────────────────────────────────────
def test_episode_filename_is_numbered():
    assert series_mod.episode_filename(2, "某某短剧-第2集") == "02 某某短剧-第2集.mp4"
    assert series_mod.episode_filename(11, "第11集") == "11 第11集.mp4"


def test_episode_filename_sanitizes():
    assert "/" not in series_mod.episode_filename(1, "a/b:c")
    assert series_mod.episode_filename(1, "") == "01 第1集.mp4"


@pytest.mark.parametrize("title,series,expected", [
    # yt-dlp 给多分P的标题是「合集名 p01 分P名」，分P名常常就是合集名
    ("某某大战 p01 某某大战", "某某大战", "某某大战"),
    ("某某大战 p02 实机演示", "某某大战", "实机演示"),
    ("某某大战 p3 花絮", "某某大战", "花絮"),
    ("某某短剧-第1集", "某某短剧", "第1集"),
    ("完全无关的标题", "某某大战", "完全无关的标题"),
    ("", "某某大战", ""),
])
def test_clean_episode_title(title, series, expected):
    assert series_mod.clean_episode_title(title, series) == expected


def test_episode_title_not_duplicated_in_pipeline(monkeypatch):
    """回归：真实多分P下载产出的文件名不能是「01 某某 p01 某某.mp4」。"""
    clean = series_mod.clean_episode_title("某某大战 p01 某某大战", "某某大战")
    assert series_mod.episode_filename(1, clean) == "01 某某大战.mp4"


#: 真实 yt-dlp 给 B站分P链接（?p=1）的标题（实测）
REAL_MULTIP_TITLE = ("极极极极，极限战场👉🏻首曝极首测，所见极所玩 "
                     "p02 实机演示：你的主场，「极」将开场！")


def test_clean_page_title_removes_duplicated_series_name():
    from xydl.utils import clean_page_title

    assert clean_page_title("某某大战 p01 某某大战") == "某某大战"
    assert clean_page_title("某某大战 p02 实机演示") == "实机演示"
    assert clean_page_title("普通标题") == "普通标题"
    assert clean_page_title("") == ""
    # 真实样本：分P名是自己独立的名字 → 取更具体的那个
    assert clean_page_title(REAL_MULTIP_TITLE).startswith("实机演示")


def test_page_mark_regex():
    from xydl.utils import PAGE_MARK_RE

    assert PAGE_MARK_RE.search("https://www.bilibili.com/video/BV1x?p=1")
    assert PAGE_MARK_RE.search("https://x/y?a=1&p=12")
    assert not PAGE_MARK_RE.search("https://www.bilibili.com/video/BV1x")


def test_archive_roundtrip(tmp_path):
    files = []
    for name in ("01 a.mp4", "02 b.mp4"):
        path = tmp_path / name
        path.write_bytes(b"x" * 10)
        files.append(path)

    target, error = series_mod.archive(files, tmp_path / "out.zip")
    assert error == ""
    assert target is not None and target.exists()
    with zipfile.ZipFile(target) as zf:
        assert sorted(zf.namelist()) == ["01 a.mp4", "02 b.mp4"]


def test_archive_without_files(tmp_path):
    target, error = series_mod.archive([], tmp_path / "out.zip")
    assert target is None and error


# ── 链接判定 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://www.bilibili.com/video/BV1xx411c7mD", True),
    ("https://b23.tv/abcdefg", True),
    ("https://space.bilibili.com/946974/video", True),
    ("https://www.bilibili.com/bangumi/play/ss26801", True),
    ("https://weixin.qq.com/sph/AJizVCRhAt", False),
    ("https://www.douyin.com/video/123", False),
])
def test_is_bilibili_url(url, expected):
    assert series_mod.is_bilibili_url(url) is expected


@pytest.mark.parametrize("url,expected", [
    ("https://www.bilibili.com/video/BV1xx411c7mD", True),
    ("https://weixin.qq.com/sph/AJizVCRhAt", True),
    ("https://www.douyin.com/video/123", False),
])
def test_is_series_capable(url, expected):
    assert series_mod.is_series_capable(url) is expected


# ── 展开 ──────────────────────────────────────────────────────────────
def test_plan_bilibili_collection(cfg):
    engine = FakeEngine(COLLECTION_JSON)
    plan = series_mod.plan("https://space.bilibili.com/946974/channel/collectiondetail?sid=1",
                           config=cfg, engine=engine)
    assert plan.is_series
    assert plan.count == 3
    assert plan.title == "合集｜影视飓风"
    assert plan.episodes[0].url == "https://www.bilibili.com/video/BV1be41127ft"
    assert engine.calls, "应该真的探测过一次"


def test_plan_bilibili_multi_part(cfg):
    plan = series_mod.plan("https://www.bilibili.com/video/BV1PTHS6MEC5",
                           config=cfg, engine=FakeEngine(MULTIP_JSON))
    assert plan.count == 2
    assert plan.episodes[1].url.endswith("?p=2")


def test_plan_bilibili_single_video_is_not_series(cfg):
    plan = series_mod.plan("https://www.bilibili.com/video/BV1xx411c7mD",
                           config=cfg, engine=FakeEngine(SINGLE_JSON))
    assert not plan.is_series
    assert plan.count == 1
    assert plan.episodes[0].url == "https://www.bilibili.com/video/BV1xx411c7mD"


def test_plan_bilibili_without_engine(cfg):
    plan = series_mod.plan("https://www.bilibili.com/video/BV1xx411c7mD",
                           config=cfg, engine=None)
    assert not plan.is_series
    assert "yt-dlp" in plan.note


def test_plan_respects_max_episodes(cfg):
    cfg.set("download.series.max_episodes", 2)
    plan = series_mod.plan("https://space.bilibili.com/946974/video",
                           config=cfg, engine=FakeEngine(COLLECTION_JSON))
    assert plan.count == 2
    assert plan.truncated, "达到上限要标记出来"


def test_plan_applies_episode_range(cfg):
    cfg.set("download.series.episode_range", "2,3")
    plan = series_mod.plan("https://space.bilibili.com/946974/video",
                           config=cfg, engine=FakeEngine(COLLECTION_JSON))
    assert [e.index for e in plan.episodes] == [2, 3]


def test_plan_disabled(cfg):
    cfg.set("download.series.enabled", False)
    engine = FakeEngine(COLLECTION_JSON)
    plan = series_mod.plan("https://www.bilibili.com/video/BV1xx411c7mD",
                           config=cfg, engine=engine)
    assert not plan.is_series
    assert not engine.calls, "关掉之后不该再去探测"


def test_plan_unknown_platform_is_single(cfg):
    plan = series_mod.plan("https://www.douyin.com/video/123", config=cfg)
    assert not plan.is_series
    assert plan.source == "single"


def test_episodes_from_ytdlp_builds_urls_from_ids():
    info = {"entries": [
        {"id": "BV1be41127ft"},
        {"id": "267851"},
        {"id": "av123"},
        {"id": None, "url": "https://x/y"},
    ]}
    eps = series_mod._episodes_from_ytdlp(info, limit=10)
    urls = [e.url for e in eps]
    assert urls[0] == "https://www.bilibili.com/video/BV1be41127ft"
    assert urls[1] == "https://www.bilibili.com/bangumi/play/ep267851"
    assert urls[2] == "https://www.bilibili.com/video/av123"
    assert urls[3] == "https://x/y"


def test_episode_hint_uses_series_title():
    ep = series_mod.Episode(index=3)
    assert ep.hint("某某短剧") == "某某短剧 第3集"
    assert series_mod.Episode(index=1, title="自定义").hint("某某短剧") == "自定义"


# ── 指定剧集 vs 全部：边界 ────────────────────────────────────────────
@pytest.mark.parametrize("spec,expected", [
    ("1-10,15", 15), ("3", 3), ("12-9", 12), ("", 0), ("乱写的", 0), ("0", 0),
])
def test_range_max_index(spec, expected):
    assert series_mod.range_max_index(spec) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://www.bilibili.com/video/BV1x?p=3",
     "https://www.bilibili.com/video/BV1x"),
    ("https://www.bilibili.com/video/BV1x?p=3&t=9",
     "https://www.bilibili.com/video/BV1x?t=9"),
    ("https://www.bilibili.com/video/BV1x", "https://www.bilibili.com/video/BV1x"),
    ("https://www.bilibili.com/video/BV1x?P=12", "https://www.bilibili.com/video/BV1x"),
])
def test_strip_page_param(url, expected):
    assert series_mod.strip_page_param(url) == expected


def test_range_beyond_max_episodes_extends_probe_window(cfg):
    """「上限 2 + 指定第 5 集」必须能取到第 5 集。

    旧行为：先按上限把列表砍到前 2 条，范围再在这 2 条里匹配 → 匹配不到 →
    静默退化成"去下载合集页"。真踩过。
    """
    cfg.set("download.series.max_episodes", 2)
    cfg.set("download.series.episode_range", "5")
    info = {"_type": "playlist", "title": "十集剧",
            "entries": [{"url": f"https://www.bilibili.com/video/BV{i}"}
                        for i in range(10)]}
    engine = FakeEngine(info)
    plan = series_mod.plan("https://space.bilibili.com/1/video", config=cfg,
                           engine=engine)
    assert plan.error == "", plan.error
    assert [e.index for e in plan.episodes] == [5]
    probed_url, probed_limit = engine.calls[0]
    assert probed_limit >= 6, f"探测窗口要覆盖到第 5 集，实际 {probed_limit}"


def test_range_mode_does_not_report_truncation(cfg):
    """指定了区间就不该再说"已达单次上限"——用户已经明确选了自己要的集。"""
    cfg.set("download.series.max_episodes", 3)
    cfg.set("download.series.episode_range", "2")
    info = {"_type": "playlist", "title": "十集剧",
            "entries": [{"url": f"https://www.bilibili.com/video/BV{i}"}
                        for i in range(10)]}
    plan = series_mod.plan("https://space.bilibili.com/1/video", config=cfg,
                           engine=FakeEngine(info))
    assert plan.truncated is False


def test_page_param_url_probes_whole_playlist(cfg):
    """``?p=3`` 的链接**永远**去掉 p 去探测整部。

    回归用户报的 bug：粘了合集中的某一 P，只下到这一条。
    原因就是 yt-dlp 拿到 ``?p=3`` 只返回那一集，于是看不到整部。
    去掉 p 之后：多分P 会返回全部条目 → 展开成整部；真的只有一条时退化成单集。
    想只要这一集用 ``--single``（那是不展开，语义不同）。
    """
    info = {"_type": "playlist", "title": "多分P",
            "entries": [{"url": f"https://www.bilibili.com/video/BV1x?p={i}"}
                        for i in range(1, 6)]}
    engine = FakeEngine(info)
    plan = series_mod.plan("https://www.bilibili.com/video/BV1x?p=3", config=cfg,
                           engine=engine)
    assert "p=3" not in engine.calls[0][0], "探测时必须去掉 p，否则看不到整部"
    assert [e.index for e in plan.episodes] == [1, 2, 3, 4, 5], "应展开成整部"

    # 有区间时仍然只选区间内的
    cfg.set("download.series.episode_range", "2,4")
    engine2 = FakeEngine(info)
    plan2 = series_mod.plan("https://www.bilibili.com/video/BV1x?p=3", config=cfg,
                            engine=engine2)
    assert [e.index for e in plan2.episodes] == [2, 4]


def test_range_mode_on_single_video_url_still_single(cfg):
    """单集视频 + 区间：yt-dlp 只返回一个条目 → 仍按单集处理，不报错。"""
    cfg.set("download.series.episode_range", "1")
    plan = series_mod.plan("https://www.bilibili.com/video/BV1xx411c7mD",
                           config=cfg, engine=FakeEngine(SINGLE_JSON))
    assert not plan.is_series
    assert plan.error == ""


# ── B站合集：粘"合集里的某一集"也要识别成整部 ────────────────────────
class _FakeResp:
    def __init__(self, text: str):
        self.text = text          # Response.text 是属性，不是方法


def _view_payload(**data) -> str:
    import json as _json

    return _json.dumps({"code": 0, "data": data})


def test_bilibili_view_api_returns_whole_season(monkeypatch, cfg):
    """粘合集中的某一集 → 公开 view 接口的 ``ugc_season`` 给出**整部**。

    用户报的 bug：粘了合集里第 14 集，只下到那一条。原因是 yt-dlp 拿到单个 BV
    只会返回那一集，看不到合集；必须走这个接口。
    """
    payload = _view_payload(
        title="【提欧第十四集】…",
        pages=[{"page": 1, "part": "【提欧第十四集】…"}],
        ugc_season={"id": 8643951, "title": "看看提欧", "ep_count": 3, "sections": [
            {"episodes": [
                {"bvid": "BV1PhMF6hEJ1", "title": "提欧第一集"},
                {"bvid": "BV1LuN46SE9R", "title": "【提欧第二集】"},
                {"bvid": "BV12xpi66Edp", "title": "【提欧第十四集】"},
            ]}]},
    )
    monkeypatch.setattr(series_mod, "request",
                        lambda *a, **k: _FakeResp(payload))
    episodes, note = series_mod.bilibili_season_episodes(
        "https://www.bilibili.com/video/BV12xpi66Edp?vd_source=abc", cfg)

    assert note == ""
    assert [e.index for e in episodes] == [1, 2, 3]
    assert episodes[0].url.endswith("BV1PhMF6hEJ1")
    assert episodes[2].url.endswith("BV12xpi66Edp")


def test_bilibili_view_api_code_zero_is_success(monkeypatch, cfg):
    """回归：``code=0`` 是**成功**，不能用 ``code or -1`` 判（0 是 falsy）。

    踩过：写成 ``int(payload.get("code") or -1) != 0``，于是成功的响应反而被判成失败。
    """
    payload = _view_payload(
        pages=[{"page": 1, "part": "p1"}, {"page": 2, "part": "p2"}],
    )
    monkeypatch.setattr(series_mod, "request", lambda *a, **k: _FakeResp(payload))
    episodes, note = series_mod.bilibili_season_episodes(
        "https://www.bilibili.com/video/BV12xpi66Edp", cfg)
    assert note == "", note
    assert len(episodes) == 2, "多分P 也要展开"


def test_bilibili_view_api_single_video_stays_single(monkeypatch, cfg):
    """普通单集视频：接口没给合集、也没多分P → 返回空，让 yt-dlp 兜底。"""
    payload = _view_payload(title="单个视频", pages=[{"page": 1, "part": "正片"}])
    monkeypatch.setattr(series_mod, "request", lambda *a, **k: _FakeResp(payload))
    episodes, note = series_mod.bilibili_season_episodes(
        "https://www.bilibili.com/video/BV1xx411c7mD", cfg)
    assert episodes == []
    assert "单集" in note or "分P" in note


# ── 流水线端到端（本地服务器，真下真打包）──────────────────────────────
@pytest.fixture
def pipeline(config: Config, store: Store):
    p = Pipeline(config, store)
    p.start()
    yield p
    p.stop()


def _force_series(monkeypatch, urls, title="某某短剧", *, truncated=False,
                  error="", total_hint=None):
    """把 series.plan 换成"这就是一部 N 集的剧"，用来测流水线本身。"""
    plan = series_mod.SeriesPlan(
        title=title,
        episodes=[series_mod.Episode(index=i + 1, url=u, title=f"{title}-第{i+1}集")
                  for i, u in enumerate(urls)],
        source="bilibili",
        truncated=truncated,
        error=error,
    )
    if total_hint is not None:
        plan.episodes = plan.episodes[:total_hint]

    def fake_plan(url, **kwargs):
        return plan

    monkeypatch.setattr("xydl.pipeline.series_mod.plan", fake_plan)
    return plan


def test_series_download_packages_into_zip(config, store, sample_server,
                                           pipeline, monkeypatch):
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url, url, url])

    order, created = pipeline.submit_text(url, conversation_id="series-buyer")
    assert created
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value, final.error
    deliverable = Path(final.file_path)
    assert deliverable.suffix == ".zip"
    assert deliverable.exists()

    with zipfile.ZipFile(deliverable) as zf:
        names = sorted(zf.namelist())
    assert len(names) == 3
    # 序号必须落在文件名里 —— 客户要能一眼排出顺序
    assert names[0].startswith("01 ")
    assert names[1].startswith("02 ")
    assert names[2].startswith("03 ")

    assert final.meta["series"]["count"] == 3
    assert final.meta["series"]["downloaded"] == 3
    assert final.meta["series"]["failed"] == []
    assert final.file_size == deliverable.stat().st_size


def test_series_keeps_episode_files_for_reuse(config, store, sample_server,
                                              pipeline, monkeypatch):
    """打包之外，逐集文件也要留在 downloads/<order>/episodes/ 里。"""
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url, url])
    order, _ = pipeline.submit_text(url)
    final = wait_for_terminal(store, order.id)

    episodes_dir = Path(final.file_path).parent / "episodes"
    assert episodes_dir.is_dir()
    assert len(list(episodes_dir.glob("*.mp4"))) == 2


def test_series_separate_mode_keeps_first_file_as_deliverable(
        config, store, sample_server, pipeline, monkeypatch):
    config.set("download.series.mode", "separate")
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url, url])
    order, _ = pipeline.submit_text(url)
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value, final.error
    assert Path(final.file_path).suffix == ".mp4"
    assert len(final.meta["series"]["files"]) == 2


def test_series_single_episode_stays_a_single_download(config, store,
                                                       sample_server, pipeline,
                                                       monkeypatch):
    """展开后只有一集、且那一集有 URL → 仍走普通单文件路径（不建合集目录）。"""
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url])
    order, _ = pipeline.submit_text(url)
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value
    order_dir = Path(final.file_path).parent
    assert Path(final.file_path).suffix == ".mp4", "单集不该被包成 zip"
    assert not (order_dir / "episodes").exists(), "单集不该建合集目录"


def test_single_selected_episode_downloads_that_episode(config, store,
                                                        tmp_path, sample_server,
                                                        pipeline, monkeypatch):
    """范围只选中 1 集时，要下的是**那一集**，不是买家发来的原始链接。

    真事故：合集链接 + ``episode_range=5``，因为只有 1 集、``is_series`` 是 False，
    就退回去下载原始链接；而下载命令里带 ``--no-playlist``，于是下成第 1 集，
    还报"成功"。这里用「原始链接是个网页、选集链接是个 mp4」把两种行为区分开：
    一旦退回原始链接，下载必然失败。
    """
    # 原始链接故意给一个"不是视频"的地址；选中集给一个真实可下的 mp4
    submitted = sample_server.url("page.html")
    episode_url = sample_server.url("ep5.mp4")
    (tmp_path / "serve" / "ep5.mp4").write_bytes(b"EPISODE-FIVE-CONTENT" * 100)

    plan = series_mod.SeriesPlan(
        title="某某短剧",
        episodes=[series_mod.Episode(index=5, url=episode_url, title="某某短剧-第5集")],
        source="bilibili",          # 明确表示"这是展开出来的"
    )
    monkeypatch.setattr("xydl.pipeline.series_mod.plan", lambda u, **kw: plan)

    order, _ = pipeline.submit_text(submitted)
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value, final.error
    assert Path(final.file_path).read_bytes() == b"EPISODE-FIVE-CONTENT" * 100, \
        "下到的必须是选集内容，不是原始链接"
    assert final.chosen_url == episode_url, "订单里应记录真正下载的地址"


def test_expands_series_flag_semantics():
    """``expands_series`` 与 ``is_series`` 的区别是这次修复的关键。"""
    one = series_mod.SeriesPlan(
        title="x", source="bilibili",
        episodes=[series_mod.Episode(index=5, url="u")])
    assert one.is_series is False          # 只有一集
    assert one.expands_series is True      # 但确实是"多集源展开出来的"
    plain = series_mod.SeriesPlan(title="x", source="single",
                                  episodes=[series_mod.Episode(index=1, url="u")])
    assert plain.expands_series is False


def test_series_truncated_flag_and_note_are_surfaced(config, store, sample_server,
                                                     pipeline, monkeypatch):
    """被 max_episodes 截断必须留痕：meta 里有标记，话术里有说明。

    否则 UP 主 200 个投稿被砍成 50 集，交付时看起来就是"共 50 集"，谁都发现不了。
    """
    replies: list[str] = []
    pipeline.add_deliverer(lambda o, t, f: replies.append(t))
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url, url], truncated=True)
    order, _ = pipeline.submit_text(url)
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value, final.error
    assert final.meta["series"]["truncated"] is True
    assert any("单次上限" in t for t in replies), replies
    assert any("原列表可能更长" in e["message"]
               for e in store.events(order.id)), "日志里也要留下被截断的痕迹"


def test_series_not_truncated_when_exactly_at_limit(cfg):
    """恰好等于上限 ≠ 被截断。

    probe_playlist 会多要一条（limit+1）来判断，所以"刚好 N 集、上限也是 N"
    的完整合集不能被误报成"已截断"（那对买家是假消息）。
    """
    cfg.set("download.series.max_episodes", 3)
    info = {"_type": "playlist", "title": "刚好三集",
            "entries": [{"url": f"https://www.bilibili.com/video/BV{i}"}
                        for i in range(3)]}
    plan = series_mod.plan("https://space.bilibili.com/1/video", config=cfg,
                           engine=FakeEngine(info))
    assert plan.count == 3
    assert plan.truncated is False, "刚好等于上限不是截断"


def test_series_truncated_when_over_limit(cfg):
    """超过上限才叫截断，且只保留上限那么多集。"""
    cfg.set("download.series.max_episodes", 3)
    info = {"_type": "playlist", "title": "五集",
            "entries": [{"url": f"https://www.bilibili.com/video/BV{i}"}
                        for i in range(5)]}
    plan = series_mod.plan("https://space.bilibili.com/1/video", config=cfg,
                           engine=FakeEngine(info))
    assert plan.count == 3
    assert plan.truncated is True


def test_bad_episode_range_stops_instead_of_downloading_everything(
        config, store, pipeline, monkeypatch):
    """episode_range 解析不出来时**停下**，不能"全下"。

    运营写 ``1-5`` 的本意是省流量；静默变成下全部 50 集是**放大**了损失。
    """
    config.set("download.series.episode_range", "第一到第五集")
    real_plan = series_mod.plan

    def fake_plan(url, *, config, engine=None, **kwargs):
        # 流水线传进来的是它自己的 config（带那个写坏的 range），引擎换成假的
        return real_plan(url, config=config, engine=FakeEngine(COLLECTION_JSON),
                         **kwargs)

    monkeypatch.setattr("xydl.pipeline.series_mod.plan", fake_plan)
    order, _ = pipeline.submit_text("https://space.bilibili.com/1/video")
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.NEED_MANUAL.value
    assert "episode_range" in (final.error or ""), final.error
    order_dir = Path(pipeline.download_dir) / str(order.id)
    assert not any(order_dir.rglob("*.mp4")), "配置写错时一集都不该下"


def test_series_rerun_does_not_duplicate_episode_files(config, store,
                                                       sample_server, pipeline,
                                                       monkeypatch):
    """订单重试会复用同一个 job_dir，产物不能被堆成 "01 xxx (2).mp4"。"""
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url, url])
    order, _ = pipeline.submit_text(url)
    wait_for_terminal(store, order.id)

    assert pipeline.retry(order.id)
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value, final.error
    episodes_dir = Path(final.file_path).parent / "episodes"
    names = sorted(p.name for p in episodes_dir.glob("*.mp4"))
    assert len(names) == 2, f"重试后不该堆重复集：{names}"
    assert not any("(2)" in n or "-1." in n for n in names), names


def test_series_partial_failure_reply_reveals_shortfall(config, store,
                                                        sample_server, pipeline,
                                                        monkeypatch):
    """部分失败时发给买家的话必须写明"共 N 集、少了几集"。"""
    replies: list[str] = []
    pipeline.add_deliverer(lambda o, t, f: replies.append(t))
    good = sample_server.url("sample.mp4")
    bad = sample_server.base + "/missing.mp4"
    _force_series(monkeypatch, [good, bad, good])
    order, _ = pipeline.submit_text(good)
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value, final.error
    joined = "\n".join(replies)
    assert "共 3 集" in joined, joined
    assert "1 集没下成功" in joined, joined
    assert "全部成功" not in joined


def test_series_meta_merge_keeps_existing_keys(config, store, sample_server,
                                               pipeline, monkeypatch):
    """写 series 元数据不能把订单原有的 meta 抹掉。"""
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url, url])
    order, _ = pipeline.submit_text(url, meta={"upstream_order_id": "X-9"})
    final = wait_for_terminal(store, order.id)

    assert final.meta.get("upstream_order_id") == "X-9"
    assert final.meta["series"]["downloaded"] == 2


def test_series_range_keeps_original_episode_numbers(config, store,
                                                     sample_server, pipeline,
                                                     monkeypatch):
    """只下第 9、10 集时，文件名必须是 09/10 —— 落成 01/02 客户会以为收错了。"""
    url = sample_server.url("sample.mp4")
    plan = series_mod.SeriesPlan(
        title="某某短剧",
        episodes=[series_mod.Episode(index=i, url=url, title=f"某某短剧-第{i}集")
                  for i in (9, 10)],
        source="bilibili",
    )
    monkeypatch.setattr("xydl.pipeline.series_mod.plan", lambda u, **kw: plan)

    order, _ = pipeline.submit_text(url)
    final = wait_for_terminal(store, order.id)
    assert final.status == OrderStatus.DONE.value, final.error

    episodes_dir = Path(final.file_path).parent / "episodes"
    names = sorted(p.name for p in episodes_dir.glob("*.mp4"))
    assert names[0].startswith("09 "), names
    assert names[1].startswith("10 "), names


def test_series_uses_series_done_reply(config, store, sample_server, pipeline,
                                       monkeypatch):
    replies: list[str] = []
    pipeline.add_deliverer(lambda o, t, f: replies.append(t))
    url = sample_server.url("sample.mp4")
    _force_series(monkeypatch, [url, url])
    order, _ = pipeline.submit_text(url)
    wait_for_terminal(store, order.id)

    assert any("共 2 集" in t for t in replies), replies


def test_series_partial_failure_still_delivers(config, store, sample_server,
                                               pipeline, monkeypatch):
    """一半集挂了仍要交付成功的一半，并在订单里记清楚哪几集失败。"""
    good = sample_server.url("sample.mp4")
    bad = sample_server.base + "/missing.mp4"
    _force_series(monkeypatch, [good, bad, good])
    order, _ = pipeline.submit_text(good)
    final = wait_for_terminal(store, order.id)

    assert final.status == OrderStatus.DONE.value, final.error
    series_meta = final.meta["series"]
    assert series_meta["downloaded"] == 2
    assert len(series_meta["failed"]) == 1
    assert "失败" in (final.error or "")
