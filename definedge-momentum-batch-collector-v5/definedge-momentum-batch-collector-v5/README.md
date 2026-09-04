# Definedge Momentum Batch Collector V5

## What V5 does

V5 is the **full daily collector** for the Stock Options Momentum project.

You give it **one ZIP containing all AlgoStra Positions CSVs and Activity Logs**.

V5 then:

1. Reads every AlgoStra Positions file.
2. Reads the matching Activity Logs.
3. Identifies the exact stock-option contracts.
4. Detects the same trade repeated across T7 / T8 / SL12 / V1 / LIQ20 / LIQ50 / LIQINST.
5. Downloads the same Definedge market stream **only once** where possible.
6. Pulls exact option tick data:
   - time
   - LTP
   - LTQ
   - Open Interest
7. Pulls the corresponding underlying-stock tick data.
8. Pulls the entry-time nearby option chain:
   - ATM - 1
   - ATM
   - ATM + 1
   - CE and PE
9. Calculates MFE / MAE / peak give-back / post-exit movement.
10. Creates **one final ZIP for ChatGPT analysis**.

---

# Your daily routine

## Step 1 — Download AlgoStra files

From AlgoStra, download the day's:

- **Positions CSVs**
- **Activity Logs**

for the momentum strategies you want studied.

Example strategies:

- LIQ20 BULL AP T7
- LIQ20 BULL AP T8
- LIQ20 BULL AP SL12
- LIQ20 BULL AP V1
- LIQ20 BEAR variants
- LIQ50 BULL / BEAR
- LIQINST BULL / BEAR

---

## Step 2 — Put them into ONE ZIP

The ZIP can look like:

```text
algostravirtualtradelogsandpositionscsvfiles.zip

Positions - LIQ50 BEAR AP V1.csv
Activity Log - LIQ50 BEAR AP V1.csv
Positions - LIQ20 BULL AP T7.csv
Activity Log - LIQ20 BULL AP T7.csv
Positions - LIQ20 BULL AP T8.csv
Activity Log - LIQ20 BULL AP T8.csv
...
```

You **do not** need to upload each CSV separately to Render.

---

## Step 3 — Open the Render website

The page will ask for:

### Collector password

This is the private password you set in Render as:

```text
COLLECTOR_PASSWORD
```

Enter it **once**.

Click:

```text
Send Definedge OTP
```

---

## Step 4 — Enter Definedge OTP

Definedge sends the OTP to you.

Enter it in the website.

---

## Step 5 — Upload the full AlgoStra ZIP

Choose:

```text
algostravirtualtradelogsandpositionscsvfiles.zip
```

Recommended settings:

```text
Minutes before entry: 3
Minutes after exit:   15

✓ Include ATM-1 / ATM / ATM+1 for CE and PE
```

Click:

```text
Start Full Collection
```

---

# What happens next?

You do not need to do anything.

V5 runs in the background.

The page shows progress such as:

```text
Reading AlgoStra ZIP
Resolving exact option contracts
Downloading exact option ticks
Downloading underlying ticks
Downloading option-chain ticks
Calculating MFE / MAE
Writing final ZIP
```

When finished, the page shows:

```text
Download Full Analysis ZIP
```

---

# Your final output

The downloaded file will look like:

```text
Momentum_Full_Analysis_V5_YYYY-MM-DD_HHMMSS.zip
```

Inside:

```text
algostra_original/
    all original Positions CSVs
    all original Activity Logs

market_data/
    options/
        exact traded option ticks
        1-minute backup

    underlyings/
        stock ticks
        1-minute backup

    option_chain/
        ATM-1 / ATM / ATM+1
        CE + PE tick data

analysis/
    all_strategy_trades.csv
    trade_metrics.csv
    strategy_comparison.csv
    duplicate_trade_map.csv
    activity_log_summary.csv
    activity_log_events.csv
    input_file_report.csv
    unresolved_trades.csv
    data_quality.csv

broker/
    orders.csv
    trades.csv
    positions.csv
    limits.csv

reference/
    Definedge NFO master snapshot
    Definedge NSE cash master snapshot

manifest/
    trades.json
    api_errors.json
```

---

# The most important file for ChatGPT

```text
analysis/trade_metrics.csv
```

This contains trade-by-trade information such as:

- AlgoStra entry premium
- AlgoStra exit premium
- nearest Definedge entry tick
- nearest Definedge exit tick
- MFE
- MAE
- maximum premium reached
- minimum premium reached
- time of maximum premium
- peak-to-exit profit give-back
- percentage of available move captured
- post-exit maximum premium
- post-exit minimum premium
- Open Interest at entry / exit
- underlying price movement

---

# Why duplicate_trade_map.csv matters

The same underlying trade may appear in several strategy versions.

Example:

```text
TCS PE

LIQ20 BEAR T7
LIQ20 BEAR T8
LIQ20 BEAR SL12
LIQ20 BEAR V1
```

V5 does **not** need four separate copies of the same TCS market data.

It downloads the market stream once and then compares how each strategy version handled that move.

That is what allows ChatGPT to answer:

> Did T7 really exit better than T8?

> Did SL12 cut the trade too early?

> Was the trade itself poor, or only the exit?

> How much MFE did each version capture?

---

# What to upload into ChatGPT

Upload the **final V5 output ZIP** into your:

```text
Momentum Trading / Stock Options project
```

That is the file ChatGPT should analyse against the strategy material and historical trade logs already stored in the project.

You do **not** need to separately upload all the tick CSVs one by one.

---

# Render setup

Use these settings:

## Root Directory

If the V5 folder is inside your GitHub repository:

```text
definedge-momentum-batch-collector-v5
```

## Build Command

```text
pip install -r requirements.txt
```

## Start Command

```text
gunicorn --workers 1 --threads 4 --timeout 300 app:app --bind 0.0.0.0:$PORT
```

## Compute

```text
Free
```

## Environment Variables

Add these three:

```text
DEFINEDGE_API_TOKEN
DEFINEDGE_API_SECRET
COLLECTOR_PASSWORD
```

Do not put the actual secret values into GitHub.

---

# Important timing

Definedge historical tick data is time-limited.

Therefore the safe routine is:

```text
AlgoStra trading day finishes
        ↓
download Positions + Activity Logs
        ↓
make one ZIP
        ↓
run V5 that day or by the next trading session
        ↓
keep the V5 output ZIP permanently
```

The minute-data backup is also collected for exact options and underlyings.

---

# What V5 does NOT do

V5 does **not** place trades.

It does not:

- place an order
- modify an order
- cancel an order

It is a **read-only data collector and analysis bridge**.

---

# If some trades are unresolved

Open:

```text
analysis/unresolved_trades.csv
```

V5 deliberately refuses to guess the wrong option contract.

This is safer than silently analysing the wrong strike or expiry.

---

# One-line summary

```text
ONE AlgoStra ZIP
        ↓
Render V5
        ↓
Definedge tick + option-chain bridge
        ↓
ONE complete analysis ZIP
        ↓
upload into Momentum Trading project
        ↓
ChatGPT compares entries, exits, MFE, MAE, SL/TSL and strategy variants
```
