"""시세·시가총액·뉴스 데이터 제공자.

기본 구현은 yfinance(시세, 시가총액, 영문 뉴스)와 Google 뉴스 RSS(한국어 뉴스)를 쓴다.
테스트나 다른 데이터 소스를 쓰려면 MarketDataProvider 프로토콜을 구현하면 된다.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .models import Market, NewsItem, Stock

log = logging.getLogger(__name__)


class MarketDataProvider(Protocol):
    def closes(self, ticker: str, days: int) -> pd.Series:
        """최근 `days` 거래일의 종가 (DatetimeIndex, 오래된 순)."""

    def market_cap(self, ticker: str) -> float | None: ...

    def news(self, stock: Stock) -> list[NewsItem]: ...


def empty_series() -> pd.Series:
    """빈 시세. 날짜 인덱스를 유지해야 날짜 비교 코드가 오류 없이 빈 결과를 낸다."""
    return pd.Series(dtype=float, index=pd.DatetimeIndex([]))


MARKET_TZ = {Market.KOSPI: ZoneInfo("Asia/Seoul"), Market.US: ZoneInfo("America/New_York")}


class YahooProvider:
    def __init__(self, timeout: float = 5.0) -> None:
        import yfinance as yf  # 무거운 import는 실제 사용 시점에

        self._yf = yf
        self._timeout = timeout

    @staticmethod
    def _period(days: int) -> str:
        # 거래일 기준 days개를 확보하려면 달력일로 여유 있게 받는다.
        return f"{int(days * 1.6) + 10}d"

    def closes_many(self, tickers: list[str], days: int) -> dict[str, pd.Series]:
        """여러 종목 시세를 한 번에 받는다 (종목별 요청보다 훨씬 빠름)."""
        try:
            df = self._yf.download(
                tickers, period=self._period(days), auto_adjust=True,
                group_by="ticker", progress=False, threads=True, timeout=self._timeout * 4,
            )
        except Exception as e:
            log.warning("일괄 시세 조회 실패: %s", e)
            return {}
        out = {}
        for t in tickers:
            if t not in df.columns.get_level_values(0):
                continue
            s = df[t]["Close"].dropna()
            if s.empty:
                continue
            s.index = pd.to_datetime(s.index).tz_localize(None)
            out[t] = s.iloc[-days:]
        return out

    def closes(self, ticker: str, days: int) -> pd.Series:
        try:
            df = self._yf.Ticker(ticker).history(period=self._period(days), auto_adjust=True, timeout=self._timeout)
        except Exception as e:
            log.warning("시세 조회 실패 %s: %s", ticker, e)
            return empty_series()
        if df.empty:
            return empty_series()
        s = df["Close"].dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.iloc[-days:]

    def fundamentals(self, ticker: str) -> dict[str, float | None]:
        """PER·PBR. 값이 없거나 조회에 실패하면 None."""
        try:
            info = self._yf.Ticker(ticker).info or {}
        except Exception as e:
            log.warning("재무지표 조회 실패 %s: %s", ticker, e)
            return {"per": None, "pbr": None}

        def num(key: str) -> float | None:
            v = info.get(key)
            return float(v) if isinstance(v, (int, float)) and v > 0 else None

        return {"per": num("trailingPE"), "pbr": num("priceToBook")}

    def market_cap(self, ticker: str) -> float | None:
        try:
            cap = self._yf.Ticker(ticker).fast_info.get("marketCap")
            return float(cap) if cap else None
        except Exception as e:  # yfinance는 다양한 예외를 던진다
            log.warning("시가총액 조회 실패 %s: %s", ticker, e)
            return None

    def news(self, stock: Stock) -> list[NewsItem]:
        items = self._yahoo_news(stock.ticker)
        query = f"{stock.name} 주가" if stock.market is Market.KOSPI else f"{stock.name} stock"
        lang = ("ko", "KR") if stock.market is Market.KOSPI else ("en", "US")
        items += self._google_news(query, *lang)
        # 뉴스 시각(UTC)을 그 시장의 현지 시각으로 바꾼다. 시세 날짜가 거래소 현지 날짜라서
        # 같은 기준이어야 '고점 이후', '급락일 무렵' 판정과 화면의 날짜가 맞는다.
        tz = MARKET_TZ[stock.market]
        for n in items:
            if n.published is not None:
                n.published = n.published.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
        return items

    def _yahoo_news(self, ticker: str) -> list[NewsItem]:
        try:
            raw = self._yf.Ticker(ticker).news or []
        except Exception as e:
            log.warning("Yahoo 뉴스 조회 실패 %s: %s", ticker, e)
            return []
        out = []
        for r in raw:
            c = r.get("content", r)  # yfinance 버전에 따라 구조가 다르다
            title = c.get("title")
            if not title:
                continue
            url = (c.get("canonicalUrl") or {}).get("url") or c.get("link", "")
            source = (c.get("provider") or {}).get("displayName") or c.get("publisher", "")
            published = None
            if c.get("pubDate"):
                published = pd.to_datetime(c["pubDate"]).tz_localize(None).to_pydatetime()
            elif c.get("providerPublishTime"):
                # 다른 뉴스와 같은 기준(UTC, 시간대 정보 없음)으로 맞춘다.
                published = datetime.fromtimestamp(c["providerPublishTime"], tz=timezone.utc).replace(tzinfo=None)
            out.append(NewsItem(title=title, url=url, source=source, published=published))
        return out

    def _google_news(self, query: str, hl: str, gl: str) -> list[NewsItem]:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + f"&hl={hl}&gl={gl}&ceid={gl}:{hl}"
        )
        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            log.warning("Google 뉴스 조회 실패 %s: %s", query, e)
            return []
        out: list[NewsItem] = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            published = None
            if item.findtext("pubDate"):
                published = parsedate_to_datetime(item.findtext("pubDate")).replace(tzinfo=None)
            out.append(
                NewsItem(
                    title=title,
                    url=item.findtext("link") or "",
                    source=item.findtext("source") or "Google News",
                    published=published,
                )
            )
        # 최신 뉴스 30건만 쓴다 (RSS는 날짜순이 아닐 수 있다).
        out.sort(key=lambda n: n.published or datetime.min, reverse=True)
        return out[:30]


class CachingProvider:
    """다른 제공자를 감싸서 결과를 일정 시간(ttl) 동안 재사용한다.

    - 시세는 history_days만큼 한 번에 받아 두고, 더 짧은 기간은 잘라서 돌려준다.
      그래서 하락률·기간 조건을 바꿔도 다시 내려받지 않는다.
    - 시가총액·뉴스·재무지표는 종목별로 캐시하고, warm()으로 병렬 조회한다.
    여러 사용자가 한 인스턴스를 같이 써도 되도록 잠금을 건다.
    """

    def __init__(
        self, base, ttl: float = 3600, history_days: int = 260, workers: int = 8, retry_ttl: float = 300
    ) -> None:
        self.base = base
        self.ttl = ttl
        self.retry_ttl = retry_ttl
        self.history_days = history_days
        self.workers = workers
        self._lock = threading.Lock()
        self._prices: dict[str, tuple[float, pd.Series]] = {}
        self._failed: dict[str, float] = {}  # 시세를 못 받은 종목 → 다시 시도할 시각
        # 여러 사용자가 동시에 들어와도 같은 시세를 한 번만 받도록, 받는 동안 다른 요청은 기다린다.
        self._load_lock = threading.Lock()
        self.version = 0  # 시세를 새로 받거나 버릴 때마다 올라간다 (분석 결과 캐시를 무효화하는 데 쓴다)
        self._details: dict[tuple[str, str], tuple[float, object]] = {}

    def _fresh(self, stamp: float) -> bool:
        return time.time() - stamp < self.ttl

    def clear(self) -> None:
        with self._lock:
            self._prices.clear()
            self._details.clear()
            self._failed.clear()
            self.version += 1

    def refresh_stale(self, min_age: float = 600) -> None:
        """min_age초보다 오래된 데이터만 버린다. 누군가 '새로 불러오기'를 연타해도
        방금 받은 데이터까지 지워 모두가 다시 기다리는 일이 없게 한다."""
        cutoff = time.time() - min_age
        with self._lock:
            self._prices = {t: v for t, v in self._prices.items() if v[0] >= cutoff}
            self._details = {k: v for k, v in self._details.items() if v[0] - self.ttl >= cutoff}
            self._failed.clear()
            self.version += 1

    def loaded_at(self, tickers: list[str]) -> float | None:
        """해당 종목들 시세를 받은 시각 중 가장 이른 시각 (epoch초)."""
        with self._lock:
            stamps = [self._prices[t][0] for t in tickers if t in self._prices]
        return min(stamps) if stamps else None

    def _can_retry(self, ticker: str) -> bool:
        return time.time() >= self._failed.get(ticker, 0)

    # ---- 시세 ----
    def load_prices(self, tickers: list[str]) -> None:
        """캐시에 없거나 오래된 종목만 한 번에 받아 둔다."""
        with self._load_lock:
            self._load_prices(tickers)

    def _load_prices(self, tickers: list[str]) -> None:
        with self._lock:
            missing = [
                t for t in tickers
                if (t not in self._prices or not self._fresh(self._prices[t][0])) and self._can_retry(t)
            ]
        if not missing:
            return
        many = getattr(self.base, "closes_many", None)
        got = many(missing, self.history_days) if many else {}
        # 일괄 조회에서 빠진 종목은 한 번만, 병렬로 따로 받아 본다.
        rest = [t for t in missing if t not in got or got[t].empty]
        if rest:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                got.update(zip(rest, pool.map(lambda t: self.base.closes(t, self.history_days), rest)))
        now = time.time()
        with self._lock:
            self.version += 1
            for t in missing:
                s = got.get(t)
                if s is not None and not s.empty:
                    self._prices[t] = (now, s)
                    self._failed.pop(t, None)
                else:
                    # 실패한 종목은 잠깐 기억해서 화면을 누를 때마다 다시 요청하지 않는다.
                    self._failed[t] = now + self.retry_ttl

    def closes(self, ticker: str, days: int) -> pd.Series:
        with self._lock:
            hit = self._prices.get(ticker)
        if hit is None or not self._fresh(hit[0]):
            s = self.base.closes(ticker, self.history_days) if self._can_retry(ticker) else empty_series()
            if s.empty:
                with self._lock:
                    self._failed.setdefault(ticker, time.time() + self.retry_ttl)
                # 다시 받기에 실패하면 예전 시세라도 쓴다 (기준일은 as_of로 드러난다).
                return hit[1].iloc[-days:] if hit else s
            hit = (time.time(), s)
            with self._lock:
                self._prices[ticker] = hit
                self._failed.pop(ticker, None)
        return hit[1].iloc[-days:]

    def as_of(self, tickers: list[str] | None = None) -> datetime | None:
        """시세 기준일. tickers를 주면 그 종목들(보통 시장 지수) 중 가장 오래된 마지막 거래일을 돌려줘
        한 시장만 갱신에 실패했을 때 기준일이 실제보다 새로워 보이지 않게 한다."""
        with self._lock:
            if tickers is None:
                dates = [s.index[-1] for _, s in self._prices.values() if not s.empty]
                return max(dates).to_pydatetime() if dates else None
            series = [self._prices.get(t) for t in tickers]
        if any(hit is None or hit[1].empty for hit in series):
            return None
        return min(hit[1].index[-1] for hit in series).to_pydatetime()

    # ---- 종목별 상세 ----
    def _detail(self, kind: str, ticker: str, fetch):
        key = (kind, ticker)
        with self._lock:
            hit = self._details.get(key)
        if hit is not None and time.time() < hit[0]:
            return hit[1]
        value = fetch()
        # 실패(None, 빈 목록)는 잠깐만 기억해서 곧 다시 시도한다.
        ttl = self.ttl if value else self.retry_ttl
        with self._lock:
            self._details[key] = (time.time() + ttl, value)
        return value

    def market_cap(self, ticker: str) -> float | None:
        return self._detail("cap", ticker, lambda: self.base.market_cap(ticker))

    def news(self, stock: Stock) -> list[NewsItem]:
        return self._detail("news", stock.ticker, lambda: self.base.news(stock))

    def fundamentals(self, ticker: str) -> dict[str, float | None]:
        fetch = getattr(self.base, "fundamentals", None)
        if fetch is None:
            return {"per": None, "pbr": None}
        # 값이 하나도 없으면 실패로 보고 짧게만 캐시한다.
        def get() -> dict:
            got = fetch(ticker)
            return got if any(v is not None for v in got.values()) else {}

        return self._detail("fund", ticker, get) or {"per": None, "pbr": None}

    def warm(
        self,
        stocks: list[Stock],
        on_progress: Callable[[int, int], None] | None = None,
        kinds: tuple[str, ...] = ("cap", "news", "fund"),
    ) -> None:
        """종목들의 시가총액·뉴스·재무지표 중 kinds에 해당하는 것을 병렬로 받아 둔다."""
        fetchers = {
            "cap": lambda s: self.market_cap(s.ticker),
            "news": self.news,
            "fund": lambda s: self.fundamentals(s.ticker),
        }
        jobs = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for s in stocks:
                jobs += [pool.submit(fetchers[k], s) for k in kinds]
            for i, _ in enumerate(as_completed(jobs), 1):
                if on_progress:
                    on_progress(i, len(jobs))
