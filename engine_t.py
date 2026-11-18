import os
from datetime import datetime, time
import pytz
import requests
import yfinance as yf
import pandas as pd

BKK_TZ = pytz.timezone("Asia/Bangkok")
ENGINE_VERSION = os.getenv("ENGINE_VERSION", "Code2Cash v7.7")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MORNING_REALIZED_THB = float(os.getenv("MORNING_REALIZED_THB", "0"))


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def fetch_market_snapshot() -> dict:
    """
    Pulls SET50 proxy intraday data and computes 15M EMA(9), EMA(20), MA(200),
    RSI(14), intraday range, and prior-day Value Area approximation (VAH/VAL).
    """
    df = yf.download("^SET.BK", period="10d", interval="15m", progress=False)
    if df.empty:
        raise RuntimeError("No market data returned from data feed.")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = df.index.tz_convert(BKK_TZ) if df.index.tzinfo else df.index.tz_localize("UTC").tz_convert(BKK_TZ)
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["MA200"] = df["Close"].rolling(window=200, min_periods=50).mean()
    df["RSI14"] = compute_rsi(df["Close"], 14)

    today = datetime.now(BKK_TZ).date()
    today_df = df[df.index.date == today]
    prev_df = df[df.index.date < today]

    if not prev_df.empty:
        last_prev_day = prev_df.index.date[-1]
        prev_session = prev_df[prev_df.index.date == last_prev_day]
        vah = float(prev_session["Close"].quantile(0.85))
        val = float(prev_session["Close"].quantile(0.15))
    else:
        vah = float(df["High"].tail(20).max())
        val = float(df["Low"].tail(20).min())

    if not today_df.empty:
        intraday_range = float(today_df["High"].max() - today_df["Low"].min())
    else:
        intraday_range = float(df["High"].tail(12).max() - df["Low"].tail(12).min())

    last = df.iloc[-1]
    prev = df.iloc[-2]

    return {
        "close": float(last["Close"]),
        "high": float(last["High"]),
        "low": float(last["Low"]),
        "prev_close": float(prev["Close"]),
        "ema9": float(last["EMA9"]),
        "ema20": float(last["EMA20"]),
        "ma200": float(last["MA200"]),
        "rsi": float(last["RSI14"]),
        "vah": vah,
        "val": val,
        "intraday_range": intraday_range,
    }


def classify_regime(
    close: float,
    ma200: float,
    vah: float,
    val: float,
    intraday_range: float,
    rsi: float,
) -> str:
    """
    Classifies S50Z26 into TREND_BREAKOUT (10 lots AM / 6 lots PM, 7/2/1 split)
    or RANGE_BOX (4 lots, 100% all-out at +2.8 pts).
    """
    outside_value_area = (close > vah) or (close < val)
    expanded_range = intraday_range >= 6.0
    directional_momentum = (close > ma200 and rsi >= 54.0) or (close < ma200 and rsi <= 42.0)

    if (outside_value_area or expanded_range) and directional_momentum:
        return "TREND_BREAKOUT"
    return "RANGE_BOX"


def detect_mss_signal(snap: dict) -> str | None:
    close = snap["close"]
    ema9 = snap["ema9"]
    ema20 = snap["ema20"]
    ma200 = snap["ma200"]
    rsi = snap["rsi"]

    # No extension chasing (> 2.0 pts from EMA9)
    if abs(close - ema9) > 2.0:
        return None

    # Bullish MSS
    if close > ema9 and close > ema20 and close > ma200 and rsi > 52.0:
        return "LONG"

    # Bearish MSS
    if close < ema9 and close < ema20 and close < ma200 and rsi < 48.0:
        return "SHORT"

    return None


