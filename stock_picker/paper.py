"""모의투자: 가상 원화 계좌로 코스피·미국 주식을 사고판다.

현금은 원화 하나로 관리하고, 미국 주식은 거래 시점 환율로 원화 환산해 결제한다.
수수료·세금은 반영하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

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
        realized = round(amount - cost, 2)  # 같은 가격에 팔면 정확히 0이 되도록

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
                    "pnl_pct": None if value is None or pos.cost_krw <= 0 else value / pos.cost_krw - 1,
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
        # 사용자가 손으로 고친 백업 파일도 들어올 수 있으므로 타입을 강제한다 (틀리면 ValueError/TypeError).
        positions = {}
        for k, v in d.get("positions", {}).items():
            positions[str(k)] = Position(
                ticker=str(v["ticker"]), name=str(v["name"]), currency=str(v["currency"]),
                quantity=int(v["quantity"]), avg_price=float(v["avg_price"]), cost_krw=float(v["cost_krw"]),
            )
            pos = positions[str(k)]
            if pos.avg_price <= 0 or pos.cost_krw <= 0:
                raise ValueError("평균 매수가와 원가는 0보다 커야 합니다.")
            if pos.quantity <= 0 or pos.currency not in ("KRW", "USD"):
                raise ValueError("보유 종목 정보가 올바르지 않습니다.")
        for t in d.get("trades", []):
            if t["side"] not in ("BUY", "SELL") or float(t["quantity"]) != int(float(t["quantity"])):
                raise ValueError("거래 내역 정보가 올바르지 않습니다.")
        for v in d.get("positions", {}).values():
            if float(v["quantity"]) != int(float(v["quantity"])):
                raise ValueError("보유 수량은 정수여야 합니다.")
        trades = [
            Trade(
                time=str(t["time"]), side=str(t["side"]), ticker=str(t["ticker"]), name=str(t["name"]),
                quantity=int(t["quantity"]), price=float(t["price"]), fx=float(t["fx"]),
                amount_krw=float(t["amount_krw"]), realized_krw=float(t.get("realized_krw", 0.0)),
                currency=str(t.get("currency", "KRW")),
            )
            for t in d.get("trades", [])
        ]
        portfolio = cls(
            name=str(d["name"]),
            initial_cash=float(d["initial_cash"]),
            cash=float(d["cash"]),
            positions=positions,
            trades=trades,
        )
        numbers = [portfolio.initial_cash, portfolio.cash]
        numbers += [x for p in positions.values() for x in (p.avg_price, p.cost_krw)]
        numbers += [x for t in trades for x in (t.price, t.fx, t.amount_krw, t.realized_krw)]
        if not all(math.isfinite(x) for x in numbers):
            raise ValueError("숫자가 아닌 값(NaN 등)이 들어 있습니다.")
        if portfolio.initial_cash <= 0 or portfolio.cash < 0:
            raise ValueError("시작 금액은 0보다 크고, 현금은 0 이상이어야 합니다.")
        return portfolio


class PortfolioStore:
    """계좌를 계좌 이름별 JSON 파일로 저장한다.

    같은 계좌를 여러 탭·기기에서 동시에 써도 거래가 사라지지 않도록,
    거래는 transaction() 안에서 '최신 상태 읽기 → 변경 → 저장'을 계좌별 잠금으로 묶는다.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._parsed: dict[Path, tuple[tuple[int, int], dict]] = {}  # 파일 → (수정 시각, 읽은 내용): 바뀌지 않았으면 다시 읽지 않는다
        self._locks_guard = threading.Lock()

    def _path(self, name: str) -> Path:
        name = name.strip()
        safe = re.sub(r"[^\w가-힣-]", "_", name)[:40]
        if not safe:
            raise ValueError("계좌 이름이 비어 있습니다.")
        # 'a b'와 'a_b'처럼 다른 이름이 같은 파일이 되지 않도록 원래 이름의 해시를 붙인다.
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        return self.root / f"{safe}-{digest}.json"

    def _lock(self, name: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(name.strip(), threading.Lock())

    def load(self, name: str) -> Portfolio:
        path = self._path(name)
        try:
            st_ = path.stat()
            mtime = (st_.st_mtime_ns, st_.st_size)
        except FileNotFoundError:
            return Portfolio(name=name.strip())
        cached = self._parsed.get(path)
        if cached is None or cached[0] != mtime:
            cached = (mtime, json.loads(path.read_text(encoding="utf-8")))
            self._parsed[path] = cached
        # 매번 새 객체를 만들어 돌려준다 (호출한 쪽이 고쳐도 캐시가 바뀌지 않도록).
        return Portfolio.from_dict(cached[1])

    def save(self, portfolio: Portfolio) -> None:
        path = self._path(portfolio.name)
        tmp = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(portfolio.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def replace(self, portfolio: Portfolio) -> None:
        """계좌 전체를 바꾼다 (복원·초기화). 진행 중인 거래와 겹치지 않게 잠근다."""
        with self._lock(portfolio.name):
            self.save(portfolio)

    @contextmanager
    def transaction(self, name: str) -> Iterator[Portfolio]:
        """최신 계좌를 읽어 넘겨주고, 블록이 예외 없이 끝나면 저장한다."""
        with self._lock(name):
            portfolio = self.load(name)
            yield portfolio
            self.save(portfolio)


def _now() -> str:
    # 서버가 어느 나라에 있든 한국 시각으로 기록한다.
    return datetime.now(KST).replace(tzinfo=None).isoformat(timespec="seconds")
