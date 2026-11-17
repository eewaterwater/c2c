import os
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, time
import pytz

BKK_TZ = pytz.timezone("Asia/Bangkok")

# ==============================================================================
# CODE2CASH v7.6 INSTITUTIONAL ENGINE — S50Z26 & DR/DRx (NOV 16, 2026 SPEC)
# ==============================================================================
ENGINE_CONFIG = {
    "version": "v7.6",
    "symbol": "S50Z26",
    "multiplier": 200,          # THB 200 per index point (0.1 pt = THB 20)
    "base_contracts": 10,       # 7 T1 Bank / 2 T2 Support / 1 T3 EMA(9) Runner
    "split": (7, 2, 1),
    "max_stop_pts": 3.0,        # Hard stop cap (THB 6,000 max on 10 contracts)
    "t1_target_pts": 2.5,       # Tranche 1 bank target (+2.5 pts)
    "t2_target_pts": 4.0,       # Tranche 2 structural shelf (+4.0 pts)
    "rsi_long_min": 52.0,
    "rsi_short_max": 48.0,
    "rsi_oversold_veto": 26.0,  # Avoid chasing Shorts into extreme oversold < 26
    "rsi_overbought_veto": 74.0,
    "windows": {
        "morning_start": time(10, 15),
        "morning_cutoff": time(11, 45),
        "morning_flatten": time(12, 25),
        "afternoon_start": time(14, 18),
        "afternoon_cutoff": time(15, 30),
        "eod_flatten": time(15, 58),
    },
    # Nov 16, 2026 SET/mai DR Tick Size Tiers
    "dr_tick_tiers": [
        (0.00, 2.00, 0.01),
        (2.00, 10.00, 0.02),
        (10.00, 25.00, 0.05),
        (25.00, 50.00, 0.10),
        (50.00, 100.00, 0.25),
        (100.00, 200.00, 0.50),
        (200.00, 400.00, 1.00),
        (400.00, float("inf"), 2.00),
    ],
}


