"""하락 원인 판별.

세 가지 신호를 조합한다.
1. 같은 기간 벤치마크 지수가 얼마나 빠졌는가 → 시장 전반 하락인지
2. 고점 이후 하루에 크게 빠진 날(갭 하락)이 있는가 → 사건이 터졌는지
3. 고점 이후 뉴스 제목에 악재 표현이 있는가 → 무슨 사건인지
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from .config import ScreenConfig
from .models import DropAnalysis, DropCause, NewsItem

# 하락을 설명하는 악재 표현. 중립적인 단어("FDA", "규제", "가이던스")만으로는 잡지 않고
# 부정적인 맥락이 붙은 표현만 쓴다. 영어는 정규식(대소문자 무시, 단어 경계).
_KO: dict[str, list[str]] = {
    "실적 부진": ["어닝쇼크", "어닝 쇼크", "실적 쇼크", "실적 부진", "적자 전환", "적자전환",
               "영업손실", "어닝 미스", "컨센서스 하회", "기대 하회"],
    "전망 하향": ["가이던스 하향", "전망 하향", "목표가 하향", "목표주가 하향", "투자의견 하향"],
    "소송·수사": ["피소", "소송 패소", "집단소송", "압수수색", "검찰 수사", "기소", "과징금", "제재"],
    "임상·인허가 실패": ["임상 실패", "임상 중단", "허가 거절", "승인 거절"],
    "주식 희석": ["유상증자", "전환사채", "블록딜"],
    "경영 리스크": ["횡령", "배임", "상장폐지", "대표 사임", "대표 사퇴", "분식"],
    "사고·운영 차질": ["리콜", "화재 사고", "공장 화재", "공장 불", "폭발 사고", "파업", "해킹", "개인정보 유출", "가동 중단"],
    "무역 규제": ["관세 부과", "관세 폭탄", "수출 규제", "수출 통제"],
    "계약 해지": ["계약 해지", "수주 취소", "공급 중단"],
}
_EN: dict[str, list[str]] = {
    "실적 부진": [r"earnings miss", r"miss(es|ed)? (estimates|expectations|forecasts)",
               r"profit warning", r"revenue miss", r"quarterly loss"],
    "전망 하향": [r"(cuts?|lowers?|slashes?|weak) (guidance|outlook|forecast)",
               r"guidance cut", r"downgraded?s?", r"(price target|pt) cut",
               r"cuts? (price target|pt)"],
    "소송·수사": [r"lawsuit", r"sued", r"class action", r"(opens?|launches?|faces?|doj|sec|ftc|eu) probe",
               r"under investigation", r"antitrust (suit|case|probe)", r"fined", r"sec charges?"],
    "임상·인허가 실패": [r"fda (rejects?|rejection)", r"complete response letter", r"clinical hold",
                   r"trial (fails?|failed|failure)"],
    "주식 희석": [r"(stock|share|secondary) offering", r"dilution"],
    "경영 리스크": [r"fraud", r"accounting (irregularities|probe)", r"restatement",
               r"ceo (resigns|steps down|ousted|exits)"],
    "사고·운영 차질": [r"recalls?", r"recalled", r"outage", r"data breach", r"hacked",
                  r"workers? strike", r"plant (fire|explosion)"],
    "무역 규제": [r"hit by tariffs", r"tariff (hit|blow)", r"export (ban|curbs|restrictions)"],
    "계약 해지": [r"contract (cancel+ed|terminated|loss)", r"loses? (contract|key customer)"],
}

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(re.escape(kw)), label) for label, kws in _KO.items() for kw in kws
] + [
    (re.compile(rf"\b{p}\b", re.IGNORECASE), label) for label, ps in _EN.items() for p in ps
]

# 급락일과 이 기간 안에 나온 뉴스를 '핵심 근거'로 본다
KEY_NEWS_WINDOW_DAYS = 3


def match_keywords(title: str, company: str = "") -> list[str]:
    # 회사 이름 자체가 악재 표현을 품는 경우(예: 삼성'화재')를 막기 위해 이름을 지우고 본다.
    if company:
        title = title.replace(company, " ")
    return sorted({label for pattern, label in _PATTERNS if pattern.search(title)})


def analyze_drop(
    closes: pd.Series,
    index_closes: pd.Series,
    peak_date: datetime,
    news: list[NewsItem],
    config: ScreenConfig,
    company: str = "",
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

    # 날짜를 모르는 뉴스는 하락과의 관계를 판단할 수 없어 제외한다.
    recent = [n for n in news if n.published is not None and n.published >= peak_date]
    event_news = []
    for n in recent:
        kws = match_keywords(n.title, company)
        if kws:
            n.matched_keywords = kws
            event_news.append(n)
    if worst_day is not None:
        event_news.sort(key=lambda n: abs((n.published - worst_day).total_seconds()))
    else:
        event_news.sort(key=lambda n: n.published, reverse=True)
    shock_day = worst_ret <= -config.event_day_drop
    near = (
        [n for n in event_news if abs((n.published - worst_day).days) <= KEY_NEWS_WINDOW_DAYS]
        if shock_day
        else []
    )

    market_driven = index_ret <= -config.market_drop and excess > -config.excess_drop

    if market_driven:
        cause = DropCause.MARKET
        summary = (
            f"같은 기간 시장 지수도 {index_ret:.1%} 빠졌고, 이 종목은 시장보다 "
            f"{abs(min(excess, 0)):.1%}p 더 빠지는 데 그쳤습니다. 시장 전체가 내려앉으면서 함께 빠진 것으로 보입니다."
        )
    elif event_news or shock_day:
        cause = DropCause.EVENT
        labels = lambda ns: ", ".join(sorted({k for n in ns for k in n.matched_keywords}))  # noqa: E731
        lead = f"시장({index_ret:+.1%})보다 {abs(min(excess, 0)):.1%}p 더 빠졌습니다. "
        if near:
            body = (
                f"{worst_day:%m월 %d일} 하루에 {worst_ret:.1%} 급락했고, 그 무렵 악재 뉴스({labels(near)})가 나왔습니다. "
                "이 사건이 하락을 이끈 것으로 보입니다."
            )
        elif shock_day and event_news:
            body = (
                f"{worst_day:%m월 %d일} 하루에 {worst_ret:.1%} 급락했지만, 그날 무렵의 원인 뉴스는 찾지 못했습니다. "
                f"급락 이후에 관련 악재({labels(event_news)})가 보도됐습니다."
            )
        elif shock_day:
            body = (
                f"{worst_day:%m월 %d일} 하루에 {worst_ret:.1%} 급락했습니다. 무언가 사건이 있었던 것으로 보이지만 "
                "원인 뉴스는 찾지 못했습니다."
            )
        else:
            body = f"하루 급락은 없었지만 고점 이후 악재 뉴스({labels(event_news)})가 나왔습니다."
        summary = lead + body
    else:
        cause = DropCause.NO_CLEAR_REASON
        news_part = (
            f"고점 이후 뉴스 {len(recent)}건에서도 악재 표현을 찾지 못했습니다"
            if recent
            else "관련 뉴스를 찾지 못해 뉴스로는 원인을 확인할 수 없었습니다"
        )
        summary = (
            f"시장({index_ret:+.1%})보다 {abs(min(excess, 0)):.1%}p 더 빠졌지만, 하루 급락은 없었고 "
            f"{news_part}. 매도세가 이어지며 서서히 빠진 것으로 보입니다."
        )

    return DropAnalysis(
        cause=cause,
        summary=summary,
        index_return=index_ret,
        excess_return=excess,
        worst_day_return=worst_ret,
        worst_day=worst_day,
        event_news=event_news,
        news_checked=len(recent),
        key_news=len(near),
    )
