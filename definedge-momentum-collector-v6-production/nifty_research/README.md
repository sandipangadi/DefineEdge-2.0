# NIFTY Historical Research Mode — DS-01 / CVP-01

Status: research-only; not wired to the production Flask routes.

## Safety boundary

The production stock-options pipeline remains unchanged. `app_v6.py` continues to load the frozen V5 reconstruction engine. This directory is an additive index-research layer only. Do not import it from production until its resolver and output are validated.

## Objective

Collect one-minute historical data required to test whether DS-01 Directional Straddle and CVP-01 Camarilla/VWAP/Option-P&F add information beyond the existing NIFTY P&F baseline.

## Required streams

For each trading date in a selectable range within the Definedge intraday-history limit:

1. NIFTY spot, 1 minute.
2. Front/appropriate NIFTY futures contract, 1 minute, OHLCV + OI when supplied.
3. Current-expiry options around the dynamically resolved ATM: ATM CE/PE, ATM+200 CE/PE, ATM-200 CE/PE.
4. Next-expiry versions of the same six legs.
5. Neighboring strikes should be retained around ATM changes so the research engine can reconstruct the correct leg without look-ahead.

All contract selection must be timestamp/date aware. Expired-contract tokens must be resolved from the NFO master snapshot rather than hard-coded.

## Definedge history contract

Historical path used by the existing research architecture:

`/sds/history/{segment}/{token}/{timeframe}/{from}/{to}`

Authentication: `Authorization: api_session_key`.

Research segments: NSE for spot where applicable; NFO for futures/options. Timeframe: minute.

## Raw package layout

```
NIFTY_Research_<from>_<to>.zip
  manifest.json
  masters/
    nse_master_snapshot.csv
    nfo_master_snapshot.csv
  raw/
    spot/
    futures/
    options_current/
    options_next/
  derived/
    atm_map.csv
    expiry_map.csv
    data_quality.csv
  validation/
    ds01_events.csv
    cvp01_events.csv
    comparison_ladder.csv
```

No derived indicator may overwrite raw market data.

## DS-01 reconstruction

For each minute, build same-strike straddle values from synchronized CE+PE prices for ATM+200 and ATM-200. Preserve ATM straddle as a control. Test the hypothesis that relative/falling behavior of +200 versus -200 straddles contains subsequent NIFTY directional information.

Outputs must include signal timestamp, expiry bucket, DTE, time-of-day, subsequent NIFTY return over 15/30/60/120 minutes, MFE, MAE, and current-versus-next-expiry comparison. Do not encode the source video's directional interpretation as truth; it is the hypothesis being tested.

## CVP-01 reconstruction

Derive daily Camarilla levels from prior-session NIFTY data. Compute futures VWAP intraday. Build ATM CE/PE 200-EMA high/low channel and option P&F (research default 1% box) patterns. Record each component independently before testing confluence.

Outputs must distinguish:
- P&F baseline alone
- baseline + Camarilla state
- baseline + futures VWAP state
- baseline + ATM option/EMA/P&F confirmation
- full CVP-01 confluence

## Controlled comparison ladder

`P&F baseline -> + DS-01 -> + CVP-01 -> DS-01 + CVP-01`

Compare signal count, false-signal rate, hit rate, 15/30/60/120-minute MFE/MAE, DTE/expiry behavior, time-of-day behavior, and expectancy/PF where a tradable rule can be defined without look-ahead.

## Evidence governance

Universe tag: INDEX OPTIONS.

Current status for DS-01: HYPOTHESIS.
Current status for CVP-01: HYPOTHESIS.

Promotion requires independent historical evidence and then forward testing. Stock-option V6/LIQ20/LIQ50 evidence is not admissible as empirical validation of these index hypotheses.

## Next implementation unit

Implement a token/expiry/ATM resolver and Definedge minute downloader behind this specification, then validate it on a small date window before requesting the user's OTP for the first six-month collection.
