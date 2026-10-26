"""실제 YahooProvider 코드를 가짜 yfinance 모듈로 돌려 본다 (네트워크 없음)."""
import sys
import types

import pandas as pd
import pytest

from stock_picker.data import CachingProvider, YahooProvider
from stock_picker.models import Market, Stock

DATES = pd.bdate_range(end="2026-10-23", periods=260)
calls = {"download": 0, "history": 0, "info": 0}


def _series(t):
    return pd.Series(range(100, 100 + len(DATES)), index=DATES, dtype=float)


def _download(tickers, **kw):
    calls["download"] += 1
    return pd.concat({t: pd.DataFrame({"Open": _series(t), "Close": _series(t)}) for t in tickers}, axis=1)


class _Ticker:
    def __init__(self, t):
        self.t = t

    def history(self, **kw):
        calls["history"] += 1
        df = pd.DataFrame({"Close": _series(self.t)})
        df.index = df.index.tz_localize("America/New_York")
        return df

    fast_info = {"marketCap": 2e11}
    info = {"trailingPE": 15.5, "priceToBook": -1}
    news = [
        {"content": {"title": "Acme misses estimates", "pubDate": "2026-10-01T12:00:00Z",
                     "canonicalUrl": {"url": "https://x"}, "provider": {"displayName": "Wire"}}},
        {"title": "Old format item", "providerPublishTime": 1790000000, "link": "https://y", "publisher": "Old"},
    ]

    def __getattribute__(self, name):
        if name == "info":
            calls["info"] += 1
        return object.__getattribute__(self, name)


@pytest.fixture
def provider(monkeypatch):
    fake = types.SimpleNamespace(download=_download, Ticker=_Ticker)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    for k in calls:
        calls[k] = 0
    return YahooProvider()


def test_provider_has_all_methods():
    for m in ("closes_many", "closes", "fundamentals", "market_cap", "news"):
        assert callable(getattr(YahooProvider, m, None)), m


def test_closes_many_parses_grouped_download(provider):
    got = provider.closes_many(["AAA", "BBB"], 60)
    assert set(got) == {"AAA", "BBB"}
    assert len(got["AAA"]) == 60 and got["AAA"].index.tz is None


def test_fundamentals_ignores_invalid_values(provider):
    assert provider.fundamentals("AAA") == {"per": 15.5, "pbr": None}


def test_news_parses_both_formats_as_naive_utc(provider, monkeypatch):
    monkeypatch.setattr(provider, "_google_news", lambda *a: [])
    items = provider.news(Stock("AAA", "Acme", Market.US))
    assert [n.title for n in items] == ["Acme misses estimates", "Old format item"]
    assert all(n.published.tzinfo is None for n in items)
    assert items[0].published.hour == 12


def test_caching_provider_uses_one_bulk_download(provider):
    cp = CachingProvider(provider)
    cp.load_prices(["AAA", "BBB", "CCC"])
    cp.closes("AAA", 60)
    cp.closes("BBB", 250)
    assert calls == {"download": 1, "history": 0, "info": 0}
    cp.fundamentals("AAA")
    cp.fundamentals("AAA")
    assert calls["info"] == 1
