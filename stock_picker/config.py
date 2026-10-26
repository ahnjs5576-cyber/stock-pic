from __future__ import annotations

from dataclasses import dataclass

from .models import Market


@dataclass
class ScreenConfig:
    # 고점 대비 이 비율 이상 하락한 종목만 추천 (0.20 = 20%)
    drop_threshold: float = 0.20
    # "최근"의 범위: 최근 N 거래일 중 최고 종가를 고점으로 본다 (약 3개월)
    lookback_days: int = 60
    # 대형주 기준 시가총액 (각 시장 통화 기준)
    min_market_cap: dict[Market, float] | None = None
    # 하루에 이만큼 이상 빠지면 사건성 급락으로 본다
    event_day_drop: float = 0.08
    # 지수가 이만큼 이상 빠졌으면 시장 전반 하락으로 본다
    market_drop: float = 0.10
    # 종목이 지수 대비 이 이상 더 빠졌으면 개별 요인으로 본다
    excess_drop: float = 0.10

    def __post_init__(self) -> None:
        if self.min_market_cap is None:
            self.min_market_cap = {
                Market.KOSPI: 5e12,   # 5조원
                Market.US: 10e9,      # 100억 달러 (S&P 기준 대형주)
            }
