#!/usr/bin/env python3
"""xydl 命令行入口。

    uv run run.py serve              # 启动接单服务（默认命令）
    uv run run.py once "<买家消息>"    # 同步处理一条，打印全过程
    uv run run.py parse "<买家消息>"   # 只看链接识别结果（不联网）
    uv run run.py resolve <url>      # 只做短链展开 / 落源
    uv run run.py download <url>     # 只下载
    uv run run.py login douyin       # 登录抖音并配好 cookie（下不了抖音时用）
    uv run run.py cookies            # 查看浏览器里有没有目标站点的 cookie
    uv run run.py doctor             # 环境自检（代理、引擎、搜索源、cookie）
    uv run run.py selftest           # 离线自测（起本地 HTTP 服务验证全链路）

注意：本机没有 `python` 命令，用 `uv run` 或 `.venv\\Scripts\\python.exe`。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

# 允许直接 `python run.py` 而不必先 pip install
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xydl import __version__, linkparse                       # noqa: E402
from xydl.channels import build_channels                      # noqa: E402
from xydl.channels.base import ChannelManager                 # noqa: E402
from xydl.config import ROOT, Config                           # noqa: E402
from xydl.downloader import Downloader, job_dir_for            # noqa: E402
from xydl.models import OrderStatus, human_size                # noqa: E402
from xydl.pipeline import Pipeline                             # noqa: E402
from xydl.resolver import Resolver                             # noqa: E402
from xydl.search import build_router                           # noqa: E402
from xydl.store import Store                                   # noqa: E402
from xydl.utils import now_str, snippet                        # noqa: E402

BANNER = r"""
   ___  _  _  ___  _
  / __|| || ||   \| |    视频代下载 · 半自动接单流水线
  \__ \ \_/ / | | | |__  链接识别 → 短链展开/搜索落源 → 下载 → 回话
  |___/  |_/  |___/|____|  v{version}
"""


# ══════════════════════════════════════════════════════════════════════
#  公共装配
# ══════════════════════════════════════════════════════════════════════
def setup_stdio() -> None:
    """Windows 控制台默认 cp936，中文/emoji 会炸，这里统一成 UTF-8。

    同时打开行缓冲：stdout 被重定向到文件/管道时 Python 默认是块缓冲，
    启动横幅会一直卡在缓冲区里，看起来像"什么都没输出"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(  # type: ignore[union-attr]
                encoding="utf-8", errors="replace", line_buffering=True
            )
        except (AttributeError, ValueError):
            pass


def build_everything(config_path: str | None = None):
    config = Config.load(config_path).apply_env_overrides()
    dirs = config.ensure_dirs()
    store = Store(dirs["data_dir"] / "xydl.sqlite3")
    pipeline = Pipeline(config, store)
    return config, store, pipeline


def print_env_banner(pipeline: Pipeline, config: Config, channel_desc: str = "") -> None:
    print(f"  配置文件   {config.path}")
    print(f"  下载目录   {pipeline.download_dir}")
    print(f"  下载引擎   {' / '.join(pipeline.downloader.available_engines())}")
    print(f"  搜索源     {pipeline.resolver.router.describe()}")
    print(f"  并发数     {pipeline.workers}")
    if channel_desc:
        print(f"  渠道       {channel_desc}")


# ══════════════════════════════════════════════════════════════════════
#  子命令
# ══════════════════════════════════════════════════════════════════════
def cmd_serve(args) -> int:
    config, store, pipeline = build_everything(args.config)
    channels = build_channels(pipeline, config)
    manager = ChannelManager(channels)

    if not channels:
        print("⚠ 没有启用任何渠道。请在 config.json 里打开 channels.console.enabled")
        return 2

    print(BANNER.format(version=__version__))
    pipeline.start()
    manager.start()

    # 渠道启动失败（最常见的是端口被占用）必须立刻退出并说清楚原因，
    # 否则进程会挂着、什么都不干，而用户看到的只是一个空窗口。
    if manager.errors:
        for name, exc in manager.errors:
            print(f"\n❌ 渠道 [{name}] 启动失败：{exc}")
        manager.stop()
        pipeline.stop()
        store.close()
        print("\n  常见原因与处理：")
        print("   · 端口被占用 → 你很可能已经开了一个窗口在跑。关掉旧的再启动，")
        print("     或改 config.json 里的 server.port（比如换成 8766）。")
        print("   · 排查命令  →  netstat -ano | findstr :8765")
        return 2

    print_env_banner(pipeline, config, manager.describe())

    if not pipeline.downloader.ytdlp.available:
        print("\n  ⚠ 没找到 yt-dlp —— 只能下载直链文件，B站/抖音/YouTube 这类")
        print("    视频页面全都解析不了。多半是用错了 Python 解释器。")
        print("    修复：双击 start.bat 启动（会自动用项目自带的虚拟环境），")
        print("    或先手动跑一次  uv sync")

    console_url = ""
    for channel in channels:
        if getattr(channel, "name", "") == "console":
            console_url = getattr(channel, "url", "")
    if console_url:
        print(f"\n  👉 打开控制台： {console_url}\n")
        if config.get("server.open_browser", True):
            try:
                import webbrowser
                webbrowser.open(console_url)
            except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
                pass
    print("  按 Ctrl+C 停止\n")

    stop_event = threading.Event()
    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        manager.stop()
        pipeline.stop()
        store.close()
    return 0


