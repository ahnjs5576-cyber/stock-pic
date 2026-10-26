"""웹 화면: `streamlit run streamlit_app.py`"""
from __future__ import annotations

import os

import altair as alt
import pandas as pd
import streamlit as st

from stock_picker.config import ScreenConfig
from stock_picker.data import YahooProvider
from stock_picker.models import DropCause, Market, Recommendation
from stock_picker.report import ADVICE, DISCLAIMER, _fmt_cap, _fmt_price
from stock_picker.screener import screen
from stock_picker.universe import BENCHMARKS

st.set_page_config(page_title="대형주 급락 종목 추천", page_icon="📉", layout="wide")

# Streamlit Cloud의 Secrets에 ANTHROPIC_API_KEY를 넣으면 AI 분석 버튼이 켜진다.
try:
    if "ANTHROPIC_API_KEY" in st.secrets:
        os.environ.setdefault("ANTHROPIC_API_KEY", st.secrets["ANTHROPIC_API_KEY"])
except FileNotFoundError:
    pass
AI_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))

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


# ---- 사이드바: 조건 ----
with st.sidebar:
    st.header("조건")
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
    st.caption("시세는 1시간 동안 캐시됩니다.")
    if st.button("새로고침"):
        run_screen.clear()

st.title("📉 대형주 급락 종목 추천")
st.write(
    f"코스피·미국 대형주 중 **{lookback_label} 고점 대비 {drop}% 이상** 하락한 종목과 "
    "그 하락 원인을 보여줍니다."
)

with st.spinner("시세와 뉴스를 불러오는 중입니다… (처음엔 1~2분 걸릴 수 있어요)"):
    recs, ok = run_screen(market_keys, drop, lookback)

if not ok:
    st.error("시세 데이터를 가져오지 못했습니다. 잠시 후 새로고침해 주세요.")
    st.stop()

recs = [r for r in recs if r.analysis.cause.label in cause_filter]

# ---- 요약 ----
cols = st.columns(4)
cols[0].metric("추천 종목", f"{len(recs)}개")
for col, cause in zip(cols[1:], DropCause):
    col.metric(f"{CAUSE_ICON[cause]} {cause.label}", f"{sum(r.analysis.cause is cause for r in recs)}개")

if not recs:
    st.info("조건에 맞는 종목이 없습니다. 하락률 기준을 낮추거나 기간을 늘려 보세요.")
    st.caption(DISCLAIMER)
    st.stop()

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

# ---- 종목별 상세 ----
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
        with right:
            if r.closes is not None:
                df = r.closes.rename("종가").rename_axis("날짜").reset_index()
                chart = (
                    alt.Chart(df)
                    .mark_line(strokeWidth=2)
                    .encode(
                        x=alt.X("날짜:T", title=None),
                        y=alt.Y("종가:Q", title=None, scale=alt.Scale(zero=False)),
                        tooltip=[alt.Tooltip("날짜:T", format="%Y-%m-%d"), alt.Tooltip("종가:Q", format=",.2f")],
                    )
                    .properties(height=220)
                )
                st.altair_chart(chart, width="stretch")
                st.caption(f"고점 {r.peak_date:%Y-%m-%d} {_fmt_price(r.peak_price, r.currency)}")

st.divider()
st.caption(DISCLAIMER)
