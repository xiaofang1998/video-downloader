"""配置：默认值 + config.json 深合并 + 点号路径读取。

刻意用 JSON 而不是 YAML —— 标准库就能解析，少一个依赖，少一个踩坑点。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

#: 开发时的项目根目录（本文件在 <root>/xydl/config.py）。
ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的包里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """**只读资源**（webui/、内置模板等）所在目录。

    PyInstaller 会把随包资源解到 ``sys._MEIPASS``（onefile 是临时目录，
    onedir 就是 exe 旁边）。开发时就是项目根目录。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return ROOT


def app_dir() -> Path:
    """**可写数据**（data / downloads / inbox / logs / config.json）的基准目录。

    打包后必须落在 **exe 旁边**，绝不能放 ``_MEIPASS`` —— onefile 模式下那是
    临时目录，进程一退整个被删掉，用户下好的视频会凭空消失。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return ROOT

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        # 非空时，控制台 API 需要 ?token=xxx 或 X-Auth-Token 头
        "token": "",
        # 启动后自动打开浏览器
        "open_browser": True,
    },
    "paths": {
        "data_dir": "data",
        "download_dir": "downloads",
        "inbox_dir": "inbox",
        "log_dir": "logs",
    },
    "net": {
        # 代理写法（三态）：
        #   ""      或 "auto"  → 跟随系统代理（Windows 注册表里的 Internet 设置）
        #   "none"  或 "direct" → 强制直连
        #   "http://127.0.0.1:7890" → 走指定代理
        "proxy": "auto",
        "timeout_sec": 15,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        # HTTPS 证书校验；只有遇到证书链有问题的站点才建议关掉
        "verify_tls": True,
    },
    "download": {
        "workers": 2,
        # 单文件大小上限（MB），0 表示不限制
        "max_filesize_mb": 2048,
        # 单个任务整体超时（秒）
        "task_timeout_sec": 1800,
        # 是否优先用 yt-dlp（否则只用直链下载）
        "prefer_ytdlp": True,
        # 传给 yt-dlp 的额外参数，按需自行追加
        "ytdlp_extra_args": [],
        # 例如 "chrome" / "edge"，从浏览器读 cookie；留空则不用。
        # 抖音/小红书这类有风控的站点，配了游客 cookie（浏览器访问一次即可，
        # 无需登录）下得更稳；西瓜视频则是基本必须配。
        "cookies_from_browser": "",
        # Netscape 格式的 cookies.txt 路径。适合本机没装浏览器、
        # 或想用另一台机器导出的 cookie 的情况。与 cookies_from_browser 二选一。
        "cookies_file": "",
        # ffmpeg 位置。留空 = 自动查找（PATH → Cherry/scoop/winget 等常见目录）。
        # B站 / YouTube / 小红书这类只有 DASH 分离流的站点，没有 ffmpeg 就下不了。
        "ffmpeg_location": "",
        # ── cookie 下发策略 ────────────────────────────────────────
        # cookie 是**站点特定**的：西瓜不带 cookie 就下不了，
        # 而 YouTube 带了别的会话的 cookie 反而会被风控拒绝
        # （"The page needs to be reloaded"）。所以按域名决定发不发。
        #   auto  = 只在下面 cookie_domains 里匹配到的站点带（推荐）
        #   always= 总是带（老行为，容易把 YouTube 弄挂）
        #   never = 从不带
        "cookie_mode": "auto",
        # 需要携带 cookie 的站点（按域名后缀匹配）
        "cookie_domains": [
            "douyin.com", "iesdouyin.com",
            "xiaohongshu.com", "xhslink.com", "xhslink.cn",
            "ixigua.com", "toutiao.com",
            "weibo.com", "weibo.cn",
            "kuaishou.com", "gifshow.com",
            "bilibili.com", "b23.tv",
        ],
        # 传给 yt-dlp 的 --proxy；留空则跟随 net.proxy
        "proxy": "",
        # 优先画质：best / 1080 / 720 / 480
        "quality": "best",
        # 优先选「到哪都能播」的编码（H.264 + AAC）。
        # YouTube 现在默认给 AV1 + Opus，老手机、部分播放器、剪映都打不开 ——
        # 代下载是交付给客户的，兼容性比那点体积/画质差重要。
        # 关掉则按站点给的最佳编码走（可能拿到 AV1/VP9）。
        "prefer_compatible": True,
        # 下载完成后把文件重命名为 标题.ext
        "rename_by_title": True,
        # 标题最长多少字符（抖音的 title 含整段简介，必须截断）
        "title_maxlen": 60,
        # 文件名最长多少字符
        "filename_maxlen": 60,
    },
    "search": {
        # 按顺序降级尝试；只有配置了 key 的 provider 才会被启用
        "providers": ["serper", "google_cse", "duckduckgo"],
        "serper_api_key": "",
        "google_cse_key": "",
        "google_cse_cx": "",
        "max_results": 8,
        # 落源时优先命中的站点（按域名子串匹配，靠前者加权更高）
        "prefer_domains": [
            "douyin.com",
            "kuaishou.com",
            "bilibili.com",
            "xiaohongshu.com",
            "weibo.com",
            "youtube.com",
            "youtu.be",
        ],
        # 明显不可用的站点直接跳过
        "block_domains": ["csdn.net", "zhihu.com/question", "baidu.com/link"],
        "timeout_sec": 12,
    },
    "resolve": {
        # 短链展开的最大跳数
        "max_redirects": 6,
        # 是否允许「链接解析不出来时用标题去搜」
        "allow_title_search": True,
        # 展开短链时是否允许用 GET 兜底（有些服务器拒绝 HEAD）
        "allow_get_fallback": True,
    },
    "replies": {
        # 每条模板可用占位符：{title} {size} {filename} {progress} {platform}
        # {error} {url} {eta}
        "received": "亲，链接收到啦～正在帮你下载，一般 1-3 分钟出文件，稍等我发你哈 😊",
        "downloading": "正在下载中，当前进度 {progress}%，稍等一下下～",
        "done": "下载好啦！\n文件：{filename}\n大小：{size}\n请查收，有任何问题随时找我～",
        "need_manual": (
            "亲，这个链接我这边暂时没解析出来呢 🙏\n"
            "麻烦你换个方式发我：\n"
            "1）直接发视频的分享链接（不是截图）\n"
            "2）或者发「作者名 + 视频标题」，我帮你去搜"
        ),
        "failed": "抱歉，这个视频下载失败了（{error}）。\n麻烦你换一个链接试试，或者我这边给你退单～",
        "drm": (
            "亲，{platform} 的视频有版权保护，官方渠道限制下载，我这边没办法处理哦 😥\n"
            "如果其他平台也有，可以发给我试试～"
        ),
        # ── 进度回话的节流策略 ────────────────────────────────────
        # 买家不想被「进度 1%」「进度 2%」刷屏，所以只在大文件+慢下载时才推。
        "progress_step": 25,        # 每涨这么多百分点才考虑推一次
        "progress_min_seconds": 20, # 下载开始若干秒内一律不推（小文件直接跳过）
        "progress_max_count": 3,    # 单笔订单最多推几条进度
        # 是否把失败原因附在「需人工」话术后面（默认关，避免暴露技术细节）
        "append_reason": False,
    },
    "channels": {
        "console": {"enabled": True},
        "inbox": {"enabled": False, "poll_sec": 2.0},
        # 闲鱼桥接：和上游「闲鱼超级管家」对接时打开
        "xianyu_bridge": {"enabled": False},
    },
    # ── 交付：把下载好的文件变成一条能发给买家的链接 ────────────────
    # 为什么必须有这一段：闲鱼聊天只支持文本和图片，发不了视频文件。
    # 所以「把视频交给买家」只能是「传到某处 → 发链接」。
    "delivery": {
        # 上传通道：none / baidu / s3
        "uploader": "none",
        # share_link = 上传后生成分享链接一起发出去
        # upload_only = 只上传不发链接（分享权限还没批下来时的过渡）
        "mode": "share_link",
        # 发给买家的链接文案，{url} 是链接、{pwd} 是提取码。
        # 留空则用内置句式（下面 uploader.base 里的默认）。
        "link_template": "",
        # 下载中要不要给买家推进度（默认不推，免得刷屏）
        "forward_progress": False,
        # 上传失败时追加在话术后面的一句提示；留空则不追加
        "upload_fail_note": "（网盘链接稍后补发，稍等一下下～）",
        # upload_only 模式下追加的提示。注意此时**不是**失败：
        # 文件已经在网盘里了，只是没生成分享链接。
        "upload_only_note": "",

        # ── TikHub 第三方解析（抖音/TikTok 无水印）────────────────
        # 抖音/TikTok 的 a_bogus 签名本地逆向不现实，TikHub.io 在服务端维护了
        # 签名算法。填上 API key（免费注册 + 每日签到领额度）后，下载抖音/TikTok
        # 会自动走 TikHub 拿无水印直链。
        "tikhub": {
            "api_key": "",
        },

        # ── 百度网盘 ────────────────────────────────────────────────
        "baidu": {
            "app_key": "",            # 开放平台应用的 AppKey
            "secret_key": "",         # 应用的 SecretKey
            "refresh_token": "",      # 跑 `run.py baidu-login` 自动写入
            "remote_dir": "/apps/xianyu-video",
            "share_period_days": 7,   # 分享有效期（天），0 = 永久
            "share_pwd": "",          # 固定提取码；留空则每次随机 4 位
            "share_api": "old",       # old / new / auto —— 见 uploaders/baidu.py 的说明
            "upload_host": "",        # 留空自动从候选域名里试
            "slice_mb": 4,            # 普通用户固定 4MB，会员可调大
        },

        # ── 对象存储（S3 兼容：COS / OSS / R2 / MinIO / 七牛）────────
        # 交付走预签名直链：链接带有效期，过期自动失效，不用把桶设成公开。
        # 换服务商只改 endpoint + region。
        "s3": {
            # 腾讯云 COS  https://cos.ap-guangzhou.myqcloud.com      ap-guangzhou
            # 阿里云 OSS  https://s3.oss-cn-hangzhou.aliyuncs.com     oss-cn-hangzhou
            # Cloudflare R2  https://<accountid>.r2.cloudflarestorage.com   auto
            # MinIO（自建） http://192.168.1.10:9000                 us-east-1
            "endpoint": "",
            "region": "",
            "bucket": "",
            "access_key_id": "",
            "secret_access_key": "",
            "session_token": "",      # 用临时凭据（STS）时才需要
            # 上传到桶里的哪个前缀（相当于目录）
            "prefix": "xianyu-video",
            # MinIO / 私有实现常不支持 virtual-host 风格，填 true 走路径风格
            "path_style": False,
            # 预签名链接有效期（秒）。默认 7 天
            "url_expires_sec": 604800,
            # 桶公开或挂了 CDN 时填这里，直接用永久直链、不做签名
            "public_base_url": "",
            # 可选：对象的 ACL，例如 "public-read"。留空则由桶策略决定
            "acl": "",
        },

        # ── 上游桥接 ────────────────────────────────────────────────
        "bridge": {
            "enabled": True,
            "upstream_url": "http://127.0.0.1:8080",
            "deliver_path": "/api/xianyu/deliver",
            # 与上游约定的共享密钥，会放进 X-Auth-Token 头。留空则不校验。
            "token": "",
            # 只回传这些来源渠道的订单。上游把 channel 设成 xianyu；
            # 人肉粘贴的 console 单默认不会被推回闲鱼（避免测试单发给真买家）。
            "source_channels": ["xianyu"],
            "timeout_sec": 15,
            "retries": 2,
        },
    },
    "logging": {"level": "INFO", "keep_days": 14},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 override 递归合并进 base 的副本（dict 合并，其余类型整体替换）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """点号路径访问的配置对象。

    >>> cfg = Config()
    >>> cfg.get("server.port")
    8765
    >>> cfg.get("server.port", 1)
    8765
    >>> cfg.get("nope.nothing", "fallback")
    'fallback'
    """

    def __init__(self, data: dict[str, Any] | None = None, path: Path | None = None):
        self.path = path
        self._data = _deep_merge(DEFAULTS, data or {})

    # ── 加载 / 保存 ────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """从 config.json 读；文件不存在就写一份默认配置出去。"""
        cfg_path = Path(path) if path else app_dir() / "config.json"
        data: dict[str, Any] = {}
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        cfg = cls(data, path=cfg_path)
        if not cfg_path.exists():
            cfg.save()
        return cfg

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("没有指定配置文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(self.as_dict(), fh, ensure_ascii=False, indent=2)
        return target

    # ── 读取 ───────────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    # ── 路径辅助 ───────────────────────────────────────────────────
    def path_of(self, key: str) -> Path:
        """把 paths.<key> 解析成绝对路径（相对路径基于可写目录）。"""
        raw = self.get(f"paths.{key}", key)
        p = Path(str(raw))
        if not p.is_absolute():
            p = app_dir() / p
        return p

    def ensure_dirs(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for key in ("data_dir", "download_dir", "inbox_dir", "log_dir"):
            p = self.path_of(key)
            p.mkdir(parents=True, exist_ok=True)
            out[key] = p
        return out

    # ── 凭据可以从环境变量覆盖，方便不把 key 写进文件 ───────────────
    def apply_env_overrides(self, env: dict[str, str] | None = None) -> "Config":
        env = env if env is not None else os.environ
        mapping = {
            "xydl_server_token": "server.token",
            "xydl_serper_api_key": "search.serper_api_key",
            "xydl_google_cse_key": "search.google_cse_key",
            "xydl_google_cse_cx": "search.google_cse_cx",
            "xydl_proxy": "download.proxy",
        }
        for env_key, dotted in mapping.items():
            value = env.get(env_key) or env.get(env_key.upper())
            if value:
                self.set(dotted, value)
        return self

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Config path={self.path} keys={list(self._data)}>"