def cmd_once(args) -> int:
    """同步跑一条消息，把每一步都打在屏幕上。"""
    config, store, pipeline = build_everything(args.config)
    config.set("channels.inbox.enabled", False)
    config.set("channels.console.enabled", False)

    printed: list[int] = []

    def echo(order, text: str, file_path: str) -> None:
        tag = "📤 回话" if not file_path else "📦 交付"
        print(f"\n{tag} → {order.conversation_id}")
        for line in text.splitlines():
            print(f"    {line}")
        if file_path:
            print(f"    文件：{file_path}")

    pipeline.add_deliverer(echo)
    pipeline.start()
    try:
        order, created = pipeline.submit_text(
            args.text, conversation_id=args.conversation, channel="cli",
        )
        print(f"\n订单 #{order.id}（{'新单' if created else '重复，已忽略'}）"
              f"  {now_str()}")
        if not created:
            return 0

        deadline = time.time() + args.timeout
        last_stage = ""
        while time.time() < deadline:
            current = store.get(order.id)
            if current is None:
                break
            if current.stage != last_stage:
                last_stage = current.stage
                print(f"  [{current.status_label}] {current.stage}"
                      f"  {current.progress:.0f}%")
            if OrderStatus(current.status).is_terminal:
                break
            time.sleep(0.4)

        final = store.get(order.id)
        if final is None:
            print("订单消失了？")
            return 1

        print("\n" + "─" * 64)
        print(f"状态      {final.status_label}")
        print(f"标题      {final.title or '—'}")
        print(f"下载源    {final.chosen_url or '—'}")
        if final.file_path:
            print(f"文件      {final.file_path}  ({final.file_size_human})")
        if final.error:
            print(f"错误      {final.error}")
        print("─" * 64)
        print("处理日志：")
        for event in store.events(order.id, limit=40):
            print(f"  [{event['level']:<5}] {event['message']}")
        return 0 if final.status == OrderStatus.DONE.value else 1
    finally:
        pipeline.stop()
        store.close()


def cmd_parse(args) -> int:
    """只跑链接识别，不联网 —— 用来调正则/看系统怎么理解买家的话。"""
    result = linkparse.parse(args.text)
    print(f"原文      {snippet(args.text, 100)}")
    print(f"清洗后    {snippet(result.cleaned_text, 100) or '（空）'}")
    print(f"标题猜测  {result.title_guess or '（无）'}")
    print(f"结论      {result.note}")
    print(f"链接      {len(result.links)} 条")
    for idx, cand in enumerate(result.links, 1):
        print(f"  {idx}. [{cand.platform_label}] kind={cand.kind} "
              f"conf={cand.confidence} source={cand.source}")
        print(f"     {cand.url}")
        if cand.note:
            print(f"     备注：{cand.note}")
    if args.json:
        print(json.dumps([c.to_dict() for c in result.links],
                         ensure_ascii=False, indent=2))
    return 0


def cmd_resolve(args) -> int:
    config = Config.load(args.config).apply_env_overrides()
    resolver = Resolver(config)
    print(f"搜索源：{resolver.router.describe()}")

    if args.query:
        print(f"\n用标题落源：{args.query!r}")
        results = resolver.router.find_source(args.query)
        if not results:
            print("  没有结果。", resolver.router.errors)
        for idx, item in enumerate(results[:8], 1):
            print(f"  {idx}. score={item.score} [{item.domain}] {snippet(item.title, 60)}")
            print(f"     {item.url}")
        return 0 if results else 1

    for url in args.urls:
        print(f"\n展开 {url}")
        result = resolver.expand(url)
        for hop, step in enumerate(result.chain, 1):
            print(f"  {hop}. {step}")
        print(f"  最终  {result.final_url}")
        print(f"  状态  {result.status}  跳数 {result.hops}  错误 {result.error or '无'}")
    return 0


