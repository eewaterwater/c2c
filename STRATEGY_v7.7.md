# Code2Cash v7.7 — TFEX S50 Hybrid Execution & Risk Architecture

## 1. Regime-Based Sizing & Exit Architecture
Every 15M signal is classified into one of two structural regimes prior to order entry:

| Regime | Structural Condition | Position Size | Exit Architecture |
| :--- | :--- | :---: | :--- |
| **Regime A: Trend / Breakout** | Price breaks outside Prior-Day Value Area (`VAH`/`VAL`) OR intraday range `>= 6.0 pts` with 2H `EMA(20)` + 15M `MA(200)` directional momentum (`RSI >= 54` Long / `RSI <= 42` Short). | **10 Contracts** AM (`6` Contracts PM)<br>(`฿800` / `฿480` RT fee) | **`7 / 2 / 1` Runner Split** (`4 / 1 / 1` PM):<br>• **T1 (`7` cts):** `+2.5 pts` -> move stop on `3` cts to **Entry BE**<br>• **T2 (`2` cts):** `+4.0 pts` shelf<br>• **T3 (`1` ct):** Trail on **15M Close `> / < EMA(9)`** |
| **Regime B: Range / Inside Box** | Price is trapped inside a `< 6.0 pt` intraday range or inside `VAH`/`VAL` with 15M `RSI(14)` between `42 – 54`. | **4 Contracts** (`฿320` RT fee) | **100% All-Out Limit (`4 / 0 / 0`):**<br>• **Single Target (`4` cts):** `+2.8 pts` (`+฿2,240` gross)<br>• **Zero Runners:** Eliminates breakeven chop-outs and cuts fee drag by `60%`. |

---

## 2. Hard Risk & Settrade Streaming Order Invariants
1. **10-Second `MP-MKT` Stop Reflex:** Immediately after entry fill, place a conditional `MP-MKT (IOC)` stop capped at **`<= 3.0 pts`** (`2.8 pts` Trend / `2.0 pts` Range).
2. **`Av.Vol` Unblocked Stop Rule (Settrade Streaming Fix):**
   - Never place resting T2 limit orders that lock `Av.Vol` prior to arming the conditional stop.
   - After T1 (`7` contracts) fills in Regime A, cancel obsolete orders so `Av.Vol = 3`, arm **one single conditional stop** covering all **`3` remaining contracts** (`Last >= / <= Entry`, `MP-MKT`, `IOC`), and execute T2 (`2` contracts) via price alert or manual limit once price approaches the shelf.
3. **60-Minute Time-Stop Invariant:** If T1 has not filled within **60 minutes** of entry, scratch the position flat at Market.
4. **Morning "One-and-Done" Profit Gate (`>= +฿3,000 THB` Gross):**
   - Once **`+฿3,000 THB` or more** is banked before `11:00 BKK`, lock `engine_t` into **Stand-Aside Mode** for the remainder of the morning (`11:00 – 12:30 BKK`) to prevent give-backs in midday chop.

---

## 3. Session Windows & Flatten Gates (Bangkok Time — UTC+7)
- **`09:45 – 10:15 BKK`:** Opening Blackout (`0` trades).
- **`10:15 – 11:45 BKK`:** Morning Authorized Window (Max size: `10` Trend / `4` Range).
- **`11:45 – 14:18 BKK`:** Midday Freeze (`12:25 BKK` hard pre-lunch runner flatten).
- **`14:18 – 15:30 BKK`:** Afternoon Authorized Window (Max size: `6` Trend / `4` Range; `0` trades if morning banked `>= +฿5,000 THB`).
- **`15:58 BKK`:** Hard End-of-Day Flatten Gate (Zero overnight TFEX exposure).
