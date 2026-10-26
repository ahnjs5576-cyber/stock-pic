from __future__ import annotations

import logging
from collections.abc import Callable

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
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Recommendation]:
    config = config or ScreenConfig()
    stocks = stocks if stocks is not None else get_universe(markets)

    load = getattr(provider, "load_prices", None)
    if load:
        load([s.ticker for s in stocks] + [BENCHMARKS[m] for m in markets])

    # 1단계: 시세만으로 하락 종목을 고른다 (네트워크 호출 없음).
    candidates: list[tuple[Stock, pd.Series]] = []
    for stock in stocks:
        try:
            closes = provider.closes(stock.ticker, config.lookback_days)
        except Exception as e:
            log.warning("시세 조회 실패 %s: %s", stock.ticker, e)
            continue
        if len(closes) < 5:
            continue
        if drawdown_from_peak(closes)[0] <= -config.drop_threshold:
            candidates.append((stock, closes))

    # 2단계: 후보만 시가총액·뉴스·재무지표를 받는다 (가능하면 병렬).
    warm = getattr(provider, "warm", None)
    if warm:
        warm([s for s, _ in candidates], on_progress)

    index_cache: dict[Market, pd.Series] = {}
    results: list[Recommendation] = []
    for stock, closes in candidates:
        cap = provider.market_cap(stock.ticker)
        # 시가총액을 확인하지 못하면 대형주인지 알 수 없으므로 추천하지 않는다.
        if cap is None or cap < config.min_market_cap[stock.market]:
            log.info("대형주 기준 미달 또는 확인 불가로 제외: %s (%s)", stock.name, cap)
            continue

        dd, peak_date, peak = drawdown_from_peak(closes)
        if stock.market not in index_cache:
            index_cache[stock.market] = provider.closes(BENCHMARKS[stock.market], config.lookback_days)
        news = provider.news(stock)
        analysis = analyze_drop(
            closes, index_cache[stock.market], peak_date.to_pydatetime(), news, config, stock.name
        )

        price = float(closes.iloc[-1])
        low = float(closes[closes.index >= peak_date].min())
        year = provider.closes(stock.ticker, 250)
        fetch_fund = getattr(provider, "fundamentals", None)
        fund = fetch_fund(stock.ticker) if fetch_fund else {}
        results.append(
            Recommendation(
                stock=stock,
                price=price,
                peak_price=peak,
                peak_date=peak_date.to_pydatetime(),
                drawdown=dd,
                market_cap=cap,
                currency=CURRENCY[stock.market],
                analysis=analysis,
                news=news,
                closes=closes,
                low_since_peak=low,
                rebound=price / low - 1,
                trend_5d=price / float(closes.iloc[-6]) - 1 if len(closes) >= 6 else None,
                high_52w=float(year.max()) if not year.empty else None,
                low_52w=float(year.min()) if not year.empty else None,
                per=fund.get("per"),
                pbr=fund.get("pbr"),
            )
        )

    results.sort(key=lambda r: r.drawdown)
    return results
