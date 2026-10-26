from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Market(str, Enum):
    KOSPI = "KOSPI"
    US = "US"


class DropCause(str, Enum):
    EVENT = "EVENT"                    # 특정 사건(실적 쇼크, 소송, 규제 등)으로 인한 하락
    MARKET = "MARKET"                  # 시장 전반 하락에 동반
    NO_CLEAR_REASON = "NO_CLEAR_REASON"  # 확인된 사건 없이 개별적으로 하락

    @property
    def label(self) -> str:
        return {
            DropCause.EVENT: "사건성 하락",
            DropCause.MARKET: "시장 전반 하락",
            DropCause.NO_CLEAR_REASON: "뚜렷한 이유 없는 하락",
        }[self]


@dataclass(frozen=True)
class Stock:
    ticker: str
    name: str
    market: Market


@dataclass
class NewsItem:
    title: str
    url: str = ""
    source: str = ""
    published: datetime | None = None
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class DropAnalysis:
    cause: DropCause
    summary: str
    index_return: float               # 고점 이후 벤치마크 지수 수익률
    excess_return: float              # 종목 수익률 - 지수 수익률
    worst_day_return: float           # 고점 이후 가장 크게 빠진 하루 수익률
    worst_day: datetime | None
    event_news: list[NewsItem] = field(default_factory=list)
    ai_explanation: str | None = None


@dataclass
class Recommendation:
    stock: Stock
    price: float
    peak_price: float
    peak_date: datetime
    drawdown: float                   # 음수, 예: -0.25 = 고점 대비 25% 하락
    market_cap: float | None
    currency: str
    analysis: DropAnalysis
    news: list[NewsItem] = field(default_factory=list)
    closes: Any = None                # 기간 내 종가 (pandas.Series), 차트용
