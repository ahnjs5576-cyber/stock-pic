"""웹 화면: `streamlit run streamlit_app.py`"""
from __future__ import annotations

import json
import os

import altair as alt
import pandas as pd
import streamlit as st

from stock_picker.analysis import KEY_NEWS_WINDOW_DAYS
from stock_picker.config import ScreenConfig
from stock_picker.data import CachingProvider, YahooProvider
from stock_picker.models import DropCause, Market, Recommendation
from stock_picker.paper import Portfolio, PortfolioStore, TradeError
from stock_picker.report import DISCLAIMER, _fmt_cap, _fmt_price, buy_checks, range_position
from stock_picker.screener import screen
from stock_picker.universe import CURRENCY, get_universe

st.set_page_config(page_title="대형주 급락 종목", page_icon="📉", layout="wide")

# Streamlit Cloud의 Secrets에 ANTHROPIC_API_KEY를 넣으면 AI 분석 버튼이 켜진다.
try:
    if "ANTHROPIC_API_KEY" in st.secrets:
        os.environ.setdefault("ANTHROPIC_API_KEY", st.secrets["ANTHROPIC_API_KEY"])
except FileNotFoundError:
    pass
AI_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))

FX_TICKER = "KRW=X"  # 1달러당 원화
CACHE_TTL = 3600

CAUSE_STYLE = {  # 아이콘, 배지 색
    DropCause.EVENT: ("⚠️", "orange"),
    DropCause.MARKET: ("🌊", "violet"),
    DropCause.NO_CLEAR_REASON: ("❔", "gray"),
}
CAUSE_HELP = {
    DropCause.EVENT: "하루 급락이나 악재 뉴스(실적 부진, 소송, 증자 등)가 확인된 하락",
    DropCause.MARKET: "시장 지수도 크게 빠졌고, 이 종목만 유난히 더 빠지지는 않은 하락",
    DropCause.NO_CLEAR_REASON: "급락일도 악재 뉴스도 없이 서서히 빠진 하락",
}
MARKETS = {"코스피 + 미국": ("KOSPI", "US"), "코스피": ("KOSPI",), "미국": ("US",)}
PERIODS = {"최근 3개월": 60, "최근 6개월": 120, "최근 1년": 250}
SORTS = ["많이 빠진 순", "이유 없는 하락 먼저", "시장보다 더 빠진 순"]

# 화면을 바꿔도 조건이 초기화되지 않도록 위젯 값을 다시 써 둔다 (Streamlit은 화면에 없는 위젯 값을 지운다).
for _k in ("c_market", "c_drop", "c_period", "c_sort", "c_causes", "account"):
    if _k in st.session_state:
        st.session_state[_k] = st.session_state[_k]


@st.cache_resource
def get_provider() -> CachingProvider:
    """모든 사용자가 같이 쓰는 데이터 캐시 (1시간)."""
    return CachingProvider(YahooProvider(), ttl=CACHE_TTL)


@st.cache_resource
def get_store() -> PortfolioStore:
    """계좌 저장소. 모든 사용자가 같은 인스턴스(같은 계좌별 잠금)를 쓴다."""
    return PortfolioStore(os.environ.get("PAPER_DATA_DIR", "data/portfolios"))


STORE = get_store()


class QuoteUnavailable(Exception):
    pass


@st.cache_data(ttl=300, show_spinner=False)
def _quote(ticker: str) -> float:
    s = YahooProvider().closes(ticker, 5)
    if s.empty:
        raise QuoteUnavailable(ticker)  # 실패는 캐시하지 않도록 예외로 빠져나간다
    return float(s.iloc[-1])


def quote(ticker: str) -> float | None:
    """주문·평가용 가장 최근 가격 (성공한 값만 5분 캐시)."""
    try:
        return _quote(ticker)
    except QuoteUnavailable:
        return None


def fmt_krw(v: float) -> str:
    return f"{v:,.0f}원"


