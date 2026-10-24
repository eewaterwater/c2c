"""Data ingestion + pure indicator functions."""
import time
import pandas as pd
from config import settings as S


class FeedError(RuntimeError):
    """Raised when market data is missing, stale or implausible. The engine must not trade on it."""


# ---------- indicators (pure, testable) ----------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def sma(s, n):
    return s.rolling(n).mean()


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def to_2h(df_1h):
    """Pair consecutive 1h bars within each Bangkok trading day into 2H bars."""
    d = df_1h.copy()
    d.index = d.index.tz_convert(S.TIMEZONE)
    day = pd.Series(d.index.date, index=d.index)
    grp = d.groupby([day.values, d.groupby(day.values).cumcount() // 2])
    out = grp.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    out.index = pd.DatetimeIndex(grp.apply(lambda g: g.index[-1]).values)
    return out.sort_index()


# ---------- network fetchers ----------
def _download(ticker, period, interval, tries=3):
    import yfinance as yf
    df = None
    for i in range(tries):
        try:
            df = yf.download(ticker, period=period, interval=interval, auto_adjust=False, progress=False)
        except Exception as e:  # network / rate limit
            df = None
            last = e
        if df is not None and not df.empty:
            break
        time.sleep(2 * (i + 1))
    if df is None or df.empty:
        raise FeedError(f"No data returned for {ticker} ({interval}) after {tries} tries")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df[["open", "high", "low", "close"]].dropna()


def fetch_daily(ticker, period="2y"):
    return _download(ticker, period, "1d")


def fetch_2h(ticker):
    return to_2h(_download(ticker, "60d", "1h"))


def breadth_positive(tickers=None):
    """Heavyweights net positive on the day (last close vs previous close)."""
    detail = {}
    for t in tickers or S.HEAVYWEIGHTS:
        d = fetch_daily(t, "10d")
        detail[t] = bool(d["close"].iloc[-1] > d["close"].iloc[-2])
    return sum(detail.values()), detail


def emini_return_since_asia_open(now_bkk):
    """E-mini % return from 09:00 BKK today to the latest bar."""
    d = _download(S.EMINI_TICKER, "5d", "1h")
    d.index = d.index.tz_convert(S.TIMEZONE)
    today = d[(d.index.date == now_bkk.date()) & (d.index.time >= pd.Timestamp("09:00").time())]
    if today.empty:
        raise RuntimeError("No E-mini bars since 09:00 BKK")
    return (today["close"].iloc[-1] / today["open"].iloc[0] - 1) * 100


def usdthb_slope_pct(days=5):
    d = fetch_daily(S.USDTHB_TICKER, "1mo")
    return (d["close"].iloc[-1] / d["close"].iloc[-1 - days] - 1) * 100


# ---------- validation ----------
def check_index_data(daily, bars, now_bkk):
    """Guard rails for the SET50 proxy. Returns a note string (e.g. holiday) or raises FeedError."""
    last_close = float(daily["close"].iloc[-1])
    if not (S.INDEX_MIN_PLAUSIBLE <= last_close <= S.INDEX_MAX_PLAUSIBLE):
        raise FeedError(f"{S.INDEX_TICKER} last close {last_close:.1f} is outside the plausible SET50 range "
                        f"{S.INDEX_MIN_PLAUSIBLE}-{S.INDEX_MAX_PLAUSIBLE}; wrong ticker?")
    if len(daily) < S.SMA_SLOW + 10:
        raise FeedError(f"Only {len(daily)} daily bars, need {S.SMA_SLOW + 10} for SMA{S.SMA_SLOW}")
    age_days = (now_bkk.date() - bars.index[-1].date()).days
    if age_days > 4:
        raise FeedError(f"Latest 2H bar is {age_days} days old; feed looks stale")
    if age_days >= 1:
        return f"No SET bars today (last bar {bars.index[-1]:%d %b %H:%M}); likely a market holiday."
    return ""


def diagnose(now_bkk):
    """One line per feed: OK/FAIL with rows and last timestamp/close."""
    jobs = [
        (f"Index daily {S.INDEX_TICKER}", lambda: fetch_daily(S.INDEX_TICKER)),
        (f"Index 2H {S.INDEX_TICKER}", lambda: fetch_2h(S.INDEX_TICKER)),
    ] + [(f"Daily {t}", (lambda t=t: fetch_daily(t, "10d"))) for t in S.HEAVYWEIGHTS] + [
        (f"E-mini {S.EMINI_TICKER}", lambda: _download(S.EMINI_TICKER, "5d", "1h")),
        (f"USD/THB {S.USDTHB_TICKER}", lambda: fetch_daily(S.USDTHB_TICKER, "1mo")),
    ]
    lines = []
    for name, fn in jobs:
        try:
            d = fn()
            lines.append(f"OK    {name}: {len(d)} rows, last {d.index[-1]:%Y-%m-%d %H:%M} close {d['close'].iloc[-1]:.2f}")
        except Exception as e:
            lines.append(f"FAIL  {name}: {e}")
    return lines
