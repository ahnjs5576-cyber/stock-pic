"""하락 원인 판별.

세 가지 신호를 조합한다.
1. 같은 기간 벤치마크 지수가 얼마나 빠졌는가 → 시장 전반 하락인지
2. 고점 이후 하루에 크게 빠진 날(갭 하락)이 있는가 → 사건이 터졌는지
3. 기간 내 뉴스 제목에 사건성 키워드가 있는가 → 무슨 사건인지
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from .config import ScreenConfig
from .models import DropAnalysis, DropCause, NewsItem

# 하락을 설명할 수 있는 사건성 키워드 (한국어 / 영어)
EVENT_KEYWORDS: dict[str, str] = {
    # 실적·가이던스
    "어닝쇼크": "실적 부진", "실적 부진": "실적 부진", "적자": "실적 부진",
    "영업손실": "실적 부진", "실적 쇼크": "실적 부진", "가이던스": "가이던스 변경",
    "목표가 하향": "목표가 하향", "투자의견 하향": "투자의견 하향",
    "earnings miss": "실적 부진", "misses": "실적 부진", "missed estimates": "실적 부진",
    "guidance cut": "가이던스 하향", "lowers guidance": "가이던스 하향",
    "cuts outlook": "가이던스 하향", "weak outlook": "가이던스 하향",
    "downgrade": "투자의견 하향", "price target cut": "목표가 하향",
    # 법률·규제
    "소송": "소송", "제재": "규제·제재", "과징금": "규제·제재", "압수수색": "수사",
    "수사": "수사", "기소": "수사", "공정위": "규제·제재", "규제": "규제·제재",
    "lawsuit": "소송", "sued": "소송", "probe": "수사", "investigation": "수사",
    "antitrust": "규제·제재", "fined": "규제·제재", "sec charges": "수사",
    "fda rejects": "인허가 실패", "fda": "인허가 이슈", "임상 실패": "임상 실패",
    # 자본·지배구조
    "유상증자": "유상증자(희석)", "전환사채": "CB 발행(희석)", "블록딜": "대량 매도",
    "횡령": "횡령·배임", "배임": "횡령·배임", "상장폐지": "상장폐지 우려",
    "offering": "증자(희석)", "dilution": "증자(희석)", "fraud": "회계·부정",
    "accounting": "회계 이슈", "resigns": "경영진 사임", "ceo steps down": "경영진 사임",
    "사임": "경영진 사임", "대표 사퇴": "경영진 사임",
    # 사업·운영
    "리콜": "리콜", "recall": "리콜", "화재": "사고", "사고": "사고", "파업": "파업",
    "workers strike": "파업", "outage": "장애", "hacked": "보안 사고",
    "data breach": "보안 사고", "해킹": "보안 사고",
    "관세": "관세", "tariff": "관세", "수출 규제": "수출 규제", "export ban": "수출 규제",
    "계약 해지": "계약 해지", "수주 취소": "계약 해지", "layoffs": "구조조정",
}


_PATTERNS = [
    # 영어 키워드는 단어 경계로 매칭 ("fined"가 "defined"에 걸리지 않도록)
    (re.compile(rf"\b{re.escape(kw)}\b" if kw.isascii() else re.escape(kw), re.IGNORECASE), label)
    for kw, label in EVENT_KEYWORDS.items()
]


def match_keywords(title: str) -> list[str]:
    return sorted({label for pattern, label in _PATTERNS if pattern.search(title)})


def analyze_drop(
    closes: pd.Series,
    index_closes: pd.Series,
    peak_date: datetime,
    news: list[NewsItem],
    config: ScreenConfig,
) -> DropAnalysis:
    since_peak = closes[closes.index >= peak_date]
    stock_ret = since_peak.iloc[-1] / since_peak.iloc[0] - 1

    idx = index_closes[index_closes.index >= peak_date]
    index_ret = float(idx.iloc[-1] / idx.iloc[0] - 1) if len(idx) >= 2 else 0.0
    excess = float(stock_ret - index_ret)

    daily = since_peak.pct_change().dropna()
    if daily.empty:
        worst_ret, worst_day = 0.0, None
    else:
        worst_day = daily.idxmin().to_pydatetime()
        worst_ret = float(daily.min())

    event_news = []
    for n in news:
        if n.published is not None and n.published < peak_date:
            continue
        kws = match_keywords(n.title)
        if kws:
            n.matched_keywords = kws
            event_news.append(n)

    market_driven = index_ret <= -config.market_drop and excess > -config.excess_drop
    shock_day = worst_ret <= -config.event_day_drop

    if market_driven:
        cause = DropCause.MARKET
        summary = (
            f"같은 기간 지수도 {index_ret:.1%} 하락했고 종목의 초과 하락은 {excess:+.1%}p에 그쳐, "
            "시장 전반의 하락에 동반된 것으로 보입니다."
        )
    elif event_news or shock_day:
        cause = DropCause.EVENT
        parts = []
        if shock_day:
            parts.append(f"{worst_day:%Y-%m-%d}에 하루 {worst_ret:.1%} 급락")
        if event_news:
            labels = sorted({k for n in event_news for k in n.matched_keywords})
            parts.append("관련 뉴스: " + ", ".join(labels))
        summary = (
            f"지수({index_ret:+.1%}) 대비 {excess:+.1%}p 더 빠졌고, "
            + " / ".join(parts)
            + ". 특정 사건이 하락을 이끈 것으로 보입니다."
        )
    else:
        cause = DropCause.NO_CLEAR_REASON
        summary = (
            f"지수({index_ret:+.1%}) 대비 {excess:+.1%}p 하락했지만 하루 급락이나 "
            "사건성 뉴스가 확인되지 않았습니다. 수급·심리에 의한 점진적 하락일 수 있습니다."
        )

    return DropAnalysis(
        cause=cause,
        summary=summary,
        index_return=index_ret,
        excess_return=excess,
        worst_day_return=worst_ret,
        worst_day=worst_day,
        event_news=event_news,
    )
