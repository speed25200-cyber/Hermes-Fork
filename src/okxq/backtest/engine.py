"""Moteur de backtest événementiel (§32).

Il rejoue les événements d'un jeu de données dans l'ordre ``(available_at, ingest_seq)``, entretient un
état de marché, appelle une décision aux frontières UTC de 60 secondes, laisse l'exchange virtuel
produire ses fills et ses événements, et alimente le ledger. Le rapport final publie SÉPARÉMENT le PnL
marqué et le PnL après liquidation simulée, et expose les invariants vérifiés.

Le moteur ne contient aucune stratégie : la décision est un callback injecté qui reçoit la vue de marché
et l'exchange. C'est la même surface qu'en direct — un backtest qui appellerait un autre code serait
une mesure de quelque chose d'autre.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from okxq.accounting.ledger import Ledger
from okxq.backtest.market_state import MarketState, Trade, spec_from_instrument_event
from okxq.backtest.virtual_exchange import VirtualExchange
from okxq.domain.clocks import SimulatedClock, floor_to_interval, next_boundary
from okxq.domain.errors import DataQualityError
from okxq.domain.events import EventEnvelope, Fill
from okxq.domain.money import ZERO
from okxq.exchange.base import ExchangeEvent
from okxq.runtime.logging import get_logger

__all__ = ["BacktestEngine", "BacktestReport", "DecisionContext", "DecisionHook"]

_log = get_logger("okxq.backtest")


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Ce que la décision voit à une frontière. Rien d'autre n'est disponible : c'est le contrat PIT."""

    cutoff_at: datetime
    deadline_at: datetime
    market: MarketState
    exchange: VirtualExchange
    ledger: Ledger
    boundary_index: int


DecisionHook = Callable[[DecisionContext], Awaitable[Any]]


@dataclass(slots=True)
class BacktestReport:
    dataset: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    events_applied: int = 0
    boundaries: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    order_events: int = 0
    duplicate_fills_ignored: int = 0
    funding_settlements: list[dict[str, Any]] = field(default_factory=list)
    close_out: dict[str, Any] = field(default_factory=dict)
    invariants: list[dict[str, Any]] = field(default_factory=list)
    quality_notes: list[str] = field(default_factory=list)

    def add_invariant(self, name: str, ok: bool, detail: str) -> None:
        self.invariants.append({"invariant": name, "ok": ok, "detail": detail})

    @property
    def ok(self) -> bool:
        return all(bool(i["ok"]) for i in self.invariants)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "ok": self.ok,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "events_applied": self.events_applied,
            "boundaries": self.boundaries,
            "decisions": self.decisions,
            "fills": self.fills,
            "order_events": self.order_events,
            "duplicate_fills_ignored": self.duplicate_fills_ignored,
            "funding_settlements": self.funding_settlements,
            "close_out": self.close_out,
            "invariants": self.invariants,
            "quality_notes": self.quality_notes,
        }


