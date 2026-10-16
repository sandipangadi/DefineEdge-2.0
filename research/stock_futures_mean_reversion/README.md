# NSE stock-futures positional mean-reversion — exploratory cash-price proxy

**Research date:** 2026-10-16. **Status:** Unapproved hypothesis / backtest research only. No live broker orders, no OpenAlgo worker deployment, no changes to AlgoStra or Red Bar.

This folder holds reproducible Python code and generated trade/metric artifacts. The authoritative narrative, risk interpretation and decisions are maintained in **Google Drive Trading Brain**:
[Five-year and six-month research report](https://docs.google.com/document/d/1o8AUYvBNew2dlpouddWxLWatncp9FW2KqAbJGwRp64M/edit?usp=drivesdk).

## Files

- `backtest_20261016.py`: Standard-library + requests read-only cash-price signal simulation on Yahoo Finance chart data for 6 NSE stock symbols.
- `trades_20261016.csv`: Detailed trade entries and exits for both variants (includes 6 warm-up-window trades before the 2021-10-16 evaluation start). 258 rows total; 252 fall inside evaluation windows.
- `report_20261016.json`: Full provenance, rules, audit counts, buy-and-hold benchmarks and 5-year / 6-month metrics; excludes trades still open at period cutoff from completed trade figures.

## Run

```bash
python3 -m pip install requests
python3 research/stock_futures_mean_reversion/backtest_20261016.py
```

Outputs write only to `/tmp/nse_mean_revert_20261016`. The code makes HTTPS reads to Yahoo Finance only; **no market order/broker credentials or production services** are involved.

## Data and caveats

- Universe: HDFCBANK, ICICIBANK, RELIANCE, SBIN, AXISBANK, INFY, chosen as liquid FUT stock names.
- Evaluation: 2021-10-16 to 2026-10-16, plus 2026-04-16 to 2026-10-16.
- Underlying: Yahoo `*.NS` daily **stock** OHLC, adjusted via the split/dividend-adjusted closing price factor, **not** NSE expired futures prices.
- Conservative stop-first assumption if stop and target both crossed within a daily bar; next-day opening entries; 1.8R target and maximum 15-session holding.
- Deduct 0.20% notional per completed trade as a simplified roundtrip cost estimate; actual futures basis, transaction costs, slippage, roll, margin, lot sizing and physical delivery not modeled.
- Return and drawdown outputs are **sequential single-stock compounded trade returns and closed-trade drawdowns**, not leverage-aware futures strategy portfolio returns.
- No optimization / out-of-sample validation; current-day symbol selection introduces survivorship bias.
- Trades open at final cutoff are included in the raw ledger, explicitly marked, but are not counted in realized performance totals.
