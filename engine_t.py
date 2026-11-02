#!/usr/bin/env python3
import os, sys, datetime, pytz, requests, numpy as np, pandas as pd, yfinance as yf

BKK_TZ = pytz.timezone('Asia/Bangkok')
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_RISK_PTS, DEFAULT_CONTRACTS = 3.5, 10

def send_tg(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    kb = {"inline_keyboard": [[{"text": "📲 Open InnovestX", "url": "https://play.google.com/store/apps/details?id=com.scbs.easyinvest"}, {"text": "📈 TV S50Z26", "url": "https://www.tradingview.com/chart/?symbol=TFEX%3AS50Z2026"}]]}
    return requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "reply_markup": kb}, timeout=12).status_code == 200

def get_df(sym, p="1mo", itv="1d"):
    try:
        d = yf.download(sym, period=p, interval=itv, progress=False)
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
        d.columns = [str(c).lower() for c in d.columns]
        return d.dropna(subset=["close", "high", "low", "open"])
    except Exception: return pd.DataFrame()

def main():
    now_bkk = datetime.datetime.now(BKK_TZ)
    bkk_t = now_bkk.strftime("%Y-%m-%d %H:%M:%S")
    t = now_bkk.time()
    is_manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    # Execution Window & Auction Prep Check
    if datetime.time(9, 35) <= t < datetime.time(9, 45):
        send_tg(f"🌅 <b>[C2C ENGINE T: AUCTION WICK PREP]</b>\n⏱ <i>{bkk_t} BKK</i>\n• Long Wick Fade: <b>VAL - 2.5 pt stop</b>\n• Short Wick Fade: <b>VAH + 2.5 pt stop</b>\n• Bank or drag to BE by 09:58 BKK.")
        return
    if (t < datetime.time(9, 35) or datetime.time(12, 15) <= t < datetime.time(14, 15) or t >= datetime.time(16, 10)) and not is_manual:
        print(f"[{bkk_t}] Outside active trading window. Standing down.")
        return

    # Underlyings & Delta
    delta_pct = 0.0
    try:
        d_del = get_df("DELTA.BK", "5d", "1d")
        if len(d_del) >= 2: delta_pct = ((float(d_del["close"].iloc[-1]) - float(d_del["close"].iloc[-2])) / float(d_del["close"].iloc[-2])) * 100.0
    except Exception: pass

    # 15M SET50 Data
    d15 = get_df("^SET50.BK", "1mo", "15m")
    if d15.empty: d15 = get_df("^SET50", "1mo", "15m")
    if len(d15) < 20:
        send_tg(f"⚪ <b>[C2C ENGINE T: STANDING ASIDE]</b>\n⏱ <i>{bkk_t} BKK</i>\n• SET50 data feed initializing ({len(d15)} bars).\n• Cash Hold active.")
        return

    d15['ema_20'] = d15['close'].ewm(20, adjust=False).mean()
    d15['ma_200'] = d15['close'].rolling(200).mean()
    delta = d15['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    d15['rsi'] = 100 - (100 / (1 + (gain / loss.replace(0, np.nan))))

    live_p = float(d15["close"].iloc[-1])
    bar = d15.iloc[-2] if len(d15) >= 2 else d15.iloc[-1]
    cp, hp, lp, op = float(bar['close']), float(bar['high']), float(bar['low']), float(bar['open'])
    e20 = float(bar['ema_20']) if not np.isnan(bar['ema_20']) else cp
    m200 = float(bar['ma_200']) if not np.isnan(bar['ma_200']) else float(d15['close'].mean())
    rsi = float(bar['rsi']) if not np.isnan(bar['rsi']) else 50.0
    lw = min(op, cp) - lp
    uw = hp - max(op, cp)

    # Decoupler Gate
    if abs(delta_pct) > 1.5:
        send_tg(f"⚪ <b>[C2C ENGINE T: TACTICAL MONITOR - STANDING ASIDE]</b>\n⏱ <i>{bkk_t} BKK</i>\n━━━━━━━━━━━━━━━━━━\n• Live: <code>{live_p:.1f}</code> | Closed: <code>{cp:.1f}</code>\n• MA200: <code>{m200:.1f}</code> | EMA20: <code>{e20:.1f}</code>\n• <b>Veto:</b> DELTA Decoupled ({delta_pct:+.2f}% > ±1.5%)\n• Cash Hold active.")
        return

    # Check Long
    if cp > m200 and abs(cp - e20) <= 1.5 and lw >= 0.2 and cp >= op and (52.0 <= rsi <= 70.0):
        entry = round((op + cp) / 2.0, 1)
        stop = round(lp - 0.2, 1)
        risk = round(entry - stop, 2)
        if 0.4 < risk <= MAX_RISK_PTS:
            send_tg(f"🚨 <b>[ACTION REQUIRED: S50Z26 LONG ENTRY]</b>\n⏱ <i>{bkk_t} BKK</i>\n\n📋 <b>SETTRADE ONE-TAP EXECUTION:</b>\n<code>OPEN LONG S50Z26 @ {entry:.1f} | Vol: {DEFAULT_CONTRACTS} | Stop: {stop:.1f}</code>\n\n🎯 <b>BRACKET PROFILE ({DEFAULT_CONTRACTS} Contracts):</b>\n• <b>Limit Entry:</b> <b>{entry:.1f}</b>\n• <b>Hard Stop:</b> <code>{stop:.1f}</code> (-{risk:.1f} pts | Max Risk: ฿{int(risk*200*DEFAULT_CONTRACTS):,} THB)\n• <b>T1 Bank (6 cts):</b> {round(entry+3.5,1):.1f} (+3.5 pts >> BE)\n• <b>T2 Runner (40% / 4 cts):</b> {round(entry+7.0,1):.1f} (Trail EMA9)\n• <b>MA200:</b> {m200:.1f} 🟢 | <b>RSI:</b> {rsi:.1f}")
            return
        else:
            send_tg(f"⚪ <b>[C2C ENGINE T: TACTICAL MONITOR - STANDING ASIDE]</b>\n⏱ <i>{bkk_t} BKK</i>\n• <b>Veto:</b> Long stop {risk:.1f} pts exceeds {MAX_RISK_PTS} pt risk cap.")
            return

    # Check Short
    if cp < m200 and abs(cp - e20) <= 1.5 and uw >= 0.2 and cp <= op and (30.0 <= rsi <= 48.0):
        entry = round((op + cp) / 2.0, 1)
        stop = round(hp + 0.2, 1)
        risk = round(stop - entry, 2)
        if 0.4 < risk <= MAX_RISK_PTS:
            send_tg(f"🚨 <b>[ACTION REQUIRED: S50Z26 SHORT ENTRY]</b>\n⏱ <i>{bkk_t} BKK</i>\n\n📋 <b>SETTRADE ONE-TAP EXECUTION:</b>\n<code>OPEN SHORT S50Z26 @ {entry:.1f} | Vol: {DEFAULT_CONTRACTS} | Stop: {stop:.1f}</code>\n\n🎯 <b>BRACKET PROFILE ({DEFAULT_CONTRACTS} Contracts):</b>\n• <b>Limit Entry:</b> <b>{entry:.1f}</b>\n• <b>Hard Stop:</b> <code>{stop:.1f}</code> (+{risk:.1f} pts | Max Risk: ฿{int(risk*200*DEFAULT_CONTRACTS):,} THB)\n• <b>T1 Bank (6 cts):</b> {round(entry-3.5,1):.1f} (-3.5 pts >> BE)\n• <b>T2 Runner (40% / 4 cts):</b> {round(entry-7.0,1):.1f} (Trail EMA9)\n• <b>MA200:</b> {m200:.1f} 🔴 | <b>RSI:</b> {rsi:.1f}")
            return
        else:
            send_tg(f"⚪ <b>[C2C ENGINE T: TACTICAL MONITOR - STANDING ASIDE]</b>\n⏱ <i>{bkk_t} BKK</i>\n• <b>Veto:</b> Short stop {risk:.1f} pts exceeds {MAX_RISK_PTS} pt risk cap.")
            return

    # Stand-Aside Dispatch
    dist = abs(cp - e20)
    reason = f"Price extended {dist:.1f} pts from EMA20 (> 1.5 pt limit)" if dist > 1.5 else ("RSI outside confirmation bands" if (rsi < 52 or rsi > 70) else "No 15M MSS candle trigger")
    trend = "🟢 Above MA200" if cp >= m200 else "🔴 Below MA200"
    send_tg(f"⚪ <b>[C2C ENGINE T: TACTICAL MONITOR - STANDING ASIDE]</b>\n⏱ <i>{bkk_t} BKK</i>\n━━━━━━━━━━━━━━━━━━\n• <b>Live S50Z26:</b> <code>{live_p:.1f}</code> (15M Closed: {cp:.1f})\n• <b>Trend:</b> {trend} (MA200: <code>{m200:.1f}</code>)\n• <b>15M EMA(20):</b> <code>{e20:.1f}</code> (Dist: {dist:.1f} pts)\n• <b>15M RSI(14):</b> <code>{rsi:.1f}</code> | <b>DELTA:</b> <code>{delta_pct:+.2f}%</code>\n━━━━━━━━━━━━━━━━━━\n📋 <b>Status:</b> Standing Aside. ({reason})\n<i>Code2Cash Institutional Engine v7.2</i>")
    print(f"[{bkk_t}] Tactical Monitor dispatched.")

if __name__ == "__main__":
    main()
