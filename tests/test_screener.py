from datetime import datetime

import pandas as pd

from stock_picker.analysis import match_keywords
from stock_picker.config import ScreenConfig
from stock_picker.models import DropCause, Market, NewsItem, Stock
from stock_picker.report import to_json, to_markdown
from stock_picker.screener import screen

DATES = pd.bdate_range("2026-07-01", periods=60)


def series(values):
    return pd.Series(values, index=DATES[: len(values)], dtype=float)


def flat(n=60, v=100.0):
    return series([v] * n)


def gradual(start=100.0, end=70.0, n=60):
    return series([start + (end - start) * i / (n - 1) for i in range(n)])


def gap_down(n=60):
    vals = [100.0] * 30 + [75.0] * (n - 30)  # 30일째 하루 -25%
    return series(vals)


class FakeProvider:
    def __init__(self, closes, caps=None, news=None):
        self._closes, self._caps, self._news = closes, caps or {}, news or {}

    def closes(self, ticker, days):
        return self._closes[ticker].iloc[-days:]

    def market_cap(self, ticker):
        return self._caps.get(ticker, 1e15)

    def news(self, stock):
        return list(self._news.get(stock.ticker, []))


KR = [Stock("A.KS", "갑", Market.KOSPI), Stock("B.KS", "을", Market.KOSPI)]


def test_only_stocks_down_20pct_are_recommended():
    p = FakeProvider({"A.KS": gradual(end=85), "B.KS": gradual(end=75), "^KS11": flat()})
    recs = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR)
    assert [r.stock.ticker for r in recs] == ["B.KS"]
    assert round(recs[0].drawdown, 2) == -0.25


def test_small_caps_are_excluded():
    p = FakeProvider(
        {"A.KS": gradual(end=70), "B.KS": gradual(end=70), "^KS11": flat()},
        caps={"A.KS": 1e12},  # 1조원 < 5조원 기준
    )
    recs = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR)
    assert [r.stock.ticker for r in recs] == ["B.KS"]


def test_cause_no_clear_reason_for_gradual_drop_without_news():
    p = FakeProvider({"A.KS": gradual(end=70), "^KS11": flat()})
    [rec] = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    assert rec.analysis.cause is DropCause.NO_CLEAR_REASON


def test_cause_event_for_gap_down():
    p = FakeProvider({"A.KS": gap_down(), "^KS11": flat()})
    [rec] = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    assert rec.analysis.cause is DropCause.EVENT
    assert rec.analysis.worst_day_return < -0.2


def test_cause_event_from_news_keywords():
    news = [NewsItem("갑, 3분기 어닝쇼크… 영업손실 전환", published=datetime(2026, 8, 20))]
    p = FakeProvider({"A.KS": gradual(end=70), "^KS11": flat()}, news={"A.KS": news})
    [rec] = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    assert rec.analysis.cause is DropCause.EVENT
    assert "실적 부진" in rec.analysis.event_news[0].matched_keywords


def test_news_before_peak_is_ignored():
    news = [NewsItem("갑 소송 제기", published=datetime(2026, 1, 1))]
    p = FakeProvider({"A.KS": gradual(end=70), "^KS11": flat()}, news={"A.KS": news})
    [rec] = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    assert rec.analysis.cause is DropCause.NO_CLEAR_REASON


def test_cause_market_when_index_also_fell():
    p = FakeProvider({"A.KS": gradual(end=75), "^KS11": gradual(end=82)})
    [rec] = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    assert rec.analysis.cause is DropCause.MARKET


def test_keyword_matching_uses_word_boundaries():
    assert match_keywords("Company defined new strategy") == []
    assert match_keywords("Company fined $2B by EU") == ["규제·제재"]
    assert match_keywords("Apple lowers guidance") == ["가이던스 하향"]


def test_reports_render():
    p = FakeProvider({"A.KS": gap_down(), "^KS11": flat()})
    recs = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    md = to_markdown(recs)
    assert "사건성 하락" in md and "투자 권유가 아닙니다" in md
    assert '"cause": "EVENT"' in to_json(recs)
