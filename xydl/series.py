"""多剧集 / 合集展开：把一条链接变成「N 个待下载的单元」。

为什么要单独一层
----------------
「下载一条链接」和「下载一部剧」在交付上是两件事：后者要编号、要有上限、
要么打包成一个压缩包（客户好接收），要么散开成多个文件。这些决策不该塞进
下载引擎，也不该塞进渠道层，所以单独一个 planner。

覆盖的形态
----------
* **B 站**（主力，全部走 yt-dlp 的 playlist 能力，一套代码通吃）::

      https://www.bilibili.com/video/BV1xx?p=1       多分P
      https://space.bilibili.com/<mid>/channel/collectiondetail?sid=<id>   合集
      https://space.bilibili.com/<mid>/video          UP 主投稿
      https://www.bilibili.com/bangumi/play/ss<id>    番剧（按集展开）
      https://www.bilibili.com/list/<...>             列表/收藏夹

* **微信视频号**：见 :mod:`xydl.wechat_channels`。视频号没有公开的匿名列表接口，
  所以只有配了 TikHub 才可能真正列出整部剧；否则退化成单集（并在 note 里说清楚）。

设计要点：``--flat-playlist`` 只取轻量条目，**不解析每一集的详情**。
因为每集真正的标题由下载引擎在下载时自己探测（`yt-dlp -J`），这里重复解析一遍
既慢又容易触发风控。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import linkparse, wechat_channels
from .config import Config
from .http import request
from .utils import clean_media_title, sanitize_filename

#: B站公开接口：从一个 BV 就能拿到它所属的**整个合集**（ugc_season）。
#: 这是"粘合集中的某一集、也要识别成多剧集"的关键 —— yt-dlp 拿到单个 BV
#: 只会返回那一集，看不到合集；这个接口 `sections[].episodes[]` 里是全部集。
BILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")

LogFn = Callable[[str], None]


def _noop_log(_message: str) -> None:
    return None


#: B 站的域名集合（含短链与移动端）
BILIBILI_HOSTS = (
    "bilibili.com", "b23.tv", "bili2233.cn", "acg.tv", "biligame.com",
)
#: B 站作品 id 特征
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.I)


def is_bilibili_url(url: str) -> bool:
    host = (linkparse.host_of(url) or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in BILIBILI_HOSTS)


def is_series_capable(url: str) -> bool:
    """这条链接有没有可能对应"多集"（决定要不要花一次探测）。"""
    return is_bilibili_url(url) or wechat_channels.is_channels_url(url)


@dataclass
class Episode:
    """一个待下载单元。"""

    index: int
    url: str = ""
    title: str = ""
    #: 下载通道：``ytdlp``（默认，走通用引擎）或 ``wechat_channels``
    source: str = "ytdlp"
    object_id: str = ""
    export_id: str = ""

    def hint(self, series_title: str = "") -> str:
        """给下载引擎用的文件名提示。"""
        if self.title:
            return self.title
        if series_title:
            return f"{series_title} 第{self.index}集"
        return ""


@dataclass
class SeriesPlan:
    """一次展开的结论。``episodes`` 恒非空（至少一条）。"""

    title: str = ""
    episodes: list[Episode] = field(default_factory=list)
    source: str = ""
    note: str = ""
    error: str = ""
    truncated: bool = False

    @property
    def is_series(self) -> bool:
        return len(self.episodes) > 1

    @property
    def expands_series(self) -> bool:
        """这次结论真的是从「多集源」展开出来的（哪怕只选中 1 集）。

        与 :attr:`is_series` 的区别很关键：``episode_range=5`` 只选中 1 集时
        ``is_series`` 是 False，但**要下的仍然是那一集**，不是买家发来的原始链接
        （合集页链接带 ``--no-playlist`` 会下成第 1 集，属于下错集）。
        """
        return self.source in ("bilibili", "wechat_channels")

    @property
    def count(self) -> int:
        return len(self.episodes)

    def describe(self) -> str:
        if not self.is_series:
            return f"单集：{self.title or self.episodes[0].url}"
        tail = "（已截断）" if self.truncated else ""
        return f"{self.count} 集{tail}：{self.title or '未命名合集'}"


# ── 集数区间 ──────────────────────────────────────────────────────────
def parse_range(spec: str, total: int) -> list[int]:
    """解析 ``"1-5,8,10-12"`` 这类区间，返回 1-based 序号（去重、升序、限界）。

    >>> parse_range("1-3,5", 10)
    [1, 2, 3, 5]
    >>> parse_range("", 3)
    [1, 2, 3]
    >>> parse_range("9-99", 12)
    [9, 10, 11, 12]
    """
    if total <= 0:
        return []
    text = (spec or "").strip()
    if not text:
        return list(range(1, total + 1))
    picked: set[int] = set()
    for chunk in re.split(r"[,，、;\s]+", text):
        if not chunk:
            continue
        part = re.fullmatch(r"(\d+)\s*[-~至]\s*(\d+)", chunk)
        if part:
            start, end = int(part.group(1)), int(part.group(2))
            if start > end:
                start, end = end, start
            picked.update(range(max(1, start), min(total, end) + 1))
            continue
        if chunk.isdigit():
            value = int(chunk)
            if 1 <= value <= total:
                picked.add(value)
    return sorted(picked)


def range_max_index(spec: str) -> int:
    """``episode_range`` 里出现过的**最大集号**（不依赖总集数）。

    为什么需要它：探测列表有个上限（``max_episodes``），而"指定剧集"是用户的
    明确意图。如果上限先把列表砍到前 N 集、范围再在这 N 集里匹配，那么
    「上限 2、指定第 5 集」就会匹配不到 —— 实测会静默退化成"去下载合集页"。
    所以要先算出用户点名到第几集，再去取那么长的列表。

    >>> range_max_index("1-10,15")
    15
    >>> range_max_index("3")
    3
    >>> range_max_index("")
    0
    >>> range_max_index("乱写的")
    0
    """
    text = (spec or "").strip()
    if not text:
        return 0
    biggest = 0
    for chunk in re.split(r"[,，、;\s]+", text):
        if not chunk:
            continue
        part = re.fullmatch(r"(\d+)\s*[-~至]\s*(\d+)", chunk)
        if part:
            biggest = max(biggest, int(part.group(1)), int(part.group(2)))
        elif chunk.isdigit():
            biggest = max(biggest, int(chunk))
    return biggest


#: URL 里的分P参数：``?p=3``
_P_PARAM_RE = re.compile(r"^p=\d+$", re.I)


def strip_page_param(url: str) -> str:
    """去掉 B 站 URL 里的 ``p=N``。

    为什么要去：``https://www.bilibili.com/video/BVxxx?p=3`` 交给 yt-dlp 时，
    它只会返回**第 3 集这一个**（实测 ``_type=video``），根本看不到整部剧。
    用户既然显式写了 ``episode_range``（"我要这部剧的第几集"），就必须把 p 去掉
    去探测整部，否则范围永远匹配不上。

    >>> strip_page_param("https://www.bilibili.com/video/BV1x?p=3")
    'https://www.bilibili.com/video/BV1x'
    >>> strip_page_param("https://www.bilibili.com/video/BV1x?p=3&t=9")
    'https://www.bilibili.com/video/BV1x?t=9'
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    kept = [kv for kv in parts.query.split("&") if not _P_PARAM_RE.match(kv)]
    if len(kept) == len(parts.query.split("&")):
        return url
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))


