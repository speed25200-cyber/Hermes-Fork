"""Read-only monitoring dashboard (standard library only, separate process from the engine).

It reads the engine's published state (``status.json`` and the SQLite store) and serves one page plus
three JSON endpoints. It can never place or cancel an order. When exposed beyond localhost it requires a
token (``HERMES_DASHBOARD_TOKEN``), sent as ``Authorization: Bearer <token>`` or ``?token=`` once (then a
cookie); the token is compared in constant time and never logged.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAGE = (Path(__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
# Fixed, read-only queries (no parameter ever reaches SQL).
QUERIES = {
    "/api/equity": "SELECT ts, equity, gross, net, drawdown, ic_est, n_positions FROM equity ORDER BY ts",
    "/api/events": "SELECT ts, level, message FROM events ORDER BY id DESC LIMIT 40",
    "/api/ic": "SELECT ts, value FROM series WHERE name = 'ic' ORDER BY ts",
    "/api/fills": "SELECT ts, symbol, side, qty, price, fee, maker FROM fills ORDER BY id DESC LIMIT 60",
    "/api/diag": "SELECT name, ts, value FROM series WHERE name IN "
    "('ic_raw', 'ic_lag', 'btc_dd90', 'mkt_ret30', 'xs_ac1') ORDER BY ts",
}


def make_handler(state_dir: Path, token: str | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # no access log (URLs may carry the token)
            return

        def _authorized(self) -> tuple[bool, bool]:
            if not token:
                return True, False
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token):
                return True, False
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "hermes_token" and hmac.compare_digest(v, token):
                    return True, False
            q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if q and hmac.compare_digest(q, token):
                return True, True
            return False, False

        def _send(self, code: int, body: bytes, ctype: str, set_cookie: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"
            )
            if set_cookie and token:
                self.send_header("Set-Cookie", f"hermes_token={token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            ok, set_cookie = self._authorized()
            if not ok:
                self._send(401, b"unauthorized", "text/plain")
                return
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8", set_cookie)
            elif path == "/api/status":
                p = state_dir / "status.json"
                self._send(200, p.read_bytes() if p.exists() else b"{}", "application/json")
            elif path in QUERIES:
                db = state_dir / "hermes.sqlite3"
                rows: list[object] = []
                if db.exists():
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                    try:
                        rows = [list(r) for r in con.execute(QUERIES[path])]
                    except sqlite3.OperationalError:  # a table the engine has not created yet
                        rows = []
                    finally:
                        con.close()
                self._send(200, json.dumps(rows).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def serve(state_dir: str | Path, host: str = "127.0.0.1", port: int = 8899) -> None:
    token = os.environ.get("HERMES_DASHBOARD_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit("refus : exposer le tableau de bord hors de localhost exige HERMES_DASHBOARD_TOKEN")
    httpd = ThreadingHTTPServer((host, port), make_handler(Path(state_dir), token))
    httpd.serve_forever()