def badge(cause: DropCause) -> str:
    icon, color = CAUSE_STYLE[cause]
    return f":{color}-badge[{icon} {cause.label}]"


# ======================= 상단 =======================
VIEW_RECS, VIEW_PAPER = "📉 급락 종목 찾기", "💼 모의투자"
view = st.radio("화면", [VIEW_RECS, VIEW_PAPER], key="view", horizontal=True, label_visibility="collapsed")

market_label = st.session_state.get("c_market", "코스피 + 미국")
drop = st.session_state.get("c_drop", 20)
period_label = st.session_state.get("c_period", "최근 3개월")
sort_label = st.session_state.get("c_sort", SORTS[0])
cause_labels = st.session_state.get("c_causes", [c.label for c in DropCause])

if view == VIEW_RECS:
    st.title("📉 대형주 급락 종목")
    st.markdown(
        f"코스피·미국 **대형주** 가운데 **{period_label} 고점보다 {drop}% 이상 싸진 종목**을 찾고, "
        "**왜 빠졌는지** 함께 알려드립니다."
    )
    with st.expander(f"🔧 조건 바꾸기 · {market_label} · {drop}% 이상 · {period_label} · {sort_label}"):
        c1, c2 = st.columns(2)
        c1.radio("시장", list(MARKETS), key="c_market", horizontal=True)
        c2.select_slider("고점 대비 최소 하락폭", options=[10, 15, 20, 25, 30, 40, 50], value=20,
                         format_func=lambda v: f"{v}%", key="c_drop")
        c3, c4 = st.columns(2)
        c3.selectbox("고점을 찾을 기간", list(PERIODS), key="c_period")
        c4.selectbox("정렬", SORTS, key="c_sort")
        st.multiselect("하락 원인", [c.label for c in DropCause],
                       default=[c.label for c in DropCause], key="c_causes")
    # 위젯이 막 바뀐 경우를 반영
    market_label, drop = st.session_state.c_market, st.session_state.c_drop
    period_label, sort_label = st.session_state.c_period, st.session_state.c_sort
    cause_labels = st.session_state.c_causes

# ======================= 데이터 =======================
provider = get_provider()
config = ScreenConfig(drop_threshold=drop / 100, lookback_days=PERIODS[period_label])
markets = [Market(m) for m in MARKETS[market_label]]

progress = st.empty()
with st.spinner("시세를 불러오는 중입니다… (처음 열 때는 30초~1분 걸릴 수 있어요)"):
    def on_progress(done: int, total: int) -> None:
        progress.progress(done / total, text=f"하락 종목의 뉴스와 재무 정보를 확인하는 중… ({done}/{total})")

    all_recs = screen(provider, markets, config, on_progress=on_progress)
progress.empty()
as_of = provider.as_of()
data_ok = as_of is not None

if sort_label == "이유 없는 하락 먼저":
    order = {DropCause.NO_CLEAR_REASON: 0, DropCause.MARKET: 1, DropCause.EVENT: 2}
    all_recs.sort(key=lambda r: (order[r.analysis.cause], r.drawdown))
elif sort_label == "시장보다 더 빠진 순":
    all_recs.sort(key=lambda r: r.analysis.excess_return)
recs = [r for r in all_recs if r.analysis.cause.label in cause_labels]

