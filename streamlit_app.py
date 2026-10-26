"""웹 화면: `streamlit run streamlit_app.py`"""
from __future__ import annotations

import json
import math
import os
import random
import time
import urllib.parse
from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from stock_picker.config import ScreenConfig
from stock_picker.data import CachingProvider, YahooProvider
from stock_picker.models import DropCause, Market, Recommendation, Stock
from stock_picker.paper import KST, Portfolio, PortfolioStore, TradeError
from stock_picker.report import DISCLAIMER, _fmt_cap, _fmt_price, bottom_holding, buy_checks, range_position, still_falling
from stock_picker.screener import screen
from stock_picker.universe import BENCHMARKS, CURRENCY, get_universe

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
    DropCause.NO_CLEAR_REASON: ("🔍", "green"),
}
CAUSE_HELP = {
    DropCause.EVENT: "하루 급락이나 악재 뉴스(실적 부진, 소송, 증자 등)가 확인된 하락",
    DropCause.MARKET: "시장 지수도 크게 빠졌고, 이 종목만 유난히 더 빠지지는 않은 하락",
    DropCause.NO_CLEAR_REASON: "급락일도 악재 뉴스도 없이 서서히 빠진 하락",
}
MARKETS = {"코스피 + 미국": ("KOSPI", "US"), "코스피": ("KOSPI",), "미국": ("US",)}
PERIODS = {"최근 3개월": 60, "최근 6개월": 120, "최근 1년": 250}
SORTS = ["많이 빠진 순", "하락이 멈춘 종목 먼저", "시장보다 더 빠진 순"]

# 화면을 바꿔도 조건이 초기화되지 않도록 위젯 값을 다시 써 둔다 (Streamlit은 화면에 없는 위젯 값을 지운다).
for _k in ("c_market", "c_drop", "c_period", "c_sort", "c_causes", "account", "paper_buy_ticker"):
    if _k in st.session_state:
        st.session_state[_k] = st.session_state[_k]
# 조건 기본값은 세션에 한 번만 넣는다 (위젯에 기본값을 또 주면 Streamlit이 경고한다).
for _k, _v in {"c_market": "코스피 + 미국", "c_drop": 20, "c_period": "최근 3개월", "c_sort": SORTS[0],
               "c_causes": [c.label for c in DropCause]}.items():
    st.session_state.setdefault(_k, _v)
if st.session_state.c_sort not in SORTS:  # 이전 버전에서 저장된 정렬 이름
    st.session_state.c_sort = SORTS[0]
st.session_state.c_causes = [c for c in st.session_state.c_causes if c in {d.label for d in DropCause}]
if "account" not in st.session_state:
    # 주소(?acct=...)에 계좌 이름이 있으면 그 계좌를, 없으면 방문자마다 다른 임의 계좌를 연다.
    st.session_state.account = st.query_params.get("acct") or (
        "계좌-" + "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=5))
    )
# 새로고침해도 같은 계좌가 열리도록 계좌 이름을 주소에 남긴다.
if st.session_state.account.strip() and st.query_params.get("acct") != st.session_state.account:
    st.query_params["acct"] = st.session_state.account


@st.cache_resource
def get_provider() -> CachingProvider:
    """모든 사용자가 같이 쓰는 데이터 캐시 (1시간)."""
    return CachingProvider(YahooProvider(), ttl=CACHE_TTL)


@st.cache_resource
def get_store() -> PortfolioStore:
    """계좌 저장소. 모든 사용자가 같은 인스턴스(같은 계좌별 잠금)를 쓴다."""
    return PortfolioStore(os.environ.get("PAPER_DATA_DIR", "data/portfolios"))


STORE = get_store()


def quote(ticker: str) -> float | None:
    """주문·평가용 가격: 추천 화면과 같은 캐시의 가장 최근 가격 (최대 1시간 전)."""
    s = get_provider().closes(ticker, 5)
    return None if s.empty else float(s.iloc[-1])


def fmt_krw(v: float) -> str:
    return f"{v:,.0f}원"


def md_price(v: float, currency: str) -> str:
    """마크다운에 넣을 가격. '$'가 수식 기호로 해석되지 않도록 이스케이프한다."""
    return _fmt_price(v, currency).replace("$", r"\$")


def md_text(text: str) -> str:
    """뉴스 제목 등 외부 문자열을 마크다운에 안전하게 넣는다."""
    for ch in "\\$[]*_`<>":
        text = text.replace(ch, "\\" + ch)
    return text


def news_search_url(stock: Stock, around: datetime | None) -> str:
    korean = stock.market is Market.KOSPI
    q = f"{stock.name} 주가" if korean else f"{stock.name} stock"
    if around is not None:
        q += f" after:{(around - timedelta(days=3)):%Y-%m-%d} before:{(around + timedelta(days=3)):%Y-%m-%d}"
    lang = "hl=ko&gl=KR&ceid=KR:ko" if korean else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/search?{lang}&q=" + urllib.parse.quote(q)


def pct_down(dd: float) -> float:
    """하락률을 0 쪽으로 잘라 % 단위로 (예: -24.98% → -24.9). 반올림하면 기준(25%)을 넘은 것처럼 보일 수 있다.
    잘라내기 전에 부동소수 오차를 정리한다 (-0.46이 -0.45999…로 저장돼 -45.9%가 되지 않도록)."""
    return math.trunc(round(dd * 100, 6) * 10) / 10


