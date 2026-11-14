#!/usr/bin/env python3
"""Snap-back V-hunter + slow trend rider for the make.fun BTC race.

A bot for the agent-fun/bars-replay/v1 protocol: it reads a hello line once,
then one batch per minute (each batch carries the bar that just closed, the
fills of the orders sent on the previous batch, and the account), and answers
every batch with exactly one reply echoing its seq and carrying its orders.
Orders decided on a batch fill at the next bar's open, so every signal here
is computed from closes already seen, never from the bar being filled at.

Strategy (original design, two independent legs, one position at a time):

  Leg A -- snap-back: after a sharp 15-bar drop (<= -65 bps) that has just
  started to bounce (last 3 bars >= +15 bps), buy with all cash. The idea is
  that a sudden sell-off in this pool usually over-shoots and snaps back a
  little; the bounce confirmation keeps us from catching a falling knife.
  Exit on +150 bps take-profit, -60 bps stop, a 30 bps trailing stop off the
  best close since entry, or 60 bars, whichever comes first.

  Leg B -- slow trend: when the 60-bar EMA of closes crosses above the
  240-bar EMA, buy with all cash and ride the slow leg. Exit on +120 bps
  take-profit, -80 bps stop, a 60 bps trailing stop, the cross back down,
  or 600 bars.

Both legs are long-only, full-size, and use max_slippage_bps 200 (one-minute
moves in the dev window never exceeded ~66 bps). All math is deterministic:
no wall clock, no environment, no randomness; prices are integers, signals
are plain float arithmetic applied in a fixed order, so two executions on
the same data produce byte-identical replies.
"""

import json
import sys

SYMBOL = "CBBTC"
MAX_SLIPPAGE_BPS = 200
MIN_NOTIONAL = 1_000_000  # 1 USDC in quote units

# Leg A: snap-back after a sharp drop with bounce confirmation.
DROP_LOOKBACK = 15
DROP_BPS = 65
BOUNCE_LOOKBACK = 3
BOUNCE_BPS = 15
TP_A_BPS = 150
SL_A_BPS = 60
TRAIL_A_BPS = 30
MAX_HOLD_A = 60

# Leg B: slow EMA cross.
EMA_FAST = 60
EMA_SLOW = 240
TP_B_BPS = 120
SL_B_BPS = 80
TRAIL_B_BPS = 60
MAX_HOLD_B = 600


def _buy(client_id, notional):
    return {
        "side": "buy",
        "client_id": client_id,
        "symbol": SYMBOL,
        "notional_quote_units": str(notional),
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }


def _sell(client_id, qty):
    return {
        "side": "sell",
        "client_id": client_id,
        "symbol": SYMBOL,
        "qty": str(qty),
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }


