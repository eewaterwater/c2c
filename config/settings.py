"""All tunable parameters live here. Secrets come from environment variables only."""
import os

# --- Secrets (GitHub Actions secrets, or a local env var; never commit) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Account / sizing ---
EQUITY = 1_000_000          # THB
POINT_VALUE = 200           # THB per index point per S50 contract
CONTRACTS = 10              # default size
MAX_RISK_PCT = 0.01         # hard cap: stop risk must stay under 1% of equity
INITIAL_STOP_PTS = 4.5

# --- Tranches (fractions of total contracts; remainder goes to T2B) ---
T1_FRAC, T2A_FRAC = 0.4, 0.3
T1_MIN_PTS, T1_ATR_MULT = 5.0, 1.2
T2A_PTS = 12.0
CHANDELIER_ATR_MULT = 1.5
STAGNATION_BARS = 6

# --- Data tickers (Yahoo Finance). S50 futures are NOT on Yahoo, so the SET50 index
# is used as the price proxy. Override with the INDEX_TICKER env var if needed. ---
INDEX_TICKER = os.getenv("INDEX_TICKER", "^SET50.BK")
HEAVYWEIGHTS = ["DELTA.BK", "PTT.BK", "PTTEP.BK", "BDMS.BK", "CPALL.BK"]
INDEX_MIN_PLAUSIBLE, INDEX_MAX_PLAUSIBLE = 400, 3000   # sanity range for SET50 index level
EMINI_TICKER = "ES=F"
USDTHB_TICKER = "THB=X"

# --- Regime gate thresholds ---
EMA_FAST, SMA_SLOW, ATR_N = 20, 200, 14
SLOPE_DAYS, MIN_EMA_SLOPE_PTS = 3, 0.80
MAX_EXTENSION_ATR = 2.5
MIN_BREADTH = 3
EMINI_MIN_RETURN_PCT = -0.60
USDTHB_MAX_SLOPE_PCT = 0.30
MPC_BLACKOUT_MIN = 60

# --- Macro calendar: FILL THESE IN from official sources. Empty on purpose. ---
BOT_MPC_DATETIMES_BKK = []   # e.g. "2026-12-23 14:00"
US_CPI_DATES = []            # e.g. "2026-11-10"
FOMC_DATES = []              # decision days, e.g. "2026-12-09"

# --- Schedule ---
ENTRY_SLOTS_BKK = ["11:45", "14:15"]
LATEST_ENTRY_BKK = "16:15"
TIMEZONE = "Asia/Bangkok"
