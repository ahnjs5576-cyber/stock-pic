"""웹 화면: `streamlit run streamlit_app.py`"""
from __future__ import annotations

import json
import os

import altair as alt
import pandas as pd
import streamlit as st

from stock_picker.config import ScreenConfig
from stock_picker.data import YahooProvider
from stock_picker.models import DropCause, Market, Recommendation
from stock_picker.paper import Portfolio, PortfolioStore, TradeError
from stock_picker.report import ADVICE, DISCLAIMER, _fmt_cap, _fmt_price
from stock_picker.screener import screen
from stock_picker.universe import BENCHMARKS, CURRENCY, get_universe

st.set_page_config(page_title="대형주 급락 종목 추천", page_icon="📉", layout="wide")

# Streamlit Cloud의 Secrets에 ANTHROPIC_API_KEY를 넣으면 AI 분석 버튼이 켜진다.
try:
    if "ANTHROPIC_API_KEY" in st.secrets:
        os.environ.setdefault("ANTHROPIC_API_KEY", st.secrets["ANTHROPIC_API_KEY"])
except FileNotFoundError:
    pass
AI_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))

STORE = PortfolioStore(os.environ.get("PAPER_DATA_DIR", "data/portfolios"))
FX_TICKER = "KRW=X"  # 원/달러 환율

CAUSE_ICON = {
    DropCause.EVENT: "⚠️",
    DropCause.MARKET: "🌊",
    DropCause.NO_CLEAR_REASON: "❔",
}


@st.cache_data(ttl=3600, show_spinner=False)
def run_screen(market_keys: tuple[str, ...], drop: float, lookback: int) -> tuple[list[Recommendation], bool]:
    markets = [Market(m) for m in market_keys]
    provider = YahooProvider()
    if any(provider.closes(BENCHMARKS[m], 5).empty for m in markets):
        return [], False
    config = ScreenConfig(drop_threshold=drop / 100, lookback_days=lookback)
    return screen(provider, markets, config), True


@st.cache_data(ttl=300, show_spinner=False)
def quote(ticker: str) -> float | None:
    """최근 종가 (5분 캐시)."""
    s = YahooProvider().closes(ticker, 5)
    return None if s.empty else float(s.iloc[-1])


def fmt_krw(v: float) -> str:
    return f"{v:,.0f}원"


# ---- 사이드바 ----
with st.sidebar:
    st.header("모의투자 계좌")
    account = st.text_input("계좌 이름", value="내 계좌", help="같은 이름으로 들어오면 같은 계좌가 열립니다.")
    st.divider()
    st.header("추천 조건")
    market_label = st.radio("시장", ["코스피 + 미국", "코스피", "미국"])
    market_keys = {
        "코스피 + 미국": ("KOSPI", "US"),
        "코스피": ("KOSPI",),
        "미국": ("US",),
    }[market_label]
    drop = st.slider("고점 대비 최소 하락률 (%)", 10, 50, 20, step=5)
    lookback_label = st.selectbox("고점을 찾을 기간", ["최근 3개월", "최근 6개월", "최근 1년"])
    lookback = {"최근 3개월": 60, "최근 6개월": 120, "최근 1년": 250}[lookback_label]
    cause_filter = st.multiselect(
        "하락 원인",
        [c.label for c in DropCause],
        default=[c.label for c in DropCause],
    )
    st.caption("추천 결과는 1시간, 현재가는 5분 동안 캐시됩니다.")
    if st.button("새로고침"):
        run_screen.clear()
        quote.clear()

try:
    portfolio = STORE.load(account)
except ValueError:
    st.sidebar.error("계좌 이름을 입력하세요.")
    st.stop()

# 주문 결과는 화면 배치를 바꾸지 않도록 토스트로 띄운다.
if msg := st.session_state.pop("trade_msg", None):
    st.toast(msg[1], icon="✅" if msg[0] == "ok" else "⚠️")


