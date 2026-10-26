"""시세·시가총액·뉴스 데이터 제공자.

기본 구현은 yfinance(시세, 시가총액, 영문 뉴스)와 Google 뉴스 RSS(한국어 뉴스)를 쓴다.
테스트나 다른 데이터 소스를 쓰려면 MarketDataProvider 프로토콜을 구현하면 된다.
"""
from __future__ import annotations

import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import pandas as pd
import requests

from .models import Market, NewsItem, Stock

log = logging.getLogger(__name__)


class MarketDataProvider(Protocol):
    def closes(self, ticker: str, days: int) -> pd.Series:
        """최근 `days` 거래일의 종가 (DatetimeIndex, 오래된 순)."""

    def market_cap(self, ticker: str) -> float | None: ...

    def news(self, stock: Stock) -> list[NewsItem]: ...


class YahooProvider:
    def __init__(self, timeout: float = 10.0) -> None:
        import yfinance as yf  # 무거운 import는 실제 사용 시점에

        self._yf = yf
        self._timeout = timeout
        self._cache: dict[tuple[str, int], pd.Series] = {}

    @staticmethod
    def _period(days: int) -> str:
        # 거래일 기준 days개를 확보하려면 달력일로 여유 있게 받는다.
        return f"{int(days * 1.6) + 10}d"

    def prefetch(self, tickers: list[str], days: int) -> None:
        """여러 종목 시세를 한 번에 받아 둔다 (종목별 요청보다 훨씬 빠름)."""
        try:
            df = self._yf.download(
                tickers, period=self._period(days), auto_adjust=True,
                group_by="ticker", progress=False, threads=True,
            )
        except Exception as e:
            log.warning("일괄 시세 조회 실패: %s", e)
            return
        for t in tickers:
            if t not in df.columns.get_level_values(0):
                continue
            s = df[t]["Close"].dropna()
            if s.empty:
                continue
            s.index = pd.to_datetime(s.index).tz_localize(None)
            self._cache[(t, days)] = s.iloc[-days:]

    def closes(self, ticker: str, days: int) -> pd.Series:
        if (ticker, days) in self._cache:
            return self._cache[(ticker, days)]
        try:
            df = self._yf.Ticker(ticker).history(period=self._period(days), auto_adjust=True)
        except Exception as e:
            log.warning("시세 조회 실패 %s: %s", ticker, e)
            return pd.Series(dtype=float)
        if df.empty:
            return pd.Series(dtype=float)
        s = df["Close"].dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.iloc[-days:]

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
                published = datetime.fromtimestamp(c["providerPublishTime"])
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
        out = []
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
        return out[:30]
