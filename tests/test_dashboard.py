import json
import sqlite3
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hermes.execution.broker import Fill
from hermes.live.dashboard import make_handler, reconstruct_trades, resolve_root
from hermes.live.state import StateStore


def _fake_candles(symbol, tf, limit):
    if symbol == "NOPEUSDT":
        raise KeyError(symbol)
    return [[1_700_000_000 + 1800 * i, 100.0, 101.0, 99.0, 100.5, 1e6] for i in range(5)]


@pytest.fixture
def server(tmp_path):
    store = StateStore(tmp_path / "paper")
    store.write_status({"mode": "paper", "equity": 1000.0, "positions": {"BTCUSDT": 50.0}, "updated": "2026-01-01"})
    store.event("INFO", "hello <b>")
    store.add_fills([Fill("BTCUSDT", "buy", 0.001, 50_000.0, 0.01, True, ts=1.0, notional=50.0)])
    store.put_series("ic", {__import__("pandas").Timestamp("2026-01-01", tz="UTC"): 0.05})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path, "s3cret", _fake_candles))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get(url, headers=None, follow=True):
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


AUTH = {"Authorization": "Bearer s3cret"}


def test_dashboard_requires_token_and_moves_it_to_a_cookie(server):
    assert _get(server + "/api/snapshot?mode=paper")[0] == 401
    assert _get(server + "/static/app.js")[0] == 401
    code, _, headers = _get(server + "/?token=s3cret", follow=False)
    assert code == 303 and headers["Location"] == "/"  # the token leaves the address bar
    cookie = headers.get("Set-Cookie", "")
    assert "hermes_token=s3cret" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    code, body, headers = _get(server + "/", {"Cookie": "hermes_token=s3cret"})
    assert code == 200 and b"app.js" in body
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert headers["Referrer-Policy"] == "no-referrer" and headers["X-Frame-Options"] == "DENY"
    assert _get(server + "/?token=wrong")[0] == 401


def test_snapshot_modes_and_static(server):
    code, body, _ = _get(server + "/api/snapshot?mode=paper", AUTH)
    snap = json.loads(body)
    assert code == 200 and snap["status"]["equity"] == 1000.0
    assert snap["events"][0][2] == "hello <b>"  # served as data; the page escapes it
    assert snap["fills"][0]["kind"] == "trade" and snap["fills"][0]["notional"] == 50.0
    assert snap["series"]["ic"][0][1] == 0.05
    assert snap["trades"]["open"]["BTCUSDT"]["side"] == "long"
    modes = {m["mode"]: m for m in json.loads(_get(server + "/api/modes", AUTH)[1])}
    assert modes["paper"]["has_state"] and not modes["live"]["has_state"]
    live = json.loads(_get(server + "/api/snapshot?mode=live", AUTH)[1])
    assert live["status"] == {} and live["fills"] == []
    assert _get(server + "/api/snapshot?mode=../../etc", AUTH)[0] == 400
    for f in ("app.js", "app.css", "vendor/lightweight-charts.js", "fonts/inter-latin-wght-normal.woff2"):
        assert _get(server + "/static/" + f, AUTH)[0] == 200, f
    for f, ctype in (("brand/favicon.svg", "image/svg+xml"), ("brand/mark.svg", "image/svg+xml"),
                     ("brand/apple-touch-icon.png", "image/png")):  # fmt: skip
        code, body, headers = _get(server + "/static/" + f, AUTH)
        assert code == 200 and headers["Content-Type"] == ctype and body, f
    assert _get(server + "/static/../dashboard.py", AUTH)[0] == 404


def test_candles_and_symbol_fills(server):
    c = json.loads(_get(server + "/api/candles?symbol=BTCUSDT&tf=30m", AUTH)[1])
    assert len(c["candles"]) == 5
    bad = json.loads(_get(server + "/api/candles?symbol=NOPEUSDT&tf=30m", AUTH)[1])
    assert bad["candles"] == [] and bad["error"] == "KeyError"
    assert _get(server + "/api/candles?symbol=btc/../x&tf=30m", AUTH)[0] == 400
    assert _get(server + "/api/candles?symbol=BTCUSDT&tf=7m", AUTH)[0] == 400
    f = json.loads(_get(server + "/api/fills?mode=paper&symbol=BTCUSDT", AUTH)[1])
    assert len(f) == 1 and f[0]["px_model"] == 50_000.0
    assert json.loads(_get(server + "/api/fills?mode=paper&symbol=ETHUSDT", AUTH)[1]) == []


def _f(i, ts, side, notional, px, fee=0.0, kind="trade"):
    return {"id": i, "ts": ts, "symbol": "X", "side": side, "qty": notional / px, "price": px, "fee": fee,
            "maker": 1, "notional": notional, "kind": kind, "px_model": px}  # fmt: skip


def test_trades_are_rebuilt_from_executions():
    fills = [
        _f(1, 0, "buy", 100.0, 10.0, 0.1),  # open long 10 units
        _f(2, 10, "buy", 110.0, 11.0, 0.1),  # add 10 units: entry 10.5
        _f(3, 20, "sell", 120.0, 12.0, 0.1),  # close 10 of 20 (+15)
        _f(4, 30, "sell", 300.0, 12.0, 0.3),  # close 10 (+15) and flip short 15 units
        _f(5, 40, "buy", 135.0, 9.0, 0.2, "stop"),  # cover the short at 9 (+45)
    ]
    t = reconstruct_trades(fills)
    first, second = t["closed"][1], t["closed"][0]  # most recent first
    assert first["side"] == "long" and abs(first["entry"] - 10.5) < 1e-12 and abs(first["exit"] - 12.0) < 1e-12
    assert abs(first["pnl_gross"] - 30.0) < 1e-9
    assert abs(first["fees"] - (0.1 + 0.1 + 0.1 + 0.3 * 120 / 300)) < 1e-9  # the flip's fee is split
    assert second["side"] == "short" and second["exit_kind"] == "stop" and abs(second["pnl_gross"] - 45.0) < 1e-9
    assert abs(second["fees"] - (0.3 * 180 / 300 + 0.2)) < 1e-9
    assert t["open"] == {} and t["stats"]["n"] == 2 and t["stats"]["win_rate"] == 1.0 and t["stats"]["n_stops"] == 1
    # Units: px_model can differ from the venue price (1000PEPEUSDT): sizes and P&L use USDT and model prices.
    k = reconstruct_trades([_f(1, 0, "sell", 50.0, 0.01), _f(2, 5, "buy", 45.0, 0.009)])
    assert abs(k["closed"][0]["pnl_gross"] - 5.0) < 1e-9