def cmd_download(args) -> int:
    config = Config.load(args.config).apply_env_overrides()
    downloader = Downloader(config)
    outdir = Path(args.out) if args.out else config.path_of("download_dir") / "cli"
    outdir.mkdir(parents=True, exist_ok=True)
    parsed = linkparse.parse(args.url)
    kind = parsed.links[0].kind if parsed.links else "page"

    print(f"引擎：{' / '.join(downloader.available_engines())}")
    print(f"输出：{outdir}\n")

    last = [-1.0]

    def on_progress(p):
        if p.percent - last[0] >= 2 or p.stage != "下载中":
            last[0] = p.percent
            speed = f"  {p.speed_human}" if p.speed_human else ""
            print(f"  [{p.stage}] {p.percent:5.1f}%  {human_size(p.downloaded)}"
                  f"{speed}  ETA {p.eta}", flush=True)

    started = time.time()
    result = downloader.download(args.url, job_dir_for(outdir, "cli"), kind=kind,
                                 title_hint=args.title, progress=on_progress)
    print()
    if result.ok:
        print(f"✅ 成功  {result.file_path}")
        print(f"   大小 {result.size_human}  耗时 {time.time() - started:.1f}s  "
              f"引擎 {result.engine}")
        if result.title:
            print(f"   标题 {result.title}")
        return 0
    print(f"❌ 失败  {result.error}")
    return 1


def cmd_login(args) -> int:
    """打开浏览器让你登录目标站点，然后自动把 cookie 配置写好并**当场验证**。

    这条路用的是 yt-dlp 官方支持的「读你自己浏览器的 cookie」能力。
    """
    from xydl import cookies as ck

    site_key = (args.site or "douyin").lower()
    if site_key not in ck.SITES:
        print(f"不支持的站点 {site_key!r}。可选：{', '.join(ck.SITES)}")
        return 2
    label, url, _ = ck.SITES[site_key]

    print(BANNER.format(version=__version__))
    print(f"  目标站点   {label}  {url}\n")

    browsers = [b for b in ck.find_browsers() if b.installed]
    if not browsers:
        print("❌ 没找到任何浏览器。装一个 Chrome 或 Edge 再回来。")
        return 2
    # 把启用了 App-Bound 加密的浏览器排到后面 —— 那里面新写的 cookie
    # yt-dlp 解不开，选它等于白折腾一趟。
    browsers.sort(key=lambda b: (b.app_bound, b.key))

    chosen = None
    if args.browser:
        chosen = next((b for b in browsers if b.key == args.browser.lower()), None)
        if chosen is None:
            print(f"❌ 没找到浏览器 {args.browser}；检测到的是："
                  f"{', '.join(b.key for b in browsers)}")
            return 2
        if chosen.app_bound:
            print(f"\n  ⚠ {chosen.label} 启用了 App-Bound 加密，新登录写入的 cookie")
            print("    yt-dlp 解不开。建议换用其它浏览器，或走 cookies.txt 方式。")
    else:
        chosen = browsers[0]
        if len(browsers) > 1:
            print("  浏览器可用性（越靠前越可能成功）：")
            for idx, browser in enumerate(browsers, 1):
                mark = "  ← 用它" if idx == 1 else ""
                print(f"    {idx}. {browser.describe()}{mark}")

    # ── 先看看现在有没有 ──────────────────────────────────────────
    existing = ck.pick_best(site_key)
    if existing is not None and not args.force:
        print(f"\n  ℹ {label} 的 cookie 已经在了：")
        print(f"    {existing.profile}")
        print(f"    共 {existing.for_site(site_key)} 条")
        if args.check:
            return 0
        print("    如果下载仍然失败，加 --force 重新登录一次。")
        _apply_cookie_config(args, ck, existing)
        return _after_login(args, site_key)

    if args.check:
        print(f"\n  ✗ 没找到 {label} 的 cookie")
        print(f"    跑 `run.py login {site_key}` 来配置")
        return 1

    # ── 浏览器开着的话等它关掉（用户多半已经登录过了）────────────
    reports = ck.inspect([site_key])
    locked = [r for r in reports if _is_locked(r.error)]
    if locked and not args.no_wait:
        names = "、".join(sorted({r.browser for r in locked}))
        print(f"\n  检测到 {names} 正在运行 —— cookie 数据库被它锁住了，读不出来。")
        print("  Edge/Chrome 对 cookie 库是**独占锁**，不关掉就绝对读不到。")
        print(f"\n  如果你已经在 {names} 里登录过 {label}：")
        print("    直接**完全关闭浏览器**（托盘/后台进程也要退），我会自动等它退出。")
        print(f"  最多等 {args.wait} 秒，不用再敲任何命令。\n")

        deadline = time.time() + args.wait
        dots = 0
        while time.time() < deadline:
            time.sleep(2)
            reports = ck.inspect([site_key])
            if not any(_is_locked(r.error) for r in reports):
                break
            dots += 1
            print(f"  等待浏览器关闭{'.' * (dots % 4):<4}", end="\r", flush=True)
        print(" " * 34, end="\r")

        still_locked = [r for r in reports if _is_locked(r.error)]
        if still_locked:
            print(f"  ✗ 等了 {args.wait} 秒，浏览器还开着。")
            print("    关掉之后再跑一次这个命令（或加 --wait 600 多等一会儿）。")
            return 1

        found = ck.pick_best(site_key, reports)
        if found is not None:
            print(f"  ✓ 已捕获 {label} 的 cookie")
            print(f"    浏览器   {found.browser}")
            print(f"    配置     {found.profile}")
            print(f"    条数     {found.for_site(site_key)}")
            warning = found.warn()
            if warning:
                print(f"\n  ⚠ {warning}")
                print("    导出 cookies.txt 后填到 config.json 的 "
                      "download.cookies_file 即可绕过这个限制")
                return 1
            _apply_cookie_config(args, ck, found)
            return _after_login(args, site_key)

        print(f"  ✗ 浏览器关掉了，但里面没有 {label} 的 cookie。")
        print("    可能你登录的不是这个浏览器，或者登录后没真正打开过该站点。")
        print("    下面直接开一个浏览器让你登录。")

    # ── 启动浏览器 ────────────────────────────────────────────────
    print(f"\n  正在打开 {chosen.label} …")
    try:
        subprocess.Popen([str(chosen.exe), url])
    except OSError as exc:
        print(f"  ❌ 启动浏览器失败：{exc}")
        return 2

    print(f"""
  ┌─────────────────────────────────────────────────────────────┐
  │  1. 在刚打开的浏览器窗口里打开 {label}，需要登录就登录一次     │
  │  2. 随便刷一两个视频，确认能正常播放                          │
  │  3. **完全关闭浏览器** —— 包括托盘/后台残留的进程             │
  │     （浏览器不关，cookie 数据库会被锁住，读不出来）           │
  │  4. 回到这个窗口按回车                                        │
  └─────────────────────────────────────────────────────────────┘
""")
    try:
        input("  登录并关闭浏览器后，按回车继续… ")
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消")
        return 130

    # ── 检查结果 ──────────────────────────────────────────────────
    print()
    report = ck.pick_best(site_key)
    if report is None:
        print(f"  ❌ 还是没读到 {label} 的 cookie。可能的原因：")
        print("     · 浏览器没完全关掉（后台进程还占着数据库）")
        print("     · 没有真正打开过该站点")
        for item in ck.inspect([site_key])[:6]:
            if item.error:
                print(f"     · {item.browser}: {item.error}")
        return 1

    print(f"  ✓ 已捕获 {label} 的 cookie")
    print(f"    浏览器   {report.browser}")
    print(f"    配置     {report.profile}")
    print(f"    条数     {report.for_site(site_key)}")

    _apply_cookie_config(args, ck, report)
    return _after_login(args, site_key)


