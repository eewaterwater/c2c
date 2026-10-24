"""Gates, entry plan and position state machine. No network calls in this file."""
from dataclasses import dataclass
import math
import pandas as pd
from config import settings as S
from core.market_feed import ema, sma, atr


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str


# ---------------- gates ----------------
def regime_gates(daily):
    close = daily["close"]
    e20, s200, a14 = ema(close, S.EMA_FAST), sma(close, S.SMA_SLOW), atr(daily, S.ATR_N)
    c = close.iloc[-1]
    slope = e20.iloc[-1] - e20.iloc[-1 - S.SLOPE_DAYS]
    ext = c - e20.iloc[-1]
    cap = S.MAX_EXTENSION_ATR * a14.iloc[-1]
    return [
        Gate("Trend: close > EMA20 and SMA200", bool(c > e20.iloc[-1] and c > s200.iloc[-1]),
             f"close {c:.1f}, EMA20 {e20.iloc[-1]:.1f}, SMA200 {s200.iloc[-1]:.1f}"),
        Gate(f"EMA20 {S.SLOPE_DAYS}d slope > {S.MIN_EMA_SLOPE_PTS}", bool(slope > S.MIN_EMA_SLOPE_PTS),
             f"slope {slope:+.2f} pts"),
        Gate(f"Extension <= {S.MAX_EXTENSION_ATR} ATR", bool(ext <= cap), f"ext {ext:.1f} vs cap {cap:.1f}"),
    ]


def external_gates(n_breadth, breadth_detail, emini_ret, usdthb_slope):
    up = ", ".join(k.replace(".BK", "") for k, v in breadth_detail.items() if v) or "none"
    return [
        Gate(f"Breadth >= {S.MIN_BREADTH}/5", n_breadth >= S.MIN_BREADTH, f"{n_breadth}/5 up: {up}"),
        Gate(f"E-mini since 09:00 > {S.EMINI_MIN_RETURN_PCT}%", emini_ret > S.EMINI_MIN_RETURN_PCT,
             f"{emini_ret:+.2f}%"),
        Gate(f"USD/THB 5d slope <= {S.USDTHB_MAX_SLOPE_PCT}%", usdthb_slope <= S.USDTHB_MAX_SLOPE_PCT,
             f"{usdthb_slope:+.2f}%"),
    ]


def event_status(now_bkk):
    """(mpc_blackout_active, overnight_hold_banned)."""
    blackout = False
    for s in S.BOT_MPC_DATETIMES_BKK:
        mins = (pd.Timestamp(s, tz=S.TIMEZONE) - now_bkk).total_seconds() / 60
        if 0 <= mins <= S.MPC_BLACKOUT_MIN:
            blackout = True
    today = now_bkk.strftime("%Y-%m-%d")
    return blackout, (today in S.US_CPI_DATES or today in S.FOMC_DATES)


def timing_gates(now_bkk):
    blackout, _ = event_status(now_bkk)
    return [
        Gate(f"Entry time <= {S.LATEST_ENTRY_BKK}", now_bkk.strftime("%H:%M") <= S.LATEST_ENTRY_BKK,
             now_bkk.strftime("%H:%M BKK")),
        Gate("No BOT MPC blackout", not blackout, "clear" if not blackout else "within 60 min of MPC"),
    ]


# ---------------- sizing / plan ----------------
def build_plan(entry, atr_2h, contracts=None):
    n = min(contracts or S.CONTRACTS, math.floor(S.EQUITY * S.MAX_RISK_PCT / (S.INITIAL_STOP_PTS * S.POINT_VALUE)))
    t1 = max(1, round(n * S.T1_FRAC))
    t2a = max(0, round(n * S.T2A_FRAC))
    t2b = max(0, n - t1 - t2a)
    stop = entry - S.INITIAL_STOP_PTS
    return {
        "open": True, "entry": float(entry), "atr_2h": float(atr_2h),
        "qty": {"t1": t1, "t2a": t2a, "t2b": t2b},
        "alive": {"t1": True, "t2a": t2a > 0, "t2b": t2b > 0},
        "stop_a": float(stop), "stop_b": float(stop),
        "t1_target": float(entry + max(S.T1_MIN_PTS, S.T1_ATR_MULT * atr_2h)),
        "t2a_target": float(entry + S.T2A_PTS),
        "t1_filled": False, "hh": float(entry), "bars_held": 0,
        "last_bar_ts": None, "exits": [], "risk_thb": n * S.INITIAL_STOP_PTS * S.POINT_VALUE,
    }


# ---------------- position state machine ----------------
def _exit(pos, tr, price, why, events):
    if pos["alive"][tr]:
        pos["alive"][tr] = False
        pos["exits"].append([tr, float(price), pos["qty"][tr]])
        events.append(f"{tr.upper()} x{pos['qty'][tr]} out @ {price:.1f} ({why})")


def pnl_thb(pos):
    return sum(q * (p - pos["entry"]) for _, p, q in pos["exits"]) * S.POINT_VALUE


def process_bars(pos, bars):
    """Advance an open position through new 2H bars (columns open/high/low/close).
    Within a bar, stops are checked before targets (conservative). Returns event strings."""
    events = []
    atr_s = atr(bars, S.ATR_N)
    for ts, b in bars.iterrows():
        if not pos["open"]:
            break
        if pos["last_bar_ts"] is not None and ts <= pd.Timestamp(pos["last_bar_ts"]):
            continue
        pos["bars_held"] += 1
        pos["last_bar_ts"] = str(ts)
        # 1) stops (levels as of the previous bar)
        if b["low"] <= pos["stop_a"]:
            _exit(pos, "t1", pos["stop_a"], "stop", events)
            _exit(pos, "t2a", pos["stop_a"], "stop", events)
        if b["low"] <= pos["stop_b"]:
            _exit(pos, "t2b", pos["stop_b"], "stop", events)
        # 2) targets
        if pos["alive"]["t1"] and b["high"] >= pos["t1_target"]:
            _exit(pos, "t1", pos["t1_target"], "target", events)
            pos["t1_filled"] = True
            pos["stop_a"] = max(pos["stop_a"], pos["entry"])
            pos["stop_b"] = max(pos["stop_b"], pos["entry"])
            events.append(f"Runners ratcheted to breakeven {pos['entry']:.1f}")
        if pos["alive"]["t2a"] and b["high"] >= pos["t2a_target"]:
            _exit(pos, "t2a", pos["t2a_target"], "target", events)
        # 3) chandelier trail on T2B (only after T1 fills, only moves up)
        pos["hh"] = max(pos["hh"], float(b["high"]))
        if pos["t1_filled"] and pos["alive"]["t2b"]:
            trail = pos["hh"] - S.CHANDELIER_ATR_MULT * float(atr_s.loc[ts])
            if trail > pos["stop_b"]:
                pos["stop_b"] = trail
                events.append(f"T2B trail raised to {trail:.1f}")
        # 4) stagnation exit
        if pos["bars_held"] >= S.STAGNATION_BARS and not pos["t1_filled"] and any(pos["alive"].values()):
            for tr in ("t1", "t2a", "t2b"):
                _exit(pos, tr, float(b["close"]), "stagnation", events)
        if not any(pos["alive"].values()):
            pos["open"] = False
            events.append(f"POSITION CLOSED, realized {pnl_thb(pos):+,.0f} THB")
    return events
