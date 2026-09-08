# NIFTY research resolver validation — 08 Sep 2026

Universe: INDEX OPTIONS only. Momentum V6/V6.2 production was not modified.

## Completed smoke test against the real V6 master snapshots

Source package: `Momentum_Full_Evidence_V6_2026-09-08_073832_OJAO3X.zip` (used only for its NSE/NFO master snapshots; LIQ20/LIQ50 trade evidence is not used as index evidence).

Observed raw Definedge master format:
- NFO snapshot is headerless, 77,727 rows x 15 columns.
- Positional fields match Definedge documentation: segment, token, symbol/root, trading symbol, instrument, expiry, tick size, lot size, option type, strike, price precision, multiplier, etc.
- Strike values are encoded and must be normalized; e.g. NIFTY 25,000 appears as 2,500,000 in the raw strike field.
- NSE snapshot is also headerless; NIFTY 50 cash index resolves uniquely to token `26000`.

The initial resolver incorrectly assumed a headered CSV. That was a genuine implementation defect and has been fixed on the research branch. The production Momentum collector was not touched.

## Resolver results on the real snapshot

For trade date 08-Sep-2026 and sample spot 24,873.4:
- rounded ATM: 24,850
- nearest NIFTY option expiry present in the snapshot: 15-Sep-2026
- next option expiry: 22-Sep-2026
- front NIFTY future: `NIFTY29SEP26F`, token `68407`
- current-expiry ATM CE: `NIFTY15SEP26C24850`, token `47356`
- current-expiry ATM PE: `NIFTY15SEP26P24850`, token `47358`
- resolver found all 18 requested current-expiry ladder legs and all 18 next-expiry ladder legs for ATM/±200 plus one neighboring 50-point strike on each side of each target.

NSE cash-index resolver result:
- NIFTY 50 token `26000`, symbol `Nifty 50`.

## Important negative knowledge / new blocker

The 08-Sep-2026 NFO master snapshot contains NIFTY option expiries from 15-Sep-2026 onward; it does not contain the expired weekly option tokens required to reconstruct prior weeks/months. This matters because the Definedge Historical Data API requires the `token (from Master file)` while the official Master File documentation describes the master as a latest-symbol file to be downloaded every morning.

Therefore, a current master snapshot by itself cannot support a six-month historical options collection. We must not pretend otherwise.

Before six-month DS-01/CVP-01 collection, one of these must be established:
1. an archived daily NFO-master source containing historical tokens; or
2. another Definedge/Opstra-supported route that exposes expired-contract identifiers/data; or
3. an external historical options source used only for the historical laboratory, with reconciliation back to Definedge for live/forward validation.

## Status

- Master-format parsing: VALIDATED on the real V6 snapshots.
- NIFTY cash token resolution: VALIDATED.
- Front-future resolution: VALIDATED on snapshot.
- Current/next-expiry option ladder resolution: VALIDATED on snapshot.
- Live/history HTTP request: NOT YET VALIDATED; requires authenticated Definedge session.
- Six-month historical option-chain reconstruction: BLOCKED by historical expired-token availability, not by ATM/expiry mathematics.
- DS-01: HYPOTHESIS — no performance conclusion.
- CVP-01: HYPOTHESIS — no performance conclusion.

## Next research unit

Resolve historical expired-contract token/data access first. After that, perform a one-day authenticated API smoke test, data-quality checks, and only then scale collection and run the comparison ladder:

`P&F baseline -> +DS-01 -> +CVP-01 -> DS-01+CVP-01`.