def _is_locked(error: str) -> bool:
    return "锁住" in error


def _after_login(args, site_key: str) -> int:
    """配好 cookie 之后的收尾：要么当场验证，要么告诉用户下一步。"""
    from xydl import cookies as ck

    if args.test:
        label = ck.SITES[site_key][0]
        print(f"\n  正在用这条链接实测 {label} 能不能解析…")
        downloader = Downloader(Config.load(args.config).apply_env_overrides())
        info = downloader.ytdlp.probe(args.test)
        if info:
            print(f"  ✅ 解析成功：{info.get('title')}")
            print(f"     作者 {info.get('uploader') or '-'}  "
                  f"时长 {info.get('duration') or '-'}s")
            print("\n  现在可以重启服务（start.bat）开始接单了。")
            return 0
        print("  ❌ 解析仍然失败。")
        print("     跑 `run.py doctor` 看完整状态；")
        print("     或把 `run.py once \"<链接>\"` 的输出发出来。")
        return 1

    print("\n  下一步：重启服务（start.bat），然后就可以下这个站点的视频了。")
    print("  想当场验证的话，加 --test \"<一条该站点的链接>\" 再跑一次。")
    return 0


def _apply_cookie_config(args, ck, report) -> None:
    """把 --cookies-from-browser 写进 config.json。"""
    config = Config.load(args.config).apply_env_overrides()
    spec = ck.ytdlp_spec(report.browser, report.profile)
    config.set("download.cookies_from_browser", spec)
    config.set("download.cookies_file", "")
    path = config.save()
    print(f"\n  ✓ 已写入 {path}")
    print(f'    "download": {{ "cookies_from_browser": "{spec}" }}')