def fmt_pct(dd: float) -> str:
    """화면 전체에서 쓰는 하락·등락률 표기 (소수 첫째 자리, 0 쪽으로 자름)."""
    return f"{pct_down(dd):+.1f}%"


def updown_color(v) -> str:
    """표 숫자 색: 한국 관례대로 오르면 빨강, 내리면 파랑."""
    # 화면에 0.00%로 보이는 값(부동소수 오차 포함)은 색을 칠하지 않는다.
    if not isinstance(v, (int, float)) or pd.isna(v) or round(v, 2) == 0:
        return ""
    return "color: #d6334a" if v > 0 else "color: #1F5FBF"


def stat_grid(items: list[tuple[str, str, str]]) -> None:
    """핵심 수치 카드 묶음 (넓은 화면 4칸, 휴대폰 2칸). items: (이름, 값, 설명)."""
    cells = "".join(f"<div><small>{k}</small><b>{v}</b><small>{d}</small></div>" for k, v, d in items)
    st.markdown(f'<div class="stat-grid">{cells}</div>', unsafe_allow_html=True)


st.markdown(
    """
<style>
.stat-grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:10px; margin:4px 0 10px; }
.stat-grid > div { border:1px solid rgba(128,128,128,.25); border-radius:8px; padding:8px 12px;
  display:flex; flex-direction:column; gap:2px; min-width:0; }
.stat-grid b { font-size:1.15rem; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.stat-grid small { opacity:.7; }
@media (max-width: 640px) { .stat-grid { grid-template-columns:repeat(2, minmax(0,1fr)); } }
</style>
""",
    unsafe_allow_html=True,
)


def signal_line(r: Recommendation) -> str:
    """상세 카드 맨 위의 판단 요약: 상태를 작은 배지 몇 개로."""
    a = r.analysis
    parts = []
    if still_falling(r):
        parts.append(":blue-badge[아직 하락 중]")
    elif r.trend_5d is not None and r.trend_5d >= 0.03:
        parts.append(":red-badge[반등 중]")
    elif bottom_holding(r):
        parts.append(f":gray-badge[하락 멈춤 · {r.days_since_low}거래일째 새 저점 없음]")
    else:
        parts.append(":gray-badge[바닥 확인 중]")
    if a.key_news:
        what = "급락 뒤 실적 악재 뉴스" if a.key_news_late else "급락일 무렵 악재 뉴스"
        parts.append(f":orange-badge[{what} {a.key_news}건]")
    elif a.event_news:
        parts.append(f":orange-badge[악재 뉴스 {len(a.event_news)}건]")
    if a.worst_day_return <= -0.08:
        parts.append(f":orange-badge[하루 {fmt_pct(a.worst_day_return)} 급락]")
    pos = range_position(r)
    if pos is not None:
        parts.append(":gray-badge[1년 최저권]" if pos <= 0.1 else f":gray-badge[1년 범위 하위 {pos:.0%}]")
    return " ".join(parts)


def badge(cause: DropCause) -> str:
    icon, color = CAUSE_STYLE[cause]
    return f":{color}-badge[{icon} {cause.label}]"


# ======================= 주문 처리 (버튼 콜백) =======================
def set_msg(kind: str, text: str) -> None:
    """주문 결과를 기록한다. 결과는 주문한 폼 바로 아래에 고정으로 보이고, 토스트로도 한 번 뜬다."""
    st.session_state.trade_msg = (kind, text, st.session_state.get("_order_where", ""), datetime.now(KST))
    st.session_state.toast_pending = True


def go_paper() -> None:
    st.session_state.view = VIEW_PAPER
    if st.session_state.get("detail"):  # 보던 종목을 '사기' 칸에 미리 골라 둔다
        st.session_state.paper_buy_ticker = st.session_state.detail


def show_msg(where: str) -> None:
    msg = st.session_state.get("trade_msg")
    if msg and msg[2] == where:
        (st.success if msg[0] == "ok" else st.error)(f"{msg[1]} ({msg[3]:%H:%M:%S})", icon="✅" if msg[0] == "ok" else "⚠️")
        if msg[0] == "ok" and where.startswith("rec:"):
            st.button("💼 모의투자 계좌에서 보기", key=f"go:{where}", on_click=go_paper)


def load_portfolio() -> Portfolio | None:
    """계좌를 연다. 열 수 없으면 None을 돌려주고 이유를 st.session_state.account_error에 남긴다."""
    st.session_state.account_error = None
    if not st.session_state.account.strip():
        st.session_state.account_error = "계좌 이름을 입력하세요."
        return None
    try:
        return STORE.load(st.session_state.account)
    except (ValueError, KeyError, TypeError, AttributeError):
        st.session_state.account_error = (
            "이 계좌의 저장 파일이 손상돼 열 수 없습니다. 백업 파일이 있다면 다른 계좌 이름으로 복원하거나, "
            "새 계좌 이름을 입력해 다시 시작하세요."
        )
        return None