# ======================= 급락 종목 찾기 =======================
if view == VIEW_RECS:
    top = st.columns([4, 1], vertical_alignment="center")
    if data_ok:
        top[0].caption(
            f"데이터 기준 **{as_of:%Y-%m-%d}** 종가 · 출처 Yahoo Finance, Google 뉴스 · 1시간마다 새로 불러옵니다"
        )
    if top[1].button("↻ 새로 불러오기", width="stretch"):
        provider.clear()
        _quote.clear()
        st.rerun()

    with st.expander("ℹ️ 이렇게 골라요"):
        st.markdown(
            f"""
- **대형주**: 시가총액이 코스피 {config.min_market_cap[Market.KOSPI] / 1e12:.0f}조원, 미국 {config.min_market_cap[Market.US] / 1e9:.0f}0억 달러 이상인 종목만 봅니다. 시가총액을 확인하지 못한 종목은 빼요.
- **싸진 종목**: {period_label} 동안의 가장 높은 종가보다 지금 가격이 {drop}% 이상 낮은 종목입니다.
- **하락 원인**은 세 가지로 나눕니다.
  - {badge(DropCause.MARKET)} 같은 기간 시장 지수(코스피·S&P 500)도 {config.market_drop:.0%} 이상 빠졌고, 이 종목이 시장보다 {config.excess_drop:.0%}p 넘게 더 빠지지는 않은 경우
  - {badge(DropCause.EVENT)} 하루에 {config.event_day_drop:.0%} 이상 급락한 날이 있거나, 고점 이후 뉴스 제목에 실적 부진·소송·증자 같은 악재 표현이 있는 경우
  - {badge(DropCause.NO_CLEAR_REASON)} 위 두 경우가 아닌 경우
- 뉴스 판정은 제목만 보고 자동으로 하므로 틀릴 수 있습니다. 근거 뉴스를 꼭 직접 읽어 보세요.
"""
        )

    if not data_ok:
        st.error("시세를 불러오지 못했습니다. 잠시 뒤 '새로 불러오기'를 눌러 주세요.")
    else:
        counts = " ".join(
            f"{badge(c)} **{sum(r.analysis.cause is c for r in all_recs)}**" for c in DropCause
        )
        st.markdown(f"#### 찾은 종목 {len(all_recs)}개 &nbsp; {counts}")
        if not recs:
            st.info("조건에 맞는 종목이 없습니다. '조건 바꾸기'에서 하락폭을 낮추거나 기간을 늘려 보세요.")

    if recs:
        table = pd.DataFrame(
            [
                {
                    "종목": r.stock.name,
                    "하락 원인": f"{CAUSE_STYLE[r.analysis.cause][0]} {r.analysis.cause.label}",
                    "고점 대비": r.drawdown * 100,
                    "최근 5일": None if r.trend_5d is None else r.trend_5d * 100,
                    "시장보다 더 빠진 폭": min(r.analysis.excess_return, 0) * -100,
                    "현재가": _fmt_price(r.price, r.currency),
                    "1년 가격 위치": None if (p := range_position(r)) is None else p * 100,
                    "PER": "–" if r.per is None else f"{r.per:.1f}",
                    "시가총액": _fmt_cap(r.market_cap, r.currency),
                    "종목코드": r.stock.ticker,
                }
                for r in recs
            ]
        )
        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            column_config={
                "고점 대비": st.column_config.NumberColumn(format="%.1f%%", help="기간 내 최고 종가 대비 현재가"),
                "최근 5일": st.column_config.NumberColumn(format="%+.1f%%", help="최근 5거래일 등락률. 음수면 아직 하락 중"),
                "시장보다 더 빠진 폭": st.column_config.NumberColumn(
                    format="%.1f%%p", help="같은 기간 시장 지수보다 얼마나 더 빠졌는지"
                ),
                "1년 가격 위치": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%.0f",
                    help="1년 최저가를 0, 최고가를 100으로 봤을 때 지금 가격의 위치. 낮을수록 1년 중 싼 편",
                ),
                "PER": st.column_config.TextColumn(help="주가수익비율. 낮을수록 이익 대비 싼 편 (적자거나 정보가 없으면 –)"),
            },
        )

        st.subheader("종목별로 자세히 보기")
        for r in recs:
            a = r.analysis
            icon = CAUSE_STYLE[a.cause][0]
            with st.expander(f"{icon} {r.stock.name} · 고점 대비 {r.drawdown:.1%} · {a.cause.label}"):
                left, right = st.columns([3, 2])
                with left:
                    st.markdown(f"{badge(a.cause)} &nbsp; {CAUSE_HELP[a.cause]}")
                    st.write(a.summary)
                    facts = [
                        f"현재가 **{_fmt_price(r.price, r.currency)}**",
                        f"고점 {_fmt_price(r.peak_price, r.currency)} ({r.peak_date:%m/%d})",
                    ]
                    if r.low_since_peak:
                        facts.append(f"고점 이후 최저 {_fmt_price(r.low_since_peak, r.currency)}")
                    if r.per:
                        facts.append(f"PER {r.per:.1f}")
                    if r.pbr:
                        facts.append(f"PBR {r.pbr:.2f}")
                    st.markdown(" · ".join(facts))
                    st.markdown("**매수 전에 확인하세요**")
                    st.markdown("\n".join(f"- {n}" for n in buy_checks(r)))

                    if a.event_news:
                        st.markdown("**하락 근거 뉴스**")
                        for n in a.event_news[:4]:
                            near = (
                                a.worst_day is not None
                                and abs((n.published - a.worst_day).days) <= KEY_NEWS_WINDOW_DAYS
                            )
                            title = f"[{n.title}]({n.url})" if n.url else n.title
                            mark = "🔑 급락일 무렵 · " if near else ""
                            st.markdown(
                                f"- {mark}{n.published:%Y-%m-%d} · {title} "
                                + " ".join(f":orange-badge[{k}]" for k in n.matched_keywords)
                            )
                    else:
                        st.caption(f"고점 이후 뉴스 {a.news_checked}건을 확인했지만 악재 표현은 없었습니다.")
                        latest = sorted(
                            (n for n in r.news if n.published and n.published >= r.peak_date),
                            key=lambda n: n.published, reverse=True,
                        )[:3]
                        for n in latest:
                            title = f"[{n.title}]({n.url})" if n.url else n.title
                            st.markdown(f"- {n.published:%Y-%m-%d} · {title}")

                    key = f"ai:{r.stock.ticker}:{r.peak_date:%Y%m%d}"
                    if AI_ENABLED:
                        if key not in st.session_state and st.button("🤖 AI로 원인 자세히 보기", key=f"btn:{key}"):
                            from stock_picker.ai import explain

                            with st.spinner("Claude가 웹에서 원인을 찾는 중…"):
                                st.session_state[key] = explain(r) or "AI 설명을 가져오지 못했습니다."
                        if key in st.session_state:
                            st.markdown("**AI 분석**")
                            st.write(st.session_state[key])
                with right:
                    if r.closes is not None:
                        cur = "원" if r.currency == "KRW" else "달러"
                        df = r.closes.rename("종가").rename_axis("날짜").reset_index()
                        base = alt.Chart(df).encode(x=alt.X("날짜:T", title=None))
                        line = base.mark_line(strokeWidth=2, color="#1F5FBF").encode(
                            y=alt.Y("종가:Q", title=f"종가({cur})", scale=alt.Scale(zero=False)),
                            tooltip=[alt.Tooltip("날짜:T", format="%Y-%m-%d"), alt.Tooltip("종가:Q", format=",.2f")],
                        )
                        marks = pd.DataFrame(
                            [
                                {"날짜": r.peak_date, "종가": r.peak_price, "표시": "고점"},
                                {"날짜": r.closes.index[-1], "종가": r.price, "표시": "현재"},
                            ]
                        )
                        points = alt.Chart(marks).mark_point(size=70, filled=True, color="#1F5FBF").encode(
                            x="날짜:T", y="종가:Q", tooltip=["표시", alt.Tooltip("종가:Q", format=",.2f")]
                        )
                        labels = alt.Chart(marks).mark_text(dy=-10, fontSize=11).encode(
                            x="날짜:T", y="종가:Q", text="표시"
                        )
                        threshold = alt.Chart(
                            pd.DataFrame({"y": [r.peak_price * (1 - config.drop_threshold)]})
                        ).mark_rule(strokeDash=[4, 4], color="#8a8f98").encode(y="y:Q")
                        layers = [line, threshold, points, labels]
                        if a.worst_day is not None and a.worst_day_return <= -config.event_day_drop:
                            layers.append(
                                alt.Chart(pd.DataFrame({"x": [a.worst_day]}))
                                .mark_rule(color="#d9822b", strokeDash=[2, 2])
                                .encode(x="x:T")
                            )
                        st.altair_chart(alt.layer(*layers).properties(height=230), width="stretch")
                        st.caption(f"점선: 고점보다 {drop}% 낮은 가격" + (" · 주황 선: 급락일" if len(layers) > 4 else ""))

                with st.form(key=f"buyform:{r.stock.ticker}", border=False):
                    fc = st.columns([2, 1], vertical_alignment="bottom")
                    fc[0].number_input("모의 매수 수량(주)", min_value=1, value=1, step=1, key=f"qty:{r.stock.ticker}")
                    fc[1].form_submit_button(
                        "모의 매수",
                        on_click=lambda t=r.stock.ticker, n=r.stock.name, c=r.currency: place_buy(t, n, c, f"qty:{t}"),
                        width="stretch",
                    )

    st.divider()
    st.caption(DISCLAIMER)


