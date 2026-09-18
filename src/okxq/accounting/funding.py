"""Funding (§38, T25, T26) : règlements lus dans le contrat, jamais d'intervalle universel codé en dur.

- Les instants de règlement viennent des événements ``funding`` (``funding_time_ms``,
  ``next_funding_time_ms``) ; le contrat peut changer d'intervalle sans que ce module le sache.
- Un flux n'existe QUE lorsqu'un règlement est réellement traversé avec une position détenue à cet
  instant : ``cashflow = -signed_notional_at_settlement × realized_rate`` (T25). Pas de proratisation.
- Estimation (``settled=false``) et montant réglé (``settled=true``, ``realized_rate``) sont deux objets
  distincts ; ``funding_rate_known_at(cutoff)`` ne rend que ce qui était disponible au cutoff (T26).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import CausalityError, DataQualityError, UnitError
from okxq.domain.events import EventEnvelope
from okxq.domain.instruments import InstrumentSpec
from okxq.domain.money import ZERO, dec

__all__ = [
    "FundingObservation",
    "FundingSettlement",
    "detect_funding_lookahead",
    "expected_funding_cost",
    "funding_cashflow",
    "funding_rate_known_at",
    "parse_funding_event",
    "settle_if_crossed",
    "settlements_crossed",
    "signed_notional_at_settlement",
]


def _ms_to_dt(ms: object, *, field: str) -> datetime:
    try:
        value = int(str(ms))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"{field} invalide : {ms!r}", field=field) from exc
    return datetime.fromtimestamp(value / 1000, tz=UTC)


@dataclass(frozen=True, slots=True)
class FundingObservation:
    """Une observation du canal funding, avec l'instant où elle est devenue disponible."""

    inst_id: str
    funding_rate: Decimal  # taux courant (estimation avant règlement)
    next_funding_rate: Decimal | None
    funding_time: datetime  # prochain règlement (tel que publié par le contrat)
    next_funding_time: datetime  # règlement suivant
    settled: bool
    realized_rate: Decimal | None
    available_at: datetime
    receive_ts: datetime
    ingest_seq: int = 0

    @property
    def is_estimate(self) -> bool:
        return not self.settled

    @property
    def settlement_interval(self) -> datetime | None:  # pragma: no cover - lecture directe
        return None


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    inst_id: str
    settlement_at: datetime
    realized_rate: Decimal
    signed_notional: Decimal
    cashflow: Decimal
    idempotency_key: str


def parse_funding_event(envelope: EventEnvelope) -> FundingObservation:
    if envelope.event_type != "funding":
        raise DataQualityError("événement non funding", event_type=envelope.event_type)
    p = envelope.payload
    settled = bool(p.get("settled", False))
    realized_raw = p.get("realized_rate")
    realized = dec(str(realized_raw), field="realized_rate") if realized_raw not in (None, "") else None
    if settled and realized is None:
        raise DataQualityError("règlement sans realized_rate", inst_id=str(p.get("inst_id")))
    next_rate_raw = p.get("next_funding_rate")
    return FundingObservation(
        inst_id=str(p["inst_id"]),
        funding_rate=dec(str(p["funding_rate"]), field="funding_rate"),
        next_funding_rate=dec(str(next_rate_raw), field="next_funding_rate")
        if next_rate_raw not in (None, "")
        else None,
        funding_time=_ms_to_dt(p["funding_time_ms"], field="funding_time_ms"),
        next_funding_time=_ms_to_dt(p["next_funding_time_ms"], field="next_funding_time_ms"),
        settled=settled,
        realized_rate=realized,
        available_at=envelope.available_at,
        receive_ts=envelope.receive_ts,
        ingest_seq=envelope.ingest_seq,
    )


def signed_notional_at_settlement(
    signed_contracts: Decimal, spec: InstrumentSpec, reference_price: Decimal
) -> Decimal:
    """Assiette du funding : ``contrats × v × prix de référence du règlement`` (validée)."""
    contracts = dec(signed_contracts, field="signed_contracts")
    price = dec(reference_price, field="reference_price")
    if price <= 0:
        raise UnitError("prix de référence du règlement non positif", price=str(price))
    return contracts * spec.base_units_per_contract * price


def funding_cashflow(
    signed_notional_at_settlement_: Decimal, realized_rate: Decimal, *, crossed: bool
) -> Decimal:
    """T25 : ``-notionnel signé × taux réalisé`` si le règlement est traversé, sinon 0 (jamais de prorata)."""
    if not crossed:
        return ZERO
    return -dec(signed_notional_at_settlement_, field="signed_notional") * dec(
        realized_rate, field="realized_rate"
    )


