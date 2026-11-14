# Snap-back V-hunter + slow trend rider

A trading bot for the make.fun "Race a BTC trading bot live for 24 hours"
competition. It trades Coinbase Wrapped BTC (CBBTC) against USDC on 1-minute
on-chain bars, starting with 1,000 USDC, long only.

## Protocol

The bot speaks `agent-fun/bars-replay/v1`: newline-delimited JSON on stdin /
stdout. It reads one `hello`, then one `batch` per minute (the bar that just
closed, the fills of the previous batch's orders, and the account), and
answers every batch with exactly one `reply` echoing its `seq`. Orders
decided on a batch fill at the next bar's open. Python 3.13, standard
library only, no network, no wall clock, no randomness — two runs on the
same data produce byte-identical replies.

## Strategy

Two independent legs, one position at a time, full-size entries:

- **Leg A — snap-back.** After a sharp 15-bar drop (≤ −65 bps) that has just
  started to bounce (last 3 bars ≥ +15 bps), buy with all cash. A sudden
  sell-off in this pool usually over-shoots and snaps back a little; the
  bounce confirmation keeps the bot from catching a falling knife. Exit on
  +150 bps take-profit, −60 bps stop, a 30 bps trailing stop off the best
  close since entry, or 60 bars.
- **Leg B — slow trend.** When the 60-bar EMA of closes crosses above the
  240-bar EMA, buy with all cash and ride the slow leg. Exit on +120 bps
  take-profit, −80 bps stop, a 60 bps trailing stop, the cross back down,
  or 600 bars.

All orders use `max_slippage_bps` 200 (one-minute moves in the dev window
never exceeded ~66 bps).

## Local result (dev window, `grade-local`)

- Net PnL: **+14.97 USDC** on the published 1,440-bar dev window
- Trades: 6 filled orders (3 round trips)
- Max drawdown: 0.95%
- Deterministic: two runs produce byte-identical action logs
