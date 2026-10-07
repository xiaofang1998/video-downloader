"""搜索层入口：按配置组装 provider，按顺序降级，并对结果排序。"""

from __future__ import annotations

from typing import Iterable, Sequence

from .base import SearchError, SearchProvider, SearchResult
from .providers import (
    DuckDuckGoProvider,
    GoogleCSEProvider,
    SerperProvider,
    StaticProvider,
)
from ..config import Config

PROVIDER_CLASSES: dict[str, type[SearchProvider]] = {
    SerperProvider.name: SerperProvider,
    GoogleCSEProvider.name: GoogleCSEProvider,
    DuckDuckGoProvider.name: DuckDuckGoProvider,
    StaticProvider.name: StaticProvider,
}

__all__ = [
    "SearchError",
    "SearchProvider",
    "SearchResult",
    "SearchRouter",
    "PROVIDER_CLASSES",
    "build_providers",
    "build_router",
    "rank",
]


def build_providers(config: Config) -> list[SearchProvider]:
    """按 ``search.providers`` 的顺序实例化已配置好的 provider。"""
    names: Sequence[str] = config.get("search.providers", ["duckduckgo"]) or []
    providers: list[SearchProvider] = []
    for name in names:
        cls = PROVIDER_CLASSES.get(str(name).strip().lower())
        if cls is None:
            continue
        try:
            provider = cls(config)
        except Exception:  # noqa: BLE001 - 单个 provider 装配失败不该拖垮整体
            continue
        if provider.available:
            providers.append(provider)
    return providers


def rank(
    results: Iterable[SearchResult],
    prefer_domains: Sequence[str] = (),
    block_domains: Sequence[str] = (),
) -> list[SearchResult]:
    """给搜索结果打分排序。

    权重设计上「**能不能下载**」比「搜索引擎排第几」重要得多：
    一个排第 0 位但来自冷门站点的结果，yt-dlp 多半解析不了；而排第 7 位的
    B 站/抖音链接几乎必然能下。所以偏好域名的加分（最高 0.9）刻意压过
    位次衰减带来的差距。

    命中 ``block_domains`` 的直接丢弃（这些站点几乎不可能给出可下载的视频）。
    """
    scored: list[SearchResult] = []
    for item in results:
        url_lower = item.url.lower()
        if any(block and block.lower() in url_lower for block in block_domains):
            continue
        # 位次衰减：0 → 1.00，3 → 0.45，7 → 0.26（比 1/(1+rank) 平缓）
        score = 1.0 / (1.0 + 0.4 * max(0, item.rank))
        for idx, domain in enumerate(prefer_domains):
            if domain and domain.lower() in item.domain:
                score += max(0.30, 0.90 - idx * 0.06)
                break
        if item.title:
            score += 0.05
        item.score = round(score, 4)
        scored.append(item)
    scored.sort(key=lambda r: (-r.score, r.rank))
    return scored


class SearchRouter:
    """按顺序尝试多个 provider，谁先给出结果就用谁。"""

    def __init__(self, providers: Sequence[SearchProvider], config: Config):
        self.providers = list(providers)
        self.config = config
        self.errors: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def describe(self) -> str:
        if not self.providers:
            return "未配置任何搜索源（落源功能不可用）"
        return " → ".join(f"{p.label}" for p in self.providers)

    def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        """依次尝试各 provider，返回第一个成功且非空的结果集。"""
        limit = limit or int(self.config.get("search.max_results", 8))
        self.errors = []
        for provider in self.providers:
            try:
                results = provider.search(query, limit)
            except SearchError as exc:
                self.errors.append(str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - 任何 provider 异常都只降级
                self.errors.append(f"[{provider.name}] {type(exc).__name__}: {exc}")
                continue
            if results:
                return results
            self.errors.append(f"[{provider.name}] 没有结果")
        return []

    def find_source(
        self,
        title: str,
        platform_hint: str = "",
        extra_keywords: str = "",
    ) -> list[SearchResult]:
        """拿标题去找原视频的落地页 —— 这就是「链接挂了就搜一个」的那一步。"""
        title = (title or "").strip()
        if not title:
            return []

        queries: list[str] = []
        base = f"{title} {extra_keywords}".strip()
        queries.append(f"{base} 视频")
        if platform_hint:
            queries.append(f"{base} {platform_hint}")
        queries.append(base)

        merged: list[SearchResult] = []
        seen: set[str] = set()
        for query in dict.fromkeys(q for q in queries if q.strip()):
            for item in self.search(query):
                key = item.url.split("#", 1)[0].rstrip("/").lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)

        return rank(
            merged,
            prefer_domains=self.config.get("search.prefer_domains", []) or [],
            block_domains=self.config.get("search.block_domains", []) or [],
        )


def build_router(config: Config) -> SearchRouter:
    return SearchRouter(build_providers(config), config)
