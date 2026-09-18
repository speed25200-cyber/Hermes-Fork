"""Chandelles pour la vue graphique de l'interface conservée, depuis les données ARCHIVÉES seulement.

L'API n'appelle jamais OKX : elle relit les partitions brutes ``candle.1m`` enregistrées par le
collecteur (``raw_partitions`` → fichiers parquet sous ``storage.data_root``) et agrège en 5m, 15m,
1H, 4H, 1D. Sans partition, la réponse est ``NON_DISPONIBLE`` — jamais une bougie inventée.

Schéma attendu des fichiers (colonnes du payload ``candle.1m`` de ``docs/event_schemas.md``) :
``inst_id, ts_ms, open, high, low, close, vol_contracts, confirm`` ; les nombres peuvent être des
chaînes (Decimal-safe) ou des nombres. Seules les bougies ``confirm == "1"`` sont retenues.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from okxq.persistence.models import RawPartition

BAR_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}
CANDLE_DATASETS: tuple[str, ...] = ("candle.1m", "candles_1m", "candle_1m")
DEFAULT_LIMIT = 300


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return float(d)


def _read_parquet_rows(path: Path, inst_id: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    rows: list[dict[str, Any]] = table.to_pylist()
    return [r for r in rows if str(r.get("inst_id", inst_id)) == inst_id]


def aggregate(rows_1m: list[list[float]], bar: str) -> list[list[float]]:
    """Agrège des bougies 1 minute ``[ts, o, h, l, c, vol]`` triées en bougies ``bar``."""
    width = BAR_MS[bar]
    out: list[list[float]] = []
    for ts, o, h, lo, c, v in rows_1m:
        bucket = ts - (ts % width)
        if out and out[-1][0] == bucket:
            cur = out[-1]
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], lo)
            cur[4] = c
            cur[5] += v
        else:
            out.append([bucket, o, h, lo, c, v])
    return out


def load_candles(
    session_factory: sessionmaker[Session],
    *,
    data_root: Path,
    inst_id: str,
    bar: str,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    if bar not in BAR_MS:
        return {"ok": False, "error": "BAR_INVALIDE", "instId": inst_id, "bar": bar, "rows": []}
    with session_factory() as s:
        partitions = list(
            s.execute(
                select(RawPartition)
                .where(RawPartition.dataset.in_(list(CANDLE_DATASETS)), RawPartition.inst_id == inst_id)
                .order_by(RawPartition.first_ts.asc(), RawPartition.id.asc())
            ).scalars()
        )
    if not partitions:
        return {
            "ok": False,
            "error": "NON_DISPONIBLE",
            "message": "aucune bougie archivée pour cet instrument (l'API n'interroge jamais l'exchange)",
            "instId": inst_id,
            "bar": bar,
            "rows": [],
        }
    raw: dict[int, list[float]] = {}
    missing: list[str] = []
    for part in partitions:
        path = Path(part.path)
        if not path.is_absolute():
            path = data_root / path
        if not path.exists():
            missing.append(str(path))
            continue
        try:
            rows = _read_parquet_rows(path, inst_id)
        except Exception as exc:
            missing.append(f"{path} ({type(exc).__name__})")
            continue
        for r in rows:
            if str(r.get("confirm", "1")) != "1":
                continue
            ts = _num(r.get("ts_ms"))
            o, h, lo, c = (_num(r.get(k)) for k in ("open", "high", "low", "close"))
            v = _num(r.get("vol_contracts")) or 0.0
            if ts is None or None in (o, h, lo, c):
                continue
            assert o is not None and h is not None and lo is not None and c is not None
            raw[int(ts)] = [float(int(ts)), o, h, lo, c, v]
    if not raw:
        return {
            "ok": False,
            "error": "NON_DISPONIBLE",
            "message": "partitions déclarées mais illisibles : " + "; ".join(missing[:3]),
            "instId": inst_id,
            "bar": bar,
            "rows": [],
        }
    ordered = [raw[k] for k in sorted(raw)]
    rows_out = aggregate(ordered, bar)[-limit:]
    return {
        "ok": True,
        "instId": inst_id,
        "bar": bar,
        "rows": rows_out,
        "last": rows_out[-1][4] if rows_out else None,
        "source": "archive:raw_partitions",
        "partitions": len(partitions),
        "missing": missing,
    }
