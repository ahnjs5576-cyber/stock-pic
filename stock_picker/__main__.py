from __future__ import annotations

import argparse
import logging
import sys

from .config import ScreenConfig
from .models import Market
from .report import to_json, to_markdown
from .screener import screen
from .universe import BENCHMARKS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="stock_picker",
        description="최근 고점 대비 크게 하락한 코스피·미국 대형주를 찾아 하락 원인과 함께 추천합니다.",
    )
    p.add_argument("--market", choices=["kospi", "us", "all"], default="all")
    p.add_argument("--drop", type=float, default=20, help="최소 하락률(%%), 기본 20")
    p.add_argument("--lookback", type=int, default=60, help="고점을 찾을 최근 거래일 수, 기본 60")
    p.add_argument("--ai", action="store_true", help="Claude 웹 검색으로 하락 이유 설명 추가")
    p.add_argument("--json", action="store_true", help="JSON으로 출력")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    markets = {"kospi": [Market.KOSPI], "us": [Market.US], "all": [Market.KOSPI, Market.US]}[
        args.market
    ]
    config = ScreenConfig(drop_threshold=args.drop / 100, lookback_days=args.lookback)

    from .data import YahooProvider

    provider = YahooProvider()
    for m in markets:
        if provider.closes(BENCHMARKS[m], 5).empty:
            print(f"{m.value} 시세 데이터를 가져오지 못했습니다. 네트워크 연결을 확인하세요.", file=sys.stderr)
            return 1

    recs = screen(provider, markets, config)

    if args.ai:
        from .ai import explain

        for r in recs:
            r.analysis.ai_explanation = explain(r)

    print(to_json(recs) if args.json else to_markdown(recs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
