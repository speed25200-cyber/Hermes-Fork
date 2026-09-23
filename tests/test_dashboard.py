import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hermes.live.dashboard import make_handler
from hermes.live.state import StateStore


@pytest.fixture
def server(tmp_path):
    store = StateStore(tmp_path)
    store.write_status({"mode": "paper", "equity": 1000.0, "positions": {"BTCUSDT": 50.0}})
    store.event("INFO", "hello")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path, "s3cret"))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_dashboard_requires_token(server):
    assert _get(server + "/api/status")[0] == 401
    code, body, _ = _get(server + "/api/status", {"Authorization": "Bearer s3cret"})
    assert code == 200 and json.loads(body)["equity"] == 1000.0
    code, _, headers = _get(server + "/?token=s3cret")
    assert code == 200 and "hermes_token" in headers.get("Set-Cookie", "")
    code, body, _ = _get(server + "/api/events", {"Cookie": "hermes_token=s3cret"})
    assert code == 200 and json.loads(body)[0][2] == "hello"
