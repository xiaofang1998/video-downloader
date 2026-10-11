#!/usr/bin/env python3
"""xydl 命令行入口。

    uv run run.py serve              # 启动接单服务（默认命令）
    uv run run.py once "<买家消息>"    # 同步处理一条，打印全过程
    uv run run.py parse "<买家消息>"   # 只看链接识别结果（不联网）
    uv run run.py resolve <url>      # 只做短链展开 / 落源
    uv run run.py download <url>     # 只下载（B站/视频号多剧集会自动展开）
    uv run run.py series <url>       # 看这条链接会下几集（不下载）
    uv run run.py wxinfo <url>       # 看视频号链接能解析到什么（不下载）
    uv run run.py decrypt <文件> -k <decode_key>   # 解密本地抓下来的视频号文件
    uv run run.py channels-login     # 登录视频号助手（诊断用；实测不能下视频号视频）
    uv run run.py login douyin       # 登录抖音并配好 cookie（下不了抖音时用）
    uv run run.py cookies            # 查看浏览器里有没有目标站点的 cookie
    uv run run.py baidu-login        # 授权百度网盘（交付用：发链接而不是发文件）
    uv run run.py upload <文件>       # 传一个文件到网盘，打印分享链接
    uv run run.py bridge-ping        # 检查上游「闲鱼超级管家」是否在线
    uv run run.py doctor             # 环境自检（代理、引擎、搜索源、cookie、交付）
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
from xydl.utils import now_str, sanitize_filename, snippet   # noqa: E402

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
    from xydl.uploaders import build_uploader

    print(f"  交付       {build_uploader(config).describe()}")


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

    # 控制台首页会展示「当前启用了哪些渠道」。渠道之间互相不认识，得由这里
    # 把汇总描述塞给它 —— 否则首页永远只显示「本地网页控制台」，
    # 桥接明明开着也看不出来，很容易误判成没生效。
    for channel in channels:
        setter = getattr(channel, "set_channels_desc", None)
        if callable(setter):
            setter(manager.describe())

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


def cmd_delogo(args) -> int:
    """给已下载的文件擦水印（ffmpeg delogo）。

    **有损**：用周边像素插值盖住水印区域，会留淡淡痕迹、局部画质下降。
    所以默认**另存** `xxx.nowm.mp4`，要覆盖源文件必须显式 `--in-place`。
    水印位置优先用 `--rect x:y:w:h`（准），其次按 `--platform` 预设（估），
    两者都没有就直接报错 —— 盖错地方比不盖更糟。
    """
    from xydl import watermark

    config = Config.load(args.config).apply_env_overrides()
    ffmpeg = Downloader(config).ytdlp.find_ffmpeg()
    if not ffmpeg:
        print("❌ 没找到 ffmpeg（打包版自带；源码运行请先装 ffmpeg）")
        return 2

    source = Path(args.path)
    if not source.is_file():
        print(f"❌ 找不到文件：{source}")
        return 2
    platform = args.platform or watermark.guess_platform(str(source))
    out = source if args.in_place else (Path(args.out) if args.out else None)

    width, height = watermark.probe_size(ffmpeg, source)
    print(f"源文件    {source}")
    print(f"分辨率    {width}x{height}" if width
          else "分辨率    （读不到 → 只能用 --rect 手动指定）")
    if platform:
        print(f"平台预设  {platform} —— {watermark.preset_label(platform)}")
    print(f"处理方式  {'擦水印后覆盖源文件' if args.in_place else '另存为 .nowm 文件'}")

    # 先 dry-run 把矩形算出来给人看：这一步最能避免"盖错地方"
    ok, _target, message = watermark.delogo_file(
        ffmpeg, source,
        platform="" if args.rect else platform, rect=args.rect, out=out, dry_run=True)
    if not ok:
        print(f"❌ {message}")
        return 2
    print(f"滤镜      {message}")
    if args.dry_run:
        print("（--dry-run：没有真的转码）")
        return 0

    print("\n转码中…")
    ok, target, message = watermark.delogo_file(
        ffmpeg, source,
        platform="" if args.rect else platform, rect=args.rect, out=out)
    if not ok:
        print(f"❌ {message}")
        return 1
    print(f"✅ {message}")
    print(f"   产物    {target}")
    print("   提示    delogo 是插值覆盖：矩形越准效果越好，可用 --rect 微调后重跑")
    return 0


def cmd_download(args) -> int:
    from xydl import series as series_mod

    config = Config.load(args.config).apply_env_overrides()
    if getattr(args, "single", False):
        config.set("download.series.enabled", False)
    if getattr(args, "range", ""):
        config.set("download.series.episode_range", args.range)
    if getattr(args, "quality", ""):
        config.set("download.quality", args.quality)
    if getattr(args, "watermark", ""):
        config.set("download.watermark", args.watermark)
    downloader = Downloader(config)
    outdir = Path(args.out) if args.out else config.path_of("download_dir") / "cli"
    outdir.mkdir(parents=True, exist_ok=True)
    parsed = linkparse.parse(args.url)
    kind = parsed.links[0].kind if parsed.links else "page"

    print(f"引擎：{' / '.join(downloader.available_engines())}")
    print(f"输出：{outdir}\n")

    # 先看是不是多剧集 —— 接单场景里"以为一集、结果 80 集"是真实事故
    plan = series_mod.plan(args.url, config=config, engine=downloader.ytdlp,
                           title_hint=args.title)
    if plan.error:
        print(f"❌ 合集配置有误，已停止：{plan.error}")
        return 2
    single_ep = plan.episodes[0] if plan.episodes else None
    if plan.is_series or (plan.expands_series and single_ep is not None
                          and not single_ep.url):
        print(f"识别为多剧集：{plan.describe()}\n")
        return _download_series_cli(downloader, plan, outdir, args, config)

    # 展开后只选中 1 集：要下的是**那一集**，不是原始合集链接
    # （合集链接带 --no-playlist 会下成第 1 集）
    target_url = args.url
    target_title = args.title
    if plan.expands_series and single_ep is not None and single_ep.url:
        target_url = single_ep.url
        target_title = args.title or single_ep.title
        print(f"合集里只选中 1 集，实际下载：{snippet(target_url, 90)}\n")

    last = [-1.0]

    def on_progress(p):
        if p.percent - last[0] >= 2 or p.stage != "下载中":
            last[0] = p.percent
            speed = f"  {p.speed_human}" if p.speed_human else ""
            print(f"  [{p.stage}] {p.percent:5.1f}%  {human_size(p.downloaded)}"
                  f"{speed}  ETA {p.eta}", flush=True)

    started = time.time()
    result = downloader.download(target_url, job_dir_for(outdir, "cli"), kind=kind,
                                 title_hint=target_title, progress=on_progress,
                                 quality=getattr(args, "quality", "") or None)
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


def _download_series_cli(downloader: Downloader, plan, outdir: Path, args,
                         config: Config) -> int:
    """CLI 下的多剧集下载：逐集下到 标题/ 目录，最后按需打包。

    配置统一从传进来的 ``config`` 读（而不是重新 ``Config.load``）—— 后者会丢掉
    ``--range`` 这类命令行覆盖，导致 ``--mode`` 与 ``--range`` 行为不一致。
    """
    from xydl import series as series_mod

    safe_title = sanitize_filename(plan.title or "合集", maxlen=60) or "合集"
    target_dir = outdir / safe_title
    target_dir.mkdir(parents=True, exist_ok=True)
    total = plan.count
    files: list[Path] = []
    failures: list[str] = []

    for position, episode in enumerate(plan.episodes, 1):
        print(f"\n【{position}/{total}】{snippet(episode.title or episode.url, 70)}")
        episode_dir = target_dir / f".tmp-{position:02d}"

        def on_progress(p, _pos=position):
            print(f"  [{_pos}/{total}][{p.stage}] {p.percent:5.1f}%  "
                  f"{human_size(p.downloaded)}", flush=True)

        if episode.source == "wechat_channels" and not episode.url:
            result = downloader.download_channels_episode(
                episode.object_id, episode.export_id, episode_dir,
                title_hint=episode.hint(plan.title), progress=on_progress)
        else:
            result = downloader.download(episode.url, episode_dir, kind="page",
                                         title_hint=episode.hint(plan.title),
                                         progress=on_progress)
        if not result.ok:
            failures.append(f"第{position}集：{result.error}")
            print(f"  ❌ {result.error}")
            continue

        source = Path(result.file_path)
        episode_title = (series_mod.clean_episode_title(episode.title, plan.title)
                         or series_mod.clean_episode_title(result.title, plan.title)
                         or f"第{position}集")
        # 用原始集号命名（range=9,10 时要落成 09/10，而不是 01/02）
        number = episode.index if episode.index and episode.index > 0 else position
        dest = target_dir / series_mod.episode_filename(
            number, episode_title, source.suffix or ".mp4")
        try:
            source.replace(dest)
        except OSError:
            dest = source
        files.append(dest)
        print(f"  ✅ {dest.name}  {human_size(dest.stat().st_size)}")
        # 清掉这一集的临时目录（产物已经搬走了）
        try:
            episode_dir.rmdir()
        except OSError:
            pass

    print()
    if not files:
        print(f"❌ 全部失败：{'；'.join(failures[:3])}")
        return 1

    mode = (str(args.mode or "").strip().lower()
            or str(config.get("download.series.mode", "zip") or "zip").lower())
    if mode == "zip" and len(files) > 1:
        archive_path, error = series_mod.archive(files, outdir / f"{safe_title}.zip")
        if archive_path:
            print(f"📦 已打包  {archive_path}")
        else:
            print(f"⚠ 打包失败（{error}），文件都在 {target_dir}")

    print(f"✅ 完成 {len(files)}/{total} 集"
          + (f"；失败 {len(failures)} 集：{'；'.join(failures[:2])}" if failures else ""))
    print(f"   目录 {target_dir}")
    return 0 if not failures else 1


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
  │  1. 在刚打开的浏览器里**访问一次 {label}**（大多情况不用登录，  │
  │     刷开页面、能正常播放就够了；只有西瓜/微博这类才可能要登录）  │
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


def cmd_cdp_login(args) -> int:
    """CDP 登录抖音/TikTok：登录态存进工具自己的 profile，之后下载自动用它解析。"""
    from xydl.cdp_fetch import login, login_profile_dir

    platform = (args.site or "douyin").lower()
    if platform not in ("douyin", "tiktok"):
        print(f"不支持的站点 {platform!r}。可选：douyin / tiktok")
        return 2
    url = "https://www.douyin.com/" if platform == "douyin" else "https://www.tiktok.com/"
    print(BANNER.format(version=__version__))
    print(f"  目标站点   {platform}  {url}")
    print(f"  登录态会存到 {login_profile_dir(platform)}（工具自管，与日常浏览器隔离）\n")
    print("  会在弹出的 Edge 窗口里打开站点，请扫码登录，登录完成后关闭窗口。")
    print("  （不关窗口也行，这里只等 90 秒）\n")
    login(platform, url, wait_sec=90)
    print(f"  ✓ 完成。之后下载 {platform} 时，本工具就用这个登录态解析直链。")
    return 0


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
    # 视频号：yt-dlp 没有任何 weixin/channels/finder 提取器（实测确认），
    # 走我们自研的解析链；公众号同理，正文视频基本是腾讯源。
    "wechat_channels": "weixin",
    "wechat_mp": "weixin",
    "tencent_video": "vqq",
    "iqiyi": "iqiyi",
    "youku": "youku",
    "mgtv": "mgtv",
    "biliintl": "biliintl",
    # ── 国内社区视频 ────────────────────────────────────────────────
    "acfun": "acfun",
    # ── 海外社区视频 / 短视频 ──────────────────────────────────────
    "reddit": "reddit",
    "rumble": "rumble",
    "streamable": "streamable",
    "niconico": "niconico",
    "9gag": "9gag",
    "coub": "coub",
    # ── 音频 / 播客 / 音乐平台 ────────────────────────────────────
    "soundcloud": "soundcloud",
    "bandcamp": "bandcamp",
    "mixcloud": "mixcloud",
}


#: 实测**必须**带 cookie 才能解析的平台（关掉就会失败）
MEASURED_REQUIRE_COOKIE: frozenset[str] = frozenset({"xigua"})

#: 实测**带了反而会坏**的平台（cookie 与会话不匹配会被风控拒绝）
MEASURED_BREAK_WITH_COOKIE: frozenset[str] = frozenset(
    {"youtube", "tiktok", "twitter", "instagram", "facebook"}
)

#: 实测**带不带都行**的平台
MEASURED_EITHER_WAY: frozenset[str] = frozenset({"douyin", "xiaohongshu", "bilibili"})

#: yt-dlp 没有解析器、由自研逻辑兜底的平台
SELF_RESOLVED: frozenset[str] = frozenset({"kuaishou", "wechat_channels"})

#: 自研兜底平台的说明（列表里显示给运营看）
SELF_RESOLVED_NOTES: dict[str, str] = {
    "kuaishou": "自研 GraphQL 解析（快手反爬严，可能被滑块拦截）",
    "wechat_channels": "自研解析：元宝链路拿明文 MP4；本地抓包还能拿分辨率档位",
}


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
        supported = any(key in n for n in names) or rule.key in SELF_RESOLVED

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
            if rule.key in SELF_RESOLVED:
                cookie = "不带"
                mark, note = "✅", SELF_RESOLVED_NOTES.get(rule.key, "自研解析")
            elif rule.key in MEASURED_BREAK_WITH_COOKIE:
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
    print("   · 抖音 / 小红书 / 哔哩哔哩 带不带都行（抖音偶发风控时带游客 cookie 更稳）")
    print("   · 西瓜视频必须配 —— 不带 cookie 会下不了")
    print("   · YouTube 千万不能带 —— 带了会被风控拒绝，反而下不了")
    print("   配法：start.bat login douyin")
    print()
    print("  关于多剧集：")
    print("   · B站 的分P / 合集 / 番剧 / UP主投稿 会自动展开成 N 集，打包成 zip 交付")
    print("     想只下几集：config.json 里设 download.series.episode_range = \"1-10\"")
    print("     上限：download.series.max_episodes（默认 50）")
    print("   · 视频号没有公开的作品列表接口，默认只下当前这一集")
    print("     想整部下：把各集的元宝解析链接一起粘进来（一行一条）")
    print()
    print("  关于视频号（重点）：")
    print("   · 自研链路（不需要 key、不需要登录）：把视频号链接发给腾讯元宝，")
    print("     它回一条带 token 的 feed 链接，把那条约链接粘回本工具即可下明文 MP4")
    print("   · 「扫码登录网页版再用浏览器抓」实测无效（2026-10）：预览页只渲染")
    print("     封面图 + 二维码，网页版 feed 页匿名返回 10012、登录了也拿不到媒体请求。")
    print("     run.py channels-login 只作为诊断入口保留，默认不参与下载。")
    print("   · 视频号下发的前 128KB 是 ISAAC64 加密的，本项目会自动解密")
    print("     单独验一条：run.py wxinfo <分享链接>")
    print()
    print("  升级 yt-dlp（解析器失效时用）：")
    print("     .venv\\Scripts\\python.exe -m pip install -U yt-dlp")
    return 0


def cmd_baidu_login(args) -> int:
    """走一遍百度网盘 OAuth 授权，把 refresh_token 写进配置。

    为什么需要单独一条命令：百度网盘 API 是按「应用」授权的，跟你手机号无关。
    你得先在开放平台建个应用拿到 AppKey/SecretKey，再用自己的账号点一次
    「同意授权」，我们才能拿到长期凭据（refresh_token 有效期 10 年）。
    """
    from xydl.uploaders.baidu import BaiduPanUploader

    config = Config.load(args.config).apply_env_overrides()
    if args.app_key:
        config.set("delivery.baidu.app_key", args.app_key.strip())
    if args.secret_key:
        config.set("delivery.baidu.secret_key", args.secret_key.strip())
    uploader = BaiduPanUploader(config)

    print(BANNER.format(version=__version__))
    if not uploader.app_key or not uploader.secret_key:
        print("  还缺应用的 AppKey / SecretKey。\n")
        print("  1. 打开 https://pan.baidu.com/union ，用你的百度账号登录并完成实名认证")
        print("  2. 在控制台创建一个应用，拿到 AppKey 与 SecretKey")
        print("  3. 回来跑：")
        print("       run.py baidu-login --app-key <AppKey> --secret-key <SecretKey>")
        print(f"     （凭据会写到 {config.path}）")
        return 2

    url = uploader.authorize_url(qrcode=args.qrcode)
    print("  1. 用浏览器打开下面这个地址（登录的是你自己的百度账号）：\n")
    print(f"     {url}\n")
    print("  2. 点「同意授权」。页面会显示一串 code（授权码）")
    print("  3. 把 code 粘到下面按回车\n")
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        code = input("  授权码 code： ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消")
        return 130
    if not code:
        print("  没拿到 code，已取消")
        return 1

    try:
        payload = uploader.exchange_code(code)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ❌ 换取 token 失败：{exc}")
        return 1

    config.set("delivery.baidu.refresh_token", payload.get("refresh_token", ""))
    config.set("delivery.uploader", "baidu")
    path = config.save()
    print(f"\n  ✓ 授权成功，凭据已写入 {path}")
    print(f"    refresh_token 有效期约 10 年，access_token 会自动续期")

    try:
        info = uploader.user_info()
        print(f"\n  账号   {info.get('netdisk_name') or info.get('baidu_name')}")
        print(f"  会员   {info.get('vip_type')}（0 普通 / 1 普通会员 / 2 超级会员）")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ⚠ 拿用户信息失败（不影响上传）：{exc}")

    print("\n  下一步：")
    print("    run.py upload <一个本地文件>    ← 实测上传 + 生成分享链接")
    if config.get("delivery.baidu.share_api", "old") == "old":
        print("    如果分享链接那一步失败，多半是应用没有分享权限：")
        print("      · 新版分享服务需要企业开发者认证 + 购买分享服务")
        print("      · 过渡方案：把 delivery.mode 设成 upload_only（只上传不发链接）")
    return 0


def cmd_upload(args) -> int:
    """把本地文件传到网盘，打印可分享链接 —— 用来单独验证交付通道。"""
    from pathlib import Path as _Path

    from xydl.uploaders import build_uploader

    config = Config.load(args.config).apply_env_overrides()
    src = _Path(args.path)
    if not src.is_file():
        print(f"❌ 找不到文件：{src}")
        return 1

    uploader = build_uploader(config)
    print(BANNER.format(version=__version__))
    print(f"  上传通道   {uploader.describe()}")
    print(f"  文件       {src}  ({human_size(src.stat().st_size)})")
    print(f"  模式       {config.get('delivery.mode')}\n")
    print("  正在上传…")

    started = time.time()
    result = uploader.upload(src, remote_name=args.name or "")
    elapsed = time.time() - started

    if not result.ok:
        print(f"\n  ❌ 上传失败：{result.error}")
        if "分享" in result.error:
            print("\n  文件其实已经传上去了，只是没能生成分享链接。")
            print("  可以把 delivery.mode 改成 upload_only 先跑通全流程。")
        return 1

    print(f"\n  ✅ 完成（{elapsed:.1f}s）")
    print(f"  云端路径   {result.remote_path}")
    print(f"  fs_id      {result.fs_id}")
    print(f"  链接       {result.url}")
    if result.pwd:
        print(f"  提取码     {result.pwd}")
    print("\n  发给买家的样子：")
    for line in result.reply_line(str(config.get("delivery.link_template", "") or "")).splitlines():
        print(f"    {line}")

    # ── 顺手验一下这个链接真的能用、以及桶有没有敞着 ──────────────
    # 交付的最后一环是「买家点开链接能下到东西」，这一步在本地就能验完，
    # 别等真发出去才发现 403。
    from xydl import http as http_util

    print("\n  实测链接有效性…")
    probe = http_util.request("GET", result.url, timeout=30, proxy="",
                              max_bytes=16)
    if probe.status == 200:
        print("  ✓ 链接可下载（只取了前 16 字节，没整份拉下来）")
    elif probe.status in (403, 401):
        print(f"  ❌ 链接被拒：HTTP {probe.status}")
        print(f"     {probe.text[:200]}")
        print("     常见原因：桶是私有的但签名/时钟不对；或对象 ACL 覆盖了签名")
        return 1
    else:
        print(f"  ⚠ 链接返回 HTTP {probe.status}：{probe.text[:160]}")

    if "X-Amz-Signature=" in result.url:
        bare = result.url.split("?")[0]
        unsigned = http_util.request("GET", bare, timeout=15, proxy="",
                                     max_bytes=16)
        if unsigned.status == 200:
            print("\n  ⚠⚠ 这个桶是「公有读」的 —— 不带签名的裸链接也能下载。")
            print("      意思是预签名那层保护形同虚设，任何人猜到路径就能拿文件，")
            print("      而且公开桶容易被当外链滥用（违反服务商条款、可能被限流）。")
            print("      改成私有读写即可：")
            print("        COS 控制台 → 存储桶 → 权限管理 → 访问权限 → 私有读写")
            print("        OSS 控制台 → Bucket → 读写权限 → 私有")
            print("      改完预签名链接照常可用，裸链接会变成 403。")
        elif unsigned.status in (403, 401):
            print("\n  ✓ 桶是私有的，裸链接被拒（403）—— 预签名保护正常生效")
    return 0


def cmd_bridge_ping(args) -> int:
    """探一下上游「闲鱼超级管家」在不在。"""
    from xydl.channels.xianyu_bridge import XianyuBridgeChannel

    config = Config.load(args.config).apply_env_overrides()
    config.ensure_dirs()
    store = Store(config.path_of("data_dir") / "xydl.sqlite3")
    pipeline = Pipeline(config, store)
    channel = XianyuBridgeChannel(pipeline, config)

    print(BANNER.format(version=__version__))
    print(f"  上游地址   {channel.upstream}{channel.deliver_path}")
    print(f"  回传来源   {'、'.join(sorted(channel.source_channels))}")
    print(f"  上传通道   {channel.uploader.describe()}")
    print(f"  交付模式   {channel.mode}\n")

    ok, message = channel.ping()
    store.close()
    if ok:
        print(f"  ✓ {message}")
        print("\n  上游在线。记得在 config.json 里打开 channels.xianyu_bridge.enabled")
        return 0
    print(f"  ✗ {message}")
    print("\n  常见原因：")
    print("   · 上游还没启动（python Start.py，默认 8080 端口）")
    print("   · 端口/地址不对 → 改 delivery.bridge.upstream_url")
    print("   · 上游装在另一台机器 → 填对局域网 IP，并确认防火墙放行")
    return 1


def cmd_wxinfo(args) -> int:
    """只看视频号链接能解析到什么（不下载）。

    视频号的解析链有好几级，失败原因也很不一样（没配 key / 没登录 / 内容不可播），
    所以给一条命令把**每一级的结果**都摊开，省得在流水线日志里猜。
    """
    from xydl import wechat_channels as wc
    from xydl.cdp_fetch import login_profile_dir

    config = Config.load(args.config).apply_env_overrides()
    print(BANNER.format(version=__version__))

    url = args.url.strip()
    if not wc.is_channels_url(url):
        print(f"  ✗ 这不是视频号链接：{snippet(url, 80)}")
        print("    支持：https://weixin.qq.com/sph/<码>")
        print("          https://channels.weixin.qq.com/finder-preview/pages/sph?id=<码>")
        print("          https://channels.weixin.qq.com/web/pages/feed?eid=<exportId>")
        return 2

    ref = wc.parse_ref(url)
    print(f"  短码        {ref.short_uri or '（无）'}")
    print(f"  exportId    {ref.export_id or '（无）'}")
    print(f"  channel_id  {ref.channel_id or '（无短码，只能走 exportId/objectId）'}")

    print("\n① 匿名预览接口（不需要登录）")
    feed = wc.fetch_feed_info(ref, config)
    if feed.ok:
        print(f"  ✓ 标题：{feed.title or '（无）'}")
        print(f"    作者：{feed.author or '（无）'}    点赞：{feed.like_count or '-'}")
        print(f"    封面：{snippet(feed.cover_url, 70)}")
        print(f"    exportId：{feed.export_id}")
        if feed.error:
            print(f"    ⚠ {feed.error}")
    else:
        print(f"  ✗ {feed.error}")

    print("\n② 本地抓包（自研兜底：微信里播一次就能截到直链 + decode_key）")
    from xydl import mitm as _mitm
    from xydl import wxproxy as _wxproxy

    store = _wxproxy.CaptureStore(path=_wxproxy.capture_store_path(config))
    print(f"  证书        {'已装' if _mitm.ca_installed() else '未装 → run.py channels-capture'}")
    print(f"  已抓条目    {len(store.entries())} 条")
    local = wc.resolve_media(url, config, feed=feed, allow_browser=False)
    if local.ok:
        print(f"  ✓ 直链：{snippet(local.url, 90)}")
        print(f"    decode_key：{local.decode_key or '（无）'}   来源：{local.via}")
        # 可选档位都列出来 —— 用户要的是"分辨率可选"，那就得先能看见有什么
        if local.feed is not None and local.feed.raw:
            cands = wc.feed_media_candidates(local.feed.raw.get("feedInfo") or {})
            if cands:
                print("    可选档位    " + "、".join(
                    c["label"] + (f"({c['height']}p)" if c["height"] else "")
                    for c in cands))
                if local.quality:
                    print(f"    实际选中    {local.quality}"
                          f"（偏好 {config.get('download.quality') or 'compatible'}）")
    else:
        for attempt in local.attempts:
            print(f"  · {attempt}")
        if local.error:
            # 这句才是运营真正需要的操作指引（短链要换成"复制链接"那种）
            print(f"  → {snippet(local.error, 300)}")

    profile = login_profile_dir("wechat_channels")
    print("\n③ 登录态浏览器抓取")
    print(f"  profile 目录  {profile}")
    if getattr(args, "no_browser", False):
        print("  （--no-browser：跳过实际抓取，只看 profile）")
    elif not bool(config.get("download.wechat_channels.use_browser", False)):
        print("  （已跳过：download.wechat_channels.use_browser 默认关 —— "
              "实测浏览器抓取对视频号无效）")
    else:
        # 真的去抓一次 —— 这一步才是"通道 ③ 能不能用"的唯一硬证据。
        # 只报 profile 状态是自欺欺人：cookie 库存在 ≠ 能抓到直链。
        wait = float(config.get("download.wechat_channels.browser_wait_sec", 25) or 25)
        ref2 = wc.parse_ref(url)
        if feed.export_id:
            ref2 = wc.ChannelsRef(short_uri=ref2.short_uri, export_id=feed.export_id,
                                  object_id=ref2.object_id, original_url=url)
        print(f"  正在打开登录态浏览器抓取（最多 {wait:.0f} 秒）…")
        capture = wc.capture_via_browser(
            ref2, config, wait_sec=wait, log=lambda m: print(f"    · {m}"))
        if capture.video_url:
            print(f"  ✓ 抓到直链：{snippet(capture.video_url, 96)}")
            print(f"    decode_key：{capture.decode_key or '（无，可能不需要解密）'}")
            print("    → 通道 ③ 可用，直接 `run.py download <链接>` 即可")
        else:
            print(f"  ✗ {capture.error}")
            print("    → 抓包没拿到：先在微信里播一次这个视频再试，")
            print("      或把元宝解析出的带 token 链接粘回本工具")
        print(f"  观察到的相关请求 {len(capture.visited)} 个"
              + ("：" if capture.visited else ""))
        for u in capture.visited[:6]:
            print(f"    - {snippet(u, 100)}")

    print("\n④ 解密能力自检（离线）")
    from xydl import wxdecrypt

    ok_ks = wxdecrypt.generate_keystream("2136343393", 16).hex()
    mp4_head = wxdecrypt.looks_like_media(b"\x00\x00\x00\x20ftypisom")
    print(f"  密钥流样例    {ok_ks}")
    print(f"  MP4 头识别    {'✓ 正常' if mp4_head else '✗ 异常'}")
    print("  自动解密      "
          f"{'开' if config.get('download.wechat_channels.auto_decrypt', True) else '关（不推荐）'}")
    return 0 if feed.ok else 1


def cmd_channels_login(args) -> int:
    """登录视频号助手 / 腾讯元宝，把登录态存进工具自管的 Edge profile。

    ``--yuanbao`` 是**视频号下载的入口**：元宝能把 ``/sph/xxx`` 短链解析成带 token 的
    ``playable_url``，那是唯一能拿到明文 MP4 的路线。元宝的鉴权在页面上下文里
    （cookie 里只有分析类），所以必须在页内 fetch（见 :mod:`xydl.yuanbao`），
    也就必须有一个登录过元宝的浏览器 profile。
    """
    from xydl import wechat_channels as wc
    from xydl.cdp_fetch import login_profile_dir

    if getattr(args, "yuanbao", False):
        # 视频号下载的入口：元宝能把 /sph/xxx 短链换成带 token 的 playable_url
        from xydl import yuanbao

        print(BANNER.format(version=__version__))
        print("  登录目标      腾讯元宝（视频号短链解析入口）")
        print("  说明          用工具自管的 Edge profile，和你日常浏览器完全隔离")
        print("  为什么要登录  元宝的鉴权在页面上下文里（cookie 里只有分析类），"
              "必须在页内 fetch")
        if args.check:
            ok, msg = yuanbao.check_login()
            print(("  ✅ " if ok else "  ✗ ") + msg)
            return 0 if ok else 1
        ok, msg = yuanbao.login(log=lambda m: print(f"  · {m}"),
                                wait_sec=float(args.wait))
        print(("  ✅ " if ok else "  ✗ ") + msg)
        return 0 if ok else 1

    profile = login_profile_dir("wechat_channels")
    print(BANNER.format(version=__version__))
    print(f"  登录态目录   {profile}")
    print("  说明         用的是工具自管的 Edge profile，和你日常浏览器完全隔离")
    # 说清楚这条登录通道的定位：日常下载走元宝链路，根本不需要登录。
    print("  提示         本条登录只用于诊断；日常下载走元宝链路，不需要登录")

    if args.check:
        hits = [p for p in profile.rglob("Cookies") if p.is_file()]
        if not hits:
            print("\n  ✗ 还没有登录过 → 跑 `run.py channels-login`")
            return 1
        size = sum(p.stat().st_size for p in hits)
        print(f"\n  ✓ 找到 {len(hits)} 个 cookie 库（共 {human_size(size)}）")
        print("    真正是否有效，跑 `run.py wxinfo <分享链接>` 看第 ③ 步")
        return 0

    if args.open:
        url = args.url or wc.WEB_HOME_PAGE
        import webbrowser
        print(f"\n  用默认浏览器打开（**不带工具 profile**，仅用于看清登录入口）：{url}")
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  打不开浏览器：{exc}")
        return 0

    print("\n" + "─" * 64)
    print(f"  1. 马上会弹出一个 Edge 窗口，打开 {wc.WEB_HOME_PAGE}")
    print("  2. 用**手机微信扫码**登录（登录的是你自己的微信号，凭据只留在本机）")
    print("  3. 登录成功后页面会进到视频号助手；看到就说明成了")
    print("  4. 关掉窗口后回来，本命令会自己结束")
    print("─" * 64 + "\n")

    from xydl.cdp_fetch import login

    login("wechat_channels", args.url or wc.WEB_HOME_PAGE,
          wait_sec=float(args.wait))
    print(f"\n  ✓ 登录流程结束，登录态在 {profile}")
    print("    验证：run.py wxinfo <视频号分享链接>")
    return 0


def cmd_series(args) -> int:
    """看一眼「这条链接会下几集」，不下载。

    接单前先跑它，能避免两种事故：把 UP 主的 800 个投稿全下了、
    或者以为是一集结果客户要的是整部剧。
    """
    from xydl import series as series_mod

    config = Config.load(args.config).apply_env_overrides()
    if args.max_episodes:
        config.set("download.series.max_episodes", int(args.max_episodes))
    if args.range:
        config.set("download.series.episode_range", args.range)

    print(BANNER.format(version=__version__))
    downloader = Downloader(config)
    url = args.url.strip()
    print(f"  链接         {url}")
    print(f"  多剧集展开   {'开' if config.get('download.series.enabled', True) else '关'}")
    print(f"  上限/区间    {config.get('download.series.max_episodes')} 集"
          f"  range={config.get('download.series.episode_range')!r}")
    print()

    plan = series_mod.plan(url, config=config, engine=downloader.ytdlp,
                           title_hint=args.title or "")
    # 配置层面的错误必须**明确报出来**。之前这里把 plan.error 吞了，
    # 结果"上限 2 + 指定第 5 集"会显示成"单集：<合集页URL>"，看着像正常结论。
    if plan.error:
        print("  结论         ✗ 配置有误，已停止")
        print(f"  原因         {plan.error}")
        return 2
    print(f"  结论         {plan.describe()}")
    if plan.note:
        print(f"  备注         {plan.note}")
    print()
    for episode in plan.episodes:
        label = episode.title or "（标题待下载时探测）"
        target = episode.url or f"object_id={episode.object_id}"
        print(f"  {episode.index:>3}. [{episode.source}] {snippet(label, 40)}")
        print(f"       {snippet(target, 96)}")
    if plan.truncated:
        print("\n  ⚠ 已达 download.series.max_episodes 上限，实际集数可能更多")
    return 0 if plan.episodes else 1


def cmd_channels_capture(args) -> int:
    """视频号本地抓包服务：选择性 MITM + 链式上游代理 + 系统代理可还原。

    用户视角应该是"静默"的：装当前用户证书（不需要管理员）、起代理、把系统代理
    指过来（原有 Clash 之类的代理作为上游保留），**退出时全部还原**。

    唯一需要人做的事：在**电脑端微信**里把那个视频播一下 —— 这是视频号的设计
    决定的（浏览器/网页版拿不到媒体流），任何本地方案都一样。
    """
    import signal

    from xydl import mitm, wxproxy

    config = Config.load(args.config).apply_env_overrides()

    if args.uninstall:
        ok, msg = mitm.uninstall_ca()
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1

    # 上次被强杀可能把系统代理留在这儿（finally 跑不到）。启动先自检并还原，
    # 否则用户的状态是"一跑这工具就没网"，而且他没法自己判断原因。
    state_file = config.path_of("data_dir") / "proxy_state.json"
    leftover = mitm.SystemProxy(state_file)
    if mitm.SystemProxy.leaked(int(args.port)):
        print(f"  ⚠ 检测到系统代理还指着 127.0.0.1:{args.port}（上次异常退出）")
        print(f"  {leftover.restore()}")
        if args.restore:
            return 0

    if args.restore:
        print(f"  {leftover.restore()}")
        return 0

    root = config.path_of("data_dir") / "ca"
    ca = mitm.CertAuthority(root)
    ok, msg = ca.ensure()
    print(f"  证书目录   {root}")
    print(f"  {('✅' if ok else '❌')} {msg}")
    if not ok:
        return 1

    installed = mitm.ca_installed()
    if args.status:
        print(f"  证书已装    {'是' if installed else '否'}")
        print(f"  {mitm.SystemProxy().describe()}")
        store = wxproxy.CaptureStore(path=wxproxy.capture_store_path(config))
        entries = store.entries()
        print(f"  已抓到的视频 {len(entries)} 条"
              + (f"（最近：{entries[-1].get('title') or entries[-1]['url'][:50]}）"
                 if entries else ""))
        return 0

    if not installed:
        if args.no_install_ca:
            print("  ⚠ 没有装证书（--no-install-ca）：目标域名抓不到内容，只能透传")
        else:
            ok, msg = mitm.install_ca(ca.ca_crt)
            print(f"  {('✅' if ok else '❌')} {msg}")
            if not ok:
                return 1

    upstream = str(args.upstream or "").strip()
    if not upstream:
        # 用户原本的系统代理（Clash 之类）留作上游：非目标流量照旧走它
        current = mitm.SystemProxy()._read()
        candidate = current.server.split(";")[0].strip()
        if candidate and "127.0.0.1:" + str(args.port) not in candidate:
            upstream = candidate
    if upstream and not upstream.startswith("http"):
        upstream = "http://" + upstream

    store = wxproxy.CaptureStore(path=wxproxy.capture_store_path(config))
    proxy = wxproxy.ChannelsProxy(
        store, host="127.0.0.1", port=int(args.port), ca=ca,
        upstream=upstream, log=lambda m: print(f"  · {m}", flush=True))
    proxy.start()
    if not mitm.wait_for_port("127.0.0.1", proxy.port):
        print("  ❌ 代理端口没起来")
        return 1

    sysproxy = mitm.SystemProxy(state_file)
    if args.no_system_proxy:
        print(f"  （--no-system-proxy：请自行把 *.qq.com 的流量转到 "
              f"127.0.0.1:{proxy.port}，例如 Clash 的 Global Extend Script）")
    else:
        sysproxy.snapshot()
        sysproxy.set_to("127.0.0.1", proxy.port)
        print(f"  ✅ 系统代理已指向 127.0.0.1:{proxy.port}"
              + (f"（上游 {upstream}）" if upstream else ""))

    print()
    print("=" * 66)
    # 必须显式说清注入开没开：它会把代码塞进页面自己的 JS bundle，
    # 虽然是最小侵入（只包一个页面 API、不碰 CSP/HTML/全局），也不该默认开。
    print(f"  页面注入    {'开（只在视频号页面生效）' if proxy.inject else '关'}"
          + ("" if proxy.inject else "   —— 需要它就设 XYDL_CAPTURE_INJECT=1"))
    # 日志**同时写文件**：stdout 被刷屏时会丢关键行（实测：注入回报的行被埋掉，
    # 排查时只能看到"新增 0 条"这种没有信息量的行）。写文件后可以直接搜。
    try:
        log_path = ROOT / "data" / "capture_log.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8")
        original_log = proxy.log

        def tee(message: str) -> None:
            try:
                handle.write(message + "\n")
                handle.flush()
            except OSError:
                pass
            original_log(message)

        proxy.log = tee  # type: ignore[method-assign]
        print(f"  日志文件    {log_path}")
    except OSError as exc:
        print(f"  （日志文件没建起来：{exc}，只打屏）")
    print("  现在去**电脑端微信**里打开那个视频号视频并播放一下。")
    print("  抓到之后，`run.py download <视频号分享链接>` 就会自动用它。")
    print("  按 Ctrl+C 结束（会先还原系统代理）。")
    print("=" * 66)

    stopping = threading.Event()
    started_at = time.time()

    def _stop(signum, frame):                 # noqa: ARG001
        stopping.set()

    try:
        signal.signal(signal.SIGINT, _stop)
    except (ValueError, OSError):
        pass
    try:
        while not stopping.is_set():
            time.sleep(0.5)
            if args.once and store.latest():
                print("  ✓ 已经抓到带密钥的结果，退出")
                break
            if args.timeout and time.time() - started_at > args.timeout:
                print("  到时间了，退出")
                break
    finally:
        proxy.stop()
        if not args.no_system_proxy:
            print(f"  {sysproxy.restore()}")
    print(f"  本次统计：{proxy.stats}")
    return 0


def cmd_decrypt(args) -> int:
    """解密视频号加密视频（本地文件，就地解密）。

    为什么需要这条命令：本地抓包工具（wx_channels_download / mitmproxy / 各种
    "视频号下载器"）拿到的文件，**前 128 KiB 是 ISAAC64 加密的** —— 直接播放是
    花屏，也不该直接交给客户。把文件和解密密钥喂给这条命令就能变成正常 MP4。

    只在校验通过时才覆盖原文件（box 链自洽），解错就原样保留并报错。
    """
    from xydl import wxdecrypt

    path = Path(args.path)
    if not path.exists():
        print(f"❌ 文件不存在：{path}")
        return 2
    size = path.stat().st_size
    print(f"  文件   {path}")
    print(f"  大小   {human_size(size)}")

    if not wxdecrypt.needs_decrypt(path):
        print("  ✓ 这个文件**不需要解密**（文件头已经是正常的 MP4/媒体）")
        return 0

    key = str(args.decode_key or "").strip()
    if not key:
        print("  ✗ 这个文件看起来是视频号加密格式，但没给密钥")
        print("    需要该直链**同一次响应**里的 decode_key（每次请求都会变），例如：")
        print(f"      run.py decrypt \"{path}\" --decode-key 2136343393")
        return 2

    ok, message = wxdecrypt.decrypt_file(path, key)
    if not ok:
        print(f"  ❌ 解密失败：{message}")
        print("    常见原因：decode_key 与这个文件不是同一次请求配对（key 每次都会变）")
        return 1
    print(f"  ✅ 解密成功：{message}")
    print(f"  现在可以正常播放/交付：{path}")
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
    for name in ("console", "inbox", "xianyu_bridge"):
        enabled = config.get(f"channels.{name}.enabled", False)
        extra = ""
        if name == "console":
            extra = f"  http://{config.get('server.host')}:{config.get('server.port')}"
        elif name == "xianyu_bridge":
            extra = f"  → {config.get('delivery.bridge.upstream_url')}"
        print(f"  {'✓' if enabled else '✗'} {name}{extra if enabled else ''}")

    print("\n[微信视频号]")
    from xydl.cdp_fetch import login_profile_dir

    print("  平台支持     自研链路：腾讯元宝（token feed）→ 明文 MP4；"
          "本地抓包可拿分辨率档位")
    # 用和 channels-login / 抓取完全同一个函数算路径 —— 三处各写一套的话，
    # 打包成 exe 后 ROOT 与 app_dir() 不一致，doctor 会误报"没登录过"
    profile = login_profile_dir("wechat_channels")
    logged = any(p.is_file() for p in profile.rglob("Cookies")) if profile.exists() else False
    print("  ① 元宝链路    ✓ 可用（把带 token 的 feed 链接粘回来即可下）")
    print(f"  ② 浏览器登录  {'✓ 已有登录态' if logged else '✗ 没登录过（仅诊断用，不影响下载）'}")
    print(f"                 profile: {profile}")
    print(f"  ③ 自动解密    "
          f"{'✓ 开（视频号前 128KB 必须解密）' if config.get('download.wechat_channels.auto_decrypt', True) else '✗ 关（会交付打不开的文件）'}")
    from xydl import wxdecrypt

    sample = wxdecrypt.generate_keystream(wxdecrypt.SELFTEST_DECODE_KEY, 8).hex()
    print(f"     解密引擎   黄金向量自检 {sample}（应等于 "
          f"{wxdecrypt.SELFTEST_KEYSTREAM16_HEX[:16]}）")
    if sample != wxdecrypt.SELFTEST_KEYSTREAM16_HEX[:16]:
        print("                 ✗ 与真实样本不符 —— 解密算法被改坏了，先修它再下视频号")
    if not key and not logged:
        print("  ⚠ 两条通道都没通 —— 视频号这单只能下到标题，下不到画面")

    # 交付通道：闲鱼发不了视频文件，这一步决定了「怎么把视频交给买家」
    print("\n[交付（网盘 / 对象存储）]")
    from xydl.uploaders import build_uploader

    uploader = build_uploader(config)
    name = str(config.get("delivery.uploader", "none"))
    print(f"  上传通道   {uploader.describe()}")
    print(f"  交付模式   {config.get('delivery.mode')}")
    if name == "baidu":
        token_path = config.path_of("data_dir") / "baidu_token.json"
        has_refresh = bool(str(config.get("delivery.baidu.refresh_token", "") or "").strip())
        if token_path.exists():
            print(f"  access_token  {token_path}  ✓ 已有缓存")
        print(f"  授权状态   {'✓ refresh_token 已配置' if has_refresh else '✗ 未授权 → run.py baidu-login'}")
        print(f"  分享接口   {config.get('delivery.baidu.share_api')}"
              "（新版分享需企业认证+付费，失败可改 delivery.mode=upload_only）")
    elif name == "s3":
        missing = [k for k in ("endpoint", "bucket", "access_key_id", "secret_access_key")
                   if not str(config.get(f"delivery.s3.{k}", "") or "").strip()]
        if missing:
            print(f"  ✗ 还缺配置：delivery.s3.{' / delivery.s3.'.join(missing)}")
        else:
            print("  ✓ 配置齐全（跑 run.py upload <文件> 实测一次）")
        print(f"  链接有效期 {int(config.get('delivery.s3.url_expires_sec', 604800)) // 86400} 天"
              f"（预签名直链，过期自动失效）")
    else:
        print("  ✗ 没配上传通道 —— 交付只会发文案，不会带链接")

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
    print("\n[1/6] 链接识别")
    cases = [
        ("7.92 复制打开抖音 https://v.douyin.com/iRNBho6/",
         "douyin", "short", "这个视频太搞笑了"),
        ("http://xhslink.com/a/xxxxx 打开小红书App查看", "xiaohongshu", "short", ""),
        ("https://www.bilibili.com/video/BV1xx411c7mD",
         "bilibili", "page", ""),
        ("https://v.qq.com/x/cover/abc.html", "tencent_video", "drm", ""),
        # 视频号：/sph/ 短链必须被认成"需要展开的短链"，展开后落到 channels 域名
        ("https://weixin.qq.com/sph/AJizVCRhAt",
         "wechat_channels", "short", ""),
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
    print("\n[2/6] 直链下载（本地 HTTP 服务器）")
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
    print("\n[3/6] 回话模板")
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
    print("\n[4/6] 订单存储")
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

    # ── 5. 视频号解析 + 解密（离线）──────────────────────────────────
    print("\n[5/6] 视频号解析 / 解密（离线）")
    from xydl import wechat_channels as wc
    from xydl import wxdecrypt
    from xydl import series as series_mod

    ref = wc.parse_ref("https://weixin.qq.com/sph/AJizVCRhAt")
    check("分享短码解析", ref.short_uri == "AJizVCRhAt" and ref.channel_id == "sphAJizVCRhAt",
          f"→ {ref.short_uri}")
    check("预览页识别", wc.is_channels_url(
        "https://channels.weixin.qq.com/finder-preview/pages/sph?id=x")
        and not wc.is_channels_url("https://mp.weixin.qq.com/s/abc"))
    check("剧集编号识别",
          wc.series_episode_number("出狱后，我举世无双-第1集") == 1
          and wc.series_prefix("某某短剧 EP3") == "某某短剧")

    # 解密用真实样本的黄金向量（前 16 字节密钥流），能立刻发现算法被改坏
    golden = wxdecrypt.generate_keystream(
        wxdecrypt.SELFTEST_DECODE_KEY, 16).hex()
    check("视频号解密引擎", golden == wxdecrypt.SELFTEST_KEYSTREAM16_HEX, golden)
    check("MP4 头识别",
          wxdecrypt.looks_like_media(b"\x00\x00\x00\x20ftypisom")
          and not wxdecrypt.looks_like_media(b"\x23\x76\x6a\x16"))

    check("多剧集区间解析", series_mod.parse_range("1-3,5", 10) == [1, 2, 3, 5])
    plan = series_mod.plan("https://www.douyin.com/video/1", config=config)
    check("非多剧集链接不误判", not plan.is_series)

    # ── 6. 短链展开（需要外网）─────────────────────────────────────
    print("\n[6/6] 短链展开（需要外网，失败不算致命）")
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
    p.add_argument("--single", action="store_true",
                   help="只下这一集，不做多剧集展开")
    p.add_argument("--range", default="", help="只下指定集，如 1-10,15")
    p.add_argument("--quality", default="", choices=["", "best", "1080", "720", "480", "360"],
                   help="清晰度：best 不限 / 1080 / 720 / 480 / 360（本站点没有的档会自动降级）")
    p.add_argument("--watermark", default="",
                   choices=["", "auto", "off", "delogo", "any"],
                   help="水印处理：auto 优先无水印（默认）/ off 只要无水印 / "
                        "delogo 用 ffmpeg 擦掉（有损）/ any 不挑")
    p.add_argument("--mode", default="", choices=["", "zip", "separate"],
                   help="多剧集交付方式：zip 打包 / separate 多个文件")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("delogo",
                       help="给已下载的文件擦水印（ffmpeg delogo，有损；只处理成片）")
    p.add_argument("path", help="要处理的视频文件")
    p.add_argument("--platform", default="",
                   help="按平台预设定位水印（douyin/tiktok/kuaishou/wechat_channels/"
                        "xiaohongshu），水印都在右下角")
    p.add_argument("--rect", default="",
                   help="手动指定水印区域 x:y:w:h（比预设准；推荐先用 --dry-run 看）")
    p.add_argument("-o", "--out", help="输出文件（默认是源文件同目录的 xxx.nowm.mp4）")
    p.add_argument("--in-place", action="store_true",
                   help="直接替换源文件（默认另存，避免有损处理后无法回退）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将要用的 delogo 矩形与滤镜，不真的转码")
    p.set_defaults(func=cmd_delogo)

    p = sub.add_parser("series", help="看一条链接会下几集（不下载）")
    p.add_argument("url")
    p.add_argument("-t", "--title", default="", help="标题提示")
    p.add_argument("-n", "--max-episodes", type=int, default=0,
                   help="临时改上限（不写回配置）")
    p.add_argument("-r", "--range", default="", help="临时改集数区间，如 1-10")
    p.set_defaults(func=cmd_series)

    p = sub.add_parser("wxinfo", help="看视频号链接能解析到什么（不下载）")
    p.add_argument("url", help="视频号分享链接")
    p.add_argument("--no-browser", action="store_true",
                   help="跳过第 ③ 步的登录态浏览器抓取（不弹窗、更快）")
    p.set_defaults(func=cmd_wxinfo)

    p = sub.add_parser("channels-login",
                       help="登录元宝（视频号解析入口）/ 视频号助手")
    p.add_argument("--yuanbao", action="store_true",
                   help="登录腾讯元宝（视频号短链解析的入口，只需一次）")
    p.add_argument("--check", action="store_true", help="只检查登录态，不启动浏览器")
    p.add_argument("--open", action="store_true", help="用默认浏览器打开登录页（不写登录态）")
    p.add_argument("--url", default="", help="改用这个地址登录")
    p.add_argument("--wait", type=float, default=180, help="等登录的秒数（默认 180）")
    p.set_defaults(func=cmd_channels_login)

    p = sub.add_parser("channels-capture",
                       help="视频号本地抓包服务（选择性 MITM；微信 PC 里播一次即可）")
    p.add_argument("--port", type=int, default=2023, help="本地代理端口（默认 2023）")
    p.add_argument("--upstream", default="",
                   help="链式上游代理；留空自动沿用你当前系统代理")
    p.add_argument("--status", action="store_true", help="只看状态（证书/代理/已抓条目）")
    p.add_argument("--uninstall", action="store_true", help="卸载证书后退出")
    p.add_argument("--no-system-proxy", action="store_true",
                   help="不改系统代理（自行把 qq.com 流量转到这里）")
    p.add_argument("--restore", action="store_true",
                   help="只把系统代理还原成上次记录的值（被强杀后救网用）")
    p.add_argument("--no-install-ca", action="store_true", help="不装证书（只透传）")
    p.add_argument("--once", action="store_true", help="抓到带密钥的结果就退出")
    p.add_argument("--timeout", type=float, default=0, help="最多跑多少秒")
    p.set_defaults(func=cmd_channels_capture)

    p = sub.add_parser("decrypt",
                       help="解密视频号加密视频（本地文件；本地抓包工具抓下来的那种）")
    p.add_argument("path", help="要解密的文件")
    p.add_argument("-k", "--decode-key", default="",
                   help="该直链同一次响应里的 decode_key（十进制字符串）")
    p.set_defaults(func=cmd_decrypt)

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

    p = sub.add_parser("cdp-login", help="CDP 登录抖音/TikTok（登录态，之后解析自动用它）")
    p.add_argument("site", nargs="?", default="douyin", help="douyin / tiktok")
    p.set_defaults(func=cmd_cdp_login)

    p = sub.add_parser("cookies", help="查看各浏览器里有没有目标站点的 cookie")
    p.add_argument("site", nargs="?", help="只查某一个站点")
    p.set_defaults(func=cmd_cookies)

    p = sub.add_parser("platforms", help="列出各平台支持情况（接单前先查）")
    p.set_defaults(func=cmd_platforms)

    p = sub.add_parser("baidu-login", help="授权百度网盘（写入 refresh_token）")
    p.add_argument("--app-key", help="开放平台应用的 AppKey")
    p.add_argument("--secret-key", help="开放平台应用的 SecretKey")
    p.add_argument("--qrcode", action="store_true", help="生成扫码授权页而不是普通授权页")
    p.add_argument("--open", action="store_true", help="顺便用默认浏览器打开授权页")
    p.set_defaults(func=cmd_baidu_login)

    p = sub.add_parser("upload", help="把本地文件传上网盘，打印分享链接")
    p.add_argument("path", help="要上传的文件")
    p.add_argument("-n", "--name", default="", help="云端文件名（默认沿用原名）")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("bridge-ping", help="检查上游「闲鱼超级管家」是否在线")
    p.set_defaults(func=cmd_bridge_ping)

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
