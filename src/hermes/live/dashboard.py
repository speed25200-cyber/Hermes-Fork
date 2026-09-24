"""Read-only monitoring dashboard (standard library only, separate process from the engine).

It reads what each engine mode publishes under the state root (``<root>/<mode>/status.json`` and the SQLite
store) and serves one page, its static assets and a few JSON endpoints. It can never place or cancel an
order. When exposed beyond localhost it requires a token (``HERMES_DASHBOARD_TOKEN``), sent as
``Authorization: Bearer <token>`` or once as ``?token=`` (then an HttpOnly cookie, and a redirect that removes
it from the address bar); the token is compared in constant time and never logged.

Endpoints (``mode`` is one of ``paper``, ``demo``, ``live``; request values reach SQL only as bound parameters):

* ``/api/modes``: which modes have published a state, and how fresh it is;
* ``/api/snapshot?mode=``: status, equity curve, realised-IC and regime series, latest decision, events,
  executions and the trade history reconstructed from them;
* ``/api/fills?mode=&symbol=``: one contract's executions;
* ``/api/trades?mode=&symbol=``: one contract's position episodes (chart markers);
* ``/api/candles?symbol=&tf=``: Binance USDT-M candles for the price chart (public data, cached).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

HERE = Path(__file__).parent
STATIC = HERE / "static"
MODES = ("paper", "demo", "live")
TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d")
SYMBOL = re.compile(r"^[A-Z0-9]{2,24}$")
SERIES = ("ic", "ic_raw", "ic_lag", "btc_dd90", "mkt_ret30", "xs_ac1", "shortfall_bps")
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
DUST_USDT = 0.01  # a residual smaller than this (or than 0.1 % of the episode's size) closes the episode
DUST_SHARE = 1e-3
FILL_KEYS = ("id", "ts", "symbol", "side", "qty", "price", "fee", "maker", "notional", "kind", "px_model")

CandleSource = Callable[[str, str, int], list[list[float]]]


# -- data ------------------------------------------------------------------------------------------------------
def _query(db: Path, sql: str) -> list[dict[str, object]]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    except sqlite3.OperationalError:  # a table the engine has not created yet
        return []
    finally:
        con.close()


class TradeBook:
    """Position episodes rebuilt from the executions, incrementally (only new fills are read): one episode per
    contract from the fill that opens it (from flat, or through a flip) to the one that brings it back to flat.
    Prices are in the model's units (``px_model``), sizes in USDT, P&L in USDT net of fees, funding excluded."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_id = 0
        self.book: dict[str, dict[str, float | str | int]] = {}
        self.closed: list[dict[str, object]] = []
        self.origin: tuple[object, ...] | None = None  # the store's first execution: identifies the run

    def reset(self) -> None:
        self.last_id, self.book, self.closed, self.origin = 0, {}, [], None

    def update(self, db: Path) -> None:
        with self.lock:
            if not db.exists():
                self.reset()
                return
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                # A store recreated since the last read (state reset) has another first execution, or fewer:
                # rebuild from scratch.
                first = con.execute("SELECT id, ts, symbol, side, qty FROM fills ORDER BY id LIMIT 1").fetchone()
                origin = tuple(first) if first else None
                if origin != self.origin:
                    self.reset()
                    self.origin = origin
                rows = [dict(r) for r in con.execute("SELECT * FROM fills WHERE id > ? ORDER BY id", (self.last_id,))]
            except sqlite3.OperationalError:
                rows = []
            finally:
                con.close()
            for r in rows:
                self.add(r)
                self.last_id = max(self.last_id, int(r["id"]))

    def add(self, f: dict[str, object]) -> None:
        sym = str(f["symbol"])
        px = float(f.get("px_model") or f.get("price") or 0.0)  # type: ignore[arg-type]
        if px <= 0:
            return
        notional = float(f.get("notional") or abs(float(f["qty"]) * float(f["price"])))  # type: ignore[arg-type]
        units = notional / px  # model units (e.g. 1000 coins for 1000PEPEUSDT): units x price = USDT
        dq = units if f["side"] == "buy" else -units
        fee = float(f.get("fee") or 0.0)  # type: ignore[arg-type]
        ts = float(f["ts"])  # type: ignore[arg-type]
        kind = str(f.get("kind") or "trade")
        pos = self.book.get(sym)
        if pos is None:
            self.book[sym] = self._open(sym, ts, dq, px, fee)
            return
        q = float(pos["qty"])
        pos["n_fills"] = int(pos["n_fills"]) + 1
        if (q > 0) == (dq > 0):  # adding to the position
            pos["entry"] = (abs(q) * float(pos["entry"]) + abs(dq) * px) / (abs(q) + abs(dq))
            pos["qty"] = q + dq
            pos["cost_in"] = float(pos["cost_in"]) + abs(dq) * px
            pos["max_notional"] = max(float(pos["max_notional"]), abs(q + dq) * px)
            pos["fees"] = float(pos["fees"]) + fee
            return
        close_q = min(abs(q), abs(dq))
        share = close_q / abs(dq)
        pos["realized"] = float(pos["realized"]) + close_q * (px - float(pos["entry"])) * (1 if q > 0 else -1)
        pos["exit_value"] = float(pos["exit_value"]) + close_q * px
        pos["exit_qty"] = float(pos["exit_qty"]) + close_q
        pos["fees"] = float(pos["fees"]) + fee * share
        rest = q + dq
        # Dust relative to the episode: a close split in legs (maker then taker) does not end it after one leg.
        dust = max(DUST_USDT, DUST_SHARE * float(pos["max_notional"]))
        flipped = (rest > 0) != (q > 0) and abs(rest) * px >= dust
        if not flipped and abs(rest) * px >= dust:
            pos["qty"] = rest
            return
        gross, fees = float(pos["realized"]), float(pos["fees"])
        self.closed.append(
            {
                "symbol": sym,
                "side": pos["side"],
                "opened": pos["opened"],
                "closed": ts,
                "entry": pos["entry"],
                "exit": float(pos["exit_value"]) / float(pos["exit_qty"]),
                "max_notional": round(float(pos["max_notional"]), 2),
                "pnl_gross": round(gross, 4),
                "fees": round(fees, 4),
                "pnl": round(gross - fees, 4),
                "ret": (gross - fees) / float(pos["cost_in"]) if float(pos["cost_in"]) else None,
                "n_fills": pos["n_fills"],
                "exit_kind": kind,
            }
        )
        del self.book[sym]
        if flipped:  # the remainder opens the opposite position
            self.book[sym] = self._open(sym, ts, rest, px, fee * (1 - share))

    @staticmethod
    def _open(sym: str, ts: float, qty: float, px: float, fee: float) -> dict[str, float | str | int]:
        return {
            "symbol": sym,
            "side": "long" if qty > 0 else "short",
            "opened": ts,
            "qty": qty,
            "entry": px,
            "cost_in": abs(qty) * px,
            "max_notional": abs(qty) * px,
            "exit_value": 0.0,
            "exit_qty": 0.0,
            "realized": 0.0,
            "fees": fee,
            "n_fills": 1,
        }

    def view(self, n_closed: int = 600) -> dict[str, object]:
        with self.lock:
            closed = list(self.closed)
            book = {k: dict(v) for k, v in self.book.items()}
        pnl = [float(t["pnl"]) for t in closed]  # type: ignore[arg-type]
        gain = sum(x for x in pnl if x > 0)
        loss = -sum(x for x in pnl if x <= 0)
        hold = [float(t["closed"]) - float(t["opened"]) for t in closed]  # type: ignore[arg-type]
        stats = {
            "n": len(closed),
            "win_rate": sum(1 for x in pnl if x > 0) / len(pnl) if pnl else None,
            "pnl": sum(pnl),
            "fees": sum(float(t["fees"]) for t in closed),  # type: ignore[arg-type]
            "profit_factor": gain / loss if loss > 0 else None,
            "avg_win": gain / max(1, sum(1 for x in pnl if x > 0)) if pnl else None,
            "avg_loss": -loss / max(1, sum(1 for x in pnl if x <= 0)) if pnl else None,
            "avg_hold_h": sum(hold) / len(hold) / 3600 if hold else None,
            "best": max(pnl, default=None),
            "worst": min(pnl, default=None),
            "n_long": sum(1 for t in closed if t["side"] == "long"),
            "n_short": sum(1 for t in closed if t["side"] == "short"),
            "pnl_long": sum(float(t["pnl"]) for t in closed if t["side"] == "long"),  # type: ignore[arg-type]
            "pnl_short": sum(float(t["pnl"]) for t in closed if t["side"] == "short"),  # type: ignore[arg-type]
            "n_stops": sum(1 for t in closed if t["exit_kind"] == "stop"),
        }
        opened = {
            s: {k: p[k] for k in ("side", "opened", "entry", "realized", "fees", "n_fills")} for s, p in book.items()
        }
        by: dict[str, dict[str, float]] = {}
        for t in closed:
            b = by.setdefault(str(t["symbol"]), {"pnl": 0.0, "n": 0, "wins": 0})
            b["pnl"] += float(t["pnl"])  # type: ignore[arg-type]
            b["n"] += 1
            b["wins"] += float(t["pnl"]) > 0  # type: ignore[arg-type]
        return {"closed": closed[::-1][:n_closed], "open": opened, "stats": stats, "by_symbol": by}

    def symbol_view(self, symbol: str) -> dict[str, object]:
        """Every episode of one contract (the chart's markers), most recent first."""
        with self.lock:
            closed = [t for t in self.closed if t["symbol"] == symbol][::-1]
            p = self.book.get(symbol)
            opened = {k: p[k] for k in ("side", "opened", "entry", "realized", "fees", "n_fills")} if p else None
        return {"closed": closed, "open": opened}