def place_buy(ticker: str, name: str, currency: str, qty_key: str, where: str) -> None:
    st.session_state._order_where = where
    # 폼 안의 수량은 제출 시점에 확정되므로 콜백에서 직접 읽는다.
    qty = int(st.session_state[qty_key])
    account = st.session_state.account
    price = quote(ticker)
    fx = quote(FX_TICKER) if currency == "USD" else 1.0
    if price is None or fx is None:
        set_msg("err", "현재가나 환율을 가져오지 못해 주문하지 못했습니다. 잠시 뒤 다시 시도하세요.")
        return
    try:
        # 최신 계좌를 다시 읽어서 거래해야 다른 탭에서 한 거래를 덮어쓰지 않는다.
        with STORE.transaction(account) as portfolio:
            t = portfolio.buy(ticker, name, currency, qty, price, fx)
    except TradeError as e:
        set_msg("err", str(e))
        return
    except ValueError as e:
        set_msg("err", f"계좌를 열 수 없습니다: {e}")
        return
    st.session_state[qty_key] = 1  # 같은 수량이 실수로 다시 주문되지 않도록 되돌린다
    set_msg(
        "ok", f"[{account.strip()}] {name} {qty}주 매수 ({md_price(price, currency)}, 총 {fmt_krw(t.amount_krw)})"
    )


def place_sell(ticker: str, currency: str, qty_key: str | None, where: str) -> None:
    """qty_key가 None이면 전량 매도."""
    st.session_state._order_where = where
    if qty_key is None:
        held = load_portfolio()
        qty = held.positions[ticker].quantity if held and ticker in held.positions else 0
    else:
        qty = int(st.session_state[qty_key])
    account = st.session_state.account
    price = quote(ticker)
    fx = quote(FX_TICKER) if currency == "USD" else 1.0
    if price is None or fx is None:
        set_msg("err", "현재가나 환율을 가져오지 못해 주문하지 못했습니다. 잠시 뒤 다시 시도하세요.")
        return
    try:
        with STORE.transaction(account) as portfolio:
            t = portfolio.sell(ticker, qty, price, fx)
    except (TradeError, ValueError) as e:
        set_msg("err", str(e))
        return
    if qty_key:
        st.session_state.pop(qty_key, None)
    set_msg("ok", f"{t.name} {qty}주 매도 (실현손익 {round(t.realized_krw) or 0:+,}원)")


INDEX_NAMES = {Market.KOSPI: "코스피 지수", Market.US: "S&P 500"}
STOCK_COLOR, INDEX_COLOR, SHOCK_COLOR = "#1F5FBF", "#9AA0A6", "#D9822B"


def drop_chart(r: Recommendation, provider: CachingProvider, config: ScreenConfig) -> alt.LayerChart:
    """종목과 시장 지수를 고점=100으로 맞춰 한 축에 그린다 (가격 단위가 달라도 비교할 수 있도록)."""
    a = r.analysis
    peak = pd.Timestamp(r.peak_date)
    stock = (r.closes / r.peak_price * 100).rename("값")
    frames = [pd.DataFrame({"날짜": stock.index, "값": stock.values, "가격": r.closes.values, "구분": r.stock.name})]
    idx = provider.closes(BENCHMARKS[r.stock.market], config.lookback_days)
    if not idx.empty:
        idx = idx[idx.index >= r.closes.index[0]]
    at_peak = idx[idx.index <= peak] if not idx.empty else idx
    if not idx.empty and not at_peak.empty:
        norm = idx / float(at_peak.iloc[-1]) * 100
        frames.append(pd.DataFrame({"날짜": norm.index, "값": norm.values, "가격": idx.values,
                                    "구분": INDEX_NAMES[r.stock.market]}))
    df = pd.concat(frames)
    names = list(df["구분"].unique())
    color = alt.Color("구분:N", title=None, scale=alt.Scale(domain=names, range=[STOCK_COLOR, INDEX_COLOR][: len(names)]),
                      legend=alt.Legend(orient="top", direction="horizontal"))
    x = alt.X("날짜:T", title=None, axis=alt.Axis(format="%m/%d", labelAngle=0))
    dash = alt.StrokeDash("구분:N", legend=None, scale=alt.Scale(domain=names, range=[[1, 0], [5, 3]][: len(names)]))
    lines = alt.Chart(df).mark_line(strokeWidth=2).encode(
        strokeDash=dash,
        x=x,
        y=alt.Y("값:Q", title="고점=100", scale=alt.Scale(zero=False)),
        color=color,
        tooltip=[alt.Tooltip("날짜:T", format="%Y-%m-%d"), "구분:N",
                 alt.Tooltip("값:Q", title="고점=100 기준", format=".1f"), alt.Tooltip("가격:Q", format=",.2f")],
    )
    marks = pd.DataFrame([
        {"날짜": peak, "값": 100.0, "표시": "고점"},
        {"날짜": r.closes.index[-1], "값": r.price / r.peak_price * 100, "표시": f"현재 {fmt_pct(r.drawdown)}"},
    ])
    points = alt.Chart(marks).mark_point(size=60, filled=True, color=STOCK_COLOR).encode(x="날짜:T", y="값:Q")
    # 고점 라벨은 점 오른쪽 위, 현재 라벨은 점 아래쪽에 둔다 (선·축 눈금과 겹치지 않도록).
    labels = alt.layer(
        alt.Chart(marks.iloc[[0]]).mark_text(dx=6, dy=-8, fontSize=11, align="left", fontWeight="bold")
        .encode(x="날짜:T", y="값:Q", text="표시"),
        alt.Chart(marks.iloc[[1]]).mark_text(dx=-6, dy=-10, fontSize=11, align="right", fontWeight="bold")
        .encode(x="날짜:T", y="값:Q", text="표시"),
    )
    level = 100 * (1 - config.drop_threshold)
    threshold = alt.Chart(pd.DataFrame({"y": [level]})).mark_rule(strokeDash=[4, 4], color=INDEX_COLOR).encode(y="y:Q")
    threshold_text = alt.Chart(
        pd.DataFrame({"x": [r.closes.index[0]], "y": [level], "t": [f"고점 -{config.drop_threshold:.0%} 기준"]})
    ).mark_text(align="left", dy=-6, fontSize=10, color=INDEX_COLOR).encode(x="x:T", y="y:Q", text="t")
    layers = [lines, threshold, threshold_text, points, labels]
    if a.worst_day is not None and a.worst_day_return <= -config.event_day_drop:
        layers.append(alt.Chart(pd.DataFrame({"x": [a.worst_day]})).mark_rule(color=SHOCK_COLOR, strokeDash=[2, 2])
                      .encode(x="x:T"))
    return alt.layer(*layers).properties(height=240)


