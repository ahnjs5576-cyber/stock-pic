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
    assert match_keywords("Company fined $2B by EU") == ["소송·수사"]
    assert match_keywords("Apple lowers guidance") == ["전망 하향"]


def test_neutral_or_positive_headlines_are_not_events():
    assert match_keywords("FDA clears new drug") == []
    assert match_keywords("규제 완화 기대감") == []
    assert match_keywords("흑자 전환, 적자 탈출") == []
    assert match_keywords("지금 사고 싶다면") == []


def test_company_name_is_ignored_when_matching():
    # '삼성화재'의 '화재'를 사고로 보면 안 된다
    assert match_keywords("삼성화재, 3분기 순이익 증가", "삼성화재") == []
    assert match_keywords("삼성화재 공장 화재 사고", "삼성화재") == ["사고·운영 차질"]


def test_unknown_market_cap_is_excluded():
    class NoCap(FakeProvider):
        def market_cap(self, ticker):
            return None

    p = NoCap({"A.KS": gradual(end=70), "^KS11": flat()})
    assert screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1]) == []


def test_undated_news_is_ignored():
    news = [NewsItem("갑 어닝쇼크", published=None)]
    p = FakeProvider({"A.KS": gradual(end=70), "^KS11": flat()}, news={"A.KS": news})
    [rec] = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    assert rec.analysis.cause is DropCause.NO_CLEAR_REASON
    assert rec.analysis.news_checked == 0


def test_buy_signals_are_computed():
    p = FakeProvider({"A.KS": gap_down(), "^KS11": flat()})
    [rec] = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    assert rec.low_since_peak == 75
    assert rec.rebound == 0
    assert rec.trend_5d == 0
    assert rec.high_52w == 100 and rec.low_52w == 75


def test_caching_provider_fetches_once_and_slices():
    from stock_picker.data import CachingProvider

    calls = {"closes": 0, "news": 0}

    class Counting(FakeProvider):
        def closes(self, ticker, days):
            calls["closes"] += 1
            return super().closes(ticker, days)

        def news(self, stock):
            calls["news"] += 1
            return []

    cp = CachingProvider(Counting({"A.KS": gradual(end=70), "^KS11": flat()}), history_days=60)
    for drop in (0.2, 0.25, 0.1):
        screen(cp, [Market.KOSPI], ScreenConfig(drop_threshold=drop, lookback_days=30), stocks=KR[:1])
    assert calls["closes"] == 2  # 종목 + 지수, 처음 한 번씩만
    assert calls["news"] == 1
    assert len(cp.closes("A.KS", 30)) == 30


def test_reports_render():
    p = FakeProvider({"A.KS": gap_down(), "^KS11": flat()})
    recs = screen(p, [Market.KOSPI], ScreenConfig(), stocks=KR[:1])
    md = to_markdown(recs)
    assert "사건성 하락" in md and "투자 권유가 아닙니다" in md
    assert '"cause": "EVENT"' in to_json(recs)
