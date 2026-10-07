"""各个搜索 provider 的具体实现。

「谷歌搜索」在这里有两档：
  * **serper**  —— 第三方 Google SERP 代理，一个 key 即用，最省事（推荐）。
  * **google_cse** —— Google 官方 Programmable Search JSON API，最正规，
    免费额度每天 100 次。

没配 key 时退到 **duckduckgo**（无需 key，直接抓 HTML 端点），
国内网络需要自己在 config.json 里配好 ``net.proxy``。
"""

from __future__ import annotations

import html as html_mod
import re
import urllib.parse

from .base import SearchError, SearchProvider, SearchResult
from ..http import HttpError, get_json, post_json, request

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub("", text or ""))).strip()


class SerperProvider(SearchProvider):
    """https://serper.dev —— 返回真实 Google 结果，价格便宜，接入最快。"""

    name = "serper"
    label = "Google (Serper)"
    endpoint = "https://google.serper.dev/search"

    def __init__(self, config):
        super().__init__(config)
        self.api_key = str(config.get("search.serper_api_key", "")).strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        if not self.available:
            raise SearchError(self.name, "未配置 search.serper_api_key")
        try:
            payload = post_json(
                self.endpoint,
                {
                    "q": query,
                    "num": max(1, min(limit, 20)),
                    "gl": "cn",
                    "hl": "zh-cn",
                },
                headers={"X-API-KEY": self.api_key},
                timeout=self.timeout,
                proxy=self.proxy,
                user_agent=self.user_agent,
                verify_tls=self.verify_tls,
            )
        except HttpError as exc:
            raise SearchError(self.name, str(exc)) from exc

        out: list[SearchResult] = []
        for idx, item in enumerate(payload.get("organic") or []):
            url = (item.get("link") or "").strip()
            if not url:
                continue
            out.append(SearchResult(
                title=item.get("title", ""),
                url=url,
                snippet=item.get("snippet", ""),
                provider=self.name,
                rank=idx,
            ))
            if len(out) >= limit:
                break
        if not out:
            raise SearchError(self.name, "Google 没有返回任何结果")
        return out


class GoogleCSEProvider(SearchProvider):
    """Google 官方 Programmable Search JSON API。

    需要在 https://programmablesearchengine.google.com/ 建一个「搜索整个网络」
    的引擎拿到 cx，再去 https://developers.google.com/custom-search/v1/ 拿 key。
    """

    name = "google_cse"
    label = "Google (官方 CSE)"
    endpoint = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, config):
        super().__init__(config)
        self.api_key = str(config.get("search.google_cse_key", "")).strip()
        self.cx = str(config.get("search.google_cse_cx", "")).strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.cx)

    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        if not self.available:
            raise SearchError(self.name, "未配置 search.google_cse_key / google_cse_cx")
        # 官方 API 单次最多 10 条
        num = max(1, min(limit, 10))
        try:
            payload = get_json(
                self.endpoint,
                params={"key": self.api_key, "cx": self.cx, "q": query, "num": num},
                timeout=self.timeout,
                proxy=self.proxy,
                user_agent=self.user_agent,
                verify_tls=self.verify_tls,
            )
        except HttpError as exc:
            raise SearchError(self.name, str(exc)) from exc

        out: list[SearchResult] = []
        for idx, item in enumerate(payload.get("items") or []):
            url = (item.get("link") or "").strip()
            if not url:
                continue
            out.append(SearchResult(
                title=item.get("title", ""),
                url=url,
                snippet=item.get("snippet", ""),
                provider=self.name,
                rank=idx,
            ))
        if not out:
            raise SearchError(self.name, "Google CSE 没有返回任何结果")
        return out


class DuckDuckGoProvider(SearchProvider):
    """无需 API key 的兜底：直接抓 DuckDuckGo 的 HTML 端点。"""

    name = "duckduckgo"
    label = "DuckDuckGo (免 key)"
    endpoints = (
        "https://html.duckduckgo.com/html/",
        "https://lite.duckduckgo.com/lite/",
    )
    #: class 里带 result__a 的锚点；DDG 的 DOM 偶尔调整，所以做了两种兜底
    _PATTERNS = (
        re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(
            r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(
            r'<a[^>]+class="[^"]*result-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        ),
    )

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def _unwrap(url: str) -> str:
        """DDG 把结果包成 /l/?uddg=<encoded>，这里还原真实地址。"""
        if url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/l/" in url or "/l/?" in url:
            query = urllib.parse.urlsplit(url).query
            params = urllib.parse.parse_qs(query)
            real = (params.get("uddg") or [""])[0]
            if real:
                return urllib.parse.unquote(real)
        return url

    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        errors: list[str] = []
        for endpoint in self.endpoints:
            try:
                resp = request(
                    "POST" if "html." in endpoint else "GET",
                    endpoint,
                    data=urllib.parse.urlencode({"q": query, "kl": "cn-zh"}).encode(),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": endpoint,
                    },
                    timeout=self.timeout,
                    proxy=self.proxy,
                    follow=True,
                    max_redirects=3,
                    user_agent=self.user_agent,
                    verify_tls=self.verify_tls,
                )
            except HttpError as exc:
                errors.append(f"{endpoint}: {exc}")
                continue
            if resp.error:
                errors.append(f"{endpoint}: {resp.error}")
                continue
            if not resp.ok:
                errors.append(f"{endpoint}: HTTP {resp.status}")
                continue

            body = resp.text
            out: list[SearchResult] = []
            seen: set[str] = set()
            for pattern in self._PATTERNS:
                for href, label in pattern.findall(body):
                    url = self._unwrap(href.strip())
                    if not url.startswith("http") or url in seen:
                        continue
                    seen.add(url)
                    out.append(SearchResult(
                        title=_clean(label),
                        url=url,
                        snippet="",
                        provider=self.name,
                        rank=len(out),
                    ))
                    if len(out) >= limit:
                        break
                if len(out) >= limit:
                    break
            if out:
                return out
            errors.append(f"{endpoint}: 解析不出任何结果（可能被反爬拦截）")

        raise SearchError(self.name, "; ".join(errors) or "未知错误")


class StaticProvider(SearchProvider):
    """测试与离线演示用：结果由构造参数直接给定。"""

    name = "static"
    label = "静态（测试用）"

    def __init__(self, config, results: list[SearchResult] | None = None,
                 error: str = ""):
        super().__init__(config)
        self._results = list(results or [])
        self._error = error
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        self.calls.append(query)
        if self._error:
            raise SearchError(self.name, self._error)
        return self._results[:limit]