def per_note(per: float | None) -> str:
    """PER을 대략적인 구간으로 풀어 쓴다. 업종마다 적정 수준이 달라 참고용이다."""
    if not per:
        return ""
    return "낮은 편" if per < 10 else "보통" if per < 25 else "높은 편"


def render_detail(r: Recommendation) -> None:
    """선택한 종목 하나의 상세 화면 (차트는 이 종목 것만 그린다)."""
    a = r.analysis
    shock = a.worst_day is not None and a.worst_day_return <= -config.event_day_drop
    with st.container(border=True):
        st.markdown(f"### {r.stock.name} <small>{r.stock.ticker}</small>", unsafe_allow_html=True)
        st.markdown(f"{badge(a.cause)} &nbsp; {CAUSE_HELP[a.cause]}")
        if line := signal_line(r):
            st.markdown(line)
        stat_grid([
            ("현재가", _fmt_price(r.price, r.currency), f"고점 {_fmt_price(r.peak_price, r.currency)} ({r.peak_date:%m/%d})"),
            ("고점 대비", fmt_pct(r.drawdown), f"시장 대비 {fmt_pct(a.excess_return)}p"),
            ("고점까지 오르면", fmt_pct(r.peak_price / r.price - 1), "고점 회복 시 수익률 (보장 아님)"),
            ("PER" if r.per else "1년 평균가 대비",
             f"{r.per:.1f}" if r.per else ("–" if not r.avg_52w else fmt_pct(r.price / r.avg_52w - 1)),
             f"최근 12개월 이익 기준 · {per_note(r.per)}" if r.per else "1년 평균 종가와 비교"),
        ])
        left, right = st.columns([3, 2])
        with left:
            st.write(a.summary.replace("$", r"\$"))
            facts = []
            if r.low_since_peak:
                facts.append(f"고점 이후 최저 {md_price(r.low_since_peak, r.currency)}")
            if r.avg_52w and r.per:
                facts.append(f"1년 평균가 대비 {fmt_pct(r.price / r.avg_52w - 1)}")
            if r.pbr:
                facts.append(f"PBR {r.pbr:.2f}")
            if facts:
                st.markdown(" · ".join(facts))
            if r.per or r.pbr:
                st.caption(
                    "PER(주가÷1주당 이익)이 10 아래면 낮은 편, 25 위면 높은 편으로 대략 나눴습니다. "
                    "업종마다 적정 수준이 달라 같은 업종끼리 비교하는 것이 좋습니다."
                )
        with right:
            if r.closes is not None:
                st.altair_chart(drop_chart(r, provider, config), width="stretch")
                st.caption(
                    f"고점을 100으로 맞춰 {INDEX_NAMES[r.stock.market]}와 비교 · 점선: 고점보다 {drop}% 낮은 선"
                    + (" · 주황 선: 급락일" if shock else "")
                )

        st.markdown("**매수 전에 확인하세요**")
        st.markdown("\n".join(f"- {n}" for n in buy_checks(r)))

        if shock and not a.key_news:
            st.markdown(
                f"{a.worst_day:%m/%d} 급락 무렵의 원인 뉴스는 자동으로 찾지 못했습니다. "
                f"[그 무렵 뉴스 직접 찾아보기]({news_search_url(r.stock, a.worst_day)})"
            )
        if a.event_news:
            st.markdown("**관련 악재 뉴스**")
            for i, n in enumerate(a.event_news[:4]):
                title = f"[{md_text(n.title)}]({n.url})" if n.url else md_text(n.title)
                if i < a.key_news:
                    mark = "🔑 급락 이유로 보이는 뉴스 · "
                elif shock and n.published > a.worst_day:
                    mark = "급락 이후 보도 · "
                else:
                    mark = ""
                st.markdown(f"- {mark}{n.published:%m/%d} · {title} " + " ".join(f":orange-badge[{k}]" for k in n.matched_keywords))
        else:
            if not shock:
                if a.news_checked:
                    st.caption(f"고점 이후 뉴스 {a.news_checked}건을 확인했지만 악재 표현은 없었습니다.")
                else:
                    st.markdown(f"관련 뉴스를 찾지 못했습니다. [뉴스 직접 찾아보기]({news_search_url(r.stock, None)})")
            latest = sorted(
                (n for n in r.news if n.published and n.published >= r.peak_date), key=lambda n: n.published, reverse=True
            )[:3]
            for n in latest:
                title = f"[{md_text(n.title)}]({n.url})" if n.url else md_text(n.title)
                st.markdown(f"- {n.published:%m/%d} · {title}")

        key = f"ai:{r.stock.ticker}:{r.peak_date:%Y%m%d}"
        if AI_ENABLED:
            if key not in st.session_state and st.button("🤖 AI로 원인 자세히 보기", key=f"btn:{key}"):
                from stock_picker.ai import explain

                with st.spinner("Claude가 웹에서 원인을 찾는 중…"):
                    st.session_state[key] = explain(r) or "AI 설명을 가져오지 못했습니다."
            if key in st.session_state:
                st.markdown("**AI 분석**")
                st.write(st.session_state[key])

        qty_key = f"qty:{r.stock.ticker}"
        st.session_state.setdefault(qty_key, 1)
        unit = quote(r.stock.ticker)
        fx = quote(FX_TICKER) if r.currency == "USD" else 1.0
        portfolio = load_portfolio()
        if unit and fx and portfolio:
            per_share = unit * fx
            st.caption(
                f"1주 ≈ {fmt_krw(per_share)} · 내 계좌({portfolio.name}) 현금으로 최대 {int(portfolio.cash // per_share):,}주"
            )
        with st.form(key=f"buyform:{r.stock.ticker}", border=False, enter_to_submit=False):
            fc = st.columns([2, 1], vertical_alignment="bottom")
            fc[0].number_input("모의 매수 수량(주)", min_value=1, step=1, key=qty_key)
            fc[1].form_submit_button(
                "모의 매수", on_click=place_buy,
                args=(r.stock.ticker, r.stock.name, r.currency, qty_key, f"rec:{r.stock.ticker}"),
                width="stretch",
            )
        show_msg(f"rec:{r.stock.ticker}")
        st.caption("가상 돈으로 하는 연습 매수입니다. 이 화면은 투자 권유가 아닙니다.")


