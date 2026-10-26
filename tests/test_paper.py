import pytest

from stock_picker.paper import Portfolio, PortfolioStore, TradeError


def test_buy_krw_stock_deducts_cash_and_tracks_average():
    p = Portfolio("t", initial_cash=1_000_000, cash=1_000_000)
    p.buy("005930.KS", "삼성전자", "KRW", 5, 60_000)
    p.buy("005930.KS", "삼성전자", "KRW", 5, 50_000)
    pos = p.positions["005930.KS"]
    assert pos.quantity == 10
    assert pos.avg_price == 55_000
    assert p.cash == 450_000


def test_buy_usd_stock_converts_with_fx():
    p = Portfolio("t", initial_cash=1_000_000, cash=1_000_000)
    p.buy("NKE", "Nike", "USD", 2, 100.0, fx=1_400)
    assert p.cash == 1_000_000 - 280_000
    assert p.positions["NKE"].cost_krw == 280_000


def test_cannot_overspend_or_oversell():
    p = Portfolio("t", initial_cash=100_000, cash=100_000)
    with pytest.raises(TradeError):
        p.buy("005930.KS", "삼성전자", "KRW", 2, 60_000)
    p.buy("005930.KS", "삼성전자", "KRW", 1, 60_000)
    with pytest.raises(TradeError):
        p.sell("005930.KS", 2, 60_000)
    with pytest.raises(TradeError):
        p.sell("NKE", 1, 100)


def test_sell_realizes_pnl_and_closes_position():
    p = Portfolio("t", initial_cash=1_000_000, cash=1_000_000)
    p.buy("NKE", "Nike", "USD", 4, 100.0, fx=1_400)
    t = p.sell("NKE", 2, 120.0, fx=1_500)
    assert t.realized_krw == 2 * 120 * 1_500 - 2 * 100 * 1_400
    assert p.positions["NKE"].quantity == 2
    p.sell("NKE", 2, 90.0, fx=1_400)
    assert "NKE" not in p.positions
    assert p.cash == pytest.approx(1_000_000 + p.realized_pnl())


def test_valuation_and_total():
    p = Portfolio("t", initial_cash=1_000_000, cash=1_000_000)
    p.buy("005930.KS", "삼성전자", "KRW", 10, 50_000)
    p.buy("NKE", "Nike", "USD", 1, 100.0, fx=1_400)
    rows = {r["position"].ticker: r for r in p.valuation({"005930.KS": 55_000, "NKE": 110.0}, fx=1_400)}
    assert rows["005930.KS"]["pnl_krw"] == 50_000
    assert rows["NKE"]["pnl_pct"] == pytest.approx(0.10)
    # 현재가가 없으면 원가로 평가
    assert p.total_value({}, fx=1_400) == 1_000_000


def test_store_roundtrip(tmp_path):
    store = PortfolioStore(tmp_path)
    p = store.load("내 계좌")
    p.buy("005930.KS", "삼성전자", "KRW", 3, 60_000)
    store.save(p)
    loaded = store.load("내 계좌")
    assert loaded.positions["005930.KS"].quantity == 3
    assert loaded.trades[0].side == "BUY"
    assert loaded.cash == p.cash
    with pytest.raises(ValueError):
        store.load("   ")


def test_transaction_does_not_lose_concurrent_updates(tmp_path):
    store = PortfolioStore(tmp_path)
    stale = store.load("acct")  # 다른 탭에서 미리 열어 둔 화면
    with store.transaction("acct") as p:
        p.buy("005930.KS", "삼성전자", "KRW", 2, 50_000)
    with store.transaction("acct") as p:
        p.buy("005930.KS", "삼성전자", "KRW", 1, 50_000)
    assert stale.positions == {}
    assert store.load("acct").positions["005930.KS"].quantity == 3


def test_failed_trade_is_not_saved(tmp_path):
    store = PortfolioStore(tmp_path)
    with pytest.raises(TradeError):
        with store.transaction("acct") as p:
            p.buy("005930.KS", "삼성전자", "KRW", 10_000, 50_000)
    assert store.load("acct").cash == p.initial_cash


def test_similar_names_map_to_different_files(tmp_path):
    store = PortfolioStore(tmp_path)
    assert store._path("a b") != store._path("a_b")
