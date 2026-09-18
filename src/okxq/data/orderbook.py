"""Carnet local et validateurs de séquence par canal (§4, §47, T05–T13).

Correction datée reprise du changelog OKX du 23 juin 2026 : le checksum des canaux ``books``,
``books-l2-tbt`` et ``books50-l2-tbt`` est **déprécié et fixé à zéro**. Pour ces flux, la continuité
``seqId``/``prevSeqId`` fait foi et aucun CRC n'est calculé sur ce zéro (T10). À revalider à
l'implémentation : https://www.okx.com/docs-v5/log_en/ — vérifié le 18 septembre 2026.

Règles appliquées, et pourquoi chacune existe :

- ``prevSeqId`` doit égaler le dernier ``seqId`` retenu ; les identifiants **ne sont pas** consécutifs
  de un, donc ``new == old + 1`` serait un faux rejet (T07) ;
- ``prevSeqId == seqId`` est un message de MAINTIEN : la connexion est vivante, le prix n'a pas changé.
  Confondre les deux ferait passer un marché calme pour une panne, ou l'inverse (T08) ;
- un ``seqId`` qui repart en arrière est un RESET documenté : le carnet est invalidé et un nouveau
  snapshot est exigé, jamais raccordé au précédent (T09) ;
- une quantité publiée REMPLACE le niveau ; ``0`` le supprime — ce n'est pas un delta à additionner (T11) ;
- carnet croisé, quantité négative ou non finie, prix non positif : le carnet devient INVALIDE et les
  décisions le concernant s'arrêtent, sans imputation silencieuse (T12) ;
- une nouvelle version n'est publiée qu'après validation ATOMIQUE : l'application se fait sur une copie,
  et un rejet laisse la version précédente intacte ;
- un snapshot REST et des incréments WebSocket ne sont PAS raccordables : la fusion est refusée (T13).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from okxq.domain.errors import BookInvalidError, DataQualityError, SequenceGapError
from okxq.exchange.okx.mappings import BOOK_CHANNEL_DEPTH, SEQUENCED_BOOK_CHANNELS

__all__ = [
    "BookAction",
    "BookChannelProfile",
    "BookSnapshotView",
    "BookState",
    "BookValidator",
    "IngestResult",
    "OrderBook",
    "channel_profile",
    "parse_level",
]

Level = tuple[Decimal, Decimal]

#: Facteur de tolérance mémoire : un canal annoncé à 400 niveaux ne doit pas en accumuler 4 000.
MEMORY_FACTOR: Final = 2
CHECKSUM_DEPRECATED_SINCE: Final = "2026-06-23"


class BookState(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"  # aucun snapshot reçu
    VALID = "VALID"
    INVALID = "INVALID"  # données incohérentes : décisions bloquées
    RESYNC_REQUIRED = "RESYNC_REQUIRED"  # trou ou reset : un nouveau snapshot est exigé


class BookAction(StrEnum):
    SNAPSHOT = "snapshot"
    UPDATE = "update"
    HEARTBEAT = "heartbeat"  # message sans changement (prevSeqId == seqId)


@dataclass(frozen=True, slots=True)
class BookChannelProfile:
    """Contrat d'un canal de carnet : ce qu'il garantit, et ce qu'il ne garantit pas."""

    name: str
    depth: int
    sequenced: bool
    snapshot_per_message: bool
    checksum_deprecated: bool

    @property
    def max_levels(self) -> int:
        return self.depth * MEMORY_FACTOR


def channel_profile(channel: str) -> BookChannelProfile:
    """Profil d'un canal de carnet OKX. Un canal inconnu est refusé, jamais deviné."""
    if channel not in BOOK_CHANNEL_DEPTH:
        raise DataQualityError(f"canal de carnet inconnu : {channel!r}", channel=channel)
    sequenced = channel in SEQUENCED_BOOK_CHANNELS
    return BookChannelProfile(
        name=channel,
        depth=BOOK_CHANNEL_DEPTH[channel],
        sequenced=sequenced,
        # books5 et bbo-tbt republient l'intégralité du top N à chaque message : pas de séquence à suivre.
        snapshot_per_message=not sequenced,
        checksum_deprecated=sequenced,
    )


