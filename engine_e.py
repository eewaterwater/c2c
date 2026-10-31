import os, sys, json, requests, pytz, pandas as pd, numpy as np, yfinance as yf
from datetime import datetime, timezone

token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
event_name = os.getenv("EVENT_NAME", "workflow_dispatch")
bkk_now = datetime.now(pytz.timezone("Asia/Bangkok"))
bkk_time = bkk_now.strftime("%Y-%m-%d %H:%M:%S")
today_str = bkk_now.strftime("%Y-%m-%d")
is_friday = (bkk_now.weekday() == 4)

# Live Liquid Cash Line Available (Expanded post-PLTR01 exit)
CASH_LINE = 527900.43
VAULT_B_TOTAL = 851265.13
MAX_POSITION_OUTLAY = VAULT_B_TOTAL * 0.20  # 20% Vault B Cap (฿170,253 THB)

# 1. State Maintenance
state_f = "state.json"
state = {"consecutive_wins": 0, "daily_stopouts": 0, "weekly_stopouts": 0, "date": today_str}
if os.path.exists(state_f):
    try:
        with open(state_f, "r") as f: state.update(json.load(f))
    except Exception: pass
if state.get("date") != today_str:
    state["daily_stopouts"], state["date"] = 0, today_str
cb_active = state.get("daily_stopouts", 0) >= 2 or state.get("weekly_stopouts", 0) >= 3

# 2. Calendar Check
try:
    import exchange_calendars as xc
    if not xc.get_calendar("XNYS").is_session(today_str) and event_name == "schedule":
        sys.exit(0)
except Exception: pass

def get_c(sym, p="1mo", itv="1d"):
    try:
        d = yf.download(sym, period=p, interval=itv, progress=False)
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
        return d["Close"].dropna()
    except Exception: return pd.Series(dtype=float)

# 3. Macro Feeds & 4-Regime Classification
usd_thb = float(get_c("THB=X", "5d").iloc[-1]) if len(get_c("THB=X", "5d")) else 35.0
vix = float(get_c("^VIX", "5d").iloc[-1]) if len(get_c("^VIX", "5d")) else 18.0
qqq_c = get_c("QQQ", "1y")

regime_id, r_name, r_icon, r_thb, r_lbl = 3, "Regime 3: Chop", "🟡", 15000.0, "0.50R (฿15,000)"
v_a_alloc = "35% QQQ / 25% SMH / 40% SGOV"

if len(qqq_c) >= 200:
    p, ema20, ema50 = float(qqq_c.iloc[-1]), float(qqq_c.ewm(20).mean().iloc[-1]), float(qqq_c.ewm(50).mean().iloc[-1])
    sma200 = qqq_c.rolling(200).mean()
    p200 = float(sma200.iloc[-1])
    if all(qqq_c.iloc[-3:] < (sma200.iloc[-3:] * 0.985)):
        regime_id, r_name, r_icon, r_thb, r_lbl = 4, "Regime 4: Structural Bear", "🔴", 0.0, "0.0R (Cash Gate)"
        v_a_alloc = "100% SGOV"
    elif p > ema20 > ema50 > p200:
        regime_id, r_name, r_icon = 1, "Regime 1: Confirmed Bull", "🟢"
        r_thb, r_lbl = (15000.0, "0.50R (VIX High)") if vix > 28 else (30000.0, "1.00R (฿30,000)")
        v_a_alloc = "55% QQQ / 35% SMH / 10% SGOV"
    elif p < ema20 and p > p200:
        regime_id, r_name, r_icon = 2, "Regime 2: Pullback", "🟠"
        r_thb, r_lbl = (15000.0, "0.50R (VIX High)") if vix > 28 else (22500.0, "0.75R (฿22,500)")
        v_a_alloc = "35% QQQ / 25% SMH / 40% SGOV"

if cb_active: r_thb, r_lbl = 0.0, "0.0R (Circuit Breaker)"

# 4. Hardened Pre-Earnings Blackouts
def earn_block(sym, days=5):
    try:
        cal = yf.Ticker(sym).calendar
        d = cal.get("Earnings Date", []) if isinstance(cal, dict) else (cal.T["Earnings Date"].tolist() if hasattr(cal, "T") and "Earnings Date" in cal.T else [])
        if d:
            dt = d[0].date() if hasattr(d[0], "date") else d[0]
            return 0 <= (dt - datetime.now(timezone.utc).date()).days <= days
    except Exception: pass
    return False