def get_dr_tick_size(price: float) -> float:
    """Returns exact SET/mai DR tick size effective Nov 16, 2026."""
    for low, high, tick in ENGINE_CONFIG["dr_tick_tiers"]:
        if low <= price < high:
            return tick
    return 0.01


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Computes 15M EMA(9), EMA(20), MA(200), and Wilder RSI(14)."""
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ma200"] = df["close"].rolling(window=200, min_periods=50).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    return df


def check_session_window(now_bkk: datetime) -> tuple[str, str]:
    """Enforces Code2Cash v7.6 execution windows & mandatory flatten gates."""
    t = now_bkk.time()
    w = ENGINE_CONFIG["windows"]

    if w["morning_start"] <= t <= w["morning_cutoff"]:
        return "AUTHORIZED", "Morning Trend Window (10:15–11:45 BKK)"
    if w["afternoon_start"] <= t <= w["afternoon_cutoff"]:
        return "AUTHORIZED", "Afternoon Trend Window (14:18–15:30 BKK)"
    if w["morning_flatten"] <= t < time(12, 30):
        return "FLATTEN_GATE", "Pre-Lunch Flatten Gate (12:25 BKK) — Zero Lunch Gap Risk"
    if w["eod_flatten"] <= t <= time(16, 30):
        return "FLATTEN_GATE", "EOD Flatten Gate (15:58 BKK) — Zero Overnight Risk"
    return "BLACKOUT", "Outside Authorized Entry Window — Stand Aside / Capital Defense"


def evaluate_s50_signal(df: pd.DataFrame, now_bkk: datetime = None) -> dict:
    """Evaluates 15M candle close against Code2Cash v7.6 rules."""
    if now_bkk is None:
        now_bkk = datetime.now(BKK_TZ)

    df = compute_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    status, window_note = check_session_window(now_bkk)

    price = round(float(last["close"]), 1)
    ema9 = round(float(last["ema9"]), 1)
    ema20 = round(float(last["ema20"]), 1)
    ma200 = round(float(last["ma200"]), 1)
    rsi = round(float(last["rsi14"]), 2)

    signal = "FLAT / STAND ASIDE"
    ticket = None

    if status == "AUTHORIZED":
        # BULLISH CONTINUATION / RECLAIM
        if (
            price > ema9 > ema20
            and price > ma200
            and ENGINE_CONFIG["rsi_long_min"] <= rsi <= ENGINE_CONFIG["rsi_overbought_veto"]
            and (price - ema9) <= 2.0
        ):
            stop_px = round(max(price - ENGINE_CONFIG["max_stop_pts"], min(prev["low"], last["low"]) - 0.3), 1)
            t1_px = round(price + ENGINE_CONFIG["t1_target_pts"], 1)
            t2_px = round(price + ENGINE_CONFIG["t2_target_pts"], 1)
            signal = "LONG TRIGGER (7 / 2 / 1 SPLIT)"
            ticket = {
                "side": "LONG (Open)",
                "entry": f"{price:.1f}",
                "stop_order": f"Short | Close | Vol: 10 | Last <= {stop_px:.1f} | MP-MKT (IOC)",
                "t1": f"Close 7 @ {t1_px:.1f} (+2.5 pts) -> Move Stop to {price:.1f} BE",
                "t2": f"Close 2 @ {t2_px:.1f} (+4.0 pts)",
                "t3": f"Trail 1 Runner on 15M Close < EMA(9) ({ema9:.1f})",
            }

        # BEARISH BREAKDOWN / CONTINUATION
        elif (
            price < ema9 < ema20
            and price < ma200
            and ENGINE_CONFIG["rsi_oversold_veto"] <= rsi <= ENGINE_CONFIG["rsi_short_max"]
            and (ema9 - price) <= 2.5
        ):
            stop_px = round(min(price + ENGINE_CONFIG["max_stop_pts"], max(prev["high"], last["high"]) + 0.3), 1)
            t1_px = round(price - ENGINE_CONFIG["t1_target_pts"], 1)
            t2_px = round(price - ENGINE_CONFIG["t2_target_pts"], 1)
            signal = "SHORT TRIGGER (7 / 2 / 1 SPLIT)"
            ticket = {
                "side": "SHORT (Open)",
                "entry": f"{price:.1f}",
                "stop_order": f"Long | Close | Vol: 10 | Last >= {stop_px:.1f} | MP-MKT (IOC)",
                "t1": f"Close 7 @ {t1_px:.1f} (+2.5 pts) -> Move Stop to {price:.1f} BE",
                "t2": f"Close 2 @ {t2_px:.1f} (+4.0 pts)",
                "t3": f"Trail 1 Runner on 15M Close > EMA(9) ({ema9:.1f})",
            }

    return {
        "timestamp": now_bkk.strftime("%Y-%m-%d %H:%M BKK"),
        "status": status,
        "window_note": window_note,
        "signal": signal,
        "metrics": {
            "price": price,
            "ema9": ema9,
            "ema20": ema20,
            "ma200": ma200,
            "rsi14": rsi,
        },
        "ticket": ticket,
    }


def format_telegram_alert(payload: dict) -> str:
    """Formats clean institutional Telegram message for Code2Cash_S50_Bot."""
    m = payload["metrics"]
    lines = [
        f"🤖 *Code2Cash {ENGINE_CONFIG['version']} — {ENGINE_CONFIG['symbol']}*",
        f"🕒 `{payload['timestamp']}`",
        f"🚦 *Gate:* `{payload['status']}` ({payload['window_note']})",
        "",
        f"📊 *15M Tape:* `{m['price']:.1f}` | EMA9: `{m['ema9']:.1f}` | EMA20: `{m['ema20']:.1f}` | MA200: `{m['ma200']:.1f}` | RSI: `{m['rsi14']:.2f}`",
        f"🎯 *Action:* *{payload['signal']}*",
    ]
    if payload["ticket"]:
        t = payload["ticket"]
        lines.extend([
            "",
            f"1️⃣ *Entry:* `{t['side']} 10 @ {t['entry']}`",
            f"2️⃣ *Hard Stop:* `{t['stop_order']}`",
            f"3️⃣ *T1 Bank (70%):* `{t['t1']}`",
            f"4️⃣ *T2 Shelf (20%):* `{t['t2']}`",
            f"5️⃣ *T3 Runner (10%):* `{t['t3']}`",
        ])
    return "\n".join(lines)


def send_telegram_message(text: str) -> bool:
    """Dispatches alert to Telegram using GitHub Actions Secrets."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variables.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    return resp.ok


if __name__ == "__main__":
    # Smoke test using today's Nov 17, 2026 10:40 BKK Short Breakdown parameters
    sample_closes = [1024.0] * 195 + [1021.0, 1018.5, 1016.2, 1015.0, 1014.1]
    test_df = pd.DataFrame({
        "high": [c + 0.8 for c in sample_closes],
        "low": [c - 0.6 for c in sample_closes],
        "close": sample_closes,
    })
    test_time = BKK_TZ.localize(datetime(2026, 11, 17, 10, 45))
    result = evaluate_s50_signal(test_df, now_bkk=test_time)
    msg = format_telegram_alert(result)
    print(msg)
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        send_telegram_message(msg)