# ── 打包 ─────────────────────────────────────────────────────────────
def archive(files: Iterable[Path], target: Path) -> tuple[Path | None, str]:
    """把若干文件打成 zip。返回 ``(zip 路径, 错误)``。"""
    items = [Path(p) for p in files if Path(p).exists()]
    if not items:
        return None, "没有可打包的文件"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in items:
                zf.write(item, arcname=item.name)
    except (OSError, zipfile.BadZipFile) as exc:
        return None, f"打包失败：{type(exc).__name__}: {exc}"
    return target, ""


def episode_filename(index: int, title: str, suffix: str = ".mp4",
                     maxlen: int = 60, width: int = 2) -> str:
    """给一集起文件名：``01 剧名 第1集.mp4``。

    编号是**必须的** —— 客户拿到一堆积名字相似的 mp4，没有序号根本排不出顺序。
    """
    clean = sanitize_filename(clean_media_title(title, maxlen) or f"第{index}集",
                              maxlen=maxlen)
    return f"{index:0{width}d} {clean}{suffix}"


def clean_episode_title(title: str, series_title: str = "") -> str:
    """把某一集的标题收拾成不啰嗦的样子。

    yt-dlp 对多分P视频给出的标题是 ``"<合集名> p01 <分P名>"``，而分P名又常常
    与合集名一模一样。直接落盘会得到 ``01 某某 p01 某某.mp4`` 这种废话文件名
    —— 实测踩到过。这里按顺序削掉两层冗余：合集名前缀、``pXX`` 标记。

    >>> clean_episode_title("某某大战 p01 某某大战", "某某大战")
    '某某大战'
    >>> clean_episode_title("某某大战 p02 实机演示", "某某大战")
    '实机演示'
    >>> clean_episode_title("", "某某大战")
    ''
    """
    text = clean_media_title(str(title or ""), 120)
    series = clean_media_title(str(series_title or ""), 120)
    if not text:
        return ""
    if series and text.startswith(series):
        text = text[len(series):].strip(" -—_·:：")
    # 分P标记："p01 ..." / "P12 ..."（后面没内容时留着也无妨，交给下面判空）
    match = re.match(r"^[pP]\d{1,3}\s*(.*)$", text)
    if match and match.group(1).strip():
        text = match.group(1).strip()
    return text