def parse_level(raw_price: Any, raw_qty: Any, *, inst_id: str) -> Level:
    """Convertit un niveau. Refuse NaN, infini, prix non positif et quantité négative (T12)."""
    try:
        price = Decimal(str(raw_price))
        qty = Decimal(str(raw_qty))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BookInvalidError(
            "niveau de carnet non numérique", inst_id=inst_id, level=repr((raw_price, raw_qty))
        ) from exc
    if not price.is_finite() or not qty.is_finite():
        raise BookInvalidError("niveau de carnet non fini (NaN/inf)", inst_id=inst_id)
    if price <= 0:
        raise BookInvalidError("prix de carnet non positif", inst_id=inst_id, price=str(price))
    if qty < 0:
        raise BookInvalidError("quantité de carnet négative", inst_id=inst_id, qty=str(qty))
    return price, qty


@dataclass(frozen=True, slots=True)
class BookSnapshotView:
    """Version publiée, immuable : ce que les features et l'exécution peuvent lire."""

    inst_id: str
    channel: str
    version: int
    state: BookState
    seq_id: int | None
    exchange_ts: datetime | None
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @property
    def usable(self) -> bool:
        return self.state is BookState.VALID and bool(self.bids) and bool(self.asks)

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def microprice(self) -> Decimal | None:
        """``(a*q_b + b*q_a) / (q_b + q_a)`` (§35). Indéfini si une quantité de tête est nulle."""
        if not self.bids or not self.asks:
            return None
        (b, qb), (a, qa) = self.bids[0], self.asks[0]
        if qb + qa == 0:
            return None
        return (a * qb + b * qa) / (qb + qa)

    @property
    def relative_spread(self) -> Decimal | None:
        m = self.mid
        if m is None or m <= 0:
            return None
        return (self.asks[0][0] - self.bids[0][0]) / m

    def depth_within_bps(self, bps: Decimal | int) -> tuple[Decimal, Decimal] | None:
        """Quantités cumulées dans une bande de ± ``bps`` autour du mid (unité : contrats)."""
        m = self.mid
        if m is None:
            return None
        band = m * Decimal(bps) / Decimal(10_000)
        bid_qty = sum((q for p, q in self.bids if p >= m - band), Decimal(0))
        ask_qty = sum((q for p, q in self.asks if p <= m + band), Decimal(0))
        return bid_qty, ask_qty

    def imbalance_k(self, k: int) -> Decimal | None:
        """Déséquilibre sur les ``k`` premiers niveaux (§35). Indéfini si le total est nul."""
        if k <= 0:
            raise ValueError("k doit être positif")
        b = sum((q for _, q in self.bids[:k]), Decimal(0))
        a = sum((q for _, q in self.asks[:k]), Decimal(0))
        if b + a == 0:
            return None
        return (b - a) / (b + a)


