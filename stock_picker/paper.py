"""모의투자: 가상 원화 계좌로 코스피·미국 주식을 사고판다.

현금은 원화 하나로 관리하고, 미국 주식은 거래 시점 환율로 원화 환산해 결제한다.
수수료·세금은 반영하지 않는다.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_CASH = 10_000_000.0  # 1,000만원


class TradeError(ValueError):
    pass


@dataclass
class Position:
    ticker: str
    name: str
    currency: str        # "KRW" | "USD"
    quantity: int
    avg_price: float     # 평균 매수가 (현지 통화)
    cost_krw: float      # 총 매수 원가 (원화)


@dataclass
class Trade:
    time: str            # ISO 형식
    side: str            # "BUY" | "SELL"
    ticker: str
    name: str
    quantity: int
    price: float         # 현지 통화
    fx: float            # 원/달러 환율 (원화 종목은 1)
    amount_krw: float
    realized_krw: float = 0.0  # 매도 시 실현 손익
    currency: str = "KRW"


@dataclass
class Portfolio:
    name: str
    initial_cash: float = DEFAULT_CASH
    cash: float = DEFAULT_CASH
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)

    def buy(
        self, ticker: str, name: str, currency: str, quantity: int, price: float, fx: float = 1.0
    ) -> Trade:
        if quantity <= 0:
            raise TradeError("수량은 1주 이상이어야 합니다.")
        if price <= 0:
            raise TradeError("가격 정보가 올바르지 않습니다.")
        fx = fx if currency == "USD" else 1.0
        amount = quantity * price * fx
        if amount > self.cash + 1e-6:
            raise TradeError(f"현금이 부족합니다. (필요 {amount:,.0f}원 / 보유 {self.cash:,.0f}원)")

        pos = self.positions.get(ticker)
        if pos is None:
            pos = self.positions[ticker] = Position(ticker, name, currency, 0, 0.0, 0.0)
        pos.avg_price = (pos.avg_price * pos.quantity + price * quantity) / (pos.quantity + quantity)
        pos.quantity += quantity
        pos.cost_krw += amount
        self.cash -= amount

        trade = Trade(_now(), "BUY", ticker, name, quantity, price, fx, amount, currency=currency)
        self.trades.append(trade)
        return trade

    def sell(self, ticker: str, quantity: int, price: float, fx: float = 1.0) -> Trade:
        pos = self.positions.get(ticker)
        if pos is None:
            raise TradeError("보유하지 않은 종목입니다.")
        if quantity <= 0 or quantity > pos.quantity:
            raise TradeError(f"매도 수량은 1~{pos.quantity}주 사이여야 합니다.")
        if price <= 0:
            raise TradeError("가격 정보가 올바르지 않습니다.")
        fx = fx if pos.currency == "USD" else 1.0
        amount = quantity * price * fx
        cost = pos.cost_krw * quantity / pos.quantity
        realized = amount - cost

        pos.quantity -= quantity
        pos.cost_krw -= cost
        if pos.quantity == 0:
            del self.positions[ticker]
        self.cash += amount

        trade = Trade(
            _now(), "SELL", ticker, pos.name, quantity, price, fx, amount, realized, pos.currency
        )
        self.trades.append(trade)
        return trade

    def valuation(self, prices: dict[str, float], fx: float) -> list[dict]:
        """보유 종목별 평가. prices는 현지 통화 현재가, fx는 원/달러 환율."""
        rows = []
        for pos in self.positions.values():
            price = prices.get(pos.ticker)
            value = None if price is None else pos.quantity * price * (fx if pos.currency == "USD" else 1.0)
            rows.append(
                {
                    "position": pos,
                    "price": price,
                    "value_krw": value,
                    "pnl_krw": None if value is None else value - pos.cost_krw,
                    "pnl_pct": None if value is None else value / pos.cost_krw - 1,
                }
            )
        return rows

    def total_value(self, prices: dict[str, float], fx: float) -> float:
        # 현재가를 못 받은 종목은 원가로 평가한다.
        held = sum(
            r["value_krw"] if r["value_krw"] is not None else r["position"].cost_krw
            for r in self.valuation(prices, fx)
        )
        return self.cash + held

    def realized_pnl(self) -> float:
        return sum(t.realized_krw for t in self.trades)

    # ---- 직렬화 ----
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Portfolio:
        return cls(
            name=d["name"],
            initial_cash=d["initial_cash"],
            cash=d["cash"],
            positions={k: Position(**v) for k, v in d.get("positions", {}).items()},
            trades=[Trade(**t) for t in d.get("trades", [])],
        )


class PortfolioStore:
    """계좌를 계좌 이름별 JSON 파일로 저장한다."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = re.sub(r"[^\w가-힣-]", "_", name.strip())[:40]
        if not safe:
            raise ValueError("계좌 이름이 비어 있습니다.")
        return self.root / f"{safe}.json"

    def load(self, name: str) -> Portfolio:
        path = self._path(name)
        if path.exists():
            return Portfolio.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return Portfolio(name=name.strip())

    def save(self, portfolio: Portfolio) -> None:
        path = self._path(portfolio.name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(portfolio.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