# ── 展开 ─────────────────────────────────────────────────────────────
def _episodes_from_ytdlp(info: dict[str, Any], *, limit: int) -> list[Episode]:
    """把 ``yt-dlp --flat-playlist -J`` 的结果转成 Episode 列表。"""
    entries = info.get("entries") or []
    episodes: list[Episode] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("webpage_url") or "")
        if not url:
            ident = str(item.get("id") or "")
            if ident.startswith("BV"):
                url = f"https://www.bilibili.com/video/{ident}"
            elif _BV_RE.match(ident):
                url = f"https://www.bilibili.com/video/{_BV_RE.match(ident).group(1)}"
            elif _AV_RE.fullmatch(ident):
                url = f"https://www.bilibili.com/video/{ident.lower()}"
            elif ident.isdigit():
                # 番剧/课程这类条目给的是 ep id
                url = f"https://www.bilibili.com/bangumi/play/ep{ident}"
        if not url:
            continue
        title = clean_media_title(str(item.get("title") or ""), 60)
        episodes.append(Episode(index=len(episodes) + 1, url=url, title=title))
        if len(episodes) >= limit:
            break
    return episodes


def plan(url: str, *, config: Config, engine: Any = None,
         title_hint: str = "",
         feed: "wechat_channels.ChannelsFeed | None" = None,
         log: LogFn = _noop_log) -> SeriesPlan:
    """展开一条链接。任何一步失败都退化成「单集」，绝不因此让订单失败。"""
    single = SeriesPlan(
        title=title_hint,
        episodes=[Episode(index=1, url=url, title=title_hint)],
        source="single",
    )

    if not bool(config.get("download.series.enabled", True)):
        single.note = "配置里关闭了多剧集展开"
        return single
    if not is_series_capable(url):
        return single

    limit = max(1, int(config.get("download.series.max_episodes", 50) or 50))
    # "指定剧集"是明确意图：用户点名了第 N 集，就得取到第 N 集。
    # 否则 max_episodes 先把列表砍到前 N 条、范围再在这 N 条里匹配 ——
    # 实测「上限 2 + 指定第 5 集」会匹配不到，然后静默退化成去下载合集页。
    spec = str(config.get("download.series.episode_range", "") or "")
    wanted = range_max_index(spec)
    probe_limit = max(limit, wanted)          # 探测窗口
    range_mode = bool(spec.strip())           # 是否处于"指定剧集"模式
    if wanted > limit:
        log(f"多剧集：episode_range={spec!r} 点名到第 {wanted} 集，"
            f"探测窗口从 {limit} 扩到 {probe_limit}")

    if wechat_channels.is_channels_url(url):
        return _plan_channels(url, config=config, title_hint=title_hint,
                              feed=feed, probe_limit=probe_limit,
                              range_mode=range_mode, log=log)

    if is_bilibili_url(url):
        return _plan_bilibili(url, config=config, engine=engine,
                              title_hint=title_hint, limit=probe_limit,
                              range_mode=range_mode, log=log)

    return single


