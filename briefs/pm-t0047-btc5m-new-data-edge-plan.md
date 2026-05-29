# t-0047 — BTC5M: new data sources → features → edge → dashboard

Repo: PolyMarketResearch. Branch: `feat/btc5m-flow-confluence`. Workers may commit on that branch (PM does not — PM only writes this brief on `orch/t-0047`).

## Goal

Expand the BTC5M data surface beyond the existing feeds (consensus spot, perp funding rate, OI, Deribit DVOL, Fear/Greed), find a feature with a measurable edge over the existing resolved-bets sample, and surface it in the React dashboard (`btc5m/webui`).

## Pipeline (3 workers, chained by file artifacts)

1. **researcher → `btc5m/RESEARCH_NEW_DATA_SOURCES.md`**
   Survey 6–8 candidate data sources beyond current feeds. For each: source URL, payload shape, refresh cadence, expected 5m-horizon utility, plausibility of edge, failure modes. Recommend the top 2 to implement with a paragraph each + one raw response sample. Lean toward microstructure / liquidations / options skew / cross-asset that are realistically actionable at 5m.

2. **backend → `btc5m/RESEARCH_NEW_FEATURE_EDGE.md` (+ feeds.py / features.py / a backtest script)**
   Wait for the researcher artifact. Implement the top-2 feeds (httpx adapter pattern from existing `btc5m/feeds.py`, graceful degradation, pure parsers with unit tests). Expose 1–2 derived features in `btc5m/features.py`. Run a backtest script that scores those features as a **gate** over resolved bets in `btc5m.db` and reports gross/net P&L, win-rate, and sample size with and without the gate. Write up the result honestly — if there is no edge, say so.

3. **frontend → React dashboard card + edge-gate badge**
   Wait for the backend edge file. If backend extended `/api/feeds`, surface the new feed(s) on the Feeds tab (`webui/src/components/sections/Feeds.tsx`). If backend exposed a gate verdict, show an "edge gate" badge on Overview. `npm run build` (the live `/` serves `webui/dist`). Commit on the project branch.

## Sequencing

All three are queued at once. Workers 2 and 3 poll for their upstream file before doing real work; if the upstream is missing after a generous timeout, they stop and report what they did.

## Out of scope

- Order placement (paper-only by construction).
- Refactoring existing signals.
- Changing the existing `fleet.algorobos.com` / `btc.algorobos.com` cloudflared setup (t-0046 is done).