def cause_color(text: str) -> str:
    colors = {DropCause.EVENT: "#c2410c", DropCause.MARKET: "#6d28d9", DropCause.NO_CLEAR_REASON: "#15803d"}
    for cause, color in colors.items():
        if cause.label in str(text):
            return f"color: {color}"
    return ""


@st.cache_resource
def _screen_results() -> dict:
    return {}


def cached_screen(
    provider: CachingProvider, markets: list[Market], config: ScreenConfig, on_progress
) -> tuple[list, dict]:
    """같은 조건의 분석 결과를 10분 동안 재사용한다. 버튼·토글을 누를 때마다 전체를 다시 계산하지 않도록.
    데이터가 새로 받아지면(provider.version이 바뀌면) 다시 계산한다."""
    key = (tuple(m.value for m in markets), config.drop_threshold, config.lookback_days, provider.version)
    store = _screen_results()
    hit = store.get(key)
    if hit and time.time() - hit[0] < 600:
        return list(hit[1]), dict(hit[2])
    stats: dict = {}
    result = screen(provider, markets, config, on_progress=on_progress, stats=stats)
    # 오래된 결과는 지우고, 조건 조합별로 최근 결과만 남긴다.
    for k in [k for k, v in store.items() if time.time() - v[0] >= 600 or k[3] != provider.version]:
        store.pop(k, None)
    store[key] = (time.time(), result, stats)
    return list(result), dict(stats)


# ======================= 상단 =======================
VIEW_RECS, VIEW_PAPER = "📉 급락 종목 찾기", "💼 모의투자"
view = st.radio("화면", [VIEW_RECS, VIEW_PAPER], key="view", horizontal=True, label_visibility="collapsed")

market_label, drop = st.session_state.c_market, st.session_state.c_drop
period_label, sort_label = st.session_state.c_period, st.session_state.c_sort
cause_labels = st.session_state.c_causes

if view == VIEW_RECS:
    st.title("📉 급락 대형주")
    st.markdown(
        f"{market_label.replace(' + ', '·')} **대형주** 가운데 **{period_label} 고점보다 {drop}% 이상 싸진 종목**을 찾고, "
        "**왜 빠졌는지** 함께 알려드립니다."
    )
    # 제목이 바뀌면 펼친 상자가 닫히므로 제목은 고정하고, 현재 조건은 위 문장에 보여준다.
    with st.expander("🔧 조건 바꾸기 (시장 · 하락폭 · 기간 · 정렬)"):
        c1, c2 = st.columns(2)
        c1.radio("시장", list(MARKETS), key="c_market", horizontal=True)
        c2.select_slider("고점 대비 최소 하락폭", options=[10, 15, 20, 25, 30, 40, 50],
                         format_func=lambda v: f"{v}%", key="c_drop")
        c3, c4 = st.columns(2)
        c3.selectbox("고점을 찾을 기간", list(PERIODS), key="c_period")
        c4.selectbox("정렬", SORTS, key="c_sort")
        st.multiselect("하락 원인", [c.label for c in DropCause], key="c_causes")
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

    all_recs, scan = cached_screen(provider, markets, config, on_progress)