def bilibili_season_episodes(url: str, config: Config, *,
                             log: LogFn = _noop_log) -> tuple[list["Episode"], str]:
    """从一个 B站链接（BV 号）拿到它所属的**整个合集/分P**。

    优先用公开的 ``view`` 接口（一次请求、精准）：

    * ``data.ugc_season`` —— 合集。``sections[].episodes[]`` 是全部集，
      每集有 ``bvid`` 和 ``title``。**这就是"粘了合集里某一集也要下整部"的关键**：
      yt-dlp 只拿到单个 BV 时看不到合集。
    * ``data.pages`` 长度 > 1 —— 分P 视频。
    * 两者都没有（普通单集视频）—— 返回空，由调用方回退到 yt-dlp 探测。

    返回 ``(episodes, 说明)``；拿不到就返回 ``([], 原因)``。
    """
    match = _BVID_RE.search(url or "")
    if not match:
        return [], "链接里没有 BV 号"
    bvid = match.group(1)
    try:
        resp = request("GET", f"{BILI_VIEW_API}?bvid={bvid}", timeout=20,
                       verify_tls=bool(config.get("net.verify_tls", True)))
        payload = json.loads(resp.text)      # Response.text 是属性，不是方法
    except Exception as exc:                     # 网络/解析问题都退化成"没拿到"
        return [], f"view 接口失败：{type(exc).__name__}: {exc}"

    # 注意不能用 ``code or -1``：code=0 是**成功**，但 0 是 falsy，会被当成失败。
    code = payload.get("code", -1) if isinstance(payload, dict) else -1
    if not isinstance(payload, dict) or int(code) != 0:
        return [], f"view 接口返回 {code}"

    data = payload.get("data") or {}
    season = data.get("ugc_season") or {}
    episodes_raw: list[dict] = []
    for section in season.get("sections") or []:
        episodes_raw.extend(section.get("episodes") or [])

    if len(episodes_raw) > 1:
        episodes = []
        for index, item in enumerate(episodes_raw, start=1):
            ep_bvid = str(item.get("bvid") or "") or str((item.get("arc") or {})
                                                         .get("bvid") or "")
            if not ep_bvid:
                continue
            episodes.append(Episode(
                index=index,
                url=f"https://www.bilibili.com/video/{ep_bvid}",
                title=clean_media_title(str(item.get("title") or ""), 80),
                source="bilibili",
            ))
        if len(episodes) > 1:
            log(f"B站：view 接口识别到合集《{season.get('title')}》共 {len(episodes)} 集")
            return episodes, ""

    pages = data.get("pages") or []
    if len(pages) > 1:
        episodes = [
            Episode(index=i, url=f"{url.split('?')[0]}?p={page.get('page')}",
                    title=clean_media_title(str(page.get("part") or ""), 80),
                    source="bilibili")
            for i, page in enumerate(pages, start=1)
        ]
        log(f"B站：view 接口识别到分P 共 {len(episodes)} 集")
        return episodes, ""

    return [], "这条视频既不在合集里，也不是多分P"


def _plan_bilibili(url: str, *, config: Config, engine: Any,
                   title_hint: str, limit: int, range_mode: bool = False,
                   log: LogFn = _noop_log) -> SeriesPlan:
    """B 站：一次 ``--flat-playlist`` 就能得到全部分集。"""
    def single(note: str = "", error: str = "") -> SeriesPlan:
        return SeriesPlan(title=title_hint,
                          episodes=[Episode(index=1, url=url, title=title_hint)],
                          source="single", note=note, error=error)

    if engine is None or not getattr(engine, "available", False):
        return single(note="yt-dlp 不可用，按单集处理")

    # 带 ``?p=N`` 的链接要**去掉 p 去探测整部**：yt-dlp 拿到 p 参数时只会返回
    # 那一集，于是"合集/分P"永远只下到当前这一条（用户报的 bug）。
    # 去掉 p 之后：分P 视频会返回全部分P、合集会返回全部条目 —— 探测回来只有
    # 一条时再退化成单集，语义不变。想只下这一集用 ``--single``。
    probe_url = strip_page_param(url)
    if probe_url != url:
        log(f"B站：链接带分P参数，改为探测整部（{probe_url[:80]}）")

    # ① 先用公开 view 接口看"这条 BV 属不属于某个合集/多分P"。
    #    必须放在 yt-dlp 之前：yt-dlp 拿到单个 BV 只会返回那一集，看不到合集，
    #    于是"粘贴合集里的某一集"就只下到一条（用户报的 bug）。
    api_episodes, api_note = bilibili_season_episodes(url, config, log=log)
    if len(api_episodes) > 1:
        truncated = (not range_mode) and len(api_episodes) > limit
        if truncated:
            api_episodes = api_episodes[:limit]
        selected, select_error = _select(api_episodes, config, log, source="B站")
        if select_error:
            return single(error=select_error)
        playlist_name = clean_media_title(title_hint or api_episodes[0].title, 60)
        log(f"B站：本次下载 {len(selected)} 集"
            + ("（已达上限，可用 download.series.max_episodes 调大）" if truncated else ""))
        return SeriesPlan(title=title_hint or playlist_name or "合集",
                          episodes=selected, source="bilibili",
                          truncated=truncated)
    if api_note:
        log(f"B站：{api_note}，回退到 yt-dlp 探测")

    # 多要一条：拿到 limit+1 条才能**确定**是被截断了。
    # 否则"恰好等于上限"的完整合集会被报成"已截断"，对买家是假消息。
    info = engine.probe_playlist(probe_url, limit=limit + 1)
    if not info:
        return single(note="合集探测失败，按单集处理")

    playlist_title = clean_media_title(str(info.get("title") or ""), 60)
    episodes = _episodes_from_ytdlp(info, limit=limit + 1)
    if len(episodes) <= 1:
        # 单 P 视频：yt-dlp 返回的是 video 而不是 playlist，条目只有一个
        return SeriesPlan(title=title_hint or playlist_title,
                          episodes=[Episode(index=1, url=url,
                                            title=title_hint or playlist_title)],
                          source="single")

    # 指定剧集模式下不报"已截断"：用户已经明确选了自己要的集，
    # 再说一句"已达单次上限"只会让人困惑。
    truncated = (not range_mode) and len(episodes) > limit
    if truncated:
        episodes = episodes[:limit]

    selected, select_error = _select(episodes, config, log, source="B站")
    if select_error:
        return single(error=select_error)
    plan = SeriesPlan(
        title=title_hint or playlist_title or "合集",
        episodes=selected,
        source="bilibili",
        truncated=truncated,
    )
    log(f"B站：识别到 {len(episodes)} 集，本次下载 {len(selected)} 集"
        + ("（已达上限，可用 download.series.max_episodes 调大）" if truncated else ""))
    return plan