def cmd_cookies(args) -> int:
    """只看状态：哪些浏览器、有没有目标站点的 cookie。"""
    from xydl import cookies as ck

    sites = [args.site.lower()] if args.site else list(ck.SITES)
    for site in sites:
        if site not in ck.SITES:
            print(f"不支持的站点 {site!r}")
            return 2

    print(BANNER.format(version=__version__))
    reports = ck.inspect(sites)
    if not reports:
        print("  没找到任何浏览器的配置目录。")
        return 1

    width = max(len(ck.SITES[s][0]) for s in sites)
    print(f"  {'站点':<{width}}  cookie 情况")
    for site in sites:
        label = ck.SITES[site][0]
        best = ck.pick_best(site, reports)
        if best is None:
            print(f"  {label:<{width}}  ✗ 没有（跑 run.py login {site}）")
            continue
        print(f"  {label:<{width}}  ✓ {best.for_site(site)} 条（{best.browser}）")
        warning = best.warn()
        if warning:
            print(f"  {'':<{width}}  ⚠ {warning}")
    print("\n  各浏览器 profile 明细：")
    for report in reports:
        status = report.error or "、".join(
            f"{ck.SITES[s][0]}={report.for_site(s)}" for s in sites
            if report.for_site(s)
        ) or "无目标站点 cookie"
        print(f"    {report.browser:8} {report.profile.name:12} {status}")
        if report.versions:
            print(f"    {'':8} {'':12} 加密方案 {report.versions}")
    return 0


#: 我们的平台 key → yt-dlp extractor 名字里应该出现的关键字
_EXTRACTOR_KEYS: dict[str, str] = {
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
    "bilibili": "bilibili",
    "kuaishou": "kuaishou",
    "weibo": "weibo",
    "xigua": "ixigua",
    "tiktok": "tiktok",
    "youtube": "youtube",
    "twitter": "twitter",
    "instagram": "instagram",
    "facebook": "facebook",
    "vimeo": "vimeo",
    "dailymotion": "dailymotion",
    "twitch": "twitch",
    "haokan": "haokan",
    "pipixia": "ippzone",
    "wechat": "weixin",
    "tencent_video": "vqq",
    "iqiyi": "iqiyi",
    "youku": "youku",
    "mgtv": "mgtv",
}


#: 实测**必须**带 cookie 才能解析的平台（关掉就会失败）
MEASURED_REQUIRE_COOKIE: frozenset[str] = frozenset({"douyin", "xigua"})

#: 实测**带了反而会坏**的平台（cookie 与会话不匹配会被风控拒绝）
MEASURED_BREAK_WITH_COOKIE: frozenset[str] = frozenset(
    {"youtube", "tiktok", "twitter", "instagram", "facebook"}
)

#: 实测**带不带都行**的平台
MEASURED_EITHER_WAY: frozenset[str] = frozenset({"xiaohongshu", "bilibili"})


def cmd_platforms(args) -> int:
    """列出各平台的支持情况 —— 接单前先查一下，别接了下不了的单。"""
    from xydl import linkparse

    try:
        from yt_dlp.extractor import gen_extractor_classes
        names = {ie.IE_NAME.lower() for ie in gen_extractor_classes()}
    except Exception as exc:  # noqa: BLE001
        print(f"读不到 yt-dlp 的 extractor 列表：{exc}")
        return 1

    config = Config.load(args.config).apply_env_overrides()
    cookie_domains = [str(d).lower() for d in
                      (config.get("download.cookie_domains", []) or [])]

    def sends_cookies(rule) -> bool:
        return any(any(d == dom or d.endswith("." + dom) for dom in cookie_domains)
                   for d in rule.all_domains)

    print(BANNER.format(version=__version__))
    print("  平台              支持   cookie   说明")
    print("  " + "-" * 70)
    ok: list[str] = []
    bad: list[str] = []
    for rule in linkparse.PLATFORMS:
        key = _EXTRACTOR_KEYS.get(rule.key, rule.key)
        supported = any(key in n for n in names)

        if rule.drm:
            cookie = "-"
            mark, note = "⛔", "受版权保护，按设计转人工"
            bad.append(rule.label)
        elif not supported:
            cookie = "-"
            mark, note = "❌", "yt-dlp 没有对应解析器"
            bad.append(rule.label)
        else:
            ok.append(rule.label)
            if rule.key in MEASURED_BREAK_WITH_COOKIE:
                cookie = "不带"
                mark, note = "✅", "实测带了 cookie 反而被风控拒绝"
            elif rule.key in MEASURED_REQUIRE_COOKIE:
                cookie = "必须带"
                mark, note = "✅", "实测不带 cookie 解析不了"
            elif rule.key in MEASURED_EITHER_WAY:
                cookie = "会带"
                mark, note = "✅", "实测不带也行（带上可能拿到更高画质）"
            elif sends_cookies(rule):
                cookie = "会带"
                mark, note = "✅", "未实测（无有效样本链接）"
            else:
                cookie = "不带"
                mark, note = "✅", ""
        print(f"  {mark} {rule.label:<14}{('是' if supported else '否'):<7}"
              f"{cookie:<9}{note}")

    print()
    print(f"  可用 {len(ok)} 个：{'、'.join(ok)}")
    print(f"  不可用 {len(bad)} 个：{'、'.join(bad)}")
    print()
    print("  关于 cookie（实测结论）：")
    print("   · 抖音必须配 —— 不带 cookie 会报 Fresh cookies are needed")
    print("   · YouTube 千万不能带 —— 带了会被风控拒绝，反而下不了")
    print("   · 小红书 / 哔哩哔哩 带不带都行（带上可能拿到更高画质）")
    print("   配法：start.bat login douyin")
    print()
    print("  升级 yt-dlp（解析器失效时用）：")
    print("     .venv\\Scripts\\python.exe -m pip install -U yt-dlp")
    return 0


