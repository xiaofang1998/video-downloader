"""链接识别：从买家的聊天文本里抽出链接、判定平台、猜标题。

真实闲鱼消息长这样（买家直接转发平台的分享文案）::

    😆 8Xk2mQzP 😆 http://xhslink.com/a/xxxxx  复制本条信息，打开【小红书】App查看
    7.92 复制打开抖音，看看【@小明 的作品】这个视频太搞笑了 https://v.douyin.com/iRNBho6/
    https://www.bilibili.com/video/BV1xx411c7mD?share_source=copy_web

所以要处理三件事：
  1. 从一堆中文 + emoji 里把 URL 干净地切出来（不能把后面的中文吃进去）。
  2. 认出这是什么平台、是不是短链、要不要展开。
  3. 没有链接时，尽力猜一个标题，供「搜索落源」使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ── 字符类 ────────────────────────────────────────────────────────────
# 中日韩文字与全角标点：URL 在遇到这些字符时必然结束
_CJK = (
    r"\u2e80-\u2eff"   # CJK 部首补充
    r"\u3000-\u303f"   # CJK 标点（、。「」等）
    r"\u3040-\u30ff"   # 日文假名
    r"\u3400-\u4dbf"   # CJK 扩展 A
    r"\u4e00-\u9fff"   # CJK 统一表意文字
    r"\uf900-\ufaff"   # CJK 兼容表意文字
    r"\ufe30-\ufe4f"   # CJK 兼容形式
    r"\uff00-\uffef"   # 全角字符（！？，：等）
)
#: URL 里允许出现的单个字符（不含分隔符与中日韩文字）
_URL_CHAR = r"[^\s<>\"'`\|" + _CJK + r"]"
_URL_BODY = _URL_CHAR + "+"

#: 带协议的 URL
URL_RE = re.compile(r"(?i)(?:https?://|www\.)" + _URL_BODY)
#: 裸域名（闲鱼买家手打时很常见：v.douyin.com/xxxx）
BARE_RE = re.compile(
    r"(?i)(?:[a-z0-9][a-z0-9\-]{0,62}\.)+"
    r"(?:com|cn|net|org|tv|me|be|io|cc|xyz|top|link|video|app|site|live|fun|vip)"
    r"(?::\d{1,5})?(?:/" + _URL_CHAR + r"*)?"
)
#: 结尾要剥掉的垃圾字符（中文标点已在字符类里排除，这里主要处理英文标点）
_TRAILING_JUNK = ".,;:!?)]}>。，；：！？、》】」』\"'"

#: 直链媒体的扩展名
MEDIA_EXT_RE = re.compile(
    r"(?i)\.(mp4|m4v|flv|f4v|mov|webm|mkv|avi|ts|m3u8|mpd|mp3|m4a)(?:$|\?)"
)
#: HLS / DASH 流，需要 yt-dlp + ffmpeg
STREAM_EXT_RE = re.compile(r"(?i)\.(m3u8|mpd|ts)(?:$|\?)")


# ── 平台规则表 ────────────────────────────────────────────────────────
DRM_NOTE = "正版长视频通常有 DRM 保护，无法下载"


@dataclass(frozen=True)
class PlatformRule:
    """一个平台。

    关键点：**短链是按域名判定的，不是按平台判定的**。
    ``v.douyin.com/xxx`` 需要展开，但 ``douyin.com/video/123`` 本身就是完整页；
    把后者也当短链去展开只是浪费一次请求，反之前者当普通页就会直接下载失败。
    """

    key: str
    label: str
    domains: tuple[str, ...]
    short_domains: tuple[str, ...] = ()
    drm: bool = False
    note: str = ""

    @property
    def all_domains(self) -> tuple[str, ...]:
        return self.domains + self.short_domains


#: 顺序不重要，detect_platform() 按域名长度从长到短匹配
PLATFORMS: tuple[PlatformRule, ...] = (
    # ── 国内短视频 ────────────────────────────────────────────────
    PlatformRule("douyin", "抖音",
                 ("douyin.com", "iesdouyin.com", "snssdk.com"),
                 short_domains=("v.douyin.com",)),
    PlatformRule("kuaishou", "快手",
                 ("kuaishou.com", "gifshow.com", "kwai.com"),
                 short_domains=("v.kuaishou.com",)),
    # 小红书有两个短链域名：.com 和 .cn（实测 .cn 很常见，漏了会认不出来）
    PlatformRule("xiaohongshu", "小红书", ("xiaohongshu.com",),
                 short_domains=("xhslink.com", "xhslink.cn")),
    PlatformRule("bilibili", "哔哩哔哩",
                 ("bilibili.com", "acg.tv", "biligame.com"),
                 short_domains=("b23.tv", "bili2233.cn")),
    PlatformRule("weibo", "微博",
                 ("weibo.com", "weibo.cn", "miaopai.com"),
                 short_domains=("t.cn",)),
    PlatformRule("wechat", "微信",
                 ("channels.weixin.qq.com", "mp.weixin.qq.com", "weixin.qq.com"),
                 note="视频号内容通常无法直接下载，需要录屏或向原作者索取"),
    PlatformRule("xigua", "西瓜视频", ("ixigua.com", "toutiao.com")),
    PlatformRule("haokan", "好看视频", ("haokan.baidu.com",)),
    PlatformRule("pipixia", "皮皮虾", ("pipix.com", "ippzone.com")),
    PlatformRule("huoshan", "抖音火山版", ("huoshan.com",)),
    # ── 国内长视频（基本都有 DRM）─────────────────────────────────
    PlatformRule("tencent_video", "腾讯视频", ("v.qq.com", "film.qq.com"),
                 drm=True, note=DRM_NOTE),
    PlatformRule("iqiyi", "爱奇艺", ("iqiyi.com", "qiyi.com"),
                 drm=True, note=DRM_NOTE),
    PlatformRule("youku", "优酷", ("youku.com",), drm=True, note=DRM_NOTE),
    PlatformRule("mgtv", "芒果TV", ("mgtv.com",), drm=True, note=DRM_NOTE),
    # ── 海外 ──────────────────────────────────────────────────────
    PlatformRule("youtube", "YouTube",
                 ("youtube.com", "youtube-nocookie.com"),
                 short_domains=("youtu.be",)),
    PlatformRule("tiktok", "TikTok", ("tiktok.com",),
                 short_domains=("vt.tiktok.com", "vm.tiktok.com")),
    PlatformRule("twitter", "X / Twitter", ("x.com", "twitter.com"),
                 short_domains=("t.co",)),
    PlatformRule("instagram", "Instagram", ("instagram.com", "instagr.am")),
    PlatformRule("facebook", "Facebook", ("facebook.com", "fb.com"),
                 short_domains=("fb.watch",)),
    PlatformRule("vimeo", "Vimeo", ("vimeo.com",)),
    PlatformRule("dailymotion", "Dailymotion", ("dailymotion.com",),
                 short_domains=("dai.ly",)),
    PlatformRule("twitch", "Twitch", ("twitch.tv",)),
    PlatformRule("biliintl", "bilibili 国际版", ("biliintl.com",)),
    # ── 电商 / 闲鱼自家（多半是买家发错）─────────────────────────
    PlatformRule("xianyu", "闲鱼/淘宝",
                 ("goofish.com", "2.taobao.com", "taobao.com", "tmall.com"),
                 short_domains=("m.tb.cn",)),
)

#: 域名 → 规则，长域名优先（b23.tv 会先于 bilibili.com 之外的规则被考虑）
_DOMAIN_INDEX: list[tuple[str, PlatformRule]] = sorted(
    ((d, rule) for rule in PLATFORMS for d in rule.all_domains),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""


def domain_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def detect_platform(url: str) -> PlatformRule | None:
    """按域名判定平台。"""
    host = host_of(url)
    if not host:
        return None
    for domain, rule in _DOMAIN_INDEX:
        if host == domain or host.endswith("." + domain):
            return rule
    return None


def is_short_link(url: str, rule: PlatformRule | None = None) -> bool:
    """这个 URL 是不是需要展开的短链？"""
    rule = rule or detect_platform(url)
    if rule is None:
        return False
    return domain_matches(host_of(url), rule.short_domains)


# ── URL 清洗 ──────────────────────────────────────────────────────────
def _rstrip_url(url: str) -> str:
    """剥掉尾随标点，但保留成对括号里的内容。"""
    while url and url[-1] in _TRAILING_JUNK:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    if url.endswith("#"):
        url = url[:-1]
    return url


def normalize_url(url: str) -> str:
    """保守归一化：补协议、小写 scheme/host、去掉末尾多余斜杠以外的都不动。"""
    url = url.strip()
    if not url:
        return ""
    if url.lower().startswith("www."):
        url = "http://" + url
    elif "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url
    netloc = host
    if parts.port and not ((scheme == "http" and parts.port == 80)
                           or (scheme == "https" and parts.port == 443)):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        auth = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{auth}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path or "", parts.query, parts.fragment))


def _kind_for(url: str, rule: PlatformRule | None) -> tuple[str, str]:
    """返回 (kind, note)。直链特征优先于平台默认值。"""
    if STREAM_EXT_RE.search(url):
        return "direct", "HLS/DASH 流，需要 yt-dlp（内部会调 ffmpeg 合流）"
    if MEDIA_EXT_RE.search(url):
        return "direct", ""
    if rule is None:
        return "page", ""
    if rule.drm:
        return "drm", rule.note
    if is_short_link(url, rule):
        return "short", rule.note
    return "page", rule.note


# ── 抽取 ──────────────────────────────────────────────────────────────
@dataclass
class ParseResult:
    """一次消息解析的完整结论。"""

    links: list["LinkCandidate"] = field(default_factory=list)
    title_guess: str = ""
    cleaned_text: str = ""
    has_link: bool = False
    note: str = ""

    @property
    def best(self):
        """置信度最高的链接候选。"""
        return max(self.links, key=lambda c: c.confidence) if self.links else None

    @property
    def needs_search(self) -> bool:
        """没有可用链接，但猜到了标题 —— 可以走搜索落源。"""
        return not self.has_link and bool(self.title_guess)


def _score(candidate_kind: str, platform: str, source: str) -> float:
    """置信度：平台识别度 + 链接类型。"""
    score = 0.35
    if platform != "unknown":
        score += 0.30
    score += {"direct": 0.25, "short": 0.20, "page": 0.15, "drm": 0.05}.get(
        candidate_kind, 0.0
    )
    if source == "message":
        score += 0.10
    return round(min(score, 1.0), 2)


def extract_links(text: str, source: str = "message") -> list["LinkCandidate"]:
    """从任意文本抽链接，去重并排序（置信度降序，同分保持出现顺序）。

    >>> [c.platform for c in extract_links("看这个 https://v.douyin.com/iRNBho6/ 好玩")]
    ['douyin']
    >>> extract_links("没有链接的一段话")
    []
    """
    from .models import LinkCandidate  # 延迟导入，避免循环依赖

    if not text:
        return []

    seen: set[str] = set()
    candidates: list[LinkCandidate] = []
    spans: list[tuple[int, int]] = []

    # 第一轮：带协议的 URL（优先级最高）
    for match in URL_RE.finditer(text):
        raw = _rstrip_url(match.group(0))
        if not raw:
            continue
        spans.append(match.span())
        url = normalize_url(raw)
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        rule = detect_platform(url)
        kind, note = _kind_for(url, rule)
        candidates.append(LinkCandidate(
            url=url,
            platform=rule.key if rule else "unknown",
            platform_label=rule.label if rule else "未知站点",
            kind=kind,
            source=source,
            confidence=_score(kind, rule.key if rule else "unknown", source),
            note=note,
            raw=raw,
        ))

    # 第二轮：裸域名（避开已被第一轮覆盖的区间）
    for match in BARE_RE.finditer(text):
        start, end = match.span()
        if any(s <= start < e for s, e in spans):
            continue
        raw = _rstrip_url(match.group(0))
        if not raw or " " in raw:
            continue
        rule = detect_platform(raw)
        if rule is None:
            # 裸域名必须是已知平台才收，否则 "example.com" 这种噪声太多
            continue
        url = normalize_url(raw)
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        kind, note = _kind_for(url, rule)
        candidates.append(LinkCandidate(
            url=url,
            platform=rule.key,
            platform_label=rule.label,
            kind=kind,
            source=source,
            confidence=round(_score(kind, rule.key, source) - 0.05, 2),
            note=note,
            raw=raw,
        ))

    candidates.sort(key=lambda c: -c.confidence)
    return candidates


# ── 分享文案清洗 / 标题猜测 ────────────────────────────────────────────
#: 包裹平台名/标题的括号。**必须最先去掉**，否则 "打开【小红书】App查看" 这种
#: 文案后面的 boilerplate 规则全都匹配不上。
_BRACKETS_RE = re.compile(r"[【】「」『』《》\[\]〔〕]")
#: 零宽字符与不间断空格
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\ufeff\u00a0]")
#: 被括号包住的内容，通常是作者名或作品名
_BRACKET_TITLE_RE = re.compile(r"[【「『《\[]([^】」』》\]]{2,60})[】」』》\]]")
#: 【某某的作品】/【某某的视频】—— 这种括号里是**作者名**，不是标题。
#: 真实案例：抖音分享文案 "看看【钟世祺的作品】吹笛子建议收藏..." 里，
#: 真正的标题在括号**外面**，早期版本会把作者名当成标题。
_AUTHOR_BRACKET_RE = re.compile(r"^@?[^【】]{1,30}的(?:作品|视频|主页|笔记|直播间)$")

_PLATFORM_WORDS = (
    r"(?:抖音|快手|小红书|哔哩哔哩|B站|b站|微视|微博|西瓜视频|好看视频|"
    r"YouTube|油管|TikTok|Dou音|Douyin|Xiaohongshu|Instagram|Ins)"
)

_BOILERPLATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ── 第一步：先把 URL 整个拿走 ────────────────────────────────
    # 必须最先做。实测踩过：URL 结尾的数字会被「7.92 复制打开抖音」这类
    # 口令前缀规则误匹配（"...Ezc6 复制" 里的 "6 复制"），把后面的文案吃掉。
    re.compile(r"(?i)https?://\S+"),
    re.compile(r"(?i)\bwww\.\S+"),
    re.compile(r"(?i)\bv\.[a-z0-9\-]+\.(?:com|cn|tv|me|be)\S*"),
    re.compile(r"(?i)\bxhslink\.[a-z]+\S*"),
    # ── 第二步：清平台引导语 ────────────────────────────────────
    # 顺序关键：**长而具体的规则必须先跑**，否则短规则会把前缀吃掉，
    # 导致 "打开小红书 App查看" 里的 "App查看" 残留下来。
    # 另外注意：中文紧邻时 `\b` 不成立（「查」是 \w），所以用显式的前后断言。
    re.compile(_PLATFORM_WORDS + r"\s*(?<![A-Za-z])(?:App|APP|app)(?![A-Za-z])"
               r"\s*(?:查看|参见|观看|看|里|客户端)?"),
    re.compile(r"(?:打开|搜索|去|上|在|进入)\s*" + _PLATFORM_WORDS),
    re.compile(_PLATFORM_WORDS + r"(?:\s*(?:查看|参见|观看|看|里|客户端))?"),
    re.compile(r"(?<![A-Za-z])(?:App|APP|app)(?![A-Za-z])"),
    # 抖音/快手分享口令前缀，如 "7.92 复制打开抖音"。
    # 前面不能是字母或斜杠 —— 否则 URL 尾部的数字会被当成口令前缀。
    re.compile(r"(?<![\w/])\d{1,3}(?:\.\d{1,2})?\s*(?:复制|打开|观看|来|看)"),
    re.compile(r"复制(?:此|本|这)?(?:条|段)?(?:文本|文字|链接|信息|消息|口令|网址|文案)?"
               r"\s*后?\s*(?:进入|打开|去|粘贴)?"),
    # "直接查看笔记" / "点击查看详情" 这类引导语
    re.compile(r"(?:直接|点击|请|可)?\s*(?:查看|阅读|打开|浏览)\s*"
               r"(?:笔记|帖子|原文|详情|全文|内容|视频)?"),
    re.compile(r"(?:看看|分享)\s*.{0,12}?\s*的?\s*作品"),
    # "…的笔记 / 的帖子" 这类平台体裁词，去掉后留下的才是真正的内容名
    re.compile(r"的(?:笔记|帖子|微博|动态|说说|视频|作品)"),
    re.compile(r"长按(?:复制|识别|二维码)"),
    re.compile(r"(?:直接)?观看(?:完整版|视频)?"),
    re.compile(r"查看更多(?:精彩)?"),
    re.compile(r"本条信息|此条消息|这条消息"),
    re.compile(r"@[\w\u4e00-\u9fff\-\.]{1,24}"),
    re.compile(r"#\S{1,30}"),
    # 分享码：抖音/快手的 5-12 位口令，签名很有辨识度 ——
    # **同时含数字、大写、小写**，而正常英文单词不会在词中混大小写。
    # 早期版本用「后面紧跟『复制』/『打开』」来判断，但那些字早被上面的规则
    # 吃掉了，导致这个规则几乎不生效；而只用「5 字符以上 ASCII」又会把
    # 英文标题结尾的 "video" 当码删掉。
    re.compile(
        r"(?<![A-Za-z0-9])"
        r"(?=[A-Za-z0-9]{5,12}(?![A-Za-z0-9]))"
        r"(?=[A-Za-z0-9]*\d)"
        r"(?=[A-Za-z0-9]*[a-z])"
        r"(?=[A-Za-z0-9]*[A-Z])"
        r"[A-Za-z0-9]+"
    ),
    # emoji 与变体选择符
    re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2190-\u21FF]"),
    re.compile(r"\s{2,}"),
)

#: 整句都是这些内容 → 没有可用信息
_TITLE_STOPWORDS = {
    "", "帮我", "帮我下", "帮我下载", "帮忙", "帮忙下", "麻烦", "麻烦你", "麻烦你了",
    "你好", "在吗", "在不在", "老板", "老板在吗", "谢谢", "多谢", "好的", "好",
    "可以吗", "能下吗", "能下载吗", "能行吗", "多少钱", "价格", "怎么收费", "报价",
    "这个视频", "这个", "就这个", "这个链接", "就这个视频", "视频", "链接",
    "下载", "代下", "代下载", "没有链接", "没有", "无", "见图片", "看图片", "如图",
    "图片", "截图", "发你了", "发给你了", "收到了吗", "收到没", "查看", "客户端",
}

#: 常见无信息量的填充词，用于判断标题是否含真信息
_FILLER_RE = re.compile(
    r"(帮我|帮忙|麻烦你?|请你?|亲|老板|下一下|帮我下|下载|下个|下这个|这个|一下|"
    r"可以吗|能吗|好吗|行吗|谢谢|多谢|就|的|了|吧|呢|啊|嘛)"
)

#: 标题开头/结尾的噪声词。长的排在前面，保证 "就这个视频" 整体被吃掉，
#: 而不是只吃掉 "就" 之后留下 "这个视频"。
_LEADING_NOISE_RE = re.compile(
    r"^(?:就这个视频|这个视频|就这个|这个链接|就这|这个|那|这|我?想要?|我?要|"
    r"帮我下|帮我|帮忙下|帮忙|麻烦你|麻烦|请你|请|分享|亲|老板|你好|在吗|能不能|能|"
    r"可不可以|可以|下载|代下|下|来|看)+\s*[,，、。:：\s]*"
)
_TRAILING_NOISE_RE = re.compile(
    r"[\s,，、。:：]*(?:另外|还有|以及|也要|也要下|一起|都|吧|呢|啊|哦|谢谢|多谢|拜托|"
    r"可以吗|能吗|好吗|行吗)*\s*$"
)


def clean_share_text(text: str) -> str:
    """把平台分享文案里的噪声去掉，留下可能是标题的部分。

    顺序很关键：先拆括号 → 删零宽字符 → 套 boilerplate 规则 →
    **在压缩标点之前**砍掉分享短码。最后一步的顺序不能颠倒：短码的特征
    符号（``:`` ``@`` ``/``）一旦被标点压缩吃掉，就再也认不出来了。
    """
    if not text:
        return ""
    out = _BRACKETS_RE.sub(" ", text)
    out = _INVISIBLE_RE.sub(" ", out)
    for pattern in _BOILERPLATE_PATTERNS:
        out = pattern.sub(" ", out)
    out = _trim_at_share_code(out)
    out = re.sub(r"[\s\-—_=·•|,.，。:：;；!！?？~～]+", " ", out)
    return out.strip()


def _trim_at_share_code(text: str) -> str:
    """砍掉分享口令的尾巴。

    抖音/快手的分享文案后面常挂着一段无意义的短码，形如::

        ... 韩湘子秘... :3pm p@d.aa YZz:/ 02/14

    规则：**只有在已经出现过中文之后**才可能在码样 token 处截断 ——
    否则纯英文标题（"Rick Astley - Never Gonna Give You Up"）会被误伤。
    码样 token 必须含 ``@ : /`` 之类的符号，这样 "2024"、"funny" 这类
    正常词不会被当成码。
    """
    tokens = text.split()
    kept: list[str] = []
    saw_cjk = False
    for token in tokens:
        if re.search(r"[\u4e00-\u9fff]", token):
            saw_cjk = True
            kept.append(token)
            continue
        if saw_cjk and _CODE_TOKEN_RE.fullmatch(token):
            break
        kept.append(token)
    return " ".join(kept)


#: 分享短码：含 @ : / . 之一，且整体是 ASCII 符号数字串
_CODE_TOKEN_RE = re.compile(r"(?=.*[@:/])[A-Za-z0-9@:/.\-]{2,}")


def _tidy_title(value: str, maxlen: int) -> str:
    """把标题首尾的客套话削掉，并压缩空白。"""
    title = re.sub(r"\s{2,}", " ", value).strip(" \t-—_·•|,，。:：")
    # 首尾噪声可能叠加（"老板 帮我下这个 ..."），循环削直到稳定
    for _ in range(6):
        new = _LEADING_NOISE_RE.sub("", title)
        new = _TRAILING_NOISE_RE.sub("", new)
        new = new.strip(" \t-—_·•|,，。:：")
        if new == title:
            break
        title = new
    title = _trim_at_share_code(title)
    title = re.sub(r"\s{2,}", " ", title).strip()
    return title[:maxlen]


def guess_title(text: str, maxlen: int = 60) -> str:
    """猜买家想下载的是什么。优先取分享文案里被括号包住的内容。

    >>> guess_title("看看【猫咪打呼噜合集】这个太逗了 https://v.douyin.com/abc/")
    '猫咪打呼噜合集'
    >>> guess_title("http://xhslink.com/a/xxxxx 复制本条信息，打开【小红书】App查看")
    ''
    >>> guess_title("帮我下这个")
    ''
    >>> guess_title("就这个视频，猫咪打呼噜合集")
    '猫咪打呼噜合集'
    """
    if not text:
        return ""
    # 先看有没有被括号包住的标题（括号内容通常是作品名，含金量最高）
    for match in _BRACKET_TITLE_RE.finditer(text):
        value = match.group(1).strip(" @")
        # 【某某的作品】是作者名不是标题 —— 跳过这个括号，去括号外面找
        if _AUTHOR_BRACKET_RE.match(value):
            continue
        cleaned = re.sub(r"的?作品$", "", value).strip(" @")
        cleaned = _INVISIBLE_RE.sub("", cleaned)
        if _is_informative(cleaned) and not _BOILERPLATE_ONLY.search(cleaned):
            tidied = _tidy_title(cleaned, maxlen)
            if _is_informative(tidied):
                return tidied

    cleaned = clean_share_text(text)
    # 取最长的一段中文/字母数字串
    runs = re.findall(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9 ]{3,}", cleaned)
    candidates: list[str] = []
    for run in runs:
        tidied = _tidy_title(run, maxlen)
        if _is_informative(tidied):
            candidates.append(tidied)
    if not candidates:
        return ""
    return max(candidates, key=lambda r: len(re.sub(r"\s", "", r)))


#: 括号里正好是平台名的情况，如【小红书】
_BOILERPLATE_ONLY = re.compile(r"^\s*" + _PLATFORM_WORDS + r"\s*$")


def _is_informative(value: str) -> bool:
    """判断一段文本是不是「真的说了点什么」。"""
    if not value:
        return False
    core = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    if len(core) < 3:
        return False
    if core in {re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE) for s in _TITLE_STOPWORDS}:
        return False
    # 去掉填充词后还剩多少实质内容？
    stripped = _FILLER_RE.sub("", value)
    if len(re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)) < 2:
        return False
    # 分享码判别：**短**、纯 ASCII、且含数字才算码。
    # 之前只看"纯 ASCII 且 4 字符以上"，把 "Never Gonna Give You Up" 这类
    # 正常英文标题也一并枪毙了。
    if (len(core) <= 16
            and re.fullmatch(r"[A-Za-z0-9]{4,}", core)
            and re.search(r"\d", core)):
        return False
    return True


def parse(text: str) -> ParseResult:
    """一站式解析一条买家消息。"""
    links = extract_links(text)
    title = guess_title(text)
    result = ParseResult(
        links=links,
        title_guess=title,
        cleaned_text=clean_share_text(text),
        has_link=bool(links),
    )
    if links:
        top = links[0]
        if top.kind == "drm":
            result.note = top.note or "该平台有版权保护"
        elif top.kind == "short":
            result.note = f"{top.platform_label} 短链，需要先展开"
    elif title:
        result.note = "消息里没有链接，将用标题去搜索落源"
    else:
        result.note = "既没有链接也没有可用的标题，需要人工介入"
    return result


def describe(candidates: list[Any]) -> str:
    """给控制台/日志用的简短描述。"""
    if not candidates:
        return "无链接"
    parts = []
    for cand in candidates[:3]:
        marker = "" if cand.kind != "drm" else "(受保护)"
        parts.append(f"{cand.platform_label}{marker} {cand.confidence:.2f}")
    return " | ".join(parts)
