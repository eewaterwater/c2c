"""Telegram formatting + dispatch. Falls back to stdout when no token is set."""
import requests
from config import settings as S


def send(text):
    if not (S.TELEGRAM_BOT_TOKEN and S.TELEGRAM_CHAT_ID):
        print("[telegram not configured] would send:\n" + text)
        return False
    r = requests.post(f"https://api.telegram.org/bot{S.TELEGRAM_BOT_TOKEN}/sendMessage",
                      data={"chat_id": S.TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    r.raise_for_status()
    return True


def gates_block(gates):
    return "\n".join(f"{'PASS' if g.passed else 'FAIL'}  {g.name}: {g.detail}" for g in gates)


def fmt_status(now, gates, note=""):
    ok = all(g.passed for g in gates)
    head = "ALL GATES PASS" if ok else "NO TRADE"
    return f"C2C S50 | {now:%a %d %b %H:%M} BKK\n{head}\n\n{gates_block(gates)}" + (f"\n\n{note}" if note else "")


def fmt_entry(now, pos, gates):
    q = pos["qty"]
    return (f"C2C S50 LONG SIGNAL | {now:%d %b %H:%M} BKK\n"
            f"Entry ~{pos['entry']:.1f}  (2H ATR {pos['atr_2h']:.2f})\n"
            f"Hard stop {pos['stop_a']:.1f} on all {sum(q.values())} contracts "
            f"(risk {pos['risk_thb']:,.0f} THB)\n"
            f"T1: {q['t1']} @ {pos['t1_target']:.1f} -> then move runners to {pos['entry']:.1f}\n"
            f"T2A: {q['t2a']} @ {pos['t2a_target']:.1f}\n"
            f"T2B: {q['t2b']} trailing (HH - {S.CHANDELIER_ATR_MULT} x 2H ATR)\n"
            f"Stagnation: flat after {S.STAGNATION_BARS} bars if T1 not filled\n\n"
            f"{gates_block(gates)}\n\nAlert only. Place orders yourself in Settrade.")


def fmt_events(now, events, pos):
    return (f"C2C S50 POSITION UPDATE | {now:%d %b %H:%M} BKK\n" + "\n".join(events) +
            (f"\n\nStops now: T1/T2A {pos['stop_a']:.1f}, T2B {pos['stop_b']:.1f}" if pos["open"] else ""))