def _plan_channels(url: str, *, config: Config, title_hint: str,
                   feed: "wechat_channels.ChannelsFeed | None",
                   probe_limit: int, range_mode: bool = False,
                   log: LogFn = _noop_log) -> SeriesPlan:
    """视频号：只有 TikHub 才能列出剧集；否则老实按单集处理。"""
    ref = wechat_channels.parse_ref(url)
    if feed is None:
        feed = wechat_channels.fetch_feed_info(ref, config)
    title = title_hint or feed.title or ""
    single = SeriesPlan(
        title=title,
        episodes=[Episode(index=1, url=url, title=title, source="wechat_channels")],
        source="single",
    )
    if not feed.ok:
        single.note = f"视频号信息获取失败（{feed.error}），按单集处理"
        return single

    # 同样多要一条来判断"是否被上限截断"
    items, note = wechat_channels.list_series(ref, config, feed,
                                              limit=probe_limit + 1, log=log)
    if len(items) < 2:
        single.note = note or "没有识别到视频号剧集，按单集处理"
        return single

    truncated = (not range_mode) and len(items) > probe_limit
    if len(items) > probe_limit:
        items = items[:probe_limit]
    episodes = [
        Episode(index=item.index, title=item.title or "",
                source="wechat_channels",
                object_id=item.object_id, export_id=item.export_id)
        for item in items
    ]
    selected, select_error = _select(episodes, config, log, source="视频号")
    if select_error:
        single.note = select_error
        single.error = select_error
        return single
    plan = SeriesPlan(
        title=title or wechat_channels.series_prefix(feed.title) or "视频号剧集",
        episodes=selected,
        source="wechat_channels",
        truncated=truncated,
    )
    log(f"视频号：识别到 {len(episodes)} 集，本次下载 {len(selected)} 集")
    return plan


def _select(episodes: list[Episode], config: Config, log: LogFn,
            source: str = "") -> tuple[list[Episode], str]:
    """按 ``episode_range`` 过滤。返回 ``(选中的集, 错误)``。

    ``episode_range`` 写了但**一个集号都解析不出来**时返回错误而不是"全下"：
    运营写 ``"1-5"`` 的本意是省流量，静默变成下全部 50 集是**放大**了损失。
    """
    spec = str(config.get("download.series.episode_range", "") or "")
    if not spec.strip():
        return episodes, ""
    keep = set(parse_range(spec, len(episodes)))
    if not keep:
        message = (f"download.series.episode_range={spec!r} 解析不出任何有效集号"
                   f"（总共只有 {len(episodes)} 集），已停止下载，请改成 "
                   f"\"1-10\" 这种写法或留空")
        log(f"{source}：{message}")
        return [], message
    picked = [Episode(index=ep.index, url=ep.url, title=ep.title,
                      source=ep.source, object_id=ep.object_id,
                      export_id=ep.export_id)
              for ep in episodes if ep.index in keep]
    log(f"{source}：episode_range={spec!r} 选中 {len(picked)} 集")
    return picked, ""