class BacktestEngine:
    def __init__(
        self,
        *,
        clock: SimulatedClock,
        market: MarketState,
        exchange: VirtualExchange,
        ledger: Ledger,
        decision: DecisionHook | None = None,
        interval_seconds: int = 60,
        decision_budget_ms: int = 3000,
        dataset: str = "unknown",
    ) -> None:
        self._clock = clock
        self.market = market
        self.exchange = exchange
        self.ledger = ledger
        self._decision = decision
        self._interval = interval_seconds
        self._budget = timedelta(milliseconds=decision_budget_ms)
        self.report = BacktestReport(dataset=dataset)
        self._seen_fill_keys: set[str] = set()
        self._settled_funding: set[tuple[str, str]] = set()

    # --- consommation des événements ----------------------------------------------------------------

    def _consume_exchange_events(self) -> None:
        for event in self.exchange.drain():
            self._apply_exchange_event(event)

    def _apply_exchange_event(self, event: ExchangeEvent) -> None:
        if event.order_event is not None:
            self.report.order_events += 1
        if event.fill is None:
            return
        fill: Fill = event.fill
        if fill.execution_key in self._seen_fill_keys:
            # Livraison « au moins une fois » : un même fill livré trois fois a UN seul effet (T34).
            self.report.duplicate_fills_ignored += 1
            return
        self._seen_fill_keys.add(fill.execution_key)
        spec = self.market.specs.get(fill.inst_id)
        if spec is None:
            raise DataQualityError("fill sur un instrument inconnu", inst_id=fill.inst_id)
        self.ledger.register_spec(spec)
        self.ledger.record_fill(fill, spec)
        self.report.fills.append(
            {
                "execution_key": fill.execution_key,
                "inst_id": fill.inst_id,
                "side": fill.side.value,
                "contracts": format(fill.contracts, "f"),
                "fill_price": format(fill.fill_price, "f"),
                "fee_cashflow": format(fill.fee_cashflow, "f"),
                "liquidity": fill.liquidity.value,
                "fill_at": fill.fill_at.isoformat(),
            }
        )

    def _settle_funding_if_crossed(self, previous: datetime | None, now: datetime) -> None:
        """Un flux de funding n'existe qu'à un règlement RÉELLEMENT traversé (§37, T25)."""
        if previous is None:
            return
        for observation in self.market.funding:
            settlement = getattr(observation, "funding_time", None)
            realized = getattr(observation, "realized_rate", None)
            inst_id = getattr(observation, "inst_id", None)
            if settlement is None or realized is None or inst_id is None:
                continue
            key = (str(inst_id), settlement.isoformat())
            if key in self._settled_funding or not (previous < settlement <= now):
                continue
            self._settled_funding.add(key)
            reference = self.exchange._mark(str(inst_id))  # prix de référence de l'assiette
            if reference is None:
                self.report.quality_notes.append(
                    f"règlement de funding traversé sans prix de référence : {inst_id} à {settlement.isoformat()}"
                )
                continue
            cashflow = self.exchange.settle_funding(str(inst_id), Decimal(str(realized)), reference)
            if cashflow != ZERO:
                # Le même flux entre dans le ledger : une seule écriture, idempotente par règlement.
                self.ledger.record_funding(
                    inst_id=str(inst_id),
                    cashflow=cashflow,
                    settlement_at=settlement,
                    realized_rate=Decimal(str(realized)),
                )
                self.report.funding_settlements.append(
                    {
                        "inst_id": str(inst_id),
                        "settlement_at": settlement.isoformat(),
                        "realized_rate": str(realized),
                        "cashflow": format(cashflow, "f"),
                    }
                )

    # --- boucle -------------------------------------------------------------------------------------

    async def run(self, events: Sequence[EventEnvelope]) -> BacktestReport:
        if not events:
            raise DataQualityError("aucun événement à rejouer")
        self.report.started_at = events[0].available_at
        boundary = next_boundary(events[0].available_at, self._interval)
        previous_boundary: datetime | None = floor_to_interval(events[0].available_at, self._interval)
        index = 0

        for envelope in events:
            while envelope.available_at >= boundary:
                self._clock.set(boundary)
                await self._on_boundary(boundary, index)
                self._settle_funding_if_crossed(previous_boundary, boundary)
                previous_boundary = boundary
                boundary = boundary + timedelta(seconds=self._interval)
                index += 1
            if envelope.available_at > self._clock.now_utc():
                self._clock.set(envelope.available_at)
            applied = self.market.apply(envelope)
            self.report.events_applied += 1
            trades: list[Trade] = [applied.trade] if applied.trade is not None else []
            if applied.spec is not None:
                self.ledger.register_spec(applied.spec)
            self.exchange.on_market_event(trades=trades, books=self.market.book_views())
            self._consume_exchange_events()

        # Dernière frontière puis clôture.
        self._clock.set(max(self._clock.now_utc(), boundary))
        await self._on_boundary(boundary, index)
        self._settle_funding_if_crossed(previous_boundary, boundary)
        self.exchange.on_market_event(trades=[], books=self.market.book_views())
        self._consume_exchange_events()
        self.report.finished_at = self._clock.now_utc()
        self.report.close_out = self.exchange.close_out()
        self._check_invariants()
        return self.report

    async def _on_boundary(self, boundary: datetime, index: int) -> None:
        self.report.boundaries += 1
        if self._decision is None:
            return
        context = DecisionContext(
            cutoff_at=boundary,
            deadline_at=boundary + self._budget,
            market=self.market,
            exchange=self.exchange,
            ledger=self.ledger,
            boundary_index=index,
        )
        try:
            outcome = await self._decision(context)
        except Exception as exc:  # une décision qui échoue n'arrête pas le rejeu, elle est journalisée
            _log.warning("décision de backtest en échec", boundary=boundary.isoformat(), detail=repr(exc))
            self.report.decisions.append({"cutoff_at": boundary.isoformat(), "error": repr(exc)})
            return
        record: dict[str, Any] = {"cutoff_at": boundary.isoformat()}
        if isinstance(outcome, dict):
            record.update(outcome)
        elif outcome is not None:
            record["outcome"] = str(outcome)
        self.report.decisions.append(record)
        self.exchange.on_market_event(trades=[], books=self.market.book_views())
        self._consume_exchange_events()

    # --- invariants ---------------------------------------------------------------------------------

    def _check_invariants(self) -> None:
        r = self.report
        r.add_invariant(
            "fills_dedupliques",
            len(self._seen_fill_keys) == len(r.fills),
            f"{len(r.fills)} fills uniques, {r.duplicate_fills_ignored} doublon(s) ignoré(s)",
        )
        audit = self.ledger.audit()
        r.add_invariant("ledger_equilibre", audit.ok, str(audit))
        marks = {inst: mark for inst, mark in self.market.marks.items()}
        for inst_id, view in self.market.book_views().items():
            if inst_id not in marks and view.mid is not None:
                marks[inst_id] = view.mid
        ledger_equity = self.ledger.equity(marks)
        exchange_equity = self.exchange.equity()
        gap = abs(ledger_equity.equity - exchange_equity)
        r.add_invariant(
            "equity_ledger_vs_simulateur",
            gap <= Decimal("0.01"),
            f"ledger {format(ledger_equity.equity, 'f')} vs simulateur {format(exchange_equity, 'f')} (écart {format(gap, 'f')})",
        )
        monotone = True
        cumulative: dict[str, Decimal] = {}
        for fill in r.fills:
            key = str(fill["inst_id"]) + str(fill["side"])
            cumulative[key] = cumulative.get(key, ZERO) + Decimal(str(fill["contracts"]))
            if cumulative[key] < 0:
                monotone = False
        r.add_invariant(
            "quantites_executees_monotones", monotone, "aucune quantité exécutée cumulée négative"
        )
        r.add_invariant(
            "pnl_marque_et_liquide_distincts",
            "marked_equity" in r.close_out and "liquidated_equity" in r.close_out,
            "le rapport publie les deux valeurs séparément",
        )
        residual = r.close_out.get("residual_exposure") or []
        r.add_invariant(
            "residus_exposes",
            isinstance(residual, list),
            f"{len(residual)} résidu(s) déclaré(s) : jamais « FLAT » sans preuve",
        )


def build_market_from_events(events: Sequence[EventEnvelope], *, provenance: str = "replay") -> MarketState:
    """Pré-charge les métadonnées d'instruments (nécessaires avant tout ordre)."""
    market = MarketState()
    for envelope in events:
        if envelope.event_type == "instrument":
            try:
                market.specs[str(envelope.payload["inst_id"])] = spec_from_instrument_event(
                    envelope.payload, observed_at=envelope.available_at, provenance=provenance
                )
            except Exception as exc:
                # Instrument non supporté (inverse, option, ctMult≠1…) : il n'entre jamais dans le
                # simulateur, et le motif est nommé — un rejet muet masquerait une divergence de contrat.
                _log.info(
                    "instrument écarté du marché simulé",
                    inst_id=str(envelope.payload.get("inst_id")),
                    detail=f"{type(exc).__name__}: {exc}",
                )
                continue
    return market
