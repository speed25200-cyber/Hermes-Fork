"""Alignement du chemin de prix des labels sur l'horloge des décisions.

Le chemin de prix partait de l'horodatage BRUT du premier événement, décalé de la latence
d'ingestion, alors que les coupures de décision sont alignées sur l'intervalle. Aucun point du
chemin ne tombait donc dans la fenêtre d'entrée, et 100 % des labels sortaient `NO_ENTRY` : le jeu
d'entraînement était vide sans que rien ne le signale. Un jeu vide est plus dangereux qu'une erreur —
l'entraînement « réussit » et le modèle n'a rien appris.
"""

from __future__ import annotations

import polars as pl
import pytest

from okxq.research.datasets import DEFAULT_LABEL_SPECS, DatasetSpec, build_dataset
from okxq.research.labels import LabelSpec
from okxq.research.synthetic import SyntheticSpec, generate_synthetic_events

INST = "BTC-USDT-SWAP"


@pytest.fixture(scope="module")
def events():
    return generate_synthetic_events(SyntheticSpec(minutes=180, instruments=(INST,), seed=25200))


def _frame(events, specs: tuple[LabelSpec, ...]) -> pl.DataFrame:
    spec = DatasetSpec(instruments=(INST,), label_specs=specs, cutoff_interval_s=60, warmup_s=600)
    return build_dataset(events, spec=spec, synthetic=True, source="synthetique", latency_assumed=True).frame


def test_the_default_specs_produce_usable_labels(events) -> None:
    """Le critère décisif : la majorité des labels doit être exploitable, pas tous rejetés."""
    frame = _frame(events, DEFAULT_LABEL_SPECS)
    assert frame.height > 0
    counts = dict(
        zip(
            frame["label_quality"].value_counts()["label_quality"].to_list(),
            frame["label_quality"].value_counts()["count"].to_list(),
            strict=True,
        )
    )
    ok = counts.get("OK", 0)
    assert ok > 0, f"aucun label exploitable : {counts}"
    # Avant le correctif, ce ratio valait exactement 0.
    assert ok / frame.height > 0.5, f"trop de labels rejetés : {counts}"


def test_every_default_horizon_yields_usable_labels(events) -> None:
    """Un horizon qui ne produit que des rejets n'a rien à faire dans les valeurs par défaut."""
    frame = _frame(events, DEFAULT_LABEL_SPECS)
    for horizon in {s.horizon_s for s in DEFAULT_LABEL_SPECS}:
        subset = frame.filter(pl.col("horizon_s") == horizon)
        assert subset.height > 0, f"horizon {horizon} absent du jeu"
        usable = subset.filter(pl.col("label_quality") == "OK").height
        assert usable > 0, f"horizon {horizon} : aucun label exploitable"


def test_a_horizon_that_leaves_no_holding_time_is_refused_at_construction() -> None:
    """L'horizon se mesure depuis la DÉCISION : s'il ne dépasse pas la fenêtre d'entrée, l'entrée et
    la sortie tombent au même point. Refuser la combinaison vaut mieux qu'un jeu vide en silence."""
    with pytest.raises(ValueError, match="horizon_s doit dépasser entry_window_s"):
        LabelSpec(horizon_s=60, entry_window_s=60)
    with pytest.raises(ValueError, match="horizon_s doit dépasser entry_window_s"):
        LabelSpec(horizon_s=30, entry_window_s=60)
    # Contre-épreuve : la combinaison admissible passe.
    assert LabelSpec(horizon_s=120, entry_window_s=60).horizon_s == 120


def test_entries_land_on_the_decision_clock(events) -> None:
    """Les entrées doivent tomber sur des multiples de l'intervalle de décision.

    Une entrée à `:00.150` trahissait le décalage : le chemin de prix n'était pas échantillonné sur
    la même horloge que les décisions.
    """
    frame = _frame(events, DEFAULT_LABEL_SPECS)
    entries = frame.filter(pl.col("label_quality") == "OK")["entry_at"].to_list()
    assert entries, "aucune entrée à vérifier"
    decales = [e for e in entries if e.second != 0 or e.microsecond != 0]
    assert not decales, f"entrées hors de l'horloge de décision : {decales[:3]}"