def build_v77_ticket(
    side: str,
    entry: float,
    ema9: float,
    ema20: float,
    ma200: float,
    rsi: float,
    vah: float,
    val: float,
    intraday_range: float,
    morning_realized_thb: float = 0.0,
) -> dict:
    now_bkk = datetime.now(BKK_TZ)
    t = now_bkk.time()

    # 1. Morning One-and-Done Gate (>= +3,000 THB gross banked)
    if time(10, 15) <= t <= time(12, 30) and morning_realized_thb >= 3000.0:
        return {
            "version": ENGINE_VERSION,
            "status": "LOCKED_WIN",
            "reason": f"Morning target met (+฿{morning_realized_thb:,.0f} THB banked). Stand aside until 14:18 BKK.",
            "ticket": None,
        }

    # 2. Full-Day Lockout Gate (>= +5,000 THB gross banked)
    if time(14, 18) <= t <= time(15, 30) and morning_realized_thb >= 5000.0:
        return {
            "version": ENGINE_VERSION,
            "status": "LOCKED_WIN",
            "reason": f"Daily target met (+฿{morning_realized_thb:,.0f} THB banked). Stand aside for rest of day.",
            "ticket": None,
        }

    # 3. Session Windows
    in_morning = time(10, 15) <= t <= time(11, 45)
    in_afternoon = time(14, 18) <= t <= time(15, 30)
    if not (in_morning or in_afternoon):
        return {
            "version": ENGINE_VERSION,
            "status": "OUTSIDE_WINDOW",
            "reason": f"Current BKK time {now_bkk.strftime('%H:%M')} is outside authorized entry windows.",
            "ticket": None,
        }

    # 4. Regime Classification & Sizing
    regime = classify_regime(entry, ma200, vah, val, intraday_range, rsi)

    if regime == "TREND_BREAKOUT":
        contracts = 6 if in_afternoon else 10
        split = "4 / 1 / 1" if in_afternoon else "7 / 2 / 1"
        stop_pts = 2.8
        t1_pts, t2_pts = 2.5, 4.0
        trail_rule = f"15M Close {'<' if side == 'LONG' else '>'} EMA(9) ({ema9:.1f})"
    else:
        contracts = 4
        split = "4 / 0 / 0 (All-Out Limit)"
        stop_pts = 2.0
        t1_pts, t2_pts = 2.8, None
        trail_rule = "None — 100% exit at T1 (+2.8 pts) to eliminate chop & cut fees"

    if side == "SHORT":
        stop_px = round(entry + stop_pts, 1)
        t1_px = round(entry - t1_pts, 1)
        t2_px = round(entry - t2_pts, 1) if t2_pts else None
        stop_cond = f"Last >= {stop_px:.1f}"
    else:
        stop_px = round(entry - stop_pts, 1)
        t1_px = round(entry + t1_pts, 1)
        t2_px = round(entry + t2_pts, 1) if t2_pts else None
        stop_cond = f"Last <= {stop_px:.1f}"

    return {
        "version": ENGINE_VERSION,
        "status": "ARMED",
        "regime": regime,
        "ticket": {
            "symbol": "S50Z26",
            "side": side,
            "contracts": contracts,
            "split": split,
            "entry": round(entry, 1),
            "hard_stop_mkt": f"{stop_cond} (MP-MKT, IOC | -{stop_pts:.1f} pts)",
            "t1_target": f"{t1_px:.1f} (+{t1_pts:.1f} pts)",
            "t2_target": f"{t2_px:.1f} (+{t2_pts:.1f} pts)" if t2_px else "N/A (All-Out at T1)",
            "runner_rule": trail_rule,
            "av_vol_warning": "Keep Av.Vol unblocked: arm single conditional MP-MKT stop before any limit exits.",
            "time_stop": "60 mins from fill | Hard flatten: 12:25 BKK (AM) / 15:58 BKK (PM)",
        },
    }


def format_telegram_alert(result: dict, snap: dict) -> str:
    if result["status"] != "ARMED":
        return (
            f"⏸ *[{result['version']} | {result['status']}]*\n"
            f"{result['reason']}\n"
            f"• Spot Proxy: `{snap['close']:.1f}` | RSI(14): `{snap['rsi']:.1f}`"
        )

    tk = result["ticket"]
    badge = (
        "TREND 10-LOT"
        if result["regime"] == "TREND_BREAKOUT" and tk["contracts"] == 10
        else ("TREND 6-LOT" if result["regime"] == "TREND_BREAKOUT" else "RANGE 4-LOT")
    )
    emoji = "🟢" if tk["side"] == "LONG" else "🔴"

    return (
        f"{emoji} *[{result['version']} | {badge}] {tk['side']} {tk['symbol']}*\n"
        f"• *Size / Split:* `{tk['contracts']} cts` (`{tk['split']}`)\n"
        f"• *Entry Zone:* `{tk['entry']:.1f}` (RSI: `{snap['rsi']:.1f}`)\n"
        f"• *Hard Stop (`MP-MKT`):* `{tk['hard_stop_mkt']}`\n"
        f"• *T1 Bank:* `{tk['t1_target']}`\n"
        f"• *T2 Shelf:* `{tk['t2_target']}`\n"
        f"• *Runner Rule:* {tk['runner_rule']}\n"
        f"⚠️ _{tk['av_vol_warning']}_\n"
        f"⏱ _{tk['time_stop']}_"
    )


def main() -> None:
    snap = fetch_market_snapshot()
    side = detect_mss_signal(snap)

    if not side:
        now_bkk = datetime.now(BKK_TZ).strftime("%H:%M BKK")
        print(
            f"[{ENGINE_VERSION} | {now_bkk}] No MSS trigger. "
            f"Close={snap['close']:.1f}, EMA9={snap['ema9']:.1f}, "
            f"EMA20={snap['ema20']:.1f}, MA200={snap['ma200']:.1f}, RSI={snap['rsi']:.1f}"
        )
        return

    result = build_v77_ticket(
        side=side,
        entry=snap["close"],
        ema9=snap["ema9"],
        ema20=snap["ema20"],
        ma200=snap["ma200"],
        rsi=snap["rsi"],
        vah=snap["vah"],
        val=snap["val"],
        intraday_range=snap["intraday_range"],
        morning_realized_thb=MORNING_REALIZED_THB,
    )
    msg = format_telegram_alert(result, snap)
    send_telegram(msg)


if __name__ == "__main__":
    main()
