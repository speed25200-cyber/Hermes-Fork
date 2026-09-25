"""New-listing short sleeve: a second, weakly correlated return stream run next to the book.

Rule (docs/RESULTS.md, § 18; research in research/leverage_2026-09/newlisting): short every newly listed Binance
USDT-M perpetual whose token is new -- no Binance spot market, or one opened at most ``new_token_days`` before the
perpetual -- and that OKX lists, in tranches entered at each of ``entries`` hours after the perpetual's launch and all
closed at ``exit_hours``, hedged with a BTC long of ``hedge_beta`` times the notional, with a stop ``stop`` above the
first entry. New tokens drift down in their first week (airdrop and unlock selling): the day-1/3 -> day-7 short earned
+7 to +16 % per event vs BTC in each of 2023-2026 (t 2-4). The equal-weight plateau of in-sample configurations (entry
day 1 or 3, exit day 7, BTC hedge) made an out-of-sample Sharpe of 2.1 (2025-01 -> 2026-08, OKX prices; leave-one-
month-out 1.5, bootstrap 90 % [0.7, 3.6]); rebuilt with the live rules (entry windows, one 50 % stop per contract): 2.09
and 1.47. Fifteen of those out-of-sample listings (2025-08 -> 2026-03) were pre-market perpetuals shorted before the
token existed, a mechanism absent from the selection years; without them the out-of-sample Sharpe is about 1.57.
The honest forward expectation is about 1: Binance now lists two or three new crypto perpetuals a month (the rest of
its launches are stocks, FX and pre-IPO contracts), and the squeeze tail grew. OKX's own listings were tested as
extra events and not adopted (RESULTS § 19). Paper only: a forward test with a kill rule set before any live trade
(``kill_trades``, ``kill_mean``, ``kill_loss``), judged on P&L net of research-level costs and funding.

Sizing as in research: a listing gets NAV x leverage / slots x clip(sigma_ref / sigma, 0.25, 1), split equally
across its tranches, where sigma is the coin's realised daily volatility since listing (scale 0.5 without enough
bars); the sleeve's short notional never exceeds leverage x NAV. A tranche enters only within ``entry_grace_hours`` of
its hour (a missed window is not caught up later). Quantities stay fixed until the exit. The sleeve
tracks its own trades; the book is decided on the positions net of them, and the two target lists are added before
execution. One stop per contract (the brokers place one per position), at ``stop`` above the first tranche's entry.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from hermes.config import ListingSleeveConfig
from hermes.live.state import StateStore

log = logging.getLogger(__name__)

HEDGE = "BTCUSDT"
STATE_KEY = "listing_sleeve"


@dataclass
class ListingTrade:
    symbol: str
    tranche: int
    listed: str  # perpetual launch time (UTC ISO)
    entry: str
    exit_due: str
    qty: float  # signed coin quantity (negative: short)
    entry_px: float
    hedge_qty: float  # BTC quantity (positive: long)
    hedge_px: float
    stop_px: float | None
    funding: float = 0.0  # funding received (+) or paid (-) on both legs, USDT
    costs: float = 0.0  # research-level trading costs charged so far, USDT


class ListingSleeve:
    def __init__(self, cfg: ListingSleeveConfig, store: StateStore):
        self.cfg = cfg
        self.store = store
        st = store.get(STATE_KEY) or {}
        st = st if isinstance(st, dict) else {}
        self.open: list[ListingTrade] = []
        for t in st.get("open", []) or []:
            if isinstance(t, dict):
                self.open.append(ListingTrade(**{"tranche": 0, **t}))
        self.done: list[dict[str, object]] = list(st.get("done", []) or [])
        self.listings: dict[str, dict[str, object]] = dict(st.get("listings", {}) or {})  # symbol -> launch, newtok
        self.coverage: dict[str, bool] = dict(st.get("coverage", {}) or {})  # new token -> OKX-listed when due
        self.calendar_at: str | None = st.get("calendar_at")  # type: ignore[assignment]
        # Last successful refresh (calendar_at also moves on a failure, to space out the retries).
        self.refreshed_at: str | None = st.get("refreshed_at", self.calendar_at)  # type: ignore[assignment]
        self._nav = float(st.get("nav", 0.0) or 0.0)  # type: ignore[arg-type]

    # -- calendar ---------------------------------------------------------------------------------------------
    async def refresh(self, feed: object, now: pd.Timestamp) -> None:
        """New perpetuals launched within the holding window (Binance exchangeInfo), with the age of their token's
        oldest Binance spot market; at most every six hours, cached in the state store."""
        if self.calendar_at is not None and now - pd.Timestamp(self.calendar_at) < pd.Timedelta(hours=6):
            return
        launches: dict[str, int] = await feed.perp_listings()  # type: ignore[attr-defined]
        horizon = now - pd.Timedelta(hours=self.cfg.exit_hours)
        for sym, ms in launches.items():
            t0 = pd.Timestamp(ms, unit="ms", tz="UTC")
            if t0 < horizon or sym in self.listings or sym == HEDGE:
                continue
            first = getattr(feed, "first_trade", None)  # first 1-minute candle, as research; else onboardDate
            t_first = await first(sym) if first is not None else None
            t0 = t_first if t_first is not None else t0
            spot = await feed.spot_first_open(sym)  # type: ignore[attr-defined]
            newtok = spot is None or (t0 - spot) <= pd.Timedelta(days=self.cfg.new_token_days)
            self.listings[sym] = {"launch": t0.isoformat(), "new_token": bool(newtok)}
        # Listings whose window has passed are no longer needed.
        self.listings = {s: v for s, v in self.listings.items() if pd.Timestamp(str(v["launch"])) >= horizon}
        self.calendar_at = self.refreshed_at = now.isoformat()
        self._save()

    def backoff(self, now: pd.Timestamp) -> None:
        """After a failed calendar refresh: retry in about an hour rather than on every bar."""
        self.calendar_at = (now - pd.Timedelta(hours=5)).isoformat()

    def set_nav(self, nav: float) -> None:
        self._nav = float(nav)

    def _taken(self) -> set[tuple[str, int]]:
        return {(t.symbol, t.tranche) for t in self.open} | {
            (str(t.get("symbol")), int(t.get("tranche", 0)))
            for t in self.done  # type: ignore[call-overload]
        }

    def due(self, now: pd.Timestamp) -> list[tuple[str, int]]:
        """(listing, tranche) pairs of new tokens inside their entry window and not yet traded."""
        taken = self._taken()
        end = pd.Timedelta(hours=self.cfg.exit_hours)
        grace = pd.Timedelta(hours=self.cfg.entry_grace_hours)
        out = []
        for sym, v in self.listings.items():
            if not v.get("new_token"):
                continue
            t0 = pd.Timestamp(str(v["launch"]))
            for k, h in enumerate(self.cfg.entries):
                start = t0 + pd.Timedelta(hours=h)
                if (sym, k) not in taken and start <= now < min(start + grace, t0 + end):
                    out.append((sym, k))
        return sorted(out)

    def symbols_needed(self, now: pd.Timestamp) -> list[str]:
        """Contracts whose prices the next decision needs (open trades, entries due, the hedge)."""
        syms = {t.symbol for t in self.open} | {s for s, _ in self.due(now)}
        return sorted(syms | {HEDGE}) if syms else []

    # -- kill rule --------------------------------------------------------------------------------------------
    def suspended(self) -> bool:
        """Fixed before any live trade: no new entry once the last ``kill_trades`` closed trades have a mean net return
        <= ``kill_mean`` or lose more than ``kill_loss`` of the sleeve's capital between them."""
        n = self.cfg.kill_trades
        closed = [t for t in self.done if t.get("reason") != "gone" and float(t.get("notional", 0) or 0) > 0]  # type: ignore[arg-type]
        if n <= 0 or len(closed) < n:
            return False
        last = closed[-n:]
        mean = float(np.mean([float(t["pnl"]) / float(t["notional"]) for t in last]))  # type: ignore[arg-type]
        navs = [float(t.get("nav", 0.0) or 0.0) for t in last]  # type: ignore[arg-type]
        nav = next((v for v in reversed(navs) if v > 0), 0.0)
        if nav <= 0:
            return mean <= self.cfg.kill_mean
        loss = -sum(float(t["pnl"]) for t in last) / (nav * self.cfg.leverage)  # type: ignore[arg-type]
        return mean <= self.cfg.kill_mean or loss > self.cfg.kill_loss

    # -- positions --------------------------------------------------------------------------------------------
    def holdings(self, prices: dict[str, float]) -> dict[str, float]:
        """Notional the sleeve holds per contract at ``prices`` (the book is decided on positions net of it)."""
        out: dict[str, float] = {}
        for t in self.open:
            px = prices.get(t.symbol, t.entry_px)
            out[t.symbol] = out.get(t.symbol, 0.0) + t.qty * px
            if t.hedge_qty:
                hpx = prices.get(HEDGE, t.hedge_px)
                out[HEDGE] = out.get(HEDGE, 0.0) + t.hedge_qty * hpx
        return out

    def coins(self) -> set[str]:
        return {t.symbol for t in self.open}

    def accrue_funding(self, rates: dict[str, float], prices: dict[str, float]) -> None:
        """Funding settled since the last call (sum of rates per contract): a position pays qty x price x rate."""
        for t in self.open:
            r = rates.get(t.symbol, 0.0)
            if r and np.isfinite(r):
                t.funding -= t.qty * prices.get(t.symbol, t.entry_px) * r
            rh = rates.get(HEDGE, 0.0)
            if rh and np.isfinite(rh):
                t.funding -= t.hedge_qty * prices.get(HEDGE, t.hedge_px) * rh
        self._save()

    def close_all(self, prices: dict[str, float], now: pd.Timestamp, reason: str) -> None:
        """A halt or a kill closes every leg: the trades are booked (and counted by the kill rule) at ``prices``."""
        for t in list(self.open):
            self._close(t, prices.get(t.symbol, t.entry_px), prices, now, reason)
        self._save()

    def reconcile(self, held: dict[str, float], prices: dict[str, float], now: pd.Timestamp) -> None:
        """Forget trades whose short is no longer on the account (flattened by a halt, closed by hand, an order
        that never filled): the sleeve never re-targets a position the account does not hold."""
        for t in [t for t in self.open if not held.get(t.symbol, 0.0) < 0]:
            self._close(t, prices.get(t.symbol, t.entry_px), prices, now, "gone")
        self._save()

    def on_stops(self, fills: dict[str, float | None], prices: dict[str, float], now: pd.Timestamp) -> None:
        """Close every tranche of a contract whose stop fired (paper stop or exchange-side stop) at the fill."""
        for t in [t for t in self.open if t.symbol in fills]:
            px = fills[t.symbol] or t.stop_px or prices.get(t.symbol, t.entry_px)
            self._close(t, float(px), prices, now, "stop")
        self._save()

    def targets(
        self,
        now: pd.Timestamp,
        prices: dict[str, float],
        nav: float,
        tradable: set[str],
        vol_daily: dict[str, float],
        entries: bool = True,
    ) -> dict[str, float]:
        """Target notional per contract after the exits and (when ``entries``) the entries due at ``now``."""
        c = self.cfg
        for t in list(self.open):
            if now >= pd.Timestamp(t.exit_due):
                self._close(t, prices.get(t.symbol, t.entry_px), prices, now, "end")
        hpx = prices.get(HEDGE)
        due = self.due(now)
        for sym, _ in due:
            if sym not in self.coverage:
                self.coverage[sym] = sym in tradable  # OKX breadth: new tokens OKX lists when they fall due
        if entries and not self.suspended() and hpx and np.isfinite(hpx):
            for sym, k in due:
                px = prices.get(sym)
                open_syms = self.coins()
                if sym not in tradable or not px or not np.isfinite(px):
                    continue
                if sym not in open_syms and len(open_syms) >= c.slots:
                    continue
                sig = vol_daily.get(sym)
                scale = float(np.clip(c.sigma_ref / sig, 0.25, 1.0)) if sig and np.isfinite(sig) and sig > 0 else 0.5
                gross = sum(abs(t.qty) * prices.get(t.symbol, t.entry_px) for t in self.open)
                want = nav * c.leverage * scale / c.slots / len(c.entries)
                notional = min(want, max(nav * c.leverage - gross, 0.0))
                if notional <= 0:
                    continue
                t0 = pd.Timestamp(str(self.listings[sym]["launch"]))
                first = [t.stop_px for t in self.open if t.symbol == sym and t.stop_px]
                trade = ListingTrade(
                    symbol=sym,
                    tranche=k,
                    listed=t0.isoformat(),
                    entry=now.isoformat(),
                    exit_due=(t0 + pd.Timedelta(hours=c.exit_hours)).isoformat(),
                    qty=-notional / px,
                    entry_px=float(px),
                    hedge_qty=c.hedge_beta * notional / hpx,
                    hedge_px=float(hpx),
                    stop_px=first[0] if first else (float(px * (1.0 + c.stop)) if c.stop > 0 else None),
                    costs=notional * c.coin_cost + c.hedge_beta * notional * c.hedge_cost,
                )
                self.open.append(trade)
                log.info("listing sleeve: short %s tranche %d, %.2f USDT (scale %.2f)", sym, k, notional, scale)
        self._nav = nav
        self._save()
        return self.holdings(prices)

    def stop_fractions(self, prices: dict[str, float]) -> dict[str, float]:
        """Distance of each contract's stop from the current price (the brokers place stops as fractions)."""
        out: dict[str, float] = {}
        for t in self.open:
            px = prices.get(t.symbol)
            if t.symbol in out or not px or px <= 0:
                continue
            # No stop configured: a level no price reaches (the brokers otherwise apply their 15 % default).
            out[t.symbol] = max(t.stop_px / px - 1.0, 0.005) if t.stop_px else 100.0
        return out

    def summary(self, prices: dict[str, float]) -> dict[str, object]:
        pnl = sum(self._pnl(t, prices.get(t.symbol, t.entry_px), prices, exit_costs=False) for t in self.open)
        entries = " et ".join(f"+{h:.0f} h" for h in self.cfg.entries)
        return {
            "enabled": True,
            "rule": f"court nouveaux tokens (entrées {entries}, sortie +{self.cfg.exit_hours:.0f} h), "
            f"couverture BTC x{self.cfg.hedge_beta:g}, stop +{self.cfg.stop:.0%}, plafond {self.cfg.leverage:g}x",
            "suspended": self.suspended(),
            "entries": list(self.cfg.entries),
            "kill_trades": self.cfg.kill_trades,
            "kill_mean": self.cfg.kill_mean,
            "tokens_open": len(self.coins()),
            "open": [asdict(t) for t in self.open],
            "open_pnl": round(pnl, 2),
            "closed": self.done[-20:],
            "closed_pnl": round(sum(float(t.get("pnl", 0.0)) for t in self.done), 2),  # type: ignore[arg-type]
            "coverage": {"new_tokens_due": len(self.coverage), "on_okx": int(sum(self.coverage.values()))},
            "watch": {s: v for s, v in self.listings.items() if v.get("new_token")},
            "calendar": {"refreshed_at": self.refreshed_at, "listings": len(self.listings)},
        }

    # -- internals --------------------------------------------------------------------------------------------
    def _pnl(self, t: ListingTrade, px: float, prices: dict[str, float], exit_costs: bool = True) -> float:
        """Net P&L of a trade: both legs' price moves, funding, and research-level costs (entry, and exit)."""
        hpx = prices.get(HEDGE, t.hedge_px)
        gross = t.qty * (px - t.entry_px) + t.hedge_qty * (hpx - t.hedge_px)
        out = abs(t.qty) * px * self.cfg.coin_cost + abs(t.hedge_qty) * hpx * self.cfg.hedge_cost if exit_costs else 0.0
        return gross + t.funding - t.costs - out

    def _close(self, t: ListingTrade, px: float, prices: dict[str, float], now: pd.Timestamp, reason: str) -> None:
        pnl = self._pnl(t, px, prices)
        self.done.append(
            {
                "symbol": t.symbol,
                "tranche": t.tranche,
                "entry": t.entry,
                "exit": now.isoformat(),
                "entry_px": t.entry_px,
                "exit_px": px,
                "notional": round(abs(t.qty) * t.entry_px, 2),
                "pnl": round(pnl, 2),
                "funding": round(t.funding, 2),
                "nav": round(self._nav, 2),
                "reason": reason,
            }
        )
        self.done = self.done[-200:]
        self.open.remove(t)

    def _save(self) -> None:
        self.store.put(
            STATE_KEY,
            {
                "open": [asdict(t) for t in self.open],
                "done": self.done,
                "listings": self.listings,
                "coverage": dict(list(self.coverage.items())[-500:]),
                "calendar_at": self.calendar_at,
                "refreshed_at": self.refreshed_at,
                "nav": self._nav,
            },
        )
