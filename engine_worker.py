"""Scheduled entry point. Usage: python engine_worker.py [--dry-run]"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings as S
from core import market_feed as mf, strategy_engine as se, notifier as nt

STATE = Path("state/state.json")


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"position": None}


def save(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def main(dry=False):
    now = datetime.now(ZoneInfo(S.TIMEZONE))
    send = (lambda t: print(t)) if dry else nt.send
    state = load()
    pos = state.get("position")
    bars = mf.fetch_2h(S.INDEX_TICKER)
    daily = mf.fetch_daily(S.INDEX_TICKER)
    note = mf.check_index_data(daily, bars, now)
    if note:                       # holiday: nothing new to evaluate
        send(f"C2C S50 | {now:%a %d %b %H:%M} BKK\n{note} Skipped.")
        return

    # 1) manage open position
    if pos and pos["open"]:
        events = se.process_bars(pos, bars)
        if events:
            send(nt.fmt_events(now, events, pos))
        _, no_overnight = se.event_status(now)
        if pos["open"] and no_overnight and now.strftime("%H:%M") >= S.ENTRY_SLOTS_BKK[-1] \
                and not pos["t1_filled"]:
            send("C2C WARNING: CPI/FOMC day, overnight holds banned while T1 is unfilled. Flatten before the close.")

    # 2) evaluate gates
    n, detail = mf.breadth_positive()
    gates = (se.regime_gates(daily)
             + se.external_gates(n, detail, mf.emini_return_since_asia_open(now), mf.usdthb_slope_pct())
             + se.timing_gates(now))

    # 3) entry or status
    flat = not (pos and pos["open"])
    if flat and all(g.passed for g in gates):
        entry = float(bars["close"].iloc[-1])
        pos = se.build_plan(entry, float(mf.atr(bars, S.ATR_N).iloc[-1]))
        pos["last_bar_ts"] = str(bars.index[-1])
        state["position"] = pos
        send(nt.fmt_entry(now, pos, gates))
    else:
        note = "Position open; no new entries." if not flat else ""
        send(nt.fmt_status(now, gates, note))

    state["position"] = pos
    if not dry:
        save(state)


if __name__ == "__main__":
    now_ = datetime.now(ZoneInfo(S.TIMEZONE))
    try:
        if "--diagnose" in sys.argv:
            nt.send("C2C feed diagnostics\n" + "\n".join(mf.diagnose(now_)))
        else:
            main(dry="--dry-run" in sys.argv)
    except mf.FeedError as e:
        nt.send(f"C2C DATA ERROR, no decision made.\n{e}")
        sys.exit(1)
