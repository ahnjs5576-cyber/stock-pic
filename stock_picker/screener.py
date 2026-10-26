from __future__ import annotations

import logging

import pandas as pd

from .analysis import analyze_drop
from .config import ScreenConfig
from .data import MarketDataProvider
from .models import Market, Recommendation, Stock
from .universe import BENCHMARKS, CURRENCY, get_universe

log = logging.getLogger(__name__)


def drawdown_from_peak(closes: pd.Series) -> tuple[float, pd.Timestamp, float]:
    """(고점 대비 하락률, 고점 날짜, 고점 가격)"""
    peak_date = closes.idxmax()
    peak = float(closes.loc[peak_date])
    return float(closes.iloc[-1] / peak - 1), peak_date, peak


def screen(
    provider: MarketDataProvider,
    markets: list[Market],
    config: ScreenConfig | None = None,
    stocks: list[Stock] | None = None,
) -> list[Recommendation]:
    config = config or ScreenConfig()
    stocks = stocks if stocks is not None else get_universe(markets)

    index_cache: dict[Market, pd.Series] = {}
    results: list[Recommendation] = []

    for stock in stocks:
        try:
            closes = provider.closes(stock.ticker, config.lookback_days)
        except Exception as e:
            log.warning("시세 조회 실패 %s: %s", stock.ticker, e)
            continue
        if len(closes) < 5:
            continue

        dd, peak_date, peak = drawdown_from_peak(closes)
        if dd > -config.drop_threshold:
            continue

        cap = provider.market_cap(stock.ticker)
        if cap is not None and cap < config.min_market_cap[stock.market]:
            log.info("대형주 기준 미달로 제외: %s (%.3g)", stock.name, cap)
            continue

        if stock.market not in index_cache:
            index_cache[stock.market] = provider.closes(
                BENCHMARKS[stock.market], config.lookback_days
            )
        news = provider.news(stock)
        analysis = analyze_drop(
            closes, index_cache[stock.market], peak_date.to_pydatetime(), news, config
        )
        results.append(
            Recommendation(
                stock=stock,
                price=float(closes.iloc[-1]),
                peak_price=peak,
                peak_date=peak_date.to_pydatetime(),
                drawdown=dd,
                market_cap=cap,
                currency=CURRENCY[stock.market],
                analysis=analysis,
                news=news,
            )
        )

    results.sort(key=lambda r: r.drawdown)
    return results
