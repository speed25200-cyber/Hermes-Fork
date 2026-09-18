"""Ledger append-only à écritures équilibrées par devise (§38).

Principes :
- chaque transaction est un ensemble d'écritures dont la somme par devise est NULLE ; le compte
  ``cash:<ccy>`` porte le flux de trésorerie réel et les comptes de contrepartie (``fees``, ``funding``,
  ``pnl:realized``, ``external``, ``adjustments``) portent l'opposé. La « contribution » d'un compte
  (``-solde``) est donc lisible directement : ``fees`` contribue négativement quand on paie ;
- idempotence par ``idempotency_key`` (clé d'exécution du fill, identifiant de règlement de funding,
  identifiant de bill externe...) : une transaction déjà enregistrée n'a AUCUN second effet (T31, T34) ;
- les positions sont dérivées des fills via ``okxq.domain.positions.apply_fill`` (PnL réalisé sur la part
  fermée, scission réduction/fermeture/réouverture) ;
- une seule source de commissions est déclarée (``FeeSource``) : par fill OU agrégée par ordre, jamais
  les deux ;
- ``fee_cashflow`` est SIGNÉ : négatif = débit, positif = crédit/rebate. Jamais de valeur absolue.

Persistance : ``LedgerTransaction``/``LedgerEntry`` (et ``FillRow`` pour la déduplication des fills) via
SQLAlchemy ; fonctionne avec ``okxq.persistence.db.memory_engine()`` en tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from okxq.domain.clocks import Clock, ensure_utc
from okxq.domain.errors import LedgerError
from okxq.domain.events import Fill
from okxq.domain.ids import sha256_hex
from okxq.domain.instruments import InstrumentSpec
from okxq.domain.money import ZERO, dec, quantize_money
from okxq.domain.positions import FillSplit, Position, apply_fill
from okxq.persistence.models import FillRow, LedgerEntry, LedgerTransaction

__all__ = [
    "ACCOUNT_ADJUSTMENTS",
    "ACCOUNT_CASH",
    "ACCOUNT_EXTERNAL",
    "ACCOUNT_FEES",
    "ACCOUNT_FUNDING",
    "ACCOUNT_REALIZED",
    "Entry",
    "EquityView",
    "FeeSource",
    "Ledger",
    "LedgerAudit",
    "PositionView",
    "RecordedTransaction",
    "TxnKind",
    "account_name",
    "txn_id_for",
]

ACCOUNT_CASH = "cash"
ACCOUNT_FEES = "fees"
ACCOUNT_FUNDING = "funding"
ACCOUNT_REALIZED = "pnl:realized"
ACCOUNT_EXTERNAL = "external"
ACCOUNT_ADJUSTMENTS = "adjustments"


def account_name(base: str, ccy: str) -> str:
    """``cash`` + ``USDT`` → ``cash:USDT`` ; ``pnl:realized`` + ``USDT`` → ``pnl:realized:USDT``."""
    return f"{base}:{ccy}"


class TxnKind(StrEnum):
    FILL = "fill"
    COMMISSION = "commission"  # commission agrégée par ordre (source PER_ORDER uniquement)
    REBATE = "rebate"  # rebate observé hors fill (bill distinct)
    FUNDING = "funding"
    LIQUIDATION = "liquidation"
    ADL = "adl"
    EXTERNAL = "external"  # dépôt / retrait / transfert observé
    CORRECTION = "correction"
    MARGIN_ADJUSTMENT = "margin_adjustment"


class FeeSource(StrEnum):
    """Source UNIQUE déclarée des commissions : par fill (flux privé fills) ou agrégée par ordre."""

    PER_FILL = "per_fill"
    PER_ORDER = "per_order"


_POSITION_KINDS = frozenset({TxnKind.FILL, TxnKind.LIQUIDATION, TxnKind.ADL})


@dataclass(frozen=True, slots=True)
class Entry:
    account: str
    ccy: str
    amount: Decimal
    inst_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecordedTransaction:
    txn_id: str
    kind: TxnKind
    idempotency_key: str
    occurred_at: datetime
    entries: tuple[Entry, ...]
    applied: bool  # False : la clé existait déjà, aucun effet (idempotence)
    realized_pnl: Decimal = ZERO
    fee_cashflow: Decimal = ZERO
    split: FillSplit | None = None


@dataclass(frozen=True, slots=True)
class PositionView:
    inst_id: str
    signed_base_qty: Decimal
    signed_contracts: Decimal | None
    average_entry_price: Decimal
    closed_base_qty: Decimal
    cost_basis: Decimal
    realized_pnl: Decimal
    version: int


@dataclass(frozen=True, slots=True)
class EquityView:
    """``equity = cash_collateral + unrealized_pnl`` (§38) ; les cumuls servent aux rapports."""

    as_of: datetime
    ccy: str
    cash_collateral: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    external_cashflow_cum: Decimal
    realized_pnl_cum: Decimal
    fees_cum: Decimal
    funding_cum: Decimal
    adjustments_cum: Decimal
    unmarked_instruments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerAudit:
    transactions: int
    entries: int
    balanced: bool
    unbalanced_txn_ids: tuple[str, ...]
    cash_matches_contributions: bool
    duplicate_keys: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.balanced and self.cash_matches_contributions and not self.duplicate_keys


def txn_id_for(account_scope: str, idempotency_key: str) -> str:
    """Identifiant de transaction DÉTERMINISTE (rapports reproductibles, T70)."""
    return "txn_" + sha256_hex(f"{account_scope}\x00{idempotency_key}")[:40]


@dataclass
class _State:
    positions: dict[str, Position] = field(default_factory=dict)
    balances: dict[str, Decimal] = field(default_factory=dict)
    keys: set[str] = field(default_factory=set)
    specs: dict[str, InstrumentSpec] = field(default_factory=dict)


class Ledger:
    """Ledger d'un ``account_scope``. Toutes les écritures passent par ``_commit``."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        account_scope: str,
        clock: Clock,
        settle_ccy: str = "USDT",
        fee_source: FeeSource = FeeSource.PER_FILL,
    ) -> None:
        if not account_scope:
            raise LedgerError("account_scope vide")
        self._factory = session_factory
        self.account_scope = account_scope
        self.settle_ccy = settle_ccy
        self.fee_source = fee_source
        self._clock = clock
        self._state = _State()
        self.load()

    # --- chargement / vues ---------------------------------------------------------------------------

    def load(self) -> None:
        """Reconstruit soldes, clés et positions depuis la base (reprise après redémarrage)."""
        state = _State()
        state.specs = dict(self._state.specs)
        with self._factory() as session:
            txns = session.execute(
                select(LedgerTransaction)
                .where(LedgerTransaction.account_scope == self.account_scope)
                .order_by(LedgerTransaction.recorded_at, LedgerTransaction.txn_id)
            ).scalars()
            for txn in txns:
                state.keys.add(txn.idempotency_key)
                kind = TxnKind(txn.kind)
                if kind in _POSITION_KINDS:
                    meta = txn.metadata_
                    inst_id = str(meta["inst_id"])
                    pos = state.positions.get(inst_id) or Position(
                        inst_id=inst_id, base_ccy=str(meta["base_ccy"]), settle_ccy=str(meta["settle_ccy"])
                    )
                    pos, _ = apply_fill(pos, dec(str(meta["signed_base_qty"])), dec(str(meta["price"])))
                    state.positions[inst_id] = pos
            entries = session.execute(
                select(LedgerEntry)
                .join(LedgerTransaction, LedgerEntry.txn_id == LedgerTransaction.txn_id)
                .where(LedgerTransaction.account_scope == self.account_scope)
            ).scalars()
            for entry in entries:
                state.balances[entry.account] = state.balances.get(entry.account, ZERO) + entry.amount
        self._state = state

    def register_spec(self, spec: InstrumentSpec) -> None:
        self._state.specs[spec.inst_id] = spec

    @property
    def positions(self) -> Mapping[str, Position]:
        return dict(self._state.positions)

    def position(self, inst_id: str) -> Position | None:
        return self._state.positions.get(inst_id)

    def position_view(self, inst_id: str) -> PositionView:
        pos = self._state.positions.get(inst_id)
        spec = self._state.specs.get(inst_id)
        if pos is None:
            return PositionView(inst_id, ZERO, ZERO if spec else None, ZERO, ZERO, ZERO, ZERO, 0)
        contracts = None if spec is None else pos.signed_base_qty / spec.base_units_per_contract
        return PositionView(
            inst_id=inst_id,
            signed_base_qty=pos.signed_base_qty,
            signed_contracts=contracts,
            average_entry_price=pos.average_entry_price,
            closed_base_qty=pos.closed_base_qty,
            cost_basis=pos.cost_basis(),
            realized_pnl=pos.realized_pnl,
            version=pos.version,
        )

    def balances(self) -> dict[str, Decimal]:
        return dict(self._state.balances)

    def balance(self, account: str) -> Decimal:
        return self._state.balances.get(account, ZERO)

    def contribution(self, base: str, ccy: str | None = None) -> Decimal:
        """Contribution signée d'un compte de contrepartie au cash (``-solde``)."""
        return -self.balance(account_name(base, ccy or self.settle_ccy))

    def cash_collateral(self, ccy: str | None = None) -> Decimal:
        return self.balance(account_name(ACCOUNT_CASH, ccy or self.settle_ccy))

    def unrealized_pnl(self, marks: Mapping[str, Decimal]) -> tuple[Decimal, tuple[str, ...]]:
        """Somme des PnL latents aux marks fournis ; les positions sans mark sont listées, pas devinées."""
        total = ZERO
        missing: list[str] = []
        for inst_id, pos in self._state.positions.items():
            if pos.is_flat:
                continue
            mark = marks.get(inst_id)
            if mark is None:
                missing.append(inst_id)
                continue
            total += pos.unrealized_pnl(dec(mark, field="mark_price"))
        return total, tuple(missing)

    def equity(self, marks: Mapping[str, Decimal], *, as_of: datetime | None = None) -> EquityView:
        ccy = self.settle_ccy
        cash = self.cash_collateral(ccy)
        upnl, missing = self.unrealized_pnl(marks)
        return EquityView(
            as_of=ensure_utc(as_of) if as_of is not None else self._clock.now_utc(),
            ccy=ccy,
            cash_collateral=cash,
            unrealized_pnl=upnl,
            equity=cash + upnl,
            external_cashflow_cum=self.contribution(ACCOUNT_EXTERNAL, ccy),
            realized_pnl_cum=self.contribution(ACCOUNT_REALIZED, ccy),
            fees_cum=self.contribution(ACCOUNT_FEES, ccy),
            funding_cum=self.contribution(ACCOUNT_FUNDING, ccy),
            adjustments_cum=self.contribution(ACCOUNT_ADJUSTMENTS, ccy),
            unmarked_instruments=missing,
        )

    def has_key(self, idempotency_key: str) -> bool:
        return idempotency_key in self._state.keys

    # --- événements ----------------------------------------------------------------------------------

    def record_fill(self, fill: Fill, spec: InstrumentSpec) -> RecordedTransaction:
        """Fill du flux privé : position via ``apply_fill``, PnL réalisé sur la part fermée, commission signée.

        La commission n'est prise dans le fill que si la source déclarée est ``PER_FILL``.
        """
        return self._record_execution(fill, spec, kind=TxnKind.FILL)

    def record_liquidation(
        self, fill: Fill, spec: InstrumentSpec, *, adl: bool = False
    ) -> RecordedTransaction:
        """Liquidation ou ADL OBSERVÉE : même arithmétique qu'un fill, étiquetée distinctement."""
        return self._record_execution(fill, spec, kind=TxnKind.ADL if adl else TxnKind.LIQUIDATION)

    def _record_execution(self, fill: Fill, spec: InstrumentSpec, *, kind: TxnKind) -> RecordedTransaction:
        if fill.account_scope != self.account_scope:
            raise LedgerError(
                "fill d'un autre account_scope", expected=self.account_scope, got=fill.account_scope
            )
        if fill.inst_id != spec.inst_id:
            raise LedgerError("spec et fill désalignés", fill=fill.inst_id, spec=spec.inst_id)
        key = fill.execution_key
        occurred = ensure_utc(fill.fill_at, field="fill_at")
        if key in self._state.keys:
            return self._already(kind, key, occurred)
        self._state.specs.setdefault(spec.inst_id, spec)
        current = self._state.positions.get(spec.inst_id) or Position(
            inst_id=spec.inst_id, base_ccy=spec.base_ccy, settle_ccy=spec.settle_ccy
        )
        signed_base = fill.side.sign * fill.contracts * spec.base_units_per_contract
        new_pos, split = apply_fill(current, signed_base, fill.fill_price)
        realized = quantize_money(split.realized_pnl)
        entries: list[Entry] = []
        if realized != 0:
            entries.append(
                Entry(
                    account_name(ACCOUNT_REALIZED, spec.settle_ccy), spec.settle_ccy, -realized, spec.inst_id
                )
            )
        fee = ZERO
        if self.fee_source is FeeSource.PER_FILL and fill.fee_cashflow != 0:
            fee = quantize_money(fill.fee_cashflow)  # signé : négatif = débit, positif = rebate
            entries.append(Entry(account_name(ACCOUNT_FEES, fill.fee_ccy), fill.fee_ccy, -fee, spec.inst_id))
        meta = {
            "inst_id": spec.inst_id,
            "base_ccy": spec.base_ccy,
            "settle_ccy": spec.settle_ccy,
            "signed_base_qty": format(signed_base, "f"),
            "price": format(fill.fill_price, "f"),
            "contracts": format(fill.contracts, "f"),
            "side": fill.side.value,
            "liquidity": fill.liquidity.value,
            "reduce_qty": format(split.reduce_qty, "f"),
            "open_qty": format(split.open_qty, "f"),
            "flipped": split.flipped,
            "fee_source": self.fee_source.value,
        }
        txn = self._commit(
            kind=kind,
            idempotency_key=key,
            occurred_at=occurred,
            entries=entries,
            description=f"{kind.value} {fill.side.value} {fill.contracts} {spec.inst_id} @ {fill.fill_price}",
            metadata=meta,
            fill=fill,
        )
        if txn.applied:
            self._state.positions[spec.inst_id] = new_pos
        return RecordedTransaction(
            txn.txn_id, kind, key, occurred, txn.entries, txn.applied, realized, fee, split
        )

    def record_order_commission(
        self,
        *,
        client_order_id: str,
        fee_cashflow: Decimal,
        fee_ccy: str,
        occurred_at: datetime,
        inst_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RecordedTransaction:
        """Commission AGRÉGÉE par ordre : refusée si la source déclarée est ``PER_FILL`` (pas de double comptage)."""
        if self.fee_source is not FeeSource.PER_ORDER:
            raise LedgerError(
                "commission par ordre refusée : la source déclarée des commissions est per_fill",
                client_order_id=client_order_id,
            )
        amount = quantize_money(dec(fee_cashflow, field="fee_cashflow"))
        key = idempotency_key or f"commission:{client_order_id}"
        return self._simple(
            TxnKind.COMMISSION,
            key,
            occurred_at,
            ACCOUNT_FEES,
            fee_ccy,
            amount,
            inst_id,
            f"commission ordre {client_order_id}",
        )

    def record_rebate(
        self,
        *,
        amount: Decimal,
        ccy: str,
        occurred_at: datetime,
        idempotency_key: str,
        inst_id: str | None = None,
    ) -> RecordedTransaction:
        """Rebate observé comme bill distinct (positif = crédit). Un rebate négatif n'existe pas."""
        value = quantize_money(dec(amount, field="amount"))
        if value <= 0:
            raise LedgerError("un rebate est strictement positif", amount=str(value))
        return self._simple(
            TxnKind.REBATE, idempotency_key, occurred_at, ACCOUNT_FEES, ccy, value, inst_id, "rebate"
        )

    def record_funding(
        self,
        *,
        inst_id: str,
        cashflow: Decimal,
        settlement_at: datetime,
        idempotency_key: str | None = None,
        ccy: str | None = None,
        realized_rate: Decimal | None = None,
        signed_notional: Decimal | None = None,
    ) -> RecordedTransaction:
        """Règlement de funding réellement traversé (montant signé : négatif = payé)."""
        settled = ensure_utc(settlement_at, field="settlement_at")
        key = idempotency_key or f"funding:{inst_id}:{int(settled.timestamp() * 1000)}"
        amount = quantize_money(dec(cashflow, field="cashflow"))
        meta: dict[str, Any] = {"inst_id": inst_id}
        if realized_rate is not None:
            meta["realized_rate"] = format(realized_rate, "f")
        if signed_notional is not None:
            meta["signed_notional_at_settlement"] = format(signed_notional, "f")
        return self._simple(
            TxnKind.FUNDING,
            key,
            settled,
            ACCOUNT_FUNDING,
            ccy or self.settle_ccy,
            amount,
            inst_id,
            f"funding {inst_id}",
            meta,
        )

    def record_external_cashflow(
        self,
        *,
        amount: Decimal,
        occurred_at: datetime,
        idempotency_key: str,
        description: str = "transfert externe observé",
        ccy: str | None = None,
    ) -> RecordedTransaction:
        """Dépôt (+) / retrait (−) / transfert observé : flux EXTERNE, neutralisé dans le PnL de stratégie."""
        value = quantize_money(dec(amount, field="amount"))
        if value == 0:
            raise LedgerError("flux externe nul", idempotency_key=idempotency_key)
        return self._simple(
            TxnKind.EXTERNAL,
            idempotency_key,
            occurred_at,
            ACCOUNT_EXTERNAL,
            ccy or self.settle_ccy,
            value,
            None,
            description,
        )

    def record_correction(
        self,
        *,
        amount: Decimal,
        occurred_at: datetime,
        idempotency_key: str,
        description: str,
        ccy: str | None = None,
    ) -> RecordedTransaction:
        """Correction observée (bill d'ajustement de l'exchange, réconciliation) : jamais une réécriture."""
        value = quantize_money(dec(amount, field="amount"))
        return self._simple(
            TxnKind.CORRECTION,
            idempotency_key,
            occurred_at,
            ACCOUNT_ADJUSTMENTS,
            ccy or self.settle_ccy,
            value,
            None,
            description,
        )

    def record_margin_adjustment(
        self,
        *,
        inst_id: str,
        amount: Decimal,
        occurred_at: datetime,
        idempotency_key: str,
        ccy: str | None = None,
    ) -> RecordedTransaction:
        """Ajustement de marge OBSERVÉ avec effet de trésorerie (ex. bill d'ajustement isolé).

        Un simple transfert entre solde libre et marge isolée ne change pas l'equity et n'est pas
        enregistré ici : seul un flux qui modifie le collatéral total l'est.
        """
        value = quantize_money(dec(amount, field="amount"))
        return self._simple(
            TxnKind.MARGIN_ADJUSTMENT,
            idempotency_key,
            occurred_at,
            ACCOUNT_ADJUSTMENTS,
            ccy or self.settle_ccy,
            value,
            inst_id,
            f"ajustement de marge {inst_id}",
        )

    # --- audit ---------------------------------------------------------------------------------------

    def audit(self) -> LedgerAudit:
        """Relit la base : chaque transaction est équilibrée par devise, cash = Σ contributions, clés uniques."""
        with self._factory() as session:
            txns = list(
                session.execute(
                    select(LedgerTransaction).where(LedgerTransaction.account_scope == self.account_scope)
                ).scalars()
            )
            entries = list(
                session.execute(
                    select(LedgerEntry)
                    .join(LedgerTransaction, LedgerEntry.txn_id == LedgerTransaction.txn_id)
                    .where(LedgerTransaction.account_scope == self.account_scope)
                ).scalars()
            )
        sums: dict[tuple[str, str], Decimal] = {}
        cash_by_ccy: dict[str, Decimal] = {}
        others_by_ccy: dict[str, Decimal] = {}
        for e in entries:
            sums[(e.txn_id, e.ccy)] = sums.get((e.txn_id, e.ccy), ZERO) + e.amount
            target = cash_by_ccy if e.account == account_name(ACCOUNT_CASH, e.ccy) else others_by_ccy
            target[e.ccy] = target.get(e.ccy, ZERO) + e.amount
        unbalanced = tuple(sorted({txn for (txn, _), total in sums.items() if total != 0}))
        seen: set[str] = set()
        dups: list[str] = []
        for t in txns:
            if t.idempotency_key in seen:
                dups.append(t.idempotency_key)
            seen.add(t.idempotency_key)
        currencies = set(cash_by_ccy) | set(others_by_ccy)
        cash_ok = all(cash_by_ccy.get(c, ZERO) == -others_by_ccy.get(c, ZERO) for c in currencies)
        return LedgerAudit(
            transactions=len(txns),
            entries=len(entries),
            balanced=not unbalanced,
            unbalanced_txn_ids=unbalanced,
            cash_matches_contributions=cash_ok,
            duplicate_keys=tuple(dups),
        )

    # --- internes ------------------------------------------------------------------------------------

    def _already(self, kind: TxnKind, key: str, occurred: datetime) -> RecordedTransaction:
        return RecordedTransaction(txn_id_for(self.account_scope, key), kind, key, occurred, (), False)

    def _simple(
        self,
        kind: TxnKind,
        key: str,
        occurred_at: datetime,
        counterpart: str,
        ccy: str,
        cash_amount: Decimal,
        inst_id: str | None,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> RecordedTransaction:
        occurred = ensure_utc(occurred_at, field="occurred_at")
        if key in self._state.keys:
            return self._already(kind, key, occurred)
        entries = [Entry(account_name(counterpart, ccy), ccy, -cash_amount, inst_id)]
        txn = self._commit(
            kind=kind,
            idempotency_key=key,
            occurred_at=occurred,
            entries=entries,
            description=description,
            metadata=metadata or {},
            fill=None,
        )
        return RecordedTransaction(txn.txn_id, kind, key, occurred, txn.entries, txn.applied)

    def _commit(
        self,
        *,
        kind: TxnKind,
        idempotency_key: str,
        occurred_at: datetime,
        entries: list[Entry],
        description: str,
        metadata: dict[str, Any],
        fill: Fill | None,
    ) -> RecordedTransaction:
        """Complète chaque devise par l'écriture ``cash`` opposée, vérifie l'équilibre, persiste, applique."""
        by_ccy: dict[str, Decimal] = {}
        for e in entries:
            by_ccy[e.ccy] = by_ccy.get(e.ccy, ZERO) + e.amount
        full = list(entries)
        for ccy, total in by_ccy.items():
            if total != 0:
                full.append(Entry(account_name(ACCOUNT_CASH, ccy), ccy, -total, None))
        check: dict[str, Decimal] = {}
        for e in full:
            check[e.ccy] = check.get(e.ccy, ZERO) + e.amount
        if any(v != 0 for v in check.values()):  # pragma: no cover - garde-fou
            raise LedgerError("transaction non équilibrée", key=idempotency_key)
        txn_id = txn_id_for(self.account_scope, idempotency_key)
        recorded_at = self._clock.now_utc()
        try:
            with self._factory() as session:
                existing = session.execute(
                    select(LedgerTransaction.txn_id).where(
                        LedgerTransaction.account_scope == self.account_scope,
                        LedgerTransaction.idempotency_key == idempotency_key,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    self._state.keys.add(idempotency_key)
                    return RecordedTransaction(existing, kind, idempotency_key, occurred_at, (), False)
                fill_key: str | None = None
                if fill is not None:
                    fill_key = fill.execution_key
                    if session.get(FillRow, fill_key) is None:
                        session.add(_fill_row(fill))
                session.add(
                    LedgerTransaction(
                        txn_id=txn_id,
                        account_scope=self.account_scope,
                        kind=kind.value,
                        idempotency_key=idempotency_key,
                        fill_key=fill_key,
                        occurred_at=occurred_at,
                        recorded_at=recorded_at,
                        description=description[:256],
                        metadata_=metadata,
                    )
                )
                for e in full:
                    session.add(
                        LedgerEntry(
                            txn_id=txn_id, account=e.account, ccy=e.ccy, amount=e.amount, inst_id=e.inst_id
                        )
                    )
                session.commit()
        except IntegrityError:
            # Course entre deux writers : la clé a été insérée entre la lecture et l'écriture.
            self._state.keys.add(idempotency_key)
            return RecordedTransaction(txn_id, kind, idempotency_key, occurred_at, (), False)
        self._state.keys.add(idempotency_key)
        for e in full:
            self._state.balances[e.account] = self._state.balances.get(e.account, ZERO) + e.amount
        return RecordedTransaction(txn_id, kind, idempotency_key, occurred_at, tuple(full), True)


def _fill_row(fill: Fill) -> FillRow:
    return FillRow(
        execution_key=fill.execution_key,
        account_scope=fill.account_scope,
        order_id=None,
        client_order_id=fill.client_order_id,
        exchange_order_id=fill.exchange_order_id,
        trade_id=fill.trade_id,
        inst_id=fill.inst_id,
        side=fill.side.value,
        contracts=fill.contracts,
        fill_price=fill.fill_price,
        fee_cashflow=fill.fee_cashflow,
        fee_ccy=fill.fee_ccy,
        liquidity=fill.liquidity.value,
        fill_at=fill.fill_at,
        receive_ts=fill.receive_ts,
    )
