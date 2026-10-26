from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

from .models import DropCause, Recommendation

DISCLAIMER = (
    "※ 본 결과는 정보 제공용이며 투자 권유가 아닙니다. 하락한 주식이 반드시 반등하는 것은 아니며, "
    "투자 판단과 책임은 본인에게 있습니다."
)

ADVICE = {
    "EVENT": "악재가 일회성(소송 합의, 일시적 비용 등)인지, 앞으로의 실적 전망 자체를 바꾸는 것인지 먼저 확인하세요.",
    "MARKET": "시장 전체가 빠진 경우라 시장이 회복되면 함께 오를 가능성이 있습니다. 지수 흐름을 같이 보세요.",
    "NO_CLEAR_REASON": "뉴스로 확인되는 악재 없이 빠졌습니다. 회사 가치가 그대로라면 저가 매수 기회일 수 있지만, 업황이나 실적 전망 변화는 따로 확인하세요.",
}


def buy_checks(rec: Recommendation) -> list[str]:
    """매수 전에 확인할 점: 원인별 조언 + 추세·가격 위치에 따른 신호."""
    a = rec.analysis
    notes = [ADVICE[a.cause.value]]
    if a.cause is not DropCause.MARKET and a.excess_return <= -0.10 and still_falling(rec):
        notes.append(
            f"⚠️ 시장보다 {abs(a.excess_return):.1%}p 더 빠졌고 지금도 약세가 이어지고 있습니다. "
            "이렇게 혼자 계속 약한 종목은 아직 드러나지 않은 이유가 있을 수 있으니 특히 조심하세요."
        )
    if still_falling(rec):
        notes.append("아직 하락이 멈추지 않았습니다. 한 번에 사기보다 나눠 사는 방법을 고려하세요.")
    elif rec.rebound is not None and rec.rebound >= 0.05:
        notes.append(f"고점 이후 최저가보다 {rec.rebound:.1%} 올라와, 하락이 멈추는 모습입니다.")
    return notes


def still_falling(rec: Recommendation) -> bool:
    """최근 5일 -2% 이하이거나 최근 20일 -5% 이하면 아직 하락 중으로 본다."""
    return (rec.trend_5d is not None and rec.trend_5d <= -0.02) or (
        rec.trend_20d is not None and rec.trend_20d <= -0.05
    )


def range_position(rec: Recommendation) -> float | None:
    """1년 가격 범위 안에서 현재가 위치 (0 = 1년 최저, 1 = 1년 최고)."""
    if not rec.high_52w or not rec.low_52w or rec.high_52w <= rec.low_52w:
        return None
    return (rec.price - rec.low_52w) / (rec.high_52w - rec.low_52w)


def _fmt_price(v: float, currency: str) -> str:
    return f"{v:,.0f}원" if currency == "KRW" else f"${v:,.2f}"


def _fmt_cap(v: float | None, currency: str) -> str:
    if v is None:
        return "-"
    return f"{v / 1e12:,.1f}조원" if currency == "KRW" else f"${v / 1e9:,.1f}B"


def to_markdown(recs: list[Recommendation], max_news: int = 3) -> str:
    lines = [f"# 대형주 급락 종목 추천 ({datetime.now():%Y-%m-%d})", ""]
    if not recs:
        lines += ["조건에 맞는 종목이 없습니다.", "", DISCLAIMER]
        return "\n".join(lines)

    lines += [
        "| # | 종목 | 시장 | 현재가 | 고점(날짜) | 하락률 | 시가총액 | 하락 원인 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(recs, 1):
        lines.append(
            f"| {i} | {r.stock.name} ({r.stock.ticker}) | {r.stock.market.value} "
            f"| {_fmt_price(r.price, r.currency)} "
            f"| {_fmt_price(r.peak_price, r.currency)} ({r.peak_date:%m/%d}) "
            f"| {r.drawdown:.1%} | {_fmt_cap(r.market_cap, r.currency)} | {r.analysis.cause.label} |"
        )
    lines.append("")

    for i, r in enumerate(recs, 1):
        a = r.analysis
        lines += [f"## {i}. {r.stock.name} — {a.cause.label}", "", a.summary, ""]
        if a.ai_explanation:
            lines += ["**AI 분석:** " + a.ai_explanation, ""]
        if a.event_news:
            lines.append("관련 뉴스:")
            for n in a.event_news[:max_news]:
                date = f"{n.published:%Y-%m-%d} " if n.published else ""
                link = f"[{n.title}]({n.url})" if n.url else n.title
                lines.append(f"- {date}{link} `{', '.join(n.matched_keywords)}`")
            lines.append("")
        lines += [f"> {note}" for note in buy_checks(r)] + [""]

    lines.append(DISCLAIMER)
    return "\n".join(lines)


def to_json(recs: list[Recommendation]) -> str:
    def default(o):
        if isinstance(o, datetime):
            return o.isoformat()
        if hasattr(o, "value"):
            return o.value
        raise TypeError(type(o))

    payload = []
    for r in recs:
        d = asdict(r)
        d.pop("closes")
        d.pop("news")  # 전체 뉴스는 생략, 사건성 뉴스만 analysis.event_news에 남긴다
        d["analysis"]["cause_label"] = r.analysis.cause.label
        payload.append(d)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=default)
