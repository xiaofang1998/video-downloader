"""搜索结果的数据结构与 provider 抽象。"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..config import Config
from ..http import parse_proxy_spec


@dataclass
class SearchResult:
    title: str = ""
    url: str = ""
    snippet: str = ""
    provider: str = ""
    rank: int = 0
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        try:
            return (urlsplit(self.url).hostname or "").lower()
        except ValueError:
            return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "provider": self.provider,
            "rank": self.rank,
            "score": self.score,
            "domain": self.domain,
        }


class SearchProvider(abc.ABC):
    """一个搜索引擎。子类只需实现 :meth:`search` 和 :attr:`available`。"""

    name: str = "base"
    label: str = "搜索"

    def __init__(self, config: Config):
        self.config = config
        self.timeout = float(config.get("net.timeout_sec", 15))
        self.user_agent = str(config.get("net.user_agent", ""))
        # None = 跟随系统代理；"" = 直连；其余 = 指定代理
        self.proxy = parse_proxy_spec(config.get("net.proxy"))
        self.verify_tls = bool(config.get("net.verify_tls", True))

    @property
    def available(self) -> bool:
        """当前配置下能不能用（比如有没有 API key）。"""
        return False

    @abc.abstractmethod
    def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        """执行搜索。失败时抛 :class:`SearchError`，不要返回半成品。"""


class SearchError(RuntimeError):
    """搜索失败（无 key、被限流、网络不通、解析不出结果…）。"""

    def __init__(self, provider: str, message: str):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message