def cmd_doctor(args) -> int:
    """环境自检：出问题时第一件该跑的事。"""
    from xydl.http import parse_proxy_spec, system_proxy_url

    config = Config.load(args.config).apply_env_overrides()
    print(BANNER.format(version=__version__))
    print(f"Python        {sys.version.split()[0]}  ({sys.executable})")
    print(f"项目根目录    {ROOT}")
    print(f"配置文件      {config.path}"
          f"{'' if Path(str(config.path)).exists() else '  ← 不存在，已按默认值运行'}")

    print("\n[网络]")
    spec = parse_proxy_spec(config.get("net.proxy"))
    system = system_proxy_url()
    mode = {"None": "跟随系统代理", "": "强制直连"}.get(repr(spec), f"指定代理 {spec}")
    print(f"  net.proxy   = {config.get('net.proxy')!r}  → {mode}")
    print(f"  系统代理     {system or '（未设置）'}")
    print(f"  TLS 校验     {'开' if config.get('net.verify_tls', True) else '关'}")

    print("\n[下载引擎]")
    downloader = Downloader(config)
    for line in downloader.available_engines():
        print(f"  {line}")
    if downloader.ytdlp.available:
        print(f"  yt-dlp 代理  {downloader.ytdlp.proxy or '（不传，直连）'}")
        ffmpeg = downloader.ytdlp.find_ffmpeg()
        if ffmpeg:
            print(f"  ffmpeg       已启用  {ffmpeg}")
        else:
            print("  ffmpeg       不可用 ← 重要：B站/YouTube/小红书 等只提供 DASH 分离流，")
            print("               没有可用的 ffmpeg 就无法合流，这类站点必然下载失败。")
            print(f"               查找情况：{downloader.ytdlp.describe_ffmpeg()}")
            print("               修复：装一个能直接运行的 ffmpeg，把路径填到 config.json")
            print("               的 download.ffmpeg_location（注意指向 ffmpeg.exe 本身，")
            print("               不要指向失效的 mise shims）")
        print(f"  画质         {config.get('download.quality')}")
        print(f"  单文件上限   {config.get('download.max_filesize_mb')} MB")

    print("\n[Cookie（抖音/小红书等风控站点必需）]")
    from xydl import cookies as ck

    cookie_file = str(config.get("download.cookies_file", "") or "").strip()
    browser = str(config.get("download.cookies_from_browser", "") or "").strip()
    if cookie_file:
        exists = Path(cookie_file).exists()
        print(f"  cookies_file          {cookie_file}  "
              f"{'✓ 存在' if exists else '✗ 文件不存在'}")
    if browser:
        print(f"  cookies_from_browser  {browser}")
    if not cookie_file and not browser:
        print("  ✗ 两个都没配 —— 抖音、小红书这类有风控的站点会直接下载失败")

    watch = ["douyin", "xiaohongshu", "kuaishou"]
    reports = ck.inspect(watch)
    for site in watch:
        label = ck.SITES[site][0]
        best = ck.pick_best(site, reports)
        if best is None:
            print(f"  {label:<8}✗ 浏览器里没有 —— 跑 `run.py login {site}`")
        else:
            print(f"  {label:<8}✓ {best.for_site(site):>3} 条"
                  f"（{best.browser} · {best.profile.name}）")
    for report in reports:
        if report.error:
            print(f"  提示：{report.browser} {report.profile.name} → {report.error}")
    if not [r for r in reports if r.ok]:
        browsers = [b for b in ck.find_browsers() if b.installed]
        print(f"  本机浏览器：{'、'.join(b.key for b in browsers) if browsers else '（未检测到）'}")

    print("\n[搜索源]")
    router = build_router(config)
    for provider in router.providers:
        print(f"  ✓ {provider.label}（{provider.name}）")
    missing = {"serper": "search.serper_api_key",
               "google_cse": "search.google_cse_key / google_cse_cx"}
    from xydl.search import PROVIDER_CLASSES
    for name, cls in PROVIDER_CLASSES.items():
        if name in ("static",):
            continue
        if not any(p.name == name for p in router.providers):
            print(f"  ✗ {name}  未启用（需要 {missing.get(name, '配置')}）")
    print(f"  → {router.describe()}")

    print("\n[目录]")
    for key, path in config.ensure_dirs().items():
        print(f"  {key:<14} {path}")

    print("\n[渠道]")
    for name in ("console", "inbox"):
        enabled = config.get(f"channels.{name}.enabled", False)
        extra = ""
        if name == "console":
            extra = f"  http://{config.get('server.host')}:{config.get('server.port')}"
        print(f"  {'✓' if enabled else '✗'} {name}{extra if enabled else ''}")

    # 数据库能不能打开
    print("\n[数据库]")
    try:
        store = Store(config.path_of("data_dir") / "xydl.sqlite3")
        stats = store.stats()
        print(f"  OK  订单 {stats.get('total', 0)} 条（进行中 {stats.get('active', 0)}）")
        store.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  失败：{type(exc).__name__}: {exc}")
        return 1
    return 0


