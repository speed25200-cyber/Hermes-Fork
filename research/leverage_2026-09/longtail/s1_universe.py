"""Step 1: point-in-time OKX listing calendar for every Binance USDT perp, and PIT liquidity ranks.

OKX listing: reuses the Hermes OkxListing reconstruction (read-only import): for each Binance symbol, the OKX
swap BASE-USDT-SWAP (1000/1M prefixes stripped) is 'listed' on day D if OKX's per-instrument daily trade archive
(static.okx.com) exists for D; probed on a weekly/28-day grid inside the Binance life, changes bisected to the day.
Crypto category only (equity/commodity swaps excluded). Probe cache in ./venue (copied from the earlier rounds).
"""
import sys, logging
sys.path.insert(0, "/home/user/Hermes/src")
import pandas as pd, numpy as np
from hermes.data.venue import OkxListing, activity_windows
from hermes.data.universe import STABLE_OR_INDEX, TRADFI, base_asset

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
W = "/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/longtail"


def excluded(sym):
    if not sym.isascii() or not sym.endswith("USDT"):
        return True
    b = sym[:-4]
    return b in STABLE_OR_INDEX or b in TRADFI or base_asset(sym) in TRADFI


daily = pd.read_parquet("/home/user/data/daily_volume_24cdb49d3f.parquet")
daily = daily[[c for c in daily.columns if not excluded(c)]]
daily = daily.loc["2021-06-01":"2026-09-21"]
print("daily", daily.shape)
win = activity_windows(daily)
lst = OkxListing(W).calendar(win)  # cache_dir W -> W/venue
lst.to_parquet(f"{W}/data/okx_listed_all.parquet")
print("listed calendar", lst.shape, "symbols ever listed on OKX:", int(lst.any().sum()))