def settlements_crossed(
    settlement_times: Iterable[datetime], *, held_from: datetime, held_until: datetime
) -> list[datetime]:
    """Instants ``t`` avec ``held_from < t <= held_until`` : la position est détenue à l'instant du règlement."""
    start = ensure_utc(held_from, field="held_from")
    end = ensure_utc(held_until, field="held_until")
    if end < start:
        raise CausalityError("intervalle de détention inversé")
    return sorted({ensure_utc(t) for t in settlement_times if start < ensure_utc(t) <= end})


def settle_if_crossed(
    observation: FundingObservation,
    *,
    signed_contracts: Decimal,
    spec: InstrumentSpec,
    reference_price: Decimal,
    held_from: datetime,
) -> FundingSettlement | None:
    """Produit le règlement si l'observation est un règlement réel et que la position était détenue à cet instant."""
    if not observation.settled or observation.realized_rate is None:
        return None
    if observation.inst_id != spec.inst_id:
        raise UnitError(
            "observation de funding d'un autre instrument", got=observation.inst_id, spec=spec.inst_id
        )
    contracts = dec(signed_contracts, field="signed_contracts")
    if contracts == 0:
        return None
    held_start = ensure_utc(held_from, field="held_from")
    if observation.funding_time <= held_start:
        # Règlement antérieur (ou simultané) à l'ouverture de la position : l'intervalle de détention
        # (held_from, funding_time] est vide, donc le règlement n'est PAS traversé et ne produit aucun
        # flux (T25). C'est le cas NORMAL « position ouverte après le règlement », y compris quand un
        # événement ancien arrive tard (§52.2, T15) : il doit rendre None. Sans ce garde-fou,
        # ``settlements_crossed`` recevait un intervalle inversé et levait ``CausalityError`` pour une
        # situation licite — une erreur que l'appelant serait tenté d'attraper largement, ce qui
        # masquerait les vraies violations de causalité.
        return None
    crossed = bool(
        settlements_crossed(
            [observation.funding_time], held_from=held_start, held_until=observation.funding_time
        )
    )
    if not crossed:
        return None
    notional = signed_notional_at_settlement(contracts, spec, reference_price)
    cashflow = funding_cashflow(notional, observation.realized_rate, crossed=True)
    key = f"funding:{spec.inst_id}:{int(observation.funding_time.timestamp() * 1000)}"
    return FundingSettlement(
        spec.inst_id, observation.funding_time, observation.realized_rate, notional, cashflow, key
    )


def expected_funding_cost(
    signed_notional: Decimal,
    rate_estimate: Decimal,
    settlement_times: Iterable[datetime],
    *,
    from_at: datetime,
    until: datetime,
) -> Decimal:
    """Coût ATTENDU (estimation) : somme sur les règlements que l'horizon traverse, 0 s'il n'en traverse aucun."""
    n = len(settlements_crossed(settlement_times, held_from=from_at, held_until=until))
    if n == 0:
        return ZERO
    return -dec(signed_notional, field="signed_notional") * dec(rate_estimate, field="rate_estimate") * n


def funding_rate_known_at(
    observations: Sequence[FundingObservation], inst_id: str, cutoff: datetime
) -> FundingObservation | None:
    """Dernière observation disponible au cutoff (``available_at <= cutoff``), départagée par ordre d'ingestion."""
    limit = ensure_utc(cutoff, field="cutoff")
    best: FundingObservation | None = None
    for obs in observations:
        if obs.inst_id != inst_id or obs.available_at > limit:
            continue
        if best is None or (obs.available_at, obs.ingest_seq) >= (best.available_at, best.ingest_seq):
            best = obs
    return best


def detect_funding_lookahead(
    observations: Sequence[FundingObservation], inst_id: str, cutoff: datetime, rate_used: Decimal
) -> bool:
    """T26 : vrai si ``rate_used`` n'était connaissable qu'après le cutoff (taux final utilisé comme feature antérieure).

    Le taux est « connaissable » s'il apparaît dans une observation disponible au cutoff (estimation ou
    règlement). S'il n'apparaît que dans un règlement disponible après le cutoff, c'est un lookahead.
    """
    limit = ensure_utc(cutoff, field="cutoff")
    used = dec(rate_used, field="rate_used")
    known_before = False
    appears_after_only = False
    for obs in observations:
        if obs.inst_id != inst_id:
            continue
        candidates = [obs.funding_rate]
        if obs.realized_rate is not None:
            candidates.append(obs.realized_rate)
        if obs.next_funding_rate is not None:
            candidates.append(obs.next_funding_rate)
        if used not in candidates:
            continue
        if obs.available_at <= limit:
            known_before = True
        elif obs.settled and obs.realized_rate == used:
            appears_after_only = True
    return appears_after_only and not known_before