progress.empty()
# 지수 시세가 있는 시장만 보여준다 (지수가 없으면 '시장 탓인지'를 판정할 수 없다).
ok_markets = [m for m in markets if not provider.closes(BENCHMARKS[m], 5).empty]
missing_markets = [m for m in markets if m not in ok_markets]
as_of = provider.as_of([BENCHMARKS[m] for m in ok_markets]) if ok_markets else None
_stamp = provider.loaded_at([BENCHMARKS[m] for m in ok_markets])
loaded = datetime.fromtimestamp(_stamp, KST).strftime("%H:%M") if _stamp else "-"
data_ok = as_of is not None
if not data_ok:
    all_recs = []  # 시장 지수가 없으면 원인 판정이 틀리므로 아무것도 보여주지 않는다

if sort_label == "하락이 멈춘 종목 먼저":
    all_recs.sort(key=lambda r: (still_falling(r), r.drawdown))
elif sort_label == "시장보다 더 빠진 순":
    all_recs.sort(key=lambda r: r.analysis.excess_return)
recs = [r for r in all_recs if r.analysis.cause.label in cause_labels]

# ======================= 급락 종목 찾기 =======================
if view == VIEW_RECS:
    top = st.columns([4, 1], vertical_alignment="center")
    if data_ok:
        top[0].caption(
            f"데이터 기준 **{as_of:%m/%d}** 종가 · {loaded} 불러옴 · 출처 Yahoo Finance, Google 뉴스 · 1시간마다 새로 불러옵니다"
        )
    if top[1].button("↻ 새로 불러오기", width="stretch"):
        provider.refresh_stale()  # 10분보다 오래된 것만 다시 받는다 (다른 사용자에게 부담을 주지 않도록)
        st.rerun()

    with st.expander("ℹ️ 이렇게 골라요"):
        st.markdown(
            f"""
- **대형주**: 시가총액이 코스피 {config.min_market_cap[Market.KOSPI] / 1e12:.0f}조원, 미국 {config.min_market_cap[Market.US] / 1e9:.0f}0억 달러 이상인 종목만 봅니다. 시가총액을 확인하지 못한 종목은 빼요.
- **싸진 종목**: {period_label} 동안의 가장 높은 종가보다 지금 가격이 {drop}% 이상 낮은 종목입니다.
- **하락 원인**은 세 가지로 나눕니다.
  - {badge(DropCause.MARKET)} 같은 기간 시장 지수(코스피·S&P 500)도 {config.market_drop:.0%} 이상 빠졌고, 이 종목이 시장보다 {config.excess_drop:.0%}p 넘게 더 빠지지 않았으며, 종목 하락률이 지수 하락률의 {config.market_ratio:g}배를 넘지 않는 경우
  - {badge(DropCause.EVENT)} 하루에 {config.event_day_drop:.0%} 이상 급락한 날이 있거나, 고점 이후 뉴스 제목에 실적 부진·소송·증자 같은 악재 표현이 있는 경우. 급락일 ±3일 안의 악재(실적 관련 보도는 급락 후 2주까지)를 급락 이유로 봅니다
  - {badge(DropCause.NO_CLEAR_REASON)} 위 두 경우가 아닌 경우
- 뉴스 판정은 제목만 보고 자동으로 하므로 틀릴 수 있습니다. 근거 뉴스를 꼭 직접 읽어 보세요.
"""
        )

    if not data_ok:
        st.error("시세를 불러오지 못했습니다. 잠시 뒤 '새로 불러오기'를 눌러 주세요.")
    else:
        if missing_markets:
            names = ", ".join({Market.KOSPI: "코스피", Market.US: "미국"}[m] for m in missing_markets)
            st.warning(f"{names} 시세를 불러오지 못해 그 시장 종목은 빼고 보여드립니다. 잠시 뒤 '새로 불러오기'를 눌러 주세요.")
        counts = " ".join(
            f"{badge(c)} **{sum(r.analysis.cause is c for r in all_recs)}**" for c in DropCause
        )
        shown = f" (원인 필터로 {len(recs)}개 표시)" if len(recs) != len(all_recs) else ""
        st.markdown(f"#### 찾은 종목 {len(all_recs)}개{shown} &nbsp; {counts}")
        if scan:
            st.caption(
                f"후보 {scan['checked']}개 검사 → {drop}% 이상 하락 {scan['dropped']}개 → "
                f"시가총액 기준을 넘는 대형주 {scan['large']}개"
                + (f" (시장 지수가 없는 {names} 종목 제외)" if missing_markets else "")
            )
        if not all_recs:
            st.info("조건에 맞는 종목이 없습니다. '조건 바꾸기'에서 하락폭을 낮추거나 기간을 늘려 보세요.")
        elif not recs:
            st.info("고른 하락 원인에 해당하는 종목이 없습니다. '조건 바꾸기'에서 하락 원인을 더 골라 보세요.")

    if recs:
        detailed = st.toggle("모든 정보 보기", value=False, help="끄면 휴대폰에서도 보기 쉬운 핵심 열만 보여줍니다.")
        table = pd.DataFrame(
            [
                {
                    "종목": r.stock.name,
                    "하락 원인": f"{CAUSE_STYLE[r.analysis.cause][0]} {r.analysis.cause.label}",
                    "고점 대비": pct_down(r.drawdown),
                    "1년 저점 대비": None if not r.low_52w else pct_down(r.price / r.low_52w - 1),
                    "5일 등락": None if r.trend_5d is None else pct_down(r.trend_5d),
                    "시장보다 더 빠진 폭": min(r.analysis.excess_return, 0) * -100,
                    "현재가": _fmt_price(r.price, r.currency),
                    "PER": "–" if r.per is None else f"{r.per:.1f}",
                    "시가총액": _fmt_cap(r.market_cap, r.currency),
                    "종목코드": r.stock.ticker,
                }
                for r in recs
            ]
        )
        if not detailed:
            table = table[["종목", "하락 원인", "고점 대비"]]
        styled = (
            table.style.format(
                {"고점 대비": "{:+.1f}%", "1년 저점 대비": "{:+.1f}%", "5일 등락": "{:+.1f}%",
                 "시장보다 더 빠진 폭": "{:.1f}%p"},
                na_rep="–",
            )
            .map(updown_color, subset=[c for c in ("고점 대비", "5일 등락") if c in table.columns])
            .map(cause_color, subset=["하락 원인"])
        )
        picked = st.dataframe(
            styled,
            hide_index=True,
            width="stretch" if detailed else "content",
            on_select="rerun",
            selection_mode="single-row",
            key="rec_table",
            column_config={
                "고점 대비": st.column_config.Column(help="기간 내 최고 종가 대비 현재가"),
                "1년 저점 대비": st.column_config.Column(
                    help="1년 중 가장 낮았던 종가보다 지금 얼마나 높은지. 0%에 가까울수록 1년 중 가장 싼 가격 근처"
                ),
                "5일 등락": st.column_config.Column(help="최근 5거래일 등락률. 음수면 아직 하락 중"),
                "시장보다 더 빠진 폭": st.column_config.Column(help="같은 기간 시장 지수보다 몇 %p 더 빠졌는지"),
                "PER": st.column_config.TextColumn(
                    help="주가수익비율(최근 12개월 이익 기준). 낮을수록 이익 대비 싼 편 (적자거나 정보가 없으면 –)"
                ),
            },
        )
        # 표에서 고른 종목을 보여준다 (선택을 풀면 마지막으로 보던 종목, 처음엔 첫 종목).
        rows = picked.selection.rows if picked is not None else []
        if rows:
            st.session_state.detail = recs[rows[0]].stock.ticker
        by_ticker = {r.stock.ticker: r for r in recs}
        if st.session_state.get("detail") not in by_ticker:
            st.session_state.detail = recs[0].stock.ticker
        st.caption(
            f"👆 표 왼쪽 칸을 눌러 종목을 고르면 아래 내용이 바뀝니다 (지금: **{by_ticker[st.session_state.detail].stock.name}**). "
            "표의 종목은 조건에 맞는 하락 종목일 뿐, 매수 추천이 아닙니다."
        )
        render_detail(by_ticker[st.session_state.detail])

    st.divider()
    st.caption(DISCLAIMER)


