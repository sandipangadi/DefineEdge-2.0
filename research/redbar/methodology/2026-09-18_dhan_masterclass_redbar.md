# Dr Devendra / Dhan masterclass — Red Bar methodology evidence

Source: https://youtu.be/AL4MDHc639w
Captured: 2026-09-18
Evidence type: public methodology/interview; not trade-performance evidence.
Research scope: Indian Indices Trading Brain / Red Bar only. Do not mix with Momentum stock-options research.

## High-confidence explicit rules from supplied transcript

1. **Opening / X candle**
   - First 15-minute candle is called the X candle.
   - Its Fibonacci band uses 0.44 / 0.50 / 0.56.
   - The 0.50 mean alone was said to allow fake breakouts; the +/-6% band was added around it.
   - Directional interpretation is relative to this band rather than simply following the candle colour.
   - The opening/X-candle level remains valid for the full day.

2. **Red Bar definition and timing**
   - Explicit wording: **"First red after 9:15."**
   - The Red Bar is the first subsequent red candle after the opening 9:00–9:15 candle; it is not fixed to 09:20, 09:25, 09:30, etc.
   - Dr Devendra describes the Red Bar timing as dynamic: it may appear later and the market determines when it appears.
   - The Red Bar creates the morning entry/reference level.
   - Explicit directional rule stated in the interview: **below the Red Bar level = short; above the Red Bar level = buy/long** (break above is described as the seller's stop being taken and potential upside breakout).
   - Retests of the level are explicitly discussed as valid places to use the level.

3. **Institutional / smart-money interpretation**
   - The first following red candle is hypothesised by Dr Devendra to represent a stronger seller/institutional or smart-money footprint. He explicitly presents this as his hypothesis/thought process, not as an established market fact.
   - Therefore retain this as *author methodology rationale*, not independently verified causality.

4. **Three intraday regimes / Trikal**
   - Indian/commodity implementation is divided into three sessions/regimes: Asia/morning, London/afternoon, New York/evening.
   - Morning begins from Indian market open.
   - 12:30 is treated as a reset/rewire point. He says to wait quietly from 12:30–13:00 while the market re-decides direction.
   - The 12:30–13:00 candle/window has its mean projected as the afternoon/London reference level.
   - Evening reference window is stated as 17:30–18:00 for the commodity implementation.
   - These later commodity-session times should **not** be mechanically imported into the Indian-index 6-hour session without separate validation.

5. **Mean / EMA framework**
   - 30-minute 5 EMA is a higher-timeframe observation mean.
   - Explicit equivalence used by Dr Devendra: **5 EMA on 30-minute ~= 30 EMA on 5-minute**.
   - Observation/visualisation is on 30-minute; execution can be on 1–5 minute.
   - Price becoming stretched away from the mean is treated as a mean-reversion/retest condition, not as an automatic entry by itself.
   - He states that his Red Bar framework for the Indian ~6-hour market is built around price returning/touching the EMA before around noon; this is a testable replay hypothesis, not yet independently validated.

6. **Trend filter / Renko SuperTrend**
   - Smart Renko Engine includes a slower trend component described as 30 MA plus a shorter trend MA (interviewer summarises 10 and 30).
   - A Renko-based SuperTrend is described: Renko/ATR-derived input is used instead of ordinary candle close/OHLC input to smooth the SuperTrend/trailing signal.
   - Trend direction is intended as a guardrail: avoid trading against the displayed trend/levels.

7. **Renko construction rationale**
   - Renko is described as price-movement/box based rather than fixed-time OHLC representation.
   - Box formation occurs only after price moves by the required box amount; reversal requires the corresponding downside movement.
   - The interview discusses ATR-based Renko calculations for the Smart Renko Engine.
   - Keep this distinct from the separately evidenced intraday box-size convention `typical good-day range / 12`; the video transcript does not establish that these are the same calculation.

## Direct implications for frozen Indian-index Red Bar replay

- Reconstruct the 09:00–09:15 X candle and its 0.44/0.50/0.56 band.
- Search for the **first red candle after 09:15**, preserving its actual timestamp rather than assuming a fixed 09:20/09:30 Red Bar.
- Test both sides of the Red Bar reference: downside break/acceptance and upside stop-out/breakout.
- Record first break, retest, rejection/acceptance, MFE, MAE, and whether a second entry would have required a fresh breakout.
- Add 30-minute 5 EMA / 5-minute 30 EMA distance and first return-to-mean timing as replay fields.
- Keep Trikal/session evidence and Red Bar evidence separately tagged even when both appear in the same indicator or public post.
- Do not treat institutional causality, Telegram results, or interview examples as backtest truth.

## Important unresolved items

- Exact mathematical formula converting a Red Bar candle into the publicly posted narrow Positive/Negative thresholds is still not established by this transcript.
- Exact candle type used to identify the Red Bar in the Indian-index implementation (ordinary candle vs internal Renko-derived state) should be verified from platform examples/replay.
- Exact definition of the Red Bar 'level' (high/low, mean, band edge, derived value) is not fully specified in the verbal transcript.
- Exact stop-loss/target mechanics remain open; the video mainly defines reference levels, direction and trend/mean context.
- The 12:30–13:00 and 17:30–18:00 examples are presented in the commodity/Trikal discussion and must not be assumed to map unchanged to NIFTY.

## Evidence status

This video materially upgrades the research from inferred Telegram behaviour to explicit public methodology. It supports the dynamic **first-red-after-09:15** rule and two-sided use of the Red Bar level, while leaving the exact Positive/Negative threshold calculation unresolved.