# ======================= 모의투자 =======================
def load_portfolio() -> Portfolio | None:
    try:
        return STORE.load(st.session_state.get("account", "내 계좌"))
    except ValueError:
        return None


def place_buy(ticker: str, name: str, currency: str, qty_key: str) -> None:
    # 폼 안의 수량은 제출 시점에 확정되므로 콜백에서 직접 읽는다.
    qty = int(st.session_state[qty_key])
    account = st.session_state.get("account", "내 계좌")
    price = quote(ticker)
    fx = quote(FX_TICKER) if currency == "USD" else 1.0
    if price is None or fx is None:
        st.session_state.trade_msg = ("err", "현재가나 환율을 가져오지 못해 주문하지 못했습니다. 잠시 뒤 다시 시도하세요.")
        return
    try:
        # 최신 계좌를 다시 읽어서 거래해야 다른 탭에서 한 거래를 덮어쓰지 않는다.
        with STORE.transaction(account) as portfolio:
            t = portfolio.buy(ticker, name, currency, qty, price, fx)
    except TradeError as e:
        st.session_state.trade_msg = ("err", str(e))
        return
    except ValueError:
        st.session_state.trade_msg = ("err", "모의투자 화면에서 계좌 이름을 먼저 입력하세요.")
        return
    st.session_state.trade_msg = (
        "ok", f"[{account.strip()}] {name} {qty}주 매수 ({_fmt_price(price, currency)}, 총 {fmt_krw(t.amount_krw)})"
    )