# 주문 결과 토스트 (보조 알림). 고정 메시지는 각 폼 아래에 show_msg로 보여준다.
if st.session_state.pop("toast_pending", False) and (msg := st.session_state.get("trade_msg")):
    st.toast(msg[1], icon="✅" if msg[0] == "ok" else "⚠️")

if view == VIEW_PAPER:
    st.title("💼 모의투자")
    st.caption("가상 돈으로 사고팔며 연습해 보세요. 화면에 보이는 최근 가격(최대 1시간 전)으로 체결되고, 미국 주식은 그때 환율로 원화로 계산합니다. 수수료·세금은 빼지 않습니다.")
    st.text_input(
        "계좌 이름", key="account",
        help="같은 이름을 입력하면 같은 계좌가 열립니다. 로그인이 없으니 남이 쓰기 어려운 이름을 쓰세요.",
    )
    st.caption(
        "💡 지금 주소창의 주소(끝에 ?acct=… 가 붙은 주소)를 즐겨찾기해 두면 언제든 이 계좌가 다시 열립니다. "
        "계좌 이름을 기억했다가 직접 입력해도 됩니다."
    )
    portfolio = load_portfolio()
    if portfolio is None:
        st.error(st.session_state.account_error or "계좌를 열 수 없습니다.")
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
        # 한국 증시 관례: 오르면 빨강, 내리면 파랑. 소수점 오차로 '-0원'이 나오지 않게 원 단위로 반올림한다.
        v = round(v) or 0
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
        more = st.toggle("보유 종목 자세히 보기", value=False, key="holdings_more")
        now_recs = {r.stock.ticker: r for r in all_recs}

        def status(ticker: str) -> str:
            # 지금도 급락 종목 목록에 있으면 그 원인과 고점 대비를, 없으면 목록에서 빠졌다고 알려준다.
            rec = now_recs.get(ticker)
            if rec is None:
                return "급락 목록에 없음"
            return f"{CAUSE_STYLE[rec.analysis.cause][0]} {fmt_pct(rec.drawdown)}"

        holdings = pd.DataFrame(
                [
                    {
                        "종목": r["position"].name,
                        "수량": r["position"].quantity,
                        "수익률": None if r["pnl_pct"] is None else r["pnl_pct"] * 100,
                        "평가손익": "-" if r["pnl_krw"] is None else f"{round(r['pnl_krw']) or 0:+,}원",
                        "지금 상태": status(r["position"].ticker),
                        "평균 매수가": _fmt_price(r["position"].avg_price, r["position"].currency),
                        "현재가": "-" if r["price"] is None else _fmt_price(r["price"], r["position"].currency),
                        "평가액": "-" if r["value_krw"] is None else fmt_krw(r["value_krw"]),
                        "종목코드": r["position"].ticker,
                    }
                    for r in rows
                ]
            )
        if not more:
            holdings = holdings[["종목", "수량", "수익률", "지금 상태"]]
        st.dataframe(
            holdings.style.format({"수익률": "{:+.2f}%"}, na_rep="–").map(updown_color, subset=["수익률"]),
            hide_index=True,
            width="stretch",
            column_config={"지금 상태": st.column_config.Column(
                help="이 종목이 지금도 급락 종목 목록에 있는지, 있다면 하락 원인 아이콘과 고점 대비 하락률")},
        )
    else:
        st.info("아직 보유 종목이 없습니다. 아래에서 사거나, '급락 종목 찾기'에서 종목을 골라 바로 살 수 있어요.")

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
                return f"📉 {s.name} · 고점 대비 {fmt_pct(rec_by_ticker[t].drawdown)}"
            return f"{s.name} ({t})"

        ticker = st.selectbox("종목 (📉 = 지금 급락 종목)", [s.ticker for s in options], format_func=label,
                              key="paper_buy_ticker")
        stock = by_ticker[ticker]
        currency = CURRENCY[stock.market]
        price = quote(ticker)
        fx_buy = quote(FX_TICKER) if currency == "USD" else 1.0
        if price is None:
            st.caption("현재가 확인 불가")
        elif fx_buy is None:
            st.caption(f"현재가 {md_price(price, currency)} · 환율을 확인할 수 없어 지금은 미국 주식을 살 수 없습니다")
        else:
            per_share = price * fx_buy
            st.caption(
                f"현재가 {md_price(price, currency)} · 1주 ≈ {fmt_krw(per_share)} · "
                f"현금으로 최대 {int(portfolio.cash // per_share):,}주"
            )
        with st.form("paper_buy_form", border=False, enter_to_submit=False):
            st.session_state.setdefault("paper_buy_qty", 1)
            st.number_input("수량(주)", min_value=1, step=1, key="paper_buy_qty")
            st.form_submit_button(
                "사기", type="primary", on_click=place_buy,
                args=(ticker, stock.name, currency, "paper_buy_qty", "paper_buy"),
            )
        show_msg("paper_buy")
    with sell_col:
        st.subheader("팔기")
        if portfolio.positions:
            # 객체 대신 티커를 옵션으로 써야 매도 후 수량 표시가 바로 갱신된다.
            sell_ticker = st.selectbox(
                "보유 종목",
                list(portfolio.positions),
                format_func=lambda t: portfolio.positions[t].name,
                key="paper_sell_ticker",
            )
            pos = portfolio.positions[sell_ticker]
            now = "확인 불가" if sell_ticker not in prices else md_price(prices[sell_ticker], pos.currency)
            st.caption(f"보유 {pos.quantity}주 · 현재가 {now}")
            # 보유 수량을 키에 넣어, 팔고 나면 입력칸이 새로 만들어지게 한다 (이전 값이 남지 않도록).
            qty_key = f"paper_sell_qty:{sell_ticker}:{pos.quantity}"
            with st.form("paper_sell_form", border=False, enter_to_submit=False):
                # 보유량을 넘는 수량은 입력칸 대신 주문할 때 한국어로 안내한다.
                st.number_input("수량(주)", min_value=1, value=1, step=1, key=qty_key)
                b1, b2 = st.columns(2)
                b1.form_submit_button("팔기", on_click=place_sell, args=(sell_ticker, pos.currency, qty_key, "paper_sell"))
                b2.form_submit_button(
                    f"전부 팔기 ({pos.quantity}주)", on_click=place_sell,
                    args=(sell_ticker, pos.currency, None, "paper_sell"),
                )
        else:
            st.caption("팔 종목이 없습니다.")
        show_msg("paper_sell")

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
                        "실현손익": f"{round(t.realized_krw) or 0:+,}원" if t.side == "SELL" else "",
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
                st.session_state._order_where = "manage"
                set_msg("ok", "백업 파일로 복원했습니다.")
                st.rerun()
        show_msg("manage")
        st.divider()
        start_cash = st.number_input(
            "초기화할 시작 금액(원)", min_value=1_000_000, value=max(1_000_000, int(portfolio.initial_cash)),
            step=1_000_000
        )
        confirm = st.checkbox("보유 종목과 거래 내역을 모두 지우는 데 동의합니다.", key="reset_confirm")

        def reset_account(cash: float) -> None:
            STORE.replace(Portfolio(name=portfolio.name, initial_cash=cash, cash=cash))
            st.session_state.reset_confirm = False
            st.session_state._order_where = "manage"
            set_msg("ok", f"{fmt_krw(cash)}으로 계좌를 초기화했습니다.")

        st.button("계좌 초기화", disabled=not confirm, on_click=reset_account, args=(float(start_cash),))

    st.divider()
    st.caption("※ 모의투자 결과는 실제 수익을 보장하지 않습니다. " + DISCLAIMER.removeprefix("※ "))
