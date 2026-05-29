# BTC5M Edge Scoreboard

Updated after every backtest by `PolyMarketResearch-pm`. Status vocabulary:
`live` / `forward-walk` / `dropped` / `null-needs-bigger-sample` / `pending`.

Acceptance bar for `live`: lift passes Wilson lower bound AND Bonferroni
correction on sample-N ≥ 1000 resolved bets (or ≥ 30 days) with ≥ 80%
feature coverage.

| feature                       | sample-N | gate                          | gate-WR | baseline-WR | lift   | gross/net P&L              | status                       |
|-------------------------------|---------:|-------------------------------|--------:|------------:|-------:|----------------------------|------------------------------|
| basis_zscore (CB−BN, wave-1)  |      587 | (any)                         | n/a     | n/a         | n/a    | n/a (feed null in window)  | null-needs-bigger-sample     |
| top_ls_zscore (Binance, wave-1)|     131 | `top_ls_zscore <= -1`         | 57.3%   | 50.4%       | +6.9pp | not significant @ wave 1   | forward-walk (wave 2 retest) |
| basis_zscore_extended (C-A)   |    TBD   | `\|basis_z\| >= 1`            | TBD     | TBD         | TBD    | TBD                        | pending (backend in flight)  |
| top_ls_zscore_forward (C-B)   |    TBD   | `top_ls_zscore <= -1`         | TBD     | TBD         | TBD    | TBD                        | pending (backend in flight)  |
| cross_coin_lead_lag (C-C)     |    TBD   | TBD (set by backtest)         | TBD     | TBD         | TBD    | TBD                        | pending (backend in flight)  |

## Notes per row

- **basis_zscore (CB−BN, t-0049)** — Coinbase spot vs Binance perp basis was
  null across the 3-day live capture used by the wave-1 backtest. Per the
  research-discipline rule, "null on 3 days" is NOT terminal — wave 2 must
  refetch a ≥ 30-day window (Binance perp + Coinbase spot both have history;
  consider Bybit perp as fallback if Binance OHLCV is rate-limited).
- **top_ls_zscore (Binance top-trader L/S, t-0049)** — point-estimate lift
  is meaningful (+6.9pp WR) but sample-N = 131 fails Wilson + Bonferroni.
  Wave 2 must forward-walk on a ≥ 1000 resolved-bet sample (or ≥ 30 days)
  before this gate either lands as `live` or is dropped.

## Pending (wave 2 candidates, queued)

- **basis_zscore_extended** — same gate, ≥ 30-day window backfill.
- **top_ls_zscore_forward** — same gate, forward-walk on ≥ 1000 bets.
- **wave2_new_candidate** — researcher picks from microstructure,
  liquidations, options skew, or cross-asset (deferred to wave 2 researcher
  artifact `btc5m/RESEARCH_WAVE2_CANDIDATES.md`).