def reconstruct_trades(fills: list[dict[str, object]]) -> dict[str, object]:
    """Episodes of a list of executions (oldest first by id)."""
    tb = TradeBook()
    for f in sorted(fills, key=lambda r: int(r.get("id") or 0)):  # type: ignore[arg-type]
        tb.add(f)
    return tb.view(n_closed=10**9)


def symbol_fills(db: Path, symbol: str, limit: int = 3000) -> list[dict[str, object]]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM fills WHERE symbol = ? ORDER BY id DESC LIMIT ?", (symbol, limit))
        return [{k: dict(r).get(k) for k in FILL_KEYS} for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def mode_summary(root: Path, mode: str) -> dict[str, object]:
    p = root / mode / "status.json"
    out: dict[str, object] = {"mode": mode, "has_state": p.exists()}
    if p.exists():
        try:
            s = json.loads(p.read_text())
        except (OSError, ValueError):
            return out
        strat = s.get("strategy") or {}
        out.update(
            {
                "updated": s.get("updated"),
                "equity": s.get("equity"),
                "halted": s.get("halted"),
                "n_positions": len(s.get("positions") or {}),
                "bar": strat.get("bar"),
                "rebalance_every": strat.get("rebalance_every"),
            }
        )
    return out


def snapshot(root: Path, mode: str, trades: TradeBook | None = None) -> dict[str, object]:
    d = root / mode
    db = d / "hermes.sqlite3"
    status: dict[str, object] = {}
    if (d / "status.json").exists():
        try:
            status = json.loads((d / "status.json").read_text())
        except (OSError, ValueError):
            status = {}
    names = ", ".join(f"'{n}'" for n in SERIES)  # a fixed whitelist, not request data
    series: dict[str, list[list[object]]] = {}
    for r in _query(db, f"SELECT name, ts, value FROM series WHERE name IN ({names}) ORDER BY ts"):
        series.setdefault(str(r["name"]), []).append([r["ts"], r["value"]])
    decision = None
    rows = _query(db, "SELECT ts, payload FROM decisions ORDER BY ts DESC LIMIT 1")
    if rows:
        try:
            decision = json.loads(str(rows[0]["payload"]))
        except ValueError:
            decision = None
    fills = _query(db, "SELECT * FROM fills ORDER BY id DESC LIMIT 1500")
    if trades is None:
        trades = TradeBook()
    trades.update(db)
    equity = [
        [r["ts"], r["equity"], r["gross"], r["net"], r["drawdown"], r["ic_est"], r["n_positions"], r.get("vol_ex_ante")]
        for r in _query(db, "SELECT * FROM equity ORDER BY ts")
    ]
    events = [
        [r["ts"], r["level"], r["message"]] for r in _query(db, "SELECT * FROM events ORDER BY id DESC LIMIT 300")
    ]
    return {
        "mode": mode,
        "status": status,
        "equity": equity,
        "series": series,
        "decision": decision,
        "events": events,
        "fills": [{k: f.get(k) for k in FILL_KEYS} for f in fills],
        "trades": trades.view(),
        "server_time": time.time(),
    }


def binance_candles(symbol: str, tf: str, limit: int) -> list[list[float]]:
    """Closed and current USDT-M candles from Binance's public API: [open time (s), o, h, l, c, quote volume]."""
    url = f"https://fapi.binance.com/fapi/v1/klines?{urlencode({'symbol': symbol, 'interval': tf, 'limit': limit})}"
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:  # a fixed https host
        rows = json.loads(r.read())
    return [[int(x[0]) // 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7])] for x in rows]


class _CandleCache:
    def __init__(self, source: CandleSource, ttl: float = 15.0):
        self.source, self.ttl = source, ttl
        self.lock = threading.Lock()
        self.data: dict[tuple[str, str], tuple[float, list[list[float]]]] = {}

    def get(self, symbol: str, tf: str) -> list[list[float]]:
        key = (symbol, tf)
        with self.lock:
            hit = self.data.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        rows = self.source(symbol, tf, 499)  # < 500: Binance's lighter weight class
        with self.lock:
            if len(self.data) > 256:
                self.data.clear()
            self.data[key] = (time.time(), rows)
        return rows


# -- HTTP ------------------------------------------------------------------------------------------------------
def _static_files() -> dict[str, tuple[bytes, str]]:
    out = {}
    if STATIC.exists():
        for p in STATIC.rglob("*"):
            if p.is_file() and p.suffix in CONTENT_TYPES:
                out[p.relative_to(STATIC).as_posix()] = (p.read_bytes(), CONTENT_TYPES[p.suffix])
    return out


def make_handler(
    root: Path, token: str | None, candles: CandleSource | None = None, default_mode: str = "paper"
) -> type[BaseHTTPRequestHandler]:
    page = (HERE / "dashboard.html").read_text(encoding="utf-8")
    files = _static_files()
    cache = _CandleCache(candles or binance_candles)
    books = {m: TradeBook() for m in MODES}
    guard = _Throttle()

    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes"
        sys_version = ""
        timeout = 20  # seconds: an idle or slow client cannot hold a thread forever

        def log_message(self, fmt: str, *args: object) -> None:  # no access log (URLs may carry the token)
            return

        def _authorized(self) -> tuple[bool, bool]:
            if not token:
                # Tokenless: localhost only; the Host header must name it too (no DNS rebinding from a website).
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                return host in ("127.0.0.1", "localhost", "::1"), False
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and _same(auth[7:], token):
                return True, False
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "hermes_token" and _same(v, token):
                    return True, False
            q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if q and _same(q, token):
                return True, True
            return False, False

        def _headers(self, code: int, ctype: str, length: int, cache_s: int = 0) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", f"private, max-age={cache_s}" if cache_s else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)

        def _send(self, code: int, body: bytes, ctype: str, cache_s: int = 0) -> None:
            self._headers(code, ctype, len(body), cache_s)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: object, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str, allow_nan=False).encode(), "application/json")

        def _mode(self, q: dict[str, list[str]]) -> str | None:
            m = q.get("mode", [default_mode])[0]
            return m if m in MODES else None

        def do_GET(self) -> None:
            ip = self.client_address[0]
            if guard.blocked(ip):
                self._send(429, b"too many attempts", "text/plain")
                return
            ok, from_query = self._authorized()
            if not ok:
                guard.fail(ip)
                self._send(401, b"unauthorized", "text/plain")
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if from_query:
                # The token leaves the address bar (history, screenshots): cookie, then the same URL without it.
                rest = urlencode({k: v[0] for k, v in q.items() if k != "token"})
                self.send_response(303)
                self.send_header("Location", url.path + (f"?{rest}" if rest else ""))
                self.send_header(
                    # Lax: a link opened from another app (mail, chat) is a cross-site navigation, which Strict
                    # would strip of the cookie right after the redirect. The page only serves GET requests.
                    "Set-Cookie",
                    f"hermes_token={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = url.path.rstrip("/") or "/"
            try:
                if path == "/":
                    self._send(200, page.encode(), "text/html; charset=utf-8")
                elif path.startswith("/static/"):
                    hit = files.get(path[len("/static/") :])
                    if hit is None:
                        self._send(404, b"not found", "text/plain")
                    else:
                        self._send(200, hit[0], hit[1], cache_s=3600)
                elif path == "/api/modes":
                    self._json([mode_summary(root, m) for m in MODES])
                elif path == "/api/snapshot":
                    mode = self._mode(q)
                    if mode is None:
                        self._send(400, b"bad mode", "text/plain")
                    else:
                        self._json(_finite(snapshot(root, mode, books[mode])))
                elif path == "/api/fills":
                    mode = self._mode(q)
                    sym = q.get("symbol", [""])[0]
                    if mode is None or not SYMBOL.match(sym):
                        self._send(400, b"bad mode or symbol", "text/plain")
                    else:
                        self._json(_finite(symbol_fills(root / mode / "hermes.sqlite3", sym)))
                elif path == "/api/trades":
                    mode = self._mode(q)
                    sym = q.get("symbol", [""])[0]
                    if mode is None or not SYMBOL.match(sym):
                        self._send(400, b"bad mode or symbol", "text/plain")
                    else:
                        books[mode].update(root / mode / "hermes.sqlite3")
                        self._json(_finite(books[mode].symbol_view(sym)))
                elif path == "/api/candles":
                    sym = q.get("symbol", [""])[0]
                    tf = q.get("tf", ["30m"])[0]
                    if not SYMBOL.match(sym) or tf not in TIMEFRAMES:
                        self._send(400, b"bad symbol or timeframe", "text/plain")
                        return
                    try:
                        self._json({"symbol": sym, "tf": tf, "candles": cache.get(sym, tf)})
                    except Exception as exc:  # network, unknown symbol: the chart says so
                        self._json({"symbol": sym, "tf": tf, "candles": [], "error": type(exc).__name__})
                else:
                    self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def _same(a: str, b: str) -> bool:
    """Constant-time comparison that also accepts non-ASCII input (compared as UTF-8 bytes)."""
    return hmac.compare_digest(a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape"))


class _Throttle:
    """Failed authentications per client address: after ``limit`` in ``window`` seconds, refused for as long."""

    def __init__(self, limit: int = 20, window: float = 300.0):
        self.limit, self.window = limit, window
        self.lock = threading.Lock()
        self.fails: dict[str, list[float]] = {}

    def _recent(self, ip: str, now: float) -> list[float]:
        return [t for t in self.fails.get(ip, []) if now - t < self.window]

    def fail(self, ip: str) -> None:
        now = time.time()
        with self.lock:
            if len(self.fails) > 4096:
                self.fails.clear()
            self.fails[ip] = [*self._recent(ip, now), now]

    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            recent = self._recent(ip, now)
            if recent:
                self.fails[ip] = recent
            else:
                self.fails.pop(ip, None)
            return len(recent) >= self.limit


def _finite(obj: object) -> object:
    """NaN and infinities become null (strict JSON for the browser)."""
    if isinstance(obj, float):
        return obj if obj == obj and abs(obj) != float("inf") else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_finite(v) for v in obj]
    return obj


class DashboardServer(ThreadingHTTPServer):
    """Threaded server with a cap on simultaneous connections: beyond it, new ones are closed at once."""

    daemon_threads = True
    max_connections = 48

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):  # type: ignore[no-untyped-def]
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):  # type: ignore[no-untyped-def]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def resolve_root(state: str | Path) -> tuple[Path, str]:
    """``state`` may be the state root (``state``) or one mode's directory (``state/paper``, as older service
    files pass it): returns the root and the mode shown first."""
    p = Path(state)
    if p.name in MODES and not any((p / m).exists() for m in MODES):
        return p.parent, p.name
    return p, "paper"


def serve(state: str | Path, host: str = "127.0.0.1", port: int = 8899, candles: CandleSource | None = None) -> None:
    token = os.environ.get("HERMES_DASHBOARD_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit("refus : exposer le tableau de bord hors de localhost exige HERMES_DASHBOARD_TOKEN")
    if token and len(token) < 16:
        print(
            "attention : HERMES_DASHBOARD_TOKEN court (< 16 caractères) ; en choisir un long et aléatoire", flush=True
        )
    root, first = resolve_root(state)
    httpd = DashboardServer((host, port), make_handler(root, token, candles, first))
    httpd.serve_forever()
