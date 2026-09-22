import io
import zipfile
from datetime import date

import httpx
import numpy as np
import pandas as pd

from hermes.data.binance_archive import BinanceArchive, _bar_floor_after, _read_csv_from_zip, split_periods

ROWS = [
    [1704067200000, 42000, 42100, 41900, 42050, 100, 1704070799999, 4.2e6, 1000, 60, 2.52e6, 0],
    [1704070800000, 42050, 42200, 42000, 42150, 120, 1704074399999, 5.0e6, 1100, 50, 2.1e6, 0],
]
HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"
)


def _zip(text: str, name: str = "x.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def _csv(header: bool) -> str:
    lines = [",".join(map(str, r)) for r in ROWS]
    return "\n".join(([HEADER] if header else []) + lines) + "\n"


def test_csv_with_and_without_header():
    from hermes.data.binance_archive import KLINE_COLUMNS

    a = _read_csv_from_zip(_zip(_csv(True)), KLINE_COLUMNS)
    b = _read_csv_from_zip(_zip(_csv(False)), KLINE_COLUMNS)
    assert list(a.columns) == KLINE_COLUMNS and list(b.columns) == KLINE_COLUMNS
    assert np.allclose(a[["open", "close", "taker_buy_quote_volume"]], b[["open", "close", "taker_buy_quote_volume"]])


def test_funding_instant_maps_to_bar_that_contains_it():
    # Settlement at 08:00:00.005 belongs to the bar 07:00-08:00 (a position held during that bar pays).
    ts = pd.to_datetime([1704096000005], unit="ms", utc=True)
    assert _bar_floor_after(pd.DatetimeIndex(ts), "1h")[0] == pd.Timestamp("2024-01-01 07:00", tz="UTC")
    ts2 = pd.DatetimeIndex([pd.Timestamp("2024-01-01 08:00", tz="UTC")])
    assert _bar_floor_after(ts2, "4h")[0] == pd.Timestamp("2024-01-01 04:00", tz="UTC")


def test_split_periods():
    p = split_periods(date(2024, 1, 1), date(2024, 3, 20), today=date(2024, 3, 21))
    assert [m.month for m in p.months] == [1, 2]
    assert p.days[0] == date(2024, 3, 1) and p.days[-1] == date(2024, 3, 20)


def test_klines_via_mock_transport(tmp_path):
    listing = (
        '<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<IsTruncated>false</IsTruncated>"
        "<Contents><Key>data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip</Key></Contents>"
        "</ListBucketResult>"
    )
    body = _zip(_csv(True))

    def handler(request: httpx.Request) -> httpx.Response:
        if "prefix" in request.url.params:
            return httpx.Response(200, text=listing)
        if request.url.path.endswith("BTCUSDT-1h-2024-01.zip"):
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    arch = BinanceArchive(tmp_path, workers=2, client=httpx.Client(transport=httpx.MockTransport(handler)))
    k = arch.klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 1, 31))
    assert len(k) == 2
    assert k.index[0] == pd.Timestamp("2024-01-01 00:00", tz="UTC")
    assert np.isclose(k["taker_buy_quote"].iloc[0], 2.52e6)
    # Second call is served from the on-disk cache.
    arch2 = BinanceArchive(
        tmp_path,
        workers=2,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text=listing) if "prefix" in r.url.params else httpx.Response(500)
            )
        ),
    )
    assert len(arch2.klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 1, 31))) == 2