def cmd_selftest(args) -> int:
    """离线自测：起一个本地 HTTP 服务器，把「识别 → 落源 → 下载 → 回话」跑通。

    不依赖外网，所以能用来确认「装好了」而不是「网通了」。
    """
    import functools
    import http.server
    import socketserver
    import tempfile

    from xydl.downloader import HttpEngine

    print(BANNER.format(version=__version__))
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {name}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    # ── 1. 链接识别 ────────────────────────────────────────────────
    print("\n[1/5] 链接识别")
    cases = [
        ("7.92 复制打开抖音 https://v.douyin.com/iRNBho6/",
         "douyin", "short", "这个视频太搞笑了"),
        ("http://xhslink.com/a/xxxxx 打开小红书App查看", "xiaohongshu", "short", ""),
        ("https://www.bilibili.com/video/BV1xx411c7mD",
         "bilibili", "page", ""),
        ("https://v.qq.com/x/cover/abc.html", "tencent_video", "drm", ""),
        ("就这个视频，猫咪打呼噜合集", None, None, "猫咪打呼噜合集"),
    ]
    for text, platform, kind, title in cases:
        result = linkparse.parse(text)
        if platform is None:
            check(f"无链接但识别标题：{snippet(text, 26)}",
                  not result.links and result.title_guess == title,
                  f"→ {result.title_guess!r}")
        else:
            top = result.best
            check(f"{snippet(text, 30)}",
                  top is not None and top.platform == platform and top.kind == kind,
                  f"→ {top.platform}/{top.kind} conf={top.confidence}" if top else "→ 无")

    # ── 2. 本地 HTTP 下载 ─────────────────────────────────────────
    print("\n[2/5] 直链下载（本地 HTTP 服务器）")
    serve_root = Path(tempfile.mkdtemp(prefix="xydl-serve-"))
    payload = bytes(range(256)) * 8192          # 2 MiB 可校验内容
    (serve_root / "sample.mp4").write_bytes(payload)
    (serve_root / "page.html").write_text("<html><title>x</title></html>", encoding="utf-8")

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *a):  # 自测不需要每条请求都刷屏
            pass

    handler = functools.partial(_QuietHandler, directory=str(serve_root))

    class _Quiet(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, request, client_address):  # 别把 traceback 刷屏
            pass

    httpd = _Quiet(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    config = Config.load(args.config).apply_env_overrides()
    # 本地回环必须直连，避免走了代理反而连不上自己
    engine = HttpEngine(config)
    engine.proxy = ""
    try:
        out = Path(tempfile.mkdtemp(prefix="xydl-out-")) / "job"
        seen: list[float] = []
        result = engine.download(f"http://127.0.0.1:{port}/sample.mp4", out,
                                 progress=lambda p: seen.append(p.percent))
        check("下载 mp4 成功", result.ok, result.file_path)
        check("字节数一致", result.size_bytes == len(payload),
              f"{result.size_bytes} vs {len(payload)}")
        check("收到进度回调", len(seen) >= 2, f"{len(seen)} 次")
        check("内容未损坏",
              result.ok and Path(result.file_path).read_bytes() == payload)

        result2 = engine.download(f"http://127.0.0.1:{port}/page.html",
                                  Path(tempfile.mkdtemp()) / "job2")
        check("拒绝把网页当视频存下来", not result2.ok, snippet(result2.error, 40))
    finally:
        httpd.shutdown()
        httpd.server_close()

    # ── 3. 回话模板 ────────────────────────────────────────────────
    print("\n[3/5] 回话模板")
    from xydl.models import Order
    from xydl.replies import Replies

    replies = Replies(config)
    sample = Order(id=1, title="猫咪打呼噜", file_path="C:/dl/猫咪打呼噜.mp4",
                   file_size=12_345_678, status=OrderStatus.DONE.value)
    done = replies.done(sample)
    check("完成话术含文件与体积", "猫咪打呼噜.mp4" in done and "11.8 MB" in done,
          snippet(done.replace("\n", " / "), 60))
    broken = replies.render("done", title=None)   # 缺字段也不该抛异常
    check("模板缺字段不抛异常", isinstance(broken, str))

    # ── 4. 存储层 ─────────────────────────────────────────────────
    print("\n[4/5] 订单存储")
    tmpdb = Path(tempfile.mkdtemp(prefix="xydl-db-")) / "t.sqlite3"
    store = Store(tmpdb)
    try:
        order, created = store.create_order("t:1", "test", "buyer1", "hello")
        check("建单", created and order.id is not None)
        _, again = store.create_order("t:1", "test", "buyer1", "hello")
        check("同 external_id 幂等", not again)
        store.update(order.id, status=OrderStatus.DOWNLOADING.value, progress=42.5)
        refreshed = store.get(order.id)
        check("更新状态与进度",
              refreshed.status == "downloading" and abs(refreshed.progress - 42.5) < 0.01)
        store.push_outbox(order.id, "buyer1", "你好")
        batch = store.pop_outbox()
        check("outbox 投递且只投一次",
              len(batch) == 1 and len(store.pop_outbox()) == 0)
        # create_order 自己会写一条「接单」日志，所以这里比对增量而不是绝对条数
        before = len(store.events(order.id))
        store.log(order.id, "一句日志")
        after = store.events(order.id)
        check("事件日志按序追加",
              len(after) == before + 1 and after[-1]["message"] == "一句日志",
              f"{before} → {len(after)} 条")
        check("统计可用", store.stats().get("total", 0) >= 1)
    finally:
        store.close()

    # ── 5. 短链展开（需要外网）─────────────────────────────────────
    print("\n[5/5] 短链展开（需要外网，失败不算致命）")
    resolver = Resolver(config)
    expanded = resolver.expand("https://b23.tv/BV1GJ411x7h7")
    ok = expanded.status == 200 and "bilibili.com" in expanded.final_url
    print(f"  {'✓' if ok else '!'} b23.tv → {snippet(expanded.final_url, 60)}"
          f"（{expanded.hops} 跳）{'' if ok else '  ← 外网不通或需要配代理'}")

    print("\n" + "─" * 60)
    if failures:
        print(f"❌ 自测未通过：{len(failures)} 项 —— {', '.join(failures)}")
        return 1
    print("✅ 自测全部通过，环境可用")
    return 0


# ══════════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="视频代下载 · 半自动接单流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"xydl {__version__}")
    parser.add_argument("--config", help="配置文件路径（默认 ./config.json）")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="启动接单服务（默认）")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("once", help="同步处理一条消息")
    p.add_argument("text", help="买家发来的原文")
    p.add_argument("-c", "--conversation", default="cli", help="会话标识")
    p.add_argument("-t", "--timeout", type=float, default=1800, help="等待秒数")
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("parse", help="只看链接识别（不联网）")
    p.add_argument("text")
    p.add_argument("--json", action="store_true", help="额外输出 JSON")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("resolve", help="短链展开 / 搜索落源")
    p.add_argument("urls", nargs="*", help="要展开的链接")
    p.add_argument("-q", "--query", default="", help="改用标题做搜索落源")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("download", help="只下载")
    p.add_argument("url")
    p.add_argument("-o", "--out", help="输出目录")
    p.add_argument("-t", "--title", default="", help="文件名提示")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("login", help="打开浏览器登录目标站点并配好 cookie（抖音/小红书用）")
    p.add_argument("site", nargs="?", default="douyin",
                   help="站点：douyin / xiaohongshu / kuaishou / weibo / "
                        "bilibili / tiktok / instagram")
    p.add_argument("--browser", help="用哪个浏览器（edge / chrome / firefox …）")
    p.add_argument("--force", action="store_true", help="即使已有 cookie 也重新登录")
    p.add_argument("--check", action="store_true", help="只检查，不启动浏览器")
    p.add_argument("--no-wait", action="store_true",
                   help="浏览器开着时不要等它关闭")
    p.add_argument("--wait", type=float, default=240,
                   help="等浏览器关闭的秒数（默认 240）")
    p.add_argument("--test", help="配好后立刻用这条链接验证能不能解析")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("cookies", help="查看各浏览器里有没有目标站点的 cookie")
    p.add_argument("site", nargs="?", help="只查某一个站点")
    p.set_defaults(func=cmd_cookies)

    p = sub.add_parser("platforms", help="列出各平台支持情况（接单前先查）")
    p.set_defaults(func=cmd_platforms)

    p = sub.add_parser("selftest", help="离线自测")
    p.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve"])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