def test_old_stores_gain_the_new_fill_columns(tmp_path):
    db = tmp_path / "hermes.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, side TEXT, qty REAL, "
        "price REAL, fee REAL, maker INTEGER)"
    )
    con.execute("INSERT INTO fills (ts, symbol, side, qty, price, fee, maker) VALUES (1, 'X', 'buy', 2, 3, 0, 1)")
    con.commit()
    con.close()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE equity (ts TEXT PRIMARY KEY, equity REAL, gross REAL, net REAL, drawdown REAL, "
                "ic_est REAL, n_positions INTEGER)")  # fmt: skip
    con.execute("INSERT INTO equity VALUES ('2026-01-01T00:00:00+00:00', 100, 0, 0, 0, 0, 0)")
    con.commit()
    con.close()
    store = StateStore(tmp_path)
    store.add_fills([Fill("X", "sell", -2, 4, 0.0, False, notional=8.0)], kind="stop")
    rows = sqlite3.connect(db).execute("SELECT notional, kind, px_model FROM fills ORDER BY id").fetchall()
    assert rows == [(None, None, None), (8.0, "stop", 4.0)]
    # The equity table gains the ex-ante risk of the book held (older rows: NULL, read as unknown).
    import pandas as pd

    store.add_equity(pd.Timestamp("2026-01-01 00:30", tz="UTC"), 101.0, 0.5, 0.0, 0.0, 0.03, 4, 0.18)
    assert store.equity_curve()["vol_ex_ante"].tolist()[-1] == 0.18
    from hermes.live.dashboard import snapshot

    eq = snapshot(tmp_path, "")["equity"]
    assert eq[0][7] is None and eq[1][7] == 0.18
    old = dict(zip(("id", "ts", "symbol", "side", "qty", "price", "fee", "maker"), (1, 1, "X", "buy", 2, 3, 0, 1)))
    t = reconstruct_trades([old, _f(2, 2, "sell", 8.0, 4.0)])
    assert abs(t["closed"][0]["pnl_gross"] - 2.0) < 1e-9  # the old row: notional = |qty x price|


def test_state_root_or_mode_directory(tmp_path):
    assert resolve_root(tmp_path / "paper") == (tmp_path, "paper")
    assert resolve_root(tmp_path) == (tmp_path, "paper")


def test_idle_connections_cannot_exhaust_the_server(tmp_path):
    import socket

    from hermes.live.dashboard import DashboardServer

    class Small(DashboardServer):
        max_connections = 3

    httpd = Small(("127.0.0.1", 0), make_handler(tmp_path, "s3cret", _fake_candles))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    idle = [socket.create_connection(httpd.server_address) for _ in range(6)]  # silent clients
    try:
        # Past the cap, extra connections are closed; the slots come back as soon as clients leave.
        for s in idle:
            s.close()
        import time

        time.sleep(0.3)
        assert _get(url + "/api/modes", AUTH)[0] == 200
    finally:
        httpd.shutdown()
    assert make_handler(tmp_path, None).timeout == 20


def test_auth_edge_cases_and_throttling(server, tmp_path):
    # Non-ASCII credentials are simply wrong, never a crash.
    assert _get(server + "/api/modes", {"Authorization": "Bearer caf\u00e9".encode().decode("latin-1")})[0] == 401
    assert _get(server + "/api/modes", {"Cookie": "hermes_token=\u00e9t\u00e9".encode().decode("latin-1")})[0] == 401
    # Repeated failures from one address are refused for a while.
    codes = [_get(server + "/api/modes", {"Authorization": "Bearer nope"})[0] for _ in range(25)]
    assert codes[0] == 401 and codes[-1] == 429
    assert _get(server + "/api/modes", AUTH)[0] == 429  # even the right token, until the window passes


def test_tokenless_mode_only_answers_to_localhost_names(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path, None, _fake_candles))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/modes"
    try:
        assert _get(url)[0] == 200
        assert _get(url, {"Host": "evil.example:8899"})[0] == 401  # DNS rebinding
    finally:
        httpd.shutdown()


def test_trade_history_follows_a_reset_store_and_serves_one_contract(server, tmp_path):
    t = json.loads(_get(server + "/api/trades?mode=paper&symbol=BTCUSDT", AUTH)[1])
    assert t["open"]["side"] == "long" and t["closed"] == []
    import shutil

    shutil.rmtree(tmp_path / "paper")
    store = StateStore(tmp_path / "paper")  # a fresh run: ids restart at 1
    store.add_fills([Fill("ETHUSDT", "sell", -0.1, 2000.0, 0.0, True, ts=5.0, notional=200.0)])
    snap = json.loads(_get(server + "/api/snapshot?mode=paper", AUTH)[1])
    assert set(snap["trades"]["open"]) == {"ETHUSDT"}  # the old run's BTC position is gone
