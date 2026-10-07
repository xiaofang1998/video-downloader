"""解析层：把买家的链接变成「可以直接喂给下载引擎的地址」。

两件事：
  1. **展开短链**（v.douyin.com / b23.tv / xhslink.com …），并顺手抓页面标题。
  2. **落源**：链接实在解析不出来时，拿猜到的标题去搜索引擎找原视频。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from . import linkparse
from .config import Config
from .http import HEAD_UNSUPPORTED, Response, parse_proxy_spec, request
from .models import LinkCandidate
from .search import SearchResult, build_router
from .utils import excerpt_html_title, snippet

#: 抓页面用于取标题时，最多读这么多字节
PAGE_PROBE_BYTES = 256 * 1024
#: 最多尝试解析几条候选链接
MAX_CANDIDATES = 4

#: 登录墙：路径像登录页，或 query 里带 redirect 之类参数
_AUTH_PATH_RE = re.compile(
    r"/(?:login|signin|sign_in|sign-in|passport|auth|account/login)(?:[/?#]|$)", re.I
)
_AUTH_QUERY_RE = re.compile(
    r"[?&](?:redirect|redirectpath|redirect_uri|next|return_url|goto|callback)=", re.I
)


def is_auth_wall(url: str) -> bool:
    """这个地址是不是「登录墙」而不是真正的内容页。

    实测踩过：小红书的分享短链会 302 到笔记页，**未登录时再 302 到 login**。
    一路跟重定向到底只会拿到登录墙，真正的笔记地址反而被丢掉。
    """
    return bool(_AUTH_PATH_RE.search(url) or _AUTH_QUERY_RE.search(url))


@dataclass
class ExpandResult:
    """一次短链展开的结果。"""

    original: str = ""
    final_url: str = ""
    chain: list[str] = field(default_factory=list)
    status: int = 0
    title: str = ""
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.final_url) and self.final_url.rstrip("/") != self.original.rstrip("/")

    @property
    def hops(self) -> int:
        return max(0, len(self.chain) - 1)


@dataclass
class ResolveOutcome:
    """一次消息解析的最终结论，流水线据此决定下一步。"""

    chosen: LinkCandidate | None = None
    candidates: list[LinkCandidate] = field(default_factory=list)
    title: str = ""
    note: str = ""
    errors: list[str] = field(default_factory=list)
    needs_manual: bool = False
    drm_blocked: bool = False
    search_results: list[SearchResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chosen is not None and not self.drm_blocked

    def describe(self) -> str:
        if self.chosen is None:
            return self.note or "未能定位到可下载的源"
        c = self.chosen
        via = {"message": "原始链接", "expanded": "短链展开", "search": "搜索落源"}
        return f"{c.platform_label} · {via.get(c.source, c.source)} · {snippet(c.url, 70)}"


class Resolver:
    """链接展开 + 落源。无状态，可多线程复用（内部只用不可变配置）。"""

    def __init__(self, config: Config, router=None):
        self.config = config
        self.router = router if router is not None else build_router(config)
        self.timeout = float(config.get("net.timeout_sec", 15))
        self.ua = str(config.get("net.user_agent", ""))
        # None = 跟随系统代理；"" = 直连；其余 = 指定代理
        self.proxy = parse_proxy_spec(config.get("net.proxy"))
        self.verify_tls = bool(config.get("net.verify_tls", True))
        self.max_redirects = int(config.get("resolve.max_redirects", 6))
        self.allow_get_fallback = bool(config.get("resolve.allow_get_fallback", True))

    # ── 单条链接展开 ───────────────────────────────────────────────
    def _fetch(self, url: str, method: str) -> Response:
        return request(
            method,
            url,
            follow=True,
            max_redirects=self.max_redirects,
            timeout=self.timeout,
            proxy=self.proxy,
            user_agent=self.ua,
            verify_tls=self.verify_tls,
            max_bytes=PAGE_PROBE_BYTES,
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )

    def expand(self, url: str) -> ExpandResult:
        """跟随重定向拿到落地地址。HEAD 不行就退到 GET（很多短链服务拒绝 HEAD）。"""
        result = ExpandResult(original=url, final_url=url)

        resp = self._fetch(url, "HEAD")
        need_get = (
            resp.error
            or resp.status in HEAD_UNSUPPORTED
            or resp.status >= 400
            or resp.status == 0
        )
        if need_get and self.allow_get_fallback:
            resp = self._fetch(url, "GET")

        result.chain = list(resp.chain)
        result.status = resp.status
        if resp.error:
            result.error = resp.error
            return result

        result.final_url = self._pick_landing(resp.chain, resp.url or url)

        # 有些分享页不靠 302，而是把真实地址写在 canonical / og:url / JS 里
        if not result.changed and resp.body:
            guessed = excerpt_html_title(resp.text)  # 顺手拿标题
            result.title = guessed
            meta_url = self._meta_redirect(resp.text)
            if meta_url and meta_url.rstrip("/") != url.rstrip("/"):
                result.final_url = meta_url
        elif resp.body:
            result.title = excerpt_html_title(resp.text)

        return result

    @staticmethod
    def _pick_landing(chain: list[str], fallback: str) -> str:
        """从跳转链里挑出真正的落地地址。

        倒着找第一个不是登录墙的地址 —— 因为「最后落到的页面」往往是被
        重定向去的 login，而倒数第二个才是真正的笔记/视频页。
        """
        for candidate in reversed(chain or []):
            if not is_auth_wall(candidate):
                return candidate
        return fallback

    @staticmethod
    def _meta_redirect(html: str) -> str:
        from .utils import excerpt_meta_url

        return excerpt_meta_url(html)

    def probe_title(self, url: str) -> str:
        """只为了拿标题而探一次页面；失败返回空串，不抛异常。"""
        try:
            resp = self._fetch(url, "GET")
        except Exception:  # noqa: BLE001
            return ""
        if resp.error or not resp.body:
            return ""
        return excerpt_html_title(resp.text)

    # ── 候选收敛 ───────────────────────────────────────────────────
    @staticmethod
    def recandidate(url: str, source: str, title: str = "",
                    base: float = 0.9) -> LinkCandidate:
        """用最终 URL 重新构造候选（平台/类型都要重判）。"""
        rule = linkparse.detect_platform(url)
        kind, note = linkparse._kind_for(url, rule)  # noqa: SLF001 - 同包内的内部协作
        return LinkCandidate(
            url=url,
            platform=rule.key if rule else "unknown",
            platform_label=rule.label if rule else "未知站点",
            kind=kind,
            source=source,
            title=title,
            confidence=round(min(base, 1.0), 2),
            note=note,
            raw=url,
        )

    #: 旧名，保留兼容
    _recandidate = recandidate

    def _try_candidate(self, cand: LinkCandidate,
                       errors: list[str]) -> tuple[LinkCandidate | None, str]:
        """把一个候选变成可下载地址。返回 (候选, 标题)。"""
        if cand.kind == "drm":
            return None, ""
        if cand.kind == "direct":
            return cand, cand.title
        if cand.kind == "short":
            exp = self.expand(cand.url)
            if exp.error:
                errors.append(f"展开短链失败 {snippet(cand.url, 50)}：{exp.error}")
                return None, ""
            final = exp.final_url
            if not exp.changed:
                errors.append(f"短链没有跳转，可能已失效：{snippet(cand.url, 50)}")
                return None, ""
            new = self._recandidate(final, "expanded", exp.title, base=0.92)
            errors.append(
                f"短链展开成功（{exp.hops} 跳）→ {snippet(final, 70)}"
            )
            return new, exp.title or cand.title

        # page：先看它是不是「没被认出来的短链」。
        # 平台的短链域名时有新增 —— 实测踩过：小红书的 xhslink.cn 一度没登记，
        # 结果被当成普通页面直接去下载，必然失败。展开一次的代价很低，
        # 所以对**未知域名**一律试一下跳转。
        if cand.platform == "unknown":
            exp = self.expand(cand.url)
            if exp.error:
                errors.append(f"探测跳转失败 {snippet(cand.url, 50)}：{exp.error}")
            elif exp.changed:
                neu = self._recandidate(exp.final_url, "expanded", exp.title, base=0.88)
                errors.append(
                    f"未知域名自动展开（{exp.hops} 跳）→ {snippet(exp.final_url, 70)}"
                )
                return neu, exp.title or cand.title

        title = cand.title
        if not title and cand.platform != "unknown":
            title = self.probe_title(cand.url)
        return cand, title

    # ── 主流程 ─────────────────────────────────────────────────────
    def resolve(
        self,
        candidates: Sequence[LinkCandidate],
        title_guess: str = "",
        allow_title_search: bool | None = None,
    ) -> ResolveOutcome:
        outcome = ResolveOutcome(
            candidates=list(candidates),
            title=title_guess,
            search_results=[],
        )
        allow_search = (
            bool(self.config.get("resolve.allow_title_search", True))
            if allow_title_search is None else allow_title_search
        )

        # 1) 全是受 DRM 保护的平台 → 直接判定，别浪费下载配额
        if candidates and all(c.kind == "drm" for c in candidates):
            outcome.drm_blocked = True
            outcome.needs_manual = True
            outcome.title = title_guess or candidates[0].platform_label
            outcome.note = candidates[0].note or "该平台有版权保护，无法下载"
            return outcome

        # 2) 逐个尝试候选（跳过 DRM 的）
        tried: set[str] = set()
        for cand in list(candidates)[:MAX_CANDIDATES]:
            if cand.kind == "drm" or cand.url in tried:
                continue
            tried.add(cand.url)
            resolved, title = self._try_candidate(cand, outcome.errors)
            if resolved is None:
                continue
            outcome.chosen = resolved
            if title and not outcome.title:
                outcome.title = title
            outcome.note = f"已定位：{resolved.platform_label}"
            return outcome

        # 3) 兜底：拿标题去搜
        if allow_search and title_guess:
            if not self.router.available:
                outcome.errors.append("未配置搜索源，无法用标题落源")
            else:
                results = self.router.find_source(title_guess)
                outcome.search_results = results
                outcome.errors.extend(self.router.errors)
                for item in results[:MAX_CANDIDATES]:
                    cand = self._recandidate(item.url, "search",
                                             title=item.title, base=0.55 + item.score / 10)
                    if cand.kind == "drm":
                        continue
                    resolved, title = self._try_candidate(cand, outcome.errors)
                    if resolved is None:
                        continue
                    resolved.title = resolved.title or title_guess
                    outcome.chosen = resolved
                    outcome.title = outcome.title or title_guess
                    outcome.note = f"搜索落源命中：{snippet(item.title or item.url, 50)}"
                    return outcome
                if not results:
                    outcome.errors.append(f"搜索「{title_guess}」没有找到可用结果")

        outcome.needs_manual = True
        outcome.note = (
            "没能定位到可下载的视频源，需要人工介入"
            if candidates else "消息里没有可识别的链接"
        )
        return outcome
