# Definedge Momentum Tick Collector V3

V3 is built around the actual AlgoStra strategy-log export format seen in:

**LIQ20 BEAR AP V1**

## What file do I upload?

Download the CSV from:

**AlgoStra → Logs → select the strategy → Download**

That CSV is the input. You do **not** need a second trade report.

V3 understands rows like:

```text
ENTRY1,ANGELONE:ANGELONE29SEP26P300:2500:299.6000
EXIT,ANGELONE:ANGELONE29SEP26P300/null:2500/0:302.8000/0.0000
```

It pairs ENTRY and EXIT by the exact option trading symbol.

## Website flow

1. Enter Collector Password once.
2. Click Send Definedge OTP.
3. Enter the OTP.
4. Upload the AlgoStra strategy-log CSV.
5. Click Collect & Download Analysis ZIP.

The password is no longer requested twice.

## Output

For each trade:
- exact option tick CSV: LTP, LTQ, OI
- exact option 1-minute CSV
- underlying tick CSV
- underlying 1-minute CSV
- optional ATM-1 / ATM / ATM+1, both CE and PE
- trade metadata

Also:
- `analysis/trade_summary.csv`
- `analysis/trade_metrics.csv`
- broker orders/trades/positions/limits
- current NFO/NSE master snapshots
- unresolved rows, if any

## Important

Definedge documents tick history as available for the last **2 trading sessions**.
Run the collector daily or by the next session and retain the generated ZIP.

The app is read-only. It contains no place-order, modify-order or cancel-order endpoint.
