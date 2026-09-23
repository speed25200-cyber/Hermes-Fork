"""Point-in-time listing of the execution venue (OKX), so research ranks the universe the live engine trades.

The live engine can only trade contracts that OKX lists (``OKXBroker.register`` drops the others) and takes
the top-N of *those*. A research universe drawn from all Binance contracts would therefore validate a book
that cannot be executed: an audit of the best candidate found that names OKX did not list at the time held
10 % of gross exposure but earned a third of the price P&L. This module reconstructs, day by day, whether
``okx_inst_id(symbol)`` -- the broker's own mapping -- was a crypto USDT swap on OKX.

Source: OKX's per-instrument daily trade archives (``static.okx.com``, from late 2021), which also cover
instruments delisted since; a file exists for day D if the swap traded on D. The current instrument list
(public REST, ``instCategory == "1"``: crypto, not the equity or commodity swaps that share some tickers)
tells which contracts are live today. Days are probed on a grid within each contract's Binance life (monthly
for contracts live today, weekly otherwise), then each status change is located to the day by bisection.
Probes are cached;
a probe that neither exists nor is missing (network failure) raises -- a guess would bias the universe.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import pandas as pd

from hermes.execution.okx.instruments import okx_inst_id

log = logging.getLogger(__name__)

ARCHIVE = "https://static.okx.com/cdn/okex/traderecords/trades/daily/{ymd}/{inst}-trades-{day}.zip"
INSTRUMENTS = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
ARCHIVE_START = date(2021, 12, 1)  # first day of OKX's daily trade archives (earlier days: status unknown)
GRID_DAYS = 7
# Snapshot of probes and of the instrument list (committed): research is reproducible and does not depend on
# reaching OKX (its site may refuse some regions); the network only completes what the snapshot lacks.
SEED = Path(__file__).parent / "okx_seed"


class OkxListing:
    def __init__(
        self, cache_dir: str | Path, client: httpx.Client | None = None, workers: int = 48, seed: Path | None = SEED
    ):
        self.dir = Path(cache_dir) / "venue"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0), follow_redirects=True)
        self.workers = workers
        self._probes_file = self.dir / "okx_probes.json"
        self.probes: dict[str, bool] = {}
        self.seed = seed
        if seed is not None and (seed / "probes.json.gz").exists():
            with gzip.open(seed / "probes.json.gz", "rt") as fh:
                self.probes.update(json.load(fh))
        if self._probes_file.exists():
            self.probes.update(json.loads(self._probes_file.read_text()))
        self._catalog: dict[str, tuple[str, date]] | None = None

    # -- sources -------------------------------------------------------------------------------------------
    def catalog(self) -> dict[str, tuple[str, date]]:
        """Live USDT swaps: instId -> (instCategory, first full listed day). Snapshot cached per day."""
        if self._catalog is not None:
            return self._catalog
        snap = self.dir / f"okx_instruments_{date.today().isoformat()}.json"
        if snap.exists():
            data = json.loads(snap.read_text())
        else:
            try:
                r = self.client.get(INSTRUMENTS)
                r.raise_for_status()
                data = r.json().get("data", [])
                if not data:
                    raise ValueError("empty instrument list")
                snap.write_text(json.dumps(data))
            except (httpx.HTTPError, ValueError) as exc:
                older = sorted(self.dir.glob("okx_instruments_*.json"))
                if not older and self.seed is None:
                    raise
                src = older[-1] if older else self.seed / "instruments.json"  # type: ignore[operator]
                log.warning("OKX instrument list unavailable (%s): using %s", exc, src.name)
                raw = json.loads(src.read_text())
                data = raw["data"] if isinstance(raw, dict) else raw
        out = {}
        for d in data:
            inst = str(d.get("instId", ""))
            if not inst.endswith("-USDT-SWAP") or d.get("state", "live") != "live":
                continue
            listed = pd.Timestamp(int(d.get("listTime") or 0), unit="ms", tz="UTC")
            out[inst] = (str(d.get("instCategory", "1")), (listed + pd.Timedelta(days=1)).date())
        self._catalog = out
        return out

    def _probe_one(self, inst: str, day: date) -> tuple[str, bool]:
        key = f"{inst}|{day.isoformat()}"
        url = ARCHIVE.format(ymd=day.strftime("%Y%m%d"), inst=inst, day=day.isoformat())
        last: object = None
        for _ in range(5):
            try:
                r = self.client.head(url)
                if r.status_code in (200, 404):
                    return key, r.status_code == 200
                last = r.status_code
            except httpx.HTTPError as exc:  # pragma: no cover - network dependent
                last = exc
        raise RuntimeError(f"OKX archive probe failed for {inst} on {day}: {last}")

    def _probe(self, wanted: Iterable[tuple[str, date]]) -> None:
        todo = sorted({(i, d) for i, d in wanted if f"{i}|{d.isoformat()}" not in self.probes})
        if not todo:
            return
        log.info("OKX listing: %d archive probes", len(todo))
        with ThreadPoolExecutor(self.workers) as ex:
            for n in range(0, len(todo), 5000):  # saved as it goes: an interrupted run resumes
                for key, ok in ex.map(lambda a: self._probe_one(*a), todo[n : n + 5000]):
                    self.probes[key] = ok
                self._probes_file.write_text(json.dumps(self.probes))
                log.info("OKX listing: %d/%d probes", min(n + 5000, len(todo)), len(todo))

    def _listed(self, inst: str, day: date) -> bool:
        return self.probes[f"{inst}|{day.isoformat()}"]

    # -- calendar ------------------------------------------------------------------------------------------
    def archive_end(self) -> date:
        """Latest day whose archives are published (they lag a day or two): later days are not probed."""
        today = date.today()
        try:
            for k in range(1, 15):
                day = today - timedelta(days=k)
                self._probe([("BTC-USDT-SWAP", day)])
                if self._listed("BTC-USDT-SWAP", day):
                    return day
        except RuntimeError as exc:  # archives unreachable: the snapshot's last published day
            log.warning("OKX archives unreachable (%s): using the probe snapshot", exc)
        btc = "BTC-USDT-SWAP|"
        known = [date.fromisoformat(k[len(btc) :]) for k, ok in self.probes.items() if ok and k.startswith(btc)]
        if not known:
            raise RuntimeError("no OKX trade archive known")
        return max(known)

    def calendar(self, windows: dict[str, tuple[date, date]]) -> pd.DataFrame:
        """(day x symbol) booleans: was ``okx_inst_id(symbol)`` a live crypto USDT swap on OKX that day?

        ``windows`` gives, per Binance symbol, the days that matter (its Binance life); outside them the
        value is False. Every contract is probed, including those listed today: a ``listTime`` survives a
        delisting and relisting (ZEC was off OKX from early 2024 to November 2025). Contracts listed today
        are probed monthly (a gap shorter than a month may be missed), the others weekly. Days before
        ``ARCHIVE_START`` take the first known status; days after the last published archive, today's."""
        cat = self.catalog()
        if not windows:
            return pd.DataFrame(dtype=bool)
        last = self.archive_end()
        lo = min(a for a, _ in windows.values())
        hi = max(b for _, b in windows.values())
        days = pd.date_range(lo, hi, freq="1D", tz="UTC")
        plans: dict[str, tuple[str, bool]] = {}
        grid: dict[str, set[date]] = {}
        for sym, (a, b) in windows.items():
            inst = okx_inst_id(sym)
            entry = cat.get(inst)
            if entry is not None and entry[0] != "1":
                continue  # an equity/commodity swap with the same ticker: never the crypto contract
            plans[sym] = (inst, entry is not None)
            start, end = max(a, ARCHIVE_START), min(b, last)
            if start <= end:
                # A calendar-anchored grid: the same days whatever the window, so probes are shared across
                # configurations and with the committed snapshot.
                step = 28 if entry is not None else GRID_DAYS
                k0, k1 = -(-(start - ARCHIVE_START).days // step), (end - ARCHIVE_START).days // step
                pts = [ARCHIVE_START + timedelta(days=step * k) for k in range(k0, k1 + 1)]
                grid.setdefault(inst, set()).update(pts)
        self._probe((inst, d) for inst, pts in grid.items() for d in pts)
        # Bisection between grid points whose status differs: each change located to the day.
        pending = [
            (inst, x, y)
            for inst, pts in grid.items()
            for x, y in pairwise(sorted(pts))
            if (y - x).days > 1 and self._listed(inst, x) != self._listed(inst, y)
        ]
        while pending:
            self._probe((inst, _mid(x, y)) for inst, x, y in pending)
            nxt = []
            for inst, x, y in pending:
                m = _mid(x, y)
                if self._listed(inst, x) != self._listed(inst, m) and (m - x).days > 1:
                    nxt.append((inst, x, m))
                if self._listed(inst, m) != self._listed(inst, y) and (y - m).days > 1:
                    nxt.append((inst, m, y))
            pending = nxt
        by_inst: dict[str, list[tuple[pd.Timestamp, bool]]] = {}
        for key, ok in self.probes.items():
            i, d = key.split("|")
            by_inst.setdefault(i, []).append((pd.Timestamp(d, tz="UTC"), ok))
        out = pd.DataFrame(False, index=days, columns=sorted(windows))
        after = days > pd.Timestamp(last, tz="UTC")
        for sym, (inst, listed_now) in plans.items():
            obs = [(d, ok) for d, ok in by_inst.get(inst, []) if days[0] <= d <= days[-1]]
            status = pd.Series(pd.NA, index=days, dtype="boolean")
            if obs:
                o = pd.Series(dict(obs)).sort_index()
                status.loc[o.index] = o.to_numpy()
            status[after] = listed_now
            status = status.ffill().bfill().fillna(listed_now).astype(bool)  # piecewise constant between probes
            a, b = windows[sym]
            inside = (days >= pd.Timestamp(a, tz="UTC")) & (days <= pd.Timestamp(b, tz="UTC"))
            out[sym] = status & inside
        log.info("OKX listing calendar: %d symbols, %d listed symbol-days", out.shape[1], int(out.to_numpy().sum()))
        return out


def _mid(x: date, y: date) -> date:
    return x + timedelta(days=(y - x).days // 2)


def activity_windows(daily_quote_volume: pd.DataFrame) -> dict[str, tuple[date, date]]:
    """First and last day each symbol traded on Binance (its possible universe life)."""
    out = {}
    for sym in daily_quote_volume.columns:
        s = daily_quote_volume[sym].dropna()
        s = s[s > 0]
        if len(s):
            out[sym] = (s.index[0].date(), s.index[-1].date())
    return out
