"""``okxq data inspect --manifest <manifest.json>`` : couverture, trous, instruments, checksums d'un jeu de données
(golden ``events.jsonl`` ou archive Parquet)."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import typer

from okxq.cli_cmds._common import emit
from okxq.data.archive import event_inst_id
from okxq.data.replay import load_dataset
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope
from okxq.exchange.okx.mappings import BAR_MILLISECONDS, SEQUENCED_BOOK_CHANNELS, bar_for_event_type

app = typer.Typer(help="Données de marché : inspection de jeux de données et d'archives.")


def candle_gaps(events: Sequence[EventEnvelope]) -> list[dict[str, Any]]:
    """Trous entre bougies CLÔTURÉES consécutives (``confirm == "1"``), par instrument et par barre."""
    gaps: list[dict[str, Any]] = []
    series: dict[tuple[str, str], list[int]] = {}
    for env in events:
        if not env.event_type.startswith("candle.") or env.payload.get("confirm") != "1":
            continue
        series.setdefault((event_inst_id(env), env.event_type), []).append(int(env.payload["ts_ms"]))
    for (inst_id, event_type), stamps in sorted(series.items()):
        bar_for_event_type(event_type)  # refuse un type de bougie non répertorié
        step = BAR_MILLISECONDS[event_type.removeprefix("candle.")]
        ordered = sorted(set(stamps))
        for prev, nxt in pairwise(ordered):
            if nxt - prev != step:
                gaps.append(
                    {
                        "inst_id": inst_id,
                        "event_type": event_type,
                        "from_ts_ms": prev,
                        "to_ts_ms": nxt,
                        "missing_bars": (nxt - prev) // step - 1,
                    }
                )
    return gaps


def book_sequence_gaps(events: Sequence[EventEnvelope]) -> list[dict[str, Any]]:
    """Ruptures ``prev_seq_id ≠ seq_id précédent`` sur les canaux séquencés, dans l'ordre de réception."""
    gaps: list[dict[str, Any]] = []
    last_seq: dict[tuple[str, str], int] = {}
    for env in sorted(events, key=lambda e: (e.available_at, e.ingest_seq)):
        if not env.event_type.startswith("book."):
            continue
        channel = str(env.payload.get("channel", ""))
        if channel not in SEQUENCED_BOOK_CHANNELS:
            continue
        key = (event_inst_id(env), channel)
        seq = env.payload.get("seq_id")
        prev = env.payload.get("prev_seq_id")
        if env.event_type == "book.snapshot":
            if seq is not None:
                last_seq[key] = int(seq)
            continue
        if seq is None or prev is None:
            continue
        expected = last_seq.get(key)
        if expected is not None and int(prev) != expected and int(prev) != int(seq):
            gaps.append(
                {
                    "inst_id": key[0],
                    "channel": channel,
                    "expected_prev_seq_id": expected,
                    "prev_seq_id": int(prev),
                    "seq_id": int(seq),
                    "ingest_seq": env.ingest_seq,
                }
            )
        last_seq[key] = int(seq)
    return gaps


def inspect_dataset(manifest_path: Path) -> dict[str, Any]:
    directory = manifest_path.parent
    checks: list[dict[str, str]] = []
    try:
        manifest, events = load_dataset(directory, verify=True)
        checks.append({"check": "checksum", "status": "ok", "detail": str(manifest.get("sha256"))})
    except DataQualityError as exc:
        manifest, events = load_dataset(directory, verify=False)
        checks.append({"check": "checksum", "status": "fail", "detail": str(exc)})
    coverage: dict[str, dict[str, Any]] = {}
    for env in events:
        key = f"{event_inst_id(env)}|{env.event_type}"
        entry = coverage.setdefault(
            key,
            {
                "inst_id": event_inst_id(env),
                "event_type": env.event_type,
                "rows": 0,
                "first_available_at": env.available_at.isoformat(),
                "last_available_at": env.available_at.isoformat(),
                "first_exchange_ts": None,
                "last_exchange_ts": None,
            },
        )
        entry["rows"] += 1
        entry["first_available_at"] = min(entry["first_available_at"], env.available_at.isoformat())
        entry["last_available_at"] = max(entry["last_available_at"], env.available_at.isoformat())
        if env.exchange_ts is not None:
            iso = env.exchange_ts.isoformat()
            entry["first_exchange_ts"] = (
                iso if entry["first_exchange_ts"] is None else min(entry["first_exchange_ts"], iso)
            )
            entry["last_exchange_ts"] = (
                iso if entry["last_exchange_ts"] is None else max(entry["last_exchange_ts"], iso)
            )
    found = sorted({event_inst_id(e) for e in events if event_inst_id(e) != "_"})
    declared = sorted(str(x) for x in manifest.get("instruments", []))
    checks.append(
        {
            "check": "instruments",
            "status": "ok" if found == declared else "warn",
            "detail": f"déclarés={declared} trouvés={found}",
        }
    )
    rows_declared = int(manifest.get("rows", len(events)))
    checks.append(
        {
            "check": "rows",
            "status": "ok" if rows_declared == len(events) else "fail",
            "detail": f"{len(events)}/{rows_declared}",
        }
    )
    c_gaps = candle_gaps(events)
    b_gaps = book_sequence_gaps(events)
    failed = any(c["status"] == "fail" for c in checks)
    return {
        "ok": not failed,
        "manifest": str(manifest_path),
        "dataset": manifest.get("dataset"),
        "format": manifest.get("format", "jsonl"),
        "schema_version": manifest.get("schema_version"),
        "quality_level": manifest.get("quality_level"),
        "rows": len(events),
        "first_available_at": manifest.get("first_available_at"),
        "last_available_at": manifest.get("last_available_at"),
        "instruments": found,
        "coverage": sorted(coverage.values(), key=lambda c: (str(c["inst_id"]), str(c["event_type"]))),
        "candle_gaps": c_gaps,
        "book_sequence_gaps": b_gaps,
        "checks": checks,
    }


@app.command("inspect")
def inspect_cmd(manifest: Path = typer.Option(..., "--manifest", exists=True, dir_okay=False)) -> None:
    """Inspecte un jeu de données : couverture par instrument/type, trous, instruments, checksums."""
    try:
        report = inspect_dataset(manifest)
    except DataQualityError as exc:
        typer.echo(f"jeu de données illisible : {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(report)
    if not report["ok"]:
        sys.exit(1)