def place_sell(ticker: str, currency: str, qty_key: str) -> None:
    qty = int(st.session_state[qty_key])
    account = st.session_state.get("account", "내 계좌")
    price = quote(ticker)
    fx = quote(FX_TICKER) if currency == "USD" else 1.0
    if price is None or fx is None:
        st.session_state.trade_msg = ("err", "현재가나 환율을 가져오지 못해 주문하지 못했습니다. 잠시 뒤 다시 시도하세요.")
        return
    try:
        with STORE.transaction(account) as portfolio:
            t = portfolio.sell(ticker, qty, price, fx)
    except (TradeError, ValueError) as e:
        st.session_state.trade_msg = ("err", str(e))
        return
    st.session_state.pop(qty_key, None)
    st.session_state.trade_msg = ("ok", f"{t.name} {qty}주 매도 (실현손익 {t.realized_krw:+,.0f}원)")


# 주문 결과는 화면 배치를 바꾸지 않도록 토스트로 띄운다.
if msg := st.session_state.pop("trade_msg", None):
    st.toast(msg[1], icon="✅" if msg[0] == "ok" else "⚠️")

if view == VIEW_PAPER:
    st.title("💼 모의투자")
    st.caption("가상 돈으로 사고팔며 연습해 보세요. 가장 최근 가격으로 체결되고, 미국 주식은 그때 환율로 원화로 계산합니다. 수수료·세금은 빼지 않습니다.")
    st.text_input(
        "계좌 이름", value="내 계좌", key="account",
        help="같은 이름을 입력하면 같은 계좌가 열립니다. 로그인이 없으니 남이 쓰기 어려운 이름을 쓰세요.",
    )
    portfolio = load_portfolio()
    if portfolio is None:
        st.error("계좌 이름을 입력하세요.")
        st.stop()

    fx_now = quote(FX_TICKER)
    prices = {t: p for t in portfolio.positions if (p := quote(t)) is not None}
    if fx_now is None:
        # 환율이 없으면 미국 주식은 평가하지 않고 원가로 둔다 (1달러=1원으로 계산하면 크게 틀린다).
        prices = {t: p for t, p in prices.items() if portfolio.positions[t].currency != "USD"}
        fx_now = 0.0
    missing = [portfolio.positions[t].name for t in portfolio.positions if t not in prices]
    if missing:
        st.warning(f"현재가나 환율을 가져오지 못해 매수 원가로 계산했습니다: {', '.join(missing)}")

    total = portfolio.total_value(prices, fx_now)
    ret = total / portfolio.initial_cash - 1
    unrealized = total - portfolio.initial_cash - portfolio.realized_pnl()

    def signed(v: float) -> str:
        # 한국 증시 관례: 오르면 빨강, 내리면 파랑
        color = "#d6334a" if v > 0 else "#1F5FBF" if v < 0 else "inherit"
        return f'<span style="color:{color}">{v:+,.0f}원</span>'

    # 휴대폰에서도 2×2로 보이도록 직접 배치한다 (st.columns는 좁은 화면에서 한 줄씩 쌓인다).
    st.markdown(
        f"""
<div class="acct-grid">
  <div><small>총자산</small><b>{fmt_krw(total)}</b><small>시작 금액 대비 {ret:+.2%}</small></div>
  <div><small>현금</small><b>{fmt_krw(portfolio.cash)}</b><small>바로 살 수 있는 돈</small></div>
  <div><small>평가손익</small><b>{signed(unrealized)}</b><small>아직 팔지 않은 종목</small></div>
  <div><small>실현손익</small><b>{signed(portfolio.realized_pnl())}</b><small>판 종목에서 확정</small></div>
</div>
<style>
.acct-grid {{ display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; margin:4px 0 8px; }}
.acct-grid > div {{ border:1px solid rgba(128,128,128,.25); border-radius:8px; padding:10px 12px;
  display:flex; flex-direction:column; gap:2px; min-width:0; }}
.acct-grid b {{ font-size:1.25rem; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }}
.acct-grid small {{ opacity:.7; }}
@media (max-width: 640px) {{ .acct-grid {{ grid-template-columns:repeat(2, minmax(0,1fr)); }} }}
</style>
""",
        unsafe_allow_html=True,
    )

    st.subheader("보유 종목")
    rows = portfolio.valuation(prices, fx_now)
    if rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "종목": r["position"].name,
                        "수량": r["position"].quantity,
                        "수익률": None if r["pnl_pct"] is None else r["pnl_pct"] * 100,
                        "평가손익": "-" if r["pnl_krw"] is None else f"{r['pnl_krw']:+,.0f}원",
                        "평균 매수가": _fmt_price(r["position"].avg_price, r["position"].currency),
                        "현재가": "-" if r["price"] is None else _fmt_price(r["price"], r["position"].currency),
                        "평가액": "-" if r["value_krw"] is None else fmt_krw(r["value_krw"]),
                        "종목코드": r["position"].ticker,
                    }
                    for r in rows
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={"수익률": st.column_config.NumberColumn(format="%+.2f%%")},
        )
    else:
        st.info("아직 보유 종목이 없습니다. 아래에서 사거나, '급락 종목 찾기'에서 종목을 펼쳐 바로 살 수 있어요.")

    buy_col, sell_col = st.columns(2)
    with buy_col:
        st.subheader("사기")
        rec_by_ticker = {r.stock.ticker: r for r in all_recs}
        universe = get_universe([Market.KOSPI, Market.US])
        options = [s for s in universe if s.ticker in rec_by_ticker] + [
            s for s in universe if s.ticker not in rec_by_ticker
        ]
        by_ticker = {s.ticker: s for s in universe}

        def label(t: str) -> str:
            s = by_ticker[t]
            if t in rec_by_ticker:
                return f"📉 {s.name} · 고점 대비 {rec_by_ticker[t].drawdown:.0%}"
            return f"{s.name} ({t})"

        ticker = st.selectbox("종목 (📉 = 지금 급락 종목)", [s.ticker for s in options], format_func=label,
                              key="paper_buy_ticker")
        stock = by_ticker[ticker]
        currency = CURRENCY[stock.market]
        price = quote(ticker)
        st.caption("현재가 " + ("확인 불가" if price is None else _fmt_price(price, currency)))
        with st.form("paper_buy_form", border=False):
            st.number_input("수량(주)", min_value=1, value=1, step=1, key="paper_buy_qty")
            st.form_submit_button(
                "사기", type="primary", on_click=place_buy,
                args=(ticker, stock.name, currency, "paper_buy_qty"),
            )
    with sell_col:
        st.subheader("팔기")
        if portfolio.positions:
            # 객체 대신 티커를 옵션으로 써야 매도 후 수량 표시가 바로 갱신된다.
            sell_ticker = st.selectbox(
                "보유 종목",
                list(portfolio.positions),
                format_func=lambda t: f"{portfolio.positions[t].name} · {portfolio.positions[t].quantity}주",
                key="paper_sell_ticker",
            )
            pos = portfolio.positions[sell_ticker]
            st.caption("현재가 " + ("확인 불가" if sell_ticker not in prices else _fmt_price(prices[sell_ticker], pos.currency)))
            qty_key = f"paper_sell_qty:{sell_ticker}"
            with st.form("paper_sell_form", border=False):
                st.number_input("수량(주)", min_value=1, max_value=pos.quantity, value=pos.quantity, step=1, key=qty_key)
                st.form_submit_button("팔기", on_click=place_sell, args=(sell_ticker, pos.currency, qty_key))
        else:
            st.caption("팔 종목이 없습니다.")

    st.subheader("거래 내역")
    if portfolio.trades:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "시각": t.time.replace("T", " "),
                        "구분": "사기" if t.side == "BUY" else "팔기",
                        "종목": t.name,
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
        st.caption("무료 서버는 다시 시작되면 저장된 계좌가 지워질 수 있으니, 가끔 백업 파일을 내려받아 두세요.")
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
            except (ValueError, KeyError, TypeError, AttributeError):
                st.error("올바른 백업 파일이 아닙니다.")
            else:
                restored.name = portfolio.name
                STORE.replace(restored)
                st.session_state.trade_msg = ("ok", "백업 파일로 복원했습니다.")
                st.rerun()
        st.divider()
        start_cash = st.number_input(
            "초기화할 시작 금액(원)", min_value=1_000_000, value=int(portfolio.initial_cash), step=1_000_000
        )
        confirm = st.checkbox("보유 종목과 거래 내역을 모두 지우는 데 동의합니다.", key="reset_confirm")

        def reset_account(cash: float) -> None:
            STORE.replace(Portfolio(name=portfolio.name, initial_cash=cash, cash=cash))
            st.session_state.reset_confirm = False
            st.session_state.trade_msg = ("ok", f"{fmt_krw(cash)}으로 계좌를 초기화했습니다.")

        st.button("계좌 초기화", disabled=not confirm, on_click=reset_account, args=(float(start_cash),))

    st.divider()
    st.caption("※ 모의투자 결과는 실제 수익을 보장하지 않습니다. " + DISCLAIMER)