def idx_block(sym):
    if sym in ["QQQ", "SPY", "SOXX", "SMH", "XLK"]:
        return any(earn_block(m, 2) for m in ["MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "META"])
    return False

# 5. Core Vault A Monitor
etf_lines = []
for sym, role in [("SMH", "Vault A Alpha"), ("QQQM", "Vault A Core"), ("VOO", "S&P Anchor")]:
    c = get_c(sym, "1y")
    if len(c) >= 200:
        p_c, e21, s200 = float(c.iloc[-1]), float(c.ewm(21).mean().iloc[-1]), float(c.rolling(200).mean().iloc[-1])
        st = "🟢 Bull" if p_c >= e21 and p_c > s200 else ("🟡 Pullback" if p_c > s200 else "🔴 Bear")
        etf_lines.append(f"• <b>{sym}</b> (${p_c:.2f}): {st} | 21EMA: <code>{((p_c-e21)/e21)*100:+.1f}%</code> | <i>{role}</i>")

# 6. Sector-Benchmarked Watchpool
bench_rets = {}
for b in ["QQQ", "SMH", "XLK"]:
    bc = get_c(b, "1mo")
    bench_rets[b] = (((float(bc.iloc[-1]) - float(bc.iloc[-4])) / float(bc.iloc[-4])) * 100.0) if len(bc) >= 4 else 0.0

pool = [
    ("NVDA","NVDA80X",200,"HIGH_BETA","SMH"), ("PLTR","PLTR01",1100,"HIGH_BETA","XLK"),
    ("TSLA","TSLA80X",5000,"HIGH_BETA","QQQ"), ("AMD","AMD80X",5000,"HIGH_BETA","SMH"),
    ("COIN","COIN01",2000,"HIGH_BETA","QQQ"), ("MU","MICRON01",1000,"HIGH_BETA","SMH"),
    ("SOXX","SOXX23",100,"HIGH_BETA","SMH"),   ("QQQ","QQQ80X",1000,"MEGA_CORE","QQQ"),
    ("AAPL","AAPL80X",1000,"MEGA_CORE","XLK"), ("MSFT","MSFT80X",1000,"MEGA_CORE","XLK"),
    ("GOOGL","GOOG80X",500,"MEGA_CORE","QQQ"), ("AMZN","AMZN80X",500,"MEGA_CORE","QQQ")
]

cand, metrics = [], []
for sym, dr, ratio, tier, bench in pool:
    try:
        d = yf.download(sym, period="3mo", interval="1d", progress=False)
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
        c, l = d["Close"].dropna(), d["Low"].dropna()
        if len(c) < 25: continue
        curr_p, e21 = float(c.iloc[-1]), float(c.ewm(21).mean().iloc[-1])
        ret3 = ((curr_p - float(c.iloc[-4])) / float(c.iloc[-4])) * 100.0
        rs = ret3 - bench_rets.get(bench, 0.0)
        dist21 = ((curr_p - e21) / e21) * 100.0
        
        eblock = idx_block(sym) if sym in ["QQQ", "SOXX"] else earn_block(sym, 5)
        etag = " ⚠️ EARN" if eblock else ""
        metrics.append(f"• <b>{dr}</b> (${curr_p:.2f}): RS {rs:+.1f}% | 21EMA {dist21:+.1f}%{etag}")

        # Regime 2 Pullback Gate: Suppress breakout chasing
        if -1.0 <= dist21 <= 2.0 and not eblock and rs > 0 and r_thb > 0 and not cb_active and vix <= 28:
            if not (regime_id == 2 and dist21 > 0.5):
                cand.append((rs, sym, dr, ratio, tier, bench, curr_p, float(l.iloc[-3:].min()) * 0.998, dist21))
    except Exception: pass

# 7. Asymmetric 3-Tranche Banking & GW001 Guard
cand.sort(reverse=True)
top = cand[:2]
tickets, quick_paste = [], []

for _, sym, dr, ratio, tier, bench, u_p, st_p, d21 in top:
    e_thb, s_thb = (u_p * usd_thb) / ratio, (st_p * usd_thb) / ratio
    r_unit = e_thb - s_thb
    if r_unit <= 0.001: continue
    
    raw_u = int(r_thb / r_unit)
    u = min(raw_u, int(MAX_POSITION_OUTLAY / e_thb))
    capped_str = " (Capped at 20% Vault B)" if (raw_u * e_thb) > MAX_POSITION_OUTLAY else ""
    outlay = u * e_thb
    c_warn = f"\n  • ⚠️ <b>CAPITAL:</b> Outlay ฿{outlay:,.0f} > Cash Line ฿{CASH_LINE:,.0f}" if outlay > CASH_LINE else ""
    
    # 3-Tranche Math
    t1a_u = int(u * 0.33)
    t1b_u = int(u * 0.33)
    runner_u = u - (t1a_u + t1b_u)
    
    t1a_thb = e_thb + (1.0 * r_unit)  # +1.0R (Ratchet stop to BE)
    t1b_thb = e_thb + (2.0 * r_unit)  # +2.0R (Bank second slice)
    stop_unbundled_u = u - t1a_u      # GW001 Guard: Exclude resting limit sell volume
    
    tickets.append(
        f"🎯 <b>{dr} ({sym}) - [{tier}] (vs {bench}):</b>\n"
        f"  • Entry: <b>฿{e_thb:.2f}</b> (${u_p:.2f}) [21EMA: {d21:+.1f}%]\n"
        f"  • Initial Hard Stop: <b>฿{s_thb:.2f}</b> | Sizing: <b>{u:,} units</b> (฿{outlay:,.0f}){capped_str}{c_warn}\n"
        f"  • <b>Tranche 1A (33% / {t1a_u:,} u):</b> Limit <b>฿{t1a_thb:.2f}</b> (+1.0R >> <b>RATCHET STOP TO BE</b>)\n"
        f"  • <b>Tranche 1B (33% / {t1b_u:,} u):</b> Limit <b>฿{t1b_thb:.2f}</b> (+2.0R Volatility Bank)\n"
        f"  • <b>Tranche 2 Runner (34% / {runner_u:,} u):</b> Trail Daily Close ≥ 21 EMA\n"
        f"  • <b>🛡️ GW001 Protection:</b> Active Stop Volume = <b>{stop_unbundled_u:,} units</b> (Unbundled from resting T1A limit)"
    )
    quick_paste.append(
        f"<code>BUY {dr} @ ฿{e_thb:.2f} | Vol: {u:,}\n"
        f"SELL {dr} LIMIT @ ฿{t1a_thb:.2f} | Vol: {t1a_u:,} (T1A)\n"
        f"STOP {dr} CONDITIONAL @ ฿{s_thb:.2f} | Vol: {stop_unbundled_u:,} (GW001 Unbundled)</code>"
    )

title = "🚨 <b>[ACTION REQUIRED: US DRx ENTRY]</b>" if top else "🇺🇸 <b>[C2C Engine E - Dual-Vault Briefing]</b>"
msg = [
    title, f"📅 <b>Time:</b> {bkk_time} BKK", f"💵 <b>USD/THB:</b> ฿{usd_thb:.2f} | <b>VIX:</b> {vix:.1f}",
    "━━━━━━━━━━━━━━━━━━", f"📊 <b>REGIME:</b> {r_icon} <b>{r_name}</b> | Risk: <b>{r_lbl}</b>",
    f"• <b>Vault A Target:</b> <code>{v_a_alloc}</code>",
    f"• <b>Liquid Firepower (Vault B):</b> <code>฿{CASH_LINE:,.0f} THB</code>",
    "━━━━━━━━━━━━━━━━━━", "🏛️ <b>CORE VAULT A & MACRO STACK:</b>", *etf_lines,
    "━━━━━━━━━━━━━━━━━━", "🔍 <b>WATCHPOOL METRICS:</b>", *metrics[:8], "━━━━━━━━━━━━━━━━━━"
]

if tickets:
    msg.extend(["📋 <b>OPTIMIZED ALPHA TICKETS (THB Sized):</b>", *tickets, "━━━━━━━━━━━━━━━━━━", "📲 <b>SETTRADE UNBUNDLED QUICK-PASTE:</b>", *quick_paste])
else:
    msg.append("📋 <b>EXECUTION:</b> Standing aside. Cash preserved in SCB EZ / MMF.")

if is_friday:
    msg.extend(["━━━━━━━━━━━━━━━━━━", "🛡️ <b>FRIDAY OVERNIGHT COLLAR:</b> Flatten unhit T1 before close."])

if token and chat_id:
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "\n".join(msg), "parse_mode": "HTML"}, timeout=20)
    print("Engine E cycle completed successfully.")