class Bot:
    """State for one replay run."""

    def __init__(self, hello):
        self.hello = hello
        self.warmup_bars = hello.get("warmup_bars", 0)
        self.seq = -1
        self.closes = []  # int close prices, in bar order
        self.ema_fast = None
        self.ema_slow = None
        self.ema_fast_prev = None
        self.ema_slow_prev = None
        self.cash = int(hello["capital"]["quote_units"])
        self.qty = 0
        self.entry_price = None  # int fill price of the open position
        self.entry_index = None  # index into closes[] of the fill bar
        self.entry_mode = None  # "A" snap-back or "B" slow trend
        self.peak = None  # highest close since entry
        self._pending_entry = False
        self._next = 1

    def _client_id(self, mode):
        cid = "%s%d" % (mode.lower(), self._next)
        self._next += 1
        return cid

    def on_batch(self, batch):
        self.seq = batch["seq"]
        # Fills first: a buy fill opens the position and names its mode
        # through the client id ("a..." snap-back, "b..." slow trend).
        for fill in batch.get("fills", []):
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.entry_mode = (
                    "A" if fill["client_id"].startswith("a") else "B"
                )
                self._pending_entry = True
        # Then the bar that just closed.
        bar = None
        for candidate in batch.get("bars", []):
            if candidate.get("symbol") == SYMBOL:
                bar = candidate
                break
        if bar is not None:
            close = int(bar["c"])
            self.closes.append(close)
            if self.ema_fast is None:
                self.ema_fast = float(close)
                self.ema_slow = float(close)
                self.ema_fast_prev = float(close)
                self.ema_slow_prev = float(close)
            else:
                self.ema_fast_prev = self.ema_fast
                self.ema_slow_prev = self.ema_slow
                k_fast = 2.0 / (EMA_FAST + 1)
                k_slow = 2.0 / (EMA_SLOW + 1)
                self.ema_fast = close * k_fast + self.ema_fast * (1.0 - k_fast)
                self.ema_slow = close * k_slow + self.ema_slow * (1.0 - k_slow)
            if self._pending_entry:
                self.entry_index = len(self.closes) - 1
                self.peak = close
                self._pending_entry = False
            elif self.qty > 0 and self.peak is not None and close > self.peak:
                self.peak = close
        # Then the account as it stands after this batch's fills.
        account = batch.get("account", {})
        self.cash = int(account.get("cash_quote_units", self.cash))
        positions = [
            p for p in account.get("positions", []) if p.get("symbol") == SYMBOL
        ]
        self.qty = int(positions[0]["qty"]) if positions else 0
        if self.qty == 0:
            self.entry_price = None
            self.entry_index = None
            self.entry_mode = None
            self.peak = None
            self._pending_entry = False
        return self.decide()

    def decide(self):
        if self.seq < self.warmup_bars or not self.closes:
            return []
        i = len(self.closes) - 1
        close = self.closes[-1]
        if self.qty == 0:
            if self.cash < MIN_NOTIONAL:
                return []
            # Leg A: snap-back.
            if i >= DROP_LOOKBACK:
                drop = (
                    (close - self.closes[i - DROP_LOOKBACK])
                    * 10000.0
                    / self.closes[i - DROP_LOOKBACK]
                )
                bounce = (
                    (close - self.closes[i - BOUNCE_LOOKBACK])
                    * 10000.0
                    / self.closes[i - BOUNCE_LOOKBACK]
                )
                if drop <= -DROP_BPS and bounce >= BOUNCE_BPS:
                    return [_buy(self._client_id("A"), self.cash)]
            # Leg B: slow EMA cross up.
            if (
                i >= EMA_SLOW + 2
                and self.ema_fast_prev is not None
                and self.ema_fast_prev <= self.ema_slow_prev
                and self.ema_fast > self.ema_slow
            ):
                return [_buy(self._client_id("B"), self.cash)]
            return []
        # In a position: exits depend on which leg opened it.
        if self.entry_price is None or self.entry_index is None:
            return []
        if self.entry_mode == "A":
            take_profit, stop, trail, max_hold = (
                TP_A_BPS,
                SL_A_BPS,
                TRAIL_A_BPS,
                MAX_HOLD_A,
            )
        else:
            take_profit, stop, trail, max_hold = (
                TP_B_BPS,
                SL_B_BPS,
                TRAIL_B_BPS,
                MAX_HOLD_B,
            )
        if i - self.entry_index >= max_hold:
            return [_sell(self._client_id("S"), self.qty)]
        gain_bps = (close - self.entry_price) * 10000.0 / self.entry_price
        if gain_bps >= take_profit:
            return [_sell(self._client_id("S"), self.qty)]
        if gain_bps <= -stop:
            return [_sell(self._client_id("S"), self.qty)]
        if (
            self.peak is not None
            and (self.peak - close) * 10000.0 / self.peak >= trail
        ):
            return [_sell(self._client_id("S"), self.qty)]
        if (
            self.entry_mode == "B"
            and i >= EMA_SLOW + 2
            and self.ema_fast_prev is not None
            and self.ema_fast_prev >= self.ema_slow_prev
            and self.ema_fast < self.ema_slow
        ):
            return [_sell(self._client_id("S"), self.qty)]
        return []


def main():
    bot = None
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line.decode("utf-8"))
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != "agent-fun/bars-replay/v1":
                raise SystemExit("unsupported protocol: %r" % (msg.get("protocol"),))
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                raise SystemExit("batch before hello")
            orders = bot.on_batch(msg)
            reply = json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":"),
            )
            sys.stdout.write(reply + "\n")
            sys.stdout.flush()
        elif kind == "done":
            break
        else:
            raise SystemExit("unknown message type: %r" % (kind,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
