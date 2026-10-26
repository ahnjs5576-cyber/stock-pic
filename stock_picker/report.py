from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

from .models import Recommendation

DISCLAIMER = (
    "※ 본 결과는 정보 제공용이며 투자 권유가 아닙니다. 하락한 주식이 반드시 반등하는 것은 아니며, "
    "투자 판단과 책임은 본인에게 있습니다."
)

ADVICE = {
    "EVENT": "사건이 기업 가치를 영구적으로 훼손했는지 확인한 뒤 접근하세요.",
    "MARKET": "시장 회복 시 함께 반등할 가능성이 있습니다. 지수 흐름을 같이 보세요.",
    "NO_CLEAR_REASON": "펀더멘털 변화 없이 빠졌다면 저가 매수 후보가 될 수 있습니다.",
}


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
        lines += [f"> {ADVICE[a.cause.value]}", ""]

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