@dataclass(slots=True)
class OrderBook:
    """Carnet local d'un instrument pour UN canal. L'écriture est atomique : copie, validation, publication."""

    inst_id: str
    channel: str = "books"
    profile: BookChannelProfile = field(init=False)
    state: BookState = BookState.UNINITIALIZED
    version: int = 0
    seq_id: int | None = None
    exchange_ts: datetime | None = None
    last_change_ts: datetime | None = None
    invalid_reason: str | None = None
    _bids: dict[Decimal, Decimal] = field(default_factory=dict, repr=False)
    _asks: dict[Decimal, Decimal] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.profile = channel_profile(self.channel)

    # --- lecture ----------------------------------------------------------------------------------------

    def bid_levels(self) -> tuple[Level, ...]:
        return tuple(sorted(self._bids.items(), key=lambda kv: kv[0], reverse=True))

    def ask_levels(self) -> tuple[Level, ...]:
        return tuple(sorted(self._asks.items(), key=lambda kv: kv[0]))

    def view(self) -> BookSnapshotView:
        return BookSnapshotView(
            inst_id=self.inst_id,
            channel=self.channel,
            version=self.version,
            state=self.state,
            seq_id=self.seq_id,
            exchange_ts=self.exchange_ts,
            bids=self.bid_levels(),
            asks=self.ask_levels(),
        )

    def age_of_last_change_seconds(self, now: datetime) -> float | None:
        """Ancienneté du dernier CHANGEMENT de prix — distincte de la santé de connexion (T08)."""
        if self.last_change_ts is None:
            return None
        return (now - self.last_change_ts).total_seconds()

    # --- écriture ---------------------------------------------------------------------------------------

    def invalidate(self, reason: str, *, resync: bool = True) -> None:
        self.state = BookState.RESYNC_REQUIRED if resync else BookState.INVALID
        self.invalid_reason = reason
        self._bids.clear()
        self._asks.clear()
        self.seq_id = None

    def apply_snapshot(
        self,
        bids: Iterable[Sequence[Any]],
        asks: Iterable[Sequence[Any]],
        *,
        seq_id: int | None,
        exchange_ts: datetime | None,
    ) -> BookSnapshotView:
        new_bids = dict(parse_level(p, q, inst_id=self.inst_id) for p, q, *_ in bids)
        new_asks = dict(parse_level(p, q, inst_id=self.inst_id) for p, q, *_ in asks)
        new_bids = {p: q for p, q in new_bids.items() if q > 0}
        new_asks = {p: q for p, q in new_asks.items() if q > 0}
        self._validate_candidate(new_bids, new_asks)
        changed = new_bids != self._bids or new_asks != self._asks
        self._bids, self._asks = new_bids, new_asks
        self.seq_id = seq_id
        self.exchange_ts = exchange_ts
        if changed:
            self.last_change_ts = exchange_ts
        self.state = BookState.VALID
        self.invalid_reason = None
        self.version += 1
        return self.view()

    def apply_update(
        self,
        bids: Iterable[Sequence[Any]],
        asks: Iterable[Sequence[Any]],
        *,
        seq_id: int | None,
        prev_seq_id: int | None,
        exchange_ts: datetime | None,
    ) -> tuple[BookSnapshotView, BookAction]:
        """Applique un incrément après contrôle de continuité. Lève sans modifier le carnet publié."""
        if self.state is not BookState.VALID:
            raise SequenceGapError(
                "update reçu sans carnet valide : snapshot requis",
                inst_id=self.inst_id,
                state=self.state.value,
            )
        if self.profile.sequenced:
            self._check_continuity(seq_id, prev_seq_id)
        # Copie : un rejet ne doit jamais laisser un carnet à moitié mis à jour.
        cand_bids, cand_asks = dict(self._bids), dict(self._asks)
        changed = False
        for side_levels, raws in ((cand_bids, bids), (cand_asks, asks)):
            for row in raws:
                price, qty = parse_level(row[0], row[1], inst_id=self.inst_id)
                if qty == 0:
                    if side_levels.pop(price, None) is not None:
                        changed = True
                else:
                    if side_levels.get(price) != qty:
                        changed = True
                    side_levels[price] = qty  # REMPLACEMENT, jamais une addition
        self._validate_candidate(cand_bids, cand_asks)
        self._bids, self._asks = cand_bids, cand_asks
        if seq_id is not None:
            self.seq_id = seq_id
        self.exchange_ts = exchange_ts
        if changed:
            self.last_change_ts = exchange_ts
            self.version += 1
            return self.view(), BookAction.UPDATE
        return self.view(), BookAction.HEARTBEAT

    def _check_continuity(self, seq_id: int | None, prev_seq_id: int | None) -> None:
        if seq_id is None or prev_seq_id is None:
            raise DataQualityError("canal séquencé sans seq_id/prev_seq_id", inst_id=self.inst_id)
        current = self.seq_id
        if current is None:
            raise SequenceGapError("état de séquence absent", inst_id=self.inst_id)
        if prev_seq_id == seq_id and seq_id == current:
            return  # maintien : rien à raccorder
        if seq_id < current:
            self.invalidate(f"reset de séquence ({seq_id} < {current})")
            raise SequenceGapError(
                "reset de séquence : nouveau snapshot requis",
                inst_id=self.inst_id,
                expected=current,
                got=seq_id,
            )
        if prev_seq_id != current:
            self.invalidate(f"trou de séquence (prev={prev_seq_id}, attendu {current})")
            raise SequenceGapError(
                "trou de séquence", inst_id=self.inst_id, expected=current, got=prev_seq_id
            )

    def _validate_candidate(self, bids: Mapping[Decimal, Decimal], asks: Mapping[Decimal, Decimal]) -> None:
        if bids and asks and max(bids) >= min(asks):
            reason = f"carnet croisé (bid {max(bids)} >= ask {min(asks)})"
            self.invalidate(reason, resync=True)
            raise BookInvalidError(reason, inst_id=self.inst_id)
        limit = self.profile.max_levels
        if len(bids) > limit or len(asks) > limit:
            reason = f"profondeur locale hors limite ({len(bids)}/{len(asks)} > {limit})"
            self.invalidate(reason, resync=True)
            raise BookInvalidError(reason, inst_id=self.inst_id)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Résultat d'un message de carnet : ce qui a été fait, et l'état publié après coup."""

    action: BookAction | None
    view: BookSnapshotView | None
    accepted: bool
    reason: str | None = None
    resync_required: bool = False


class BookValidator:
    """Validateur par canal et version de protocole. Un validateur par (instrument, canal).

    ``ingest`` ne lève jamais : un message fautif rend un ``IngestResult`` refusé et marque l'état. Les
    décisions consultent ``state``/``view`` ; c'est au risque de bloquer, pas au parseur.
    """

    def __init__(self, inst_id: str, channel: str = "books", *, source: str = "ws") -> None:
        self.book = OrderBook(inst_id=inst_id, channel=channel)
        self.source = source
        self.gaps = 0
        self.resets = 0
        self.invalid = 0
        self.heartbeats = 0
        self.updates = 0
        self.snapshots = 0
        self.checksum_ignored = 0

    @property
    def profile(self) -> BookChannelProfile:
        return self.book.profile

    @property
    def state(self) -> BookState:
        return self.book.state

    def view(self) -> BookSnapshotView:
        return self.book.view()

    def ingest(self, message: Mapping[str, Any], *, source: str = "ws") -> IngestResult:
        """Ingère un message normalisé ``{action, bids, asks, seq_id, prev_seq_id, checksum, ts}``.

        ``source`` distingue WS et REST : un snapshot REST ne peut PAS servir de base à des incréments
        WebSocket, et la tentative est refusée explicitement (T13).
        """
        action_raw = str(message.get("action", "update"))
        if self.profile.snapshot_per_message:
            action_raw = "snapshot"  # books5/bbo-tbt : chaque message est un top N complet
        checksum = message.get("checksum")
        if self.profile.checksum_deprecated and checksum in (0, "0", None):
            # Déprécié depuis le 23 juin 2026 : aucun CRC n'est calculé sur ce zéro.
            self.checksum_ignored += 1
        try:
            ts = _parse_ts(message.get("ts_ms") or message.get("ts"))
            if action_raw == "snapshot":
                if source != self.source and self.book.state is BookState.VALID:
                    return IngestResult(
                        None,
                        self.view(),
                        False,
                        "snapshot d'une autre source refusé (REST/WS non raccordables)",
                    )
                view = self.book.apply_snapshot(
                    message.get("bids", []),
                    message.get("asks", []),
                    seq_id=_parse_int(message.get("seq_id")),
                    exchange_ts=ts,
                )
                self.snapshots += 1
                return IngestResult(BookAction.SNAPSHOT, view, True)
            if source != self.source:
                return IngestResult(
                    None, self.view(), False, "incrément d'une autre source refusé (REST/WS non raccordables)"
                )
            view, action = self.book.apply_update(
                message.get("bids", []),
                message.get("asks", []),
                seq_id=_parse_int(message.get("seq_id")),
                prev_seq_id=_parse_int(message.get("prev_seq_id")),
                exchange_ts=ts,
            )
            if action is BookAction.HEARTBEAT:
                self.heartbeats += 1
            else:
                self.updates += 1
            return IngestResult(action, view, True)
        except SequenceGapError as exc:
            if "reset" in str(exc):
                self.resets += 1
            else:
                self.gaps += 1
            return IngestResult(None, self.view(), False, str(exc), resync_required=True)
        except (BookInvalidError, DataQualityError) as exc:
            self.invalid += 1
            return IngestResult(
                None,
                self.view(),
                False,
                str(exc),
                resync_required=self.book.state is BookState.RESYNC_REQUIRED,
            )

    def counters(self) -> dict[str, int]:
        return {
            "snapshots": self.snapshots,
            "updates": self.updates,
            "heartbeats": self.heartbeats,
            "gaps": self.gaps,
            "resets": self.resets,
            "invalid": self.invalid,
            "checksum_ignored": self.checksum_ignored,
        }


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise DataQualityError(f"entier attendu, reçu {value!r}") from exc


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise DataQualityError(f"horodatage invalide : {value!r}") from exc
