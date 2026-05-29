# t-0053 — PM decision on the BTC5M frontend integration

## Context

t-0049 backend finished with an honest verdict on the two new feeds:

- **Coinbase–Binance basis** — null on the 3-day sample; nothing to gate on.
- **Binance top-trader L/S ratio** — `top_ls_zscore <= -1` lifts win rate
  from 50.4% to 57.3% on n=131 bets, but fails the Wilson lower bound and
  Bonferroni correction. Point estimate is promising; statistical
  significance is not.

Both feeds are now live in `/api/feeds`. The frontend (t-0050) got stuck in
an interactive dispatcher prompt and needed a manual unstick.

## Decision (made on the user's behalf, t-0053)

1. **Ship the new feed values on the Feeds tab.** They are real,
   informative, and the API already serves them — surfacing them costs
   nothing and is reversible.
2. **Do NOT add an "Edge Gate" badge on Overview.** A badge that says
   ALLOWED/BLOCKED would imply the gate has been validated. It has not.
   Shipping a premature gate UI is the kind of thing we drop later with
   no audit trail.
3. **Annotate honestly** — show "forward-paper validation in progress
   (no proven edge yet)" near the new cards so the dashboard is not
   misread as a buy/sell signal.

This matches my original t-0050 instruction's "no edge → only ship Feeds
data view" branch.

## Action

- Hard-reset the frontend's stale prompt via `./orch send`.
- Queue t-0054 for the frontend with the decision pre-made, ordering it
  to execute directly and never call `AskUserQuestion` or `./fleet add`.
