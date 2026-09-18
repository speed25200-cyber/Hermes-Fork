"""Propriétés du carnet : tri, non-croisement, idempotence d'un remplacement, suppression à zéro."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from okxq.data.orderbook import BookState, BookValidator

pytestmark = pytest.mark.property

TS = 1_789_732_800_000
price = st.decimals(
    min_value=Decimal("1"), max_value=Decimal("999"), places=1, allow_nan=False, allow_infinity=False
)
qty = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("100"), places=1, allow_nan=False, allow_infinity=False
)
levels = st.lists(st.tuples(price, qty), min_size=0, max_size=8)


def seeded() -> BookValidator:
    v = BookValidator("X-USDT-SWAP", "books")
    v.ingest(
        {
            "action": "snapshot",
            "bids": [["500.0", "10"], ["499.0", "5"]],
            "asks": [["501.0", "10"], ["502.0", "5"]],
            "seq_id": 1000,
            "prev_seq_id": -1,
            "checksum": 0,
            "ts_ms": TS,
        }
    )
    return v


@settings(max_examples=150, deadline=None)
@given(bids=levels, asks=levels)
def test_book_invariants_hold_or_book_is_invalid(bids, asks):
    v = seeded()
    result = v.ingest(
        {
            "action": "update",
            "bids": [[format(p, "f"), format(q, "f")] for p, q in bids],
            "asks": [[format(p, "f"), format(q, "f")] for p, q in asks],
            "seq_id": 1001,
            "prev_seq_id": 1000,
            "checksum": 0,
            "ts_ms": TS + 1,
        }
    )
    view = v.view()
    if result.accepted:
        # tri strict, quantités strictement positives, jamais croisé
        assert [p for p, _ in view.bids] == sorted({p for p, _ in view.bids}, reverse=True)
        assert [p for p, _ in view.asks] == sorted({p for p, _ in view.asks})
        assert all(q > 0 for _, q in view.bids + view.asks)
        if view.bids and view.asks:
            assert view.bids[0][0] < view.asks[0][0]
        assert view.state is BookState.VALID
    else:
        assert not view.usable  # un refus bloque les décisions, il ne laisse pas un carnet douteux


@settings(max_examples=100, deadline=None)
@given(p=price, q=st.decimals(min_value=Decimal("0.1"), max_value=Decimal("50"), places=1))
def test_replacement_is_idempotent_and_zero_removes(p, q):
    v = seeded()
    seq = 1000
    applied = None
    for _ in range(3):
        seq += 1
        res = v.ingest(
            {
                "action": "update",
                "bids": [[format(p, "f"), format(q, "f")]],
                "asks": [],
                "seq_id": seq,
                "prev_seq_id": seq - 1,
                "checksum": 0,
                "ts_ms": TS + seq,
            }
        )
        if not res.accepted:
            return  # le niveau croisait l'ask : le refus est le comportement attendu
        current = dict(v.view().bids)
        if applied is not None:
            assert current == applied  # un remplacement identique ne change rien
        applied = current
    assert dict(v.view().bids).get(p) == q
    seq += 1
    assert v.ingest(
        {
            "action": "update",
            "bids": [[format(p, "f"), "0"]],
            "asks": [],
            "seq_id": seq,
            "prev_seq_id": seq - 1,
            "checksum": 0,
            "ts_ms": TS + seq,
        }
    ).accepted
    assert p not in dict(v.view().bids)


@settings(max_examples=80, deadline=None)
@given(steps=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=12))
def test_any_increasing_sequence_is_accepted_and_version_is_monotonic(steps):
    v = seeded()
    seq = 1000
    versions = [v.view().version]
    for step in steps:
        prev, seq = seq, seq + step
        res = v.ingest(
            {
                "action": "update",
                "bids": [["499.5", "1"]],
                "asks": [],
                "seq_id": seq,
                "prev_seq_id": prev,
                "checksum": 0,
                "ts_ms": TS + seq,
            }
        )
        assert res.accepted, res.reason  # aucun faux rejet sur des pas non unitaires
        versions.append(v.view().version)
    assert versions == sorted(versions)
    assert v.counters()["gaps"] == 0
