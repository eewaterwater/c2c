import os, sys, json, requests, pytz, pandas as pd, numpy as np, yfinance as yf
from datetime import datetime, timezone

token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
event_name = os.getenv("EVENT_NAME", "workflow_dispatch")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time = bkk_now.strftime("%Y-%m-%d %H:%M:%S")
today_str = bkk_now.strftime("%Y-%m-%d")
is_friday = (bkk_now.weekday() == 4)

# Live Liquid Cash Line Available (Updated post-NVDA80 & CPALL executions)
CASH_LINE = 743449.99
VAULT_B_TOTAL = 851265.13
MAX_POSITION_OUTLAY = VAULT_B_TOTAL * 0.20  # 20% Vault B Cap (~฿170,253 THB)

# 1. State Maintenance
state_f = "state.json"
state = {"consecutive_wins": 0, "daily_stopouts": 0, "weekly_stopouts": 0, "date": today_str}
if os.path.exists(state_f):
    try:
        with open(state_f, "r") as f:
            state.update(json.load(f))
    except Exception:
        pass

if state.get("date") != today_str:
    state["daily_stopouts"] = 0
    state["date"] = today_str

cb_active = state.get("daily_stopouts", 0) >= 2 or state.get("weekly_stopouts", 0) >= 3

# ==============================================================================
# DR SPECIFICATIONS & CONVERSION RATIOS (CORRECTED)
# ==============================================================================
DR_SPECS = {
    "MSFT80": {
        "underlying": "MSFT",
        "ratio": 2000,          # OFFICIAL KTB RATIO: 2,000 DR units = 1 US share
        "board": "SET",
        "lot_size": 100,
        "tick_size": 0.05,
        "min_fair_thb": 6.0,
        "max_fair_thb": 13.0,
    },
    "NVDA80": {
        "underlying": "NVDA",
        "ratio": 200,           # OFFICIAL KTB RATIO: 200 DR units = 1 US share
        "board": "SET",
        "lot_size": 100,
        "tick_size": 0.25,
        "min_fair_thb": 25.0,
        "max_fair_thb": 50.0,
    },
    "PLTR01": {
        "underlying": "PLTR",
        "ratio": 1000,          # BLS RATIO: 1,000 DR units = 1 US share
        "board": "SET",
        "lot_size": 100,
        "tick_size": 0.01,
        "min_fair_thb": 3.0,
        "max_fair_thb": 12.0,
    },
    "AAPL80X": {"underlying": "AAPL", "ratio": 1000, "board": "DRX", "lot_size": 1, "tick_size": 0.01},
    "TSLA80X": {"underlying": "TSLA", "ratio": 1000, "board": "DRX", "lot_size": 1, "tick_size": 0.01},
    "AMD80X":  {"underlying": "AMD",  "ratio": 1000, "board": "DRX", "lot_size": 1, "tick_size": 0.01},
}

def send_telegram(msg: str):
    if not token or not chat_id:
        print(f"\n[TELEGRAM PREVIEW]\n{msg}\n")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def calculate_bracket_plan(symbol: str, us_entry: float, us_stop: float, us_t1: float, fx_rate: float, target_outlay: float = 170000.0):
    spec = DR_SPECS[symbol]
    ratio = spec["ratio"]
    lot_size = spec.get("lot_size", 100)
    tick_size = spec.get("tick_size", 0.05)

    dr_entry = round((us_entry * fx_rate) / ratio, 2)
    dr_stop = round((us_stop * fx_rate) / ratio, 2)
    dr_stop_limit = round(dr_stop - tick_size, 2)  # Slippage buffer ensures fill into bid
    dr_t1 = round((us_t1 * fx_rate) / ratio, 2)

    total_volume = int(target_outlay / dr_entry // lot_size * lot_size)
    t1_volume = int(round(total_volume * 0.33) // lot_size * lot_size)
    stop_volume = total_volume - t1_volume  # Unbundled to prevent GW001 error

    return {
        "symbol": symbol,
        "ratio": ratio,
        "dr_entry": dr_entry,
        "dr_stop": dr_stop,
        "dr_stop_limit": dr_stop_limit,
        "dr_t1": dr_t1,
        "total_volume": total_volume,
        "t1_volume": t1_volume,
        "stop_volume": stop_volume,
        "actual_outlay": round(total_volume * dr_entry, 2),
    }

def format_telegram_alert(symbol: str, us_price: float, us_stop: float, us_t1: float, fx_rate: float, regime: str = "Regime 2 (Pullback)"):
    spec = DR_SPECS.get(symbol)
    if not spec:
        return
    plan = calculate_bracket_plan(symbol, us_price, us_stop, us_t1, fx_rate, MAX_POSITION_OUTLAY)

    min_p = spec.get("min_fair_thb", 0.0)
    max_p = spec.get("max_fair_thb", 1000.0)
    if not (min_p <= plan["dr_entry"] <= max_p):
        print(f"[GATE REJECT] {symbol} calculated price ฿{plan['dr_entry']} outside valid sanity bounds!")
        return

    msg = (
        f"🚨 <b>[Code2Cash Engine E] {symbol} BUY SETUP</b>\n"
        f"• Underlying: <b>{spec['underlying']}</b> (${us_price:.2f} USD)\n"
        f"• FX: {fx_rate:.2f} THB/USD | Verified Ratio: <b>{plan['ratio']}:1</b>\n"
        f"• Regime: <i>{regime}</i>\n"
        f"----------------------------------------\n"
        f"📋 <b>SETTRADE UNBUNDLED EXECUTION:</b>\n"
        f"👉 <b>1. Buy Entry:</b> ฿{plan['dr_entry']:.2f} THB ({plan['total_volume']:,} units / ~฿{plan['actual_outlay']:,.2f})\n"
        f"👉 <b>2. Tranche 1A Limit (33%):</b> ฿{plan['dr_t1']:.2f} THB ({plan['t1_volume']:,} units)\n"
        f"👉 <b>3. Conditional Stop (67%):</b> Trigger ฿{plan['dr_stop']:.2f} / Limit ฿{plan['dr_stop_limit']:.2f} ({plan['stop_volume']:,} units)\n"
        f"----------------------------------------\n"
        f"🛡️ <i>Unbundled volume eliminates GW001 error. On T1 fill, ratchet stop to Breakeven.</i>"
    )
    send_telegram(msg)