def place_buy(ticker: str, name: str, currency: str, qty: int) -> None:
    price = quote(ticker)
    fx = quote(FX_TICKER) if currency == "USD" else 1.0
    if price is None or fx is None:
        st.session_state.trade_msg = ("err", "현재가를 가져오지 못해 주문하지 못했습니다.")
        return
    try:
        t = portfolio.buy(ticker, name, currency, qty, price, fx)
    except TradeError as e:
        st.session_state.trade_msg = ("err", str(e))
        return
    STORE.save(portfolio)
    st.session_state.trade_msg = (
        "ok",
        f"{name} {qty}주 매수 완료 ({_fmt_price(price, currency)}, 총 {fmt_krw(t.amount_krw)})",
    )


def place_sell(ticker: str, qty: int) -> None:
    pos = portfolio.positions[ticker]
    price = quote(ticker)
    fx = quote(FX_TICKER) if pos.currency == "USD" else 1.0
    if price is None or fx is None:
        st.session_state.trade_msg = ("err", "현재가를 가져오지 못해 주문하지 못했습니다.")
        return
    try:
        t = portfolio.sell(ticker, qty, price, fx)
    except TradeError as e:
        st.session_state.trade_msg = ("err", str(e))
        return
    STORE.save(portfolio)
    # 남은 수량보다 큰 값이 입력칸에 남지 않도록 비운다.
    st.session_state.pop(f"paper_sell_qty:{ticker}", None)
    st.session_state.trade_msg = (
        "ok",
        f"{t.name} {qty}주 매도 완료 (실현손익 {t.realized_krw:+,.0f}원)",
    )


# st.tabs는 rerun 때 첫 탭으로 돌아가므로, 상태가 유지되는 라디오로 화면을 나눈다.
VIEW_RECS, VIEW_PAPER = "📉 추천 종목", "💼 모의투자"
view = st.radio("화면", [VIEW_RECS, VIEW_PAPER], key="view", horizontal=True, label_visibility="collapsed")

# ======================= 추천 종목 =======================
if view == VIEW_RECS:
    st.title("📉 대형주 급락 종목 추천")
    st.write(
        f"코스피·미국 대형주 중 **{lookback_label} 고점 대비 {drop}% 이상** 하락한 종목과 "
        "그 하락 원인을 보여줍니다."
    )

    with st.spinner("시세와 뉴스를 불러오는 중입니다… (처음엔 1~2분 걸릴 수 있어요)"):
        recs, ok = run_screen(market_keys, drop, lookback)

    if not ok:
        st.error("시세 데이터를 가져오지 못했습니다. 잠시 후 새로고침해 주세요.")
        recs = []

    recs = [r for r in recs if r.analysis.cause.label in cause_filter]

    cols = st.columns(4)
    cols[0].metric("추천 종목", f"{len(recs)}개")
    for col, cause in zip(cols[1:], DropCause):
        col.metric(f"{CAUSE_ICON[cause]} {cause.label}", f"{sum(r.analysis.cause is cause for r in recs)}개")

    if ok and not recs:
        st.info("조건에 맞는 종목이 없습니다. 하락률 기준을 낮추거나 기간을 늘려 보세요.")

    if recs:
        table = pd.DataFrame(
            [
                {
                    "종목": r.stock.name,
                    "티커": r.stock.ticker,
                    "시장": r.stock.market.value,
                    "현재가": _fmt_price(r.price, r.currency),
                    "고점": _fmt_price(r.peak_price, r.currency),
                    "고점일": r.peak_date.date(),
                    "하락률": r.drawdown * 100,
                    "지수 대비": r.analysis.excess_return * 100,
                    "시가총액": _fmt_cap(r.market_cap, r.currency),
                    "하락 원인": f"{CAUSE_ICON[r.analysis.cause]} {r.analysis.cause.label}",
                }
                for r in recs
            ]
        )
        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            column_config={
                "하락률": st.column_config.NumberColumn(format="%.1f%%"),
                "지수 대비": st.column_config.NumberColumn(format="%+.1f%%p"),
            },
        )

        st.subheader("종목별 하락 원인")
        for r in recs:
            a = r.analysis
            with st.expander(
                f"{CAUSE_ICON[a.cause]} {r.stock.name} ({r.stock.ticker}) · {r.drawdown:.1%} · {a.cause.label}"
            ):
                left, right = st.columns([3, 2])
                with left:
                    st.write(a.summary)
                    st.info(ADVICE[a.cause.value])
                    if a.event_news:
                        st.markdown("**관련 뉴스**")
                        for n in a.event_news[:5]:
                            date = f"{n.published:%Y-%m-%d} · " if n.published else ""
                            title = f"[{n.title}]({n.url})" if n.url else n.title
                            st.markdown(f"- {date}{title}  \n  `{', '.join(n.matched_keywords)}`")
                    key = f"ai:{r.stock.ticker}:{r.peak_date:%Y%m%d}"
                    if AI_ENABLED:
                        if key not in st.session_state and st.button("🤖 AI로 원인 자세히 보기", key=f"btn:{key}"):
                            from stock_picker.ai import explain

                            with st.spinner("Claude가 웹에서 원인을 찾는 중…"):
                                st.session_state[key] = explain(r) or "AI 설명을 가져오지 못했습니다."
                        if key in st.session_state:
                            st.markdown("**AI 분석**")
                            st.write(st.session_state[key])

                    st.markdown(f"**모의 매수** · `{portfolio.name}` 계좌 현금 {fmt_krw(portfolio.cash)}")
                    q_col, b_col = st.columns([2, 1])
                    qty = q_col.number_input(
                        "수량(주)", min_value=1, value=1, step=1,
                        key=f"qty:{r.stock.ticker}", label_visibility="collapsed",
                    )
                    b_col.button(
                        "매수",
                        key=f"buy:{r.stock.ticker}",
                        on_click=place_buy,
                        args=(r.stock.ticker, r.stock.name, r.currency, int(qty)),
                        width="stretch",
                    )
                with right:
                    if r.closes is not None:
                        df = r.closes.rename("종가").rename_axis("날짜").reset_index()
                        chart = (
                            alt.Chart(df)
                            .mark_line(strokeWidth=2)
                            .encode(
                                x=alt.X("날짜:T", title=None),
                                y=alt.Y("종가:Q", title=None, scale=alt.Scale(zero=False)),
                                tooltip=[
                                    alt.Tooltip("날짜:T", format="%Y-%m-%d"),
                                    alt.Tooltip("종가:Q", format=",.2f"),
                                ],
                            )
                            .properties(height=220)
                        )
                        st.altair_chart(chart, width="stretch")
                        st.caption(f"고점 {r.peak_date:%Y-%m-%d} {_fmt_price(r.peak_price, r.currency)}")

    st.divider()
    st.caption(DISCLAIMER)

