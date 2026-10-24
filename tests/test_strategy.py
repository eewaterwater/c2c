import numpy as np
import pandas as pd
from config import settings as S
from core import strategy_engine as se, market_feed as mf


def bars(rows, start="2026-10-12 12:00"):
    idx = pd.date_range(start, periods=len(rows), freq="2h", tz=S.TIMEZONE)
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def test_plan_split_and_risk():
    p = se.build_plan(1000.0, 2.0)
    assert p["qty"] == {"t1": 4, "t2a": 3, "t2b": 3}
    assert p["stop_a"] == 995.5
    assert p["risk_thb"] == 9000 and p["risk_thb"] < S.EQUITY * 0.01
    assert p["t1_target"] == 1005.0            # max(5.0, 1.2*2.0)
    assert se.build_plan(1000.0, 6.0)["t1_target"] == 1007.2


def test_risk_cap_limits_size():
    p = se.build_plan(1000.0, 2.0, contracts=50)
    assert sum(p["qty"].values()) == 11 and p["risk_thb"] <= S.EQUITY * S.MAX_RISK_PCT


def test_stop_out_all():
    p = se.build_plan(1000.0, 2.0)
    ev = se.process_bars(p, bars([[1000, 1002, 995, 996]]))
    assert not p["open"] and p["exits"] and se.pnl_thb(p) == -9000


def test_t1_fill_ratchets_then_breakeven_stop():
    p = se.build_plan(1000.0, 2.0)
    se.process_bars(p, bars([[1000, 1006, 999, 1005], [1005, 1006, 999.5, 1000]] , start="2026-10-12 12:00"))
    assert p["t1_filled"]
    # bar 2 low 999.5 <= entry(1000): runners stopped at breakeven; T1 banked +5*4
    assert not p["open"]
    assert round(se.pnl_thb(p)) == 4 * 5 * 200


def test_t2a_and_trail():
    p = se.build_plan(1000.0, 2.0)
    rows = [[1000, 1006, 1000.5, 1005], [1005, 1013, 1004, 1012], [1012, 1020, 1011, 1019]]
    se.process_bars(p, bars(rows))
    assert not p["alive"]["t1"] and not p["alive"]["t2a"]
    assert p["alive"]["t2b"] and p["stop_b"] > 1000        # trail above breakeven


def test_stagnation_exit():
    p = se.build_plan(1000.0, 2.0)
    flat = [[1000, 1002, 998, 1000]] * 6
    ev = se.process_bars(p, bars(flat))
    assert not p["open"] and any("stagnation" in e for e in ev)


def test_no_double_processing():
    p = se.build_plan(1000.0, 2.0)
    b = bars([[1000, 1002, 998, 1000]] * 2)
    se.process_bars(p, b)
    se.process_bars(p, b)
    assert p["bars_held"] == 2


def _daily(trend):
    n = 260
    close = pd.Series(np.linspace(900, 900 + trend * n, n))
    return pd.DataFrame({"open": close, "high": close + 3, "low": close - 3, "close": close})


def test_regime_gates_uptrend_passes_downtrend_fails():
    assert all(g.passed for g in se.regime_gates(_daily(+1.0))[:2])
    assert not se.regime_gates(_daily(-1.0))[0].passed


def test_extension_gate_blocks_spike():
    d = _daily(+1.0)
    d.iloc[-1] = [d["close"].iloc[-2] + 60] * 4
    assert not se.regime_gates(d)[2].passed


def test_to_2h_pairs_bars():
    idx = pd.date_range("2026-10-12 03:00", periods=4, freq="1h", tz="UTC")   # 10:00-13:00 BKK
    df = pd.DataFrame({"open": [1, 2, 3, 4], "high": [2, 3, 4, 5], "low": [0, 1, 2, 3], "close": [1.5, 2.5, 3.5, 4.5]}, index=idx)
    out = mf.to_2h(df)
    assert len(out) == 2 and out["open"].iloc[0] == 1 and out["close"].iloc[0] == 2.5 and out["high"].iloc[1] == 5


def test_check_index_data_guards():
    import pytest
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 12, 14, 15, tzinfo=ZoneInfo(S.TIMEZONE))
    d = _daily(+1.0)
    b = bars([[1000, 1002, 998, 1000]], start="2026-10-12 12:00")
    assert mf.check_index_data(d, b, now) == ""
    old = bars([[1000, 1002, 998, 1000]], start="2026-10-09 12:00")
    assert "holiday" in mf.check_index_data(d, old, now)
    stale = bars([[1000, 1002, 998, 1000]], start="2026-10-01 12:00")
    with pytest.raises(mf.FeedError):
        mf.check_index_data(d, stale, now)
    bad = d.copy(); bad.iloc[-1] = [50.0] * 4
    with pytest.raises(mf.FeedError):
        mf.check_index_data(bad, b, now)
