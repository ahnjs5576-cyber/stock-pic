"""(선택) Claude + 웹 검색으로 하락 이유를 사람이 읽기 쉬운 설명으로 정리한다.

ANTHROPIC_API_KEY(또는 `ant auth login` 프로필)가 있을 때 `--ai` 옵션으로 켠다.
"""
from __future__ import annotations

import logging

import anthropic

from .models import Recommendation

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"

SYSTEM = (
    "당신은 주식 애널리스트입니다. 주어진 종목이 최근 고점 대비 크게 하락한 이유를 웹 검색으로 "
    "확인하고 한국어로 간결하게 설명합니다. 실적·소송·규제·증자 같은 구체적 사건이 있으면 날짜와 "
    "함께 밝히고, 확인되는 사건이 없으면 '뚜렷한 개별 악재는 확인되지 않음'이라고 분명히 말하세요. "
    "추측은 추측이라고 표시하고, 매수·매도를 권유하지 마세요. 5문장 이내로 답하세요."
)


def _prompt(rec: Recommendation) -> str:
    a = rec.analysis
    worst = f"{a.worst_day:%Y-%m-%d} ({a.worst_day_return:.1%})" if a.worst_day else "-"
    return (
        f"종목: {rec.stock.name} ({rec.stock.ticker}, {rec.stock.market.value})\n"
        f"고점: {rec.peak_date:%Y-%m-%d} {rec.peak_price:,.2f} {rec.currency}\n"
        f"현재가: {rec.price:,.2f} {rec.currency} (고점 대비 {rec.drawdown:.1%})\n"
        f"같은 기간 지수 수익률: {a.index_return:+.1%}\n"
        f"가장 크게 빠진 날: {worst}\n"
        f"규칙 기반 1차 판단: {a.cause.label}\n\n"
        "이 하락의 원인을 웹에서 확인해 설명해 주세요."
    )


def explain(rec: Recommendation, client: anthropic.Anthropic | None = None) -> str | None:
    client = client or anthropic.Anthropic()
    messages = [{"role": "user", "content": _prompt(rec)}]
    try:
        for _ in range(3):  # web search가 길어지면 pause_turn으로 끊길 수 있어 이어서 요청
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"effort": "medium"},
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
    except anthropic.RateLimitError:
        log.warning("Claude 요청 한도 초과: %s", rec.stock.ticker)
        return None
    except anthropic.APIStatusError as e:
        log.warning("Claude API 오류 (%s) %s: %s", e.status_code, rec.stock.ticker, e.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Claude 연결 실패: %s", rec.stock.ticker)
        return None

    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None