# ======================= 모의투자 =======================
if view == VIEW_PAPER:
    st.title(f"💼 모의투자 · {portfolio.name}")
    st.caption("가상 원화로 거래합니다. 미국 주식은 거래 시점 환율로 환산하며, 수수료·세금은 반영하지 않습니다.")

    fx_now = quote(FX_TICKER)
    prices = {t: p for t in portfolio.positions if (p := quote(t)) is not None}
    if fx_now is None:
        fx_now = 1.0
        if any(p.currency == "USD" for p in portfolio.positions.values()):
            st.warning("환율을 가져오지 못해 미국 주식 평가액이 정확하지 않을 수 있습니다.")

    total = portfolio.total_value(prices, fx_now)
    m = st.columns(4)
    m[0].metric("총자산", fmt_krw(total), f"{total / portfolio.initial_cash - 1:+.2%}")
    m[1].metric("현금", fmt_krw(portfolio.cash))
    m[2].metric("평가손익", f"{total - portfolio.initial_cash - portfolio.realized_pnl():+,.0f}원")
    m[3].metric("실현손익", f"{portfolio.realized_pnl():+,.0f}원")

    st.subheader("보유 종목")
    rows = portfolio.valuation(prices, fx_now)
    if rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "종목": r["position"].name,
                        "티커": r["position"].ticker,
                        "수량": r["position"].quantity,
                        "평균 매수가": _fmt_price(r["position"].avg_price, r["position"].currency),
                        "현재가": "-" if r["price"] is None else _fmt_price(r["price"], r["position"].currency),
                        "매수 원가": fmt_krw(r["position"].cost_krw),
                        "평가액": "-" if r["value_krw"] is None else fmt_krw(r["value_krw"]),
                        "수익률": None if r["pnl_pct"] is None else r["pnl_pct"] * 100,
                        "평가손익": "-" if r["pnl_krw"] is None else f"{r['pnl_krw']:+,.0f}원",
                    }
                    for r in rows
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "수익률": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )
    else:
        st.info("아직 보유 종목이 없습니다. '추천 종목' 탭이나 아래에서 매수해 보세요.")

    buy_col, sell_col = st.columns(2)
    with buy_col:
        st.subheader("매수")
        universe = get_universe([Market.KOSPI, Market.US])
        stock = st.selectbox(
            "종목",
            universe,
            format_func=lambda s: f"{s.name} ({s.ticker})",
            key="paper_buy_stock",
        )
        price = quote(stock.ticker)
        currency = CURRENCY[stock.market]
        st.caption("현재가 " + ("-" if price is None else _fmt_price(price, currency)))
        qty = st.number_input("수량(주)", min_value=1, value=1, step=1, key="paper_buy_qty")
        st.button(
            "매수하기",
            on_click=place_buy,
            args=(stock.ticker, stock.name, currency, int(qty)),
            type="primary",
        )
    with sell_col:
        st.subheader("매도")
        if portfolio.positions:
            # 객체 대신 티커를 옵션으로 써야 매도 후 수량 표시가 바로 갱신된다.
            ticker = st.selectbox(
                "보유 종목",
                list(portfolio.positions),
                format_func=lambda t: f"{portfolio.positions[t].name} ({t}) · {portfolio.positions[t].quantity}주",
                key="paper_sell_stock",
            )
            pos = portfolio.positions[ticker]
            st.caption("현재가 " + ("-" if pos.ticker not in prices else _fmt_price(prices[pos.ticker], pos.currency)))
            sell_qty = st.number_input(
                "수량(주)", min_value=1, max_value=pos.quantity, value=pos.quantity, step=1,
                key=f"paper_sell_qty:{pos.ticker}",
            )
            st.button("매도하기", on_click=place_sell, args=(pos.ticker, int(sell_qty)))
        else:
            st.caption("매도할 종목이 없습니다.")

    st.subheader("거래 내역")
    if portfolio.trades:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "시각": t.time.replace("T", " "),
                        "구분": "매수" if t.side == "BUY" else "매도",
                        "종목": f"{t.name} ({t.ticker})",
                        "수량": t.quantity,
                        "체결가": _fmt_price(t.price, t.currency),
                        "금액": fmt_krw(t.amount_krw),
                        "실현손익": f"{t.realized_krw:+,.0f}원" if t.side == "SELL" else "",
                    }
                    for t in reversed(portfolio.trades)
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("거래 내역이 없습니다.")

    with st.expander("계좌 관리 (백업·복원·초기화)"):
        st.caption(
            "무료 서버는 재시작되면 저장된 계좌가 지워질 수 있으니, 가끔 백업 파일을 내려받아 두세요."
        )
        st.download_button(
            "백업 파일 내려받기",
            data=json.dumps(portfolio.to_dict(), ensure_ascii=False, indent=2),
            file_name=f"모의투자_{portfolio.name}.json",
            mime="application/json",
        )
        uploaded = st.file_uploader("백업 파일로 복원", type="json")
        if uploaded is not None and st.button("이 파일로 복원"):
            try:
                restored = Portfolio.from_dict(json.loads(uploaded.getvalue()))
            except (ValueError, KeyError, TypeError):
                st.error("올바른 백업 파일이 아닙니다.")
            else:
                restored.name = portfolio.name
                STORE.save(restored)
                st.session_state.trade_msg = ("ok", "백업 파일로 복원했습니다.")
                st.rerun()
        st.divider()
        start_cash = st.number_input(
            "초기화할 시작 금액(원)", min_value=1_000_000, value=int(portfolio.initial_cash), step=1_000_000
        )
        confirm = st.checkbox("보유 종목과 거래 내역을 모두 지우는 데 동의합니다.")
        if st.button("계좌 초기화", disabled=not confirm):
            STORE.save(Portfolio(name=portfolio.name, initial_cash=float(start_cash), cash=float(start_cash)))
            st.session_state.trade_msg = ("ok", f"{fmt_krw(start_cash)}으로 계좌를 초기화했습니다.")
            st.rerun()

    st.divider()
    st.caption("※ 모의투자 결과는 실제 수익을 보장하지 않습니다.")
