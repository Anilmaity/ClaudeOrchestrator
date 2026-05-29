# t-0053 wave 2 — widen the sample, ship ≥ 3 candidates

## Why a wave 2

Wave 1 (t-0049) returned two candidates that, under the new standing
orders, are **not terminal**:

- `basis_zscore` was null on a 3-day live capture — way under the
  ≥ 30-day / ≥ 1000-bet bar.
- `top_ls_zscore <= -1` lifts win-rate 50.4% → 57.3% on n = 131 (point
  estimate is meaningful) but fails Wilson + Bonferroni significance.

Per the discipline rules a single null cannot collapse the goal. Wave 2
ships **three** candidates in parallel.

## Wave-2 candidates (≥ 3)

1. **C-A `basis_zscore_extended`** — same Coinbase-vs-Binance basis,
   but backfilled to a **≥ 30-day** historical OHLCV window. Backend
   widens the sample before re-running the gate backtest.
2. **C-B `top_ls_zscore_forward`** — `top_ls_zscore <= -1` re-tested
   on a forward-walk over **≥ 1000 resolved bets** (or ≥ 30 days),
   using Binance's `topLongShortPositionRatio` history.
3. **C-C `wave2_new_candidate`** — researcher's pick from
   {microstructure, liquidations, options-skew, cross-asset}. Most
   defensible single picks: Binance/Bybit liquidations stream,
   Deribit 25-delta risk reversal, cumulative volume delta, or ES/DXY
   5m correlation.

## Step order

1. **researcher** picks C-C from the four families and writes
   `btc5m/RESEARCH_WAVE2_CANDIDATES.md` with the chosen feature, the
   public free-tier endpoint, payload shape, and a worked example.
2. **backend** waits for that artifact, then implements **all three**
   candidates with ≥ 30-day historical samples where the venue supports
   it; runs the gate backtest on each; writes
   `btc5m/RESEARCH_WAVE2_EDGE.md` with sample-N, gate definition,
   gate-WR, baseline-WR, lift, Wilson lower bound, and
   Bonferroni-corrected significance for each candidate.
3. **PM (me)** updates `briefs/pm-scoreboard.md` from the actual
   numbers — every candidate, even nulls.
4. **frontend** ships the Overview gate badge **only** for candidates
   that pass the bar (lift passes Wilson + Bonferroni on ≥ 1000 bets
   or ≥ 30 days with ≥ 80% coverage). Nulls just get a scoreboard row.

## No single null collapses wave 2

If a candidate still says null on the widened sample, it goes to the
scoreboard with status `null-needs-bigger-sample` or `dropped` (the
latter only if the venue's history is fully exhausted) and the wave
moves on to the next.
