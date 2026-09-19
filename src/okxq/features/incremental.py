"""Moteur INCRÉMENTAL (direct) : consomme les enveloppes une à une et calcule aux coupures demandées.

Mêmes règles de fenêtrage que ``okxq.features.batch`` (parité T16) :
- carnet : ``LiveBook`` partagé ; historique = états VALIDES dont ``ts ∈ (cutoff-300 s, cutoff]`` ;
- trades : ``ts ∈ (cutoff-300 s, cutoff]`` ; bougies : clôturées, ``open+60 <= cutoff``, dernière version
  disponible par ouverture, 61 dernières ; funding : 16 derniers reçus ; OI : ``ts > cutoff-4200 s``.

En REJEU (défaut), le moteur refuse de calculer si un événement ingéré est disponible APRÈS la
coupure (``CausalityError``) : l'appelant contrôle l'ordre ``(available_at, ingest_seq)``, et un
événement postérieur signale un défaut du jeu de données.

En MARCHE CONTINUE (``flux_continu=True``), ce refus est levé — et lui seul. La tâche de drainage
ingère en permanence : des événements postérieurs à la coupure sont toujours déjà entrés quand la
frontière de minute demande son calcul, et aucune coupure ne peut satisfaire les deux. Ce que ce
refus constatait n'était pas « j'ai utilisé des données trop récentes » mais « j'en ai en mémoire ».

La garantie point-in-time reste portée par ce qui la porte vraiment, dans les deux cas :
la SÉLECTION (``_state`` ne retient que ``ts <= cutoff``, carnet compris) et la VÉRIFICATION de
l'état construit (``PointInTimeMarketState.__post_init__`` lève si quoi que ce soit dépasse).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import CausalityError
from okxq.domain.events import EventEnvelope, JevEvaluation
from okxq.features.batch import (
    CANDLE_BARS,
    FUNDING_KEEP,
    HISTORY_WINDOW_S,
    OI_LOOKBACK_S,
    TRADE_LOOKBACK_S,
)
from okxq.features.book import LiveBook, history_point
from okxq.features.market_events import (
    BookRec,
    CandleRec,
    FundingRec,
    InstrumentRec,
    OiRec,
    PriceRec,
    TradeRec,
    parse_envelope,
    sort_key,
)
from okxq.features.registry import FeatureComputation, FeatureEngine
from okxq.features.state import (
    AnnouncementMeta,
    BookState,
    Candle,
    CandleWindow,
    FundingRecord,
    MidHistory,
    OiWindow,
    PointInTimeMarketState,
    PricePoint,
    TradeWindow,
    empty_candles,
    empty_trades,
)

CANDLE_RETENTION_S = 4 * CANDLE_BARS * 60


#: Combien de valeurs récentes garder pour les champs qui n'existaient qu'en UN exemplaire (la
#: dernière reçue). Sur un flux, la dernière reçue est postérieure à la coupure : sans un peu
#: d'historique, il n'y a RIEN à retenir à la coupure et le prix de marque disparaît des features.
#: 64 valeurs couvrent largement l'écart entre la frontière de minute et l'instant du calcul.
DERNIERES_VALEURS = 64


@dataclass
class _Buffers:
    v: float = 1.0
    book: LiveBook | None = None
    book_states: deque[tuple[float, BookState, tuple[float, float, float] | None]] = field(
        default_factory=deque
    )
    trades: deque[tuple[float, float, float, int]] = field(default_factory=deque)
    candles: dict[float, CandleRec] = field(default_factory=dict)
    # Historiques courts, et non « dernière valeur » : il faut pouvoir répondre « laquelle était
    # disponible À la coupure ? », pas seulement « laquelle est la plus récente ? ».
    intrabars: deque[CandleRec] = field(default_factory=lambda: deque(maxlen=DERNIERES_VALEURS))
    marks: deque[PriceRec] = field(default_factory=lambda: deque(maxlen=DERNIERES_VALEURS))
    indexes: deque[PriceRec] = field(default_factory=lambda: deque(maxlen=DERNIERES_VALEURS))
    funding: deque[FundingRecord] = field(default_factory=lambda: deque(maxlen=FUNDING_KEEP))
    oi: deque[OiRec] = field(default_factory=deque)

    def closed_candles(self, cutoff_s: float, bars: int = CANDLE_BARS) -> CandleWindow:
        opens = sorted(o for o in self.candles if o + 60.0 <= cutoff_s)[-bars:]
        if not opens:
            return empty_candles()
        recs = [self.candles[o] for o in opens]
        return CandleWindow(
            np.array(opens, dtype=float),
            np.array([c.open for c in recs]),
            np.array([c.high for c in recs]),
            np.array([c.low for c in recs]),
            np.array([c.close for c in recs]),
            np.array([c.volume for c in recs]),
            np.array([c.volume_quote for c in recs]),
            60,
        )

    def evict(self, cutoff_s: float) -> None:
        while self.trades and self.trades[0][0] <= cutoff_s - TRADE_LOOKBACK_S:
            self.trades.popleft()
        while self.oi and self.oi[0].ts.timestamp() <= cutoff_s - OI_LOOKBACK_S:
            self.oi.popleft()
        # l'historique de carnet est trié par réception ; on n'évince que le préfixe hors fenêtre
        while self.book_states and self.book_states[0][0] <= cutoff_s - HISTORY_WINDOW_S:
            self.book_states.popleft()
        for o in [o for o in self.candles if o < cutoff_s - CANDLE_RETENTION_S]:
            del self.candles[o]


def _dernier_disponible[T: (CandleRec, PriceRec)](valeurs: deque[T], cutoff_s: float) -> T | None:
    """Le dernier enregistrement DISPONIBLE à la coupure, ou ``None`` s'il n'y en a aucun.

    « Disponible » se lit sur ``available_at`` : c'est l'instant où l'information était connue. Un
    relevé dont l'horodatage d'échange précède la coupure mais qui n'est arrivé qu'après ne peut pas
    servir — l'utiliser serait exactement l'anticipation qu'on interdit.

    Rendre ``None`` plutôt que se rabattre sur la valeur la plus récente est délibéré : décider sans
    prix de marque est une dégradation visible et bornée ; décider avec un prix du futur est une
    faute silencieuse qui contamine tout ce qui en découle.
    """
    for rec in reversed(valeurs):
        if rec.available_at.timestamp() <= cutoff_s:
            return rec
    return None


def _point_prix(rec: PriceRec | None) -> PricePoint | None:
    return PricePoint(rec.ts, rec.available_at, rec.value) if rec is not None else None


class IncrementalFeatureEngine:
    def __init__(self, engine: FeatureEngine, *, max_levels: int = 5, flux_continu: bool = False) -> None:
        """``flux_continu`` : le moteur est alimenté par un flux qui ne s'arrête pas.

        En REJEU on contrôle l'ingestion : rien n'entre après la coupure qu'on va demander, et un
        événement postérieur signale un défaut du jeu de données ou de l'ordre de lecture. Le refus
        est alors la bonne réponse, et c'est le défaut.

        En MARCHE CONTINUE, la tâche de drainage ingère en permanence : au moment où la frontière de
        minute demande son calcul, des événements postérieurs à la coupure sont FORCÉMENT déjà
        entrés. Aucune valeur de coupure ne satisfait à la fois ce refus et la frontière — la
        plateforme rendait donc `CAUSALITY_VIOLATION` à chaque minute, indéfiniment.

        Ce drapeau ne relâche PAS la garantie point-in-time. Ce qui la porte, c'est la SÉLECTION
        (`_state` ne retient que `ts <= cutoff`) et la VÉRIFICATION de l'état construit
        (`PointInTimeState.__post_init__` lève encore si quoi que ce soit dépasse la coupure). Le
        refus levé ici ne disait pas « j'ai utilisé des données trop récentes » mais « j'en ai en
        mémoire » — ce qui, sur un flux, est vrai en permanence et n'apprend rien.
        """
        self.engine = engine
        self.max_levels = max_levels
        self.flux_continu = flux_continu
        self._buf: dict[str, _Buffers] = {}
        self._last_available_at: datetime | None = None
        self._last_cutoff: datetime | None = None
        self.ingested = 0

    @property
    def instruments(self) -> list[str]:
        return sorted(self._buf)

    def ingest(self, env: EventEnvelope) -> None:
        if self._last_available_at is not None and env.available_at < self._last_available_at:
            raise CausalityError(
                "événement ingéré hors ordre available_at", event_id=env.event_id, event_type=env.event_type
            )
        self._last_available_at = env.available_at
        rec = parse_envelope(env)
        if rec is None:
            return
        self.ingested += 1
        b = self._buf.setdefault(rec.inst_id, _Buffers())
        if isinstance(rec, InstrumentRec):
            b.v = rec.base_units_per_contract
        elif isinstance(rec, BookRec):
            if b.book is None:
                b.book = LiveBook(rec.inst_id, max_levels=self.max_levels)
            st = b.book.apply(rec)
            if st is not None:
                b.book_states.append((st.ts.timestamp(), st, history_point(st, b.v)))
        elif isinstance(rec, TradeRec):
            b.trades.append((rec.ts.timestamp(), rec.price, rec.qty, rec.side_sign))
        elif isinstance(rec, CandleRec):
            if rec.confirmed:
                b.candles[rec.open_ts.timestamp()] = rec
            else:
                b.intrabars.append(rec)
        elif isinstance(rec, PriceRec):
            if rec.kind == "mark":
                b.marks.append(rec)
            else:
                b.indexes.append(rec)
        elif isinstance(rec, FundingRec):
            b.funding.append(rec.record)
        elif isinstance(rec, OiRec):
            b.oi.append(rec)

    def _state(
        self,
        inst: str,
        cutoff: datetime,
        eligible: Sequence[str],
        peers: Sequence[str],
        announcements: Sequence[AnnouncementMeta],
        announcement_feed_available: bool,
        jev_evaluations: Sequence[JevEvaluation],
    ) -> PointInTimeMarketState:
        cs = cutoff.timestamp()
        b = self._buf.setdefault(inst, _Buffers())
        b.evict(cs)
        hist_pts = sorted(
            ((ts, h) for ts, _st, h in b.book_states if h is not None and cs - HISTORY_WINDOW_S < ts <= cs),
            key=lambda t: t[0],
        )
        # Le carnet VIVANT reflète la dernière mise à jour reçue, qui sur un flux est postérieure à
        # la coupure. Le prendre tel quel faisait échouer la vérification de l'état construit
        # (« book disponible après la coupure ») — à juste titre. On reprend donc son état AU PLUS
        # TARD à la coupure, que `book_states` conserve déjà.
        #
        # L'ordre des deux branches préserve le rejeu à l'identique : là, le carnet vivant est par
        # construction antérieur ou égal à la coupure, et c'est lui qui est retenu — y compris quand
        # sa dernière mise à jour est trop ancienne pour figurer encore dans `book_states`.
        book = None
        if b.book is not None:
            vivant = b.book.state()
            if vivant is not None and vivant.ts.timestamp() <= cs:
                book = vivant
            else:
                for ts_st, st, _h in reversed(b.book_states):
                    if ts_st <= cs:
                        book = st
                        break
        if book is not None:
            arr = np.array([[ts, h[0], h[1], h[2]] for ts, h in hist_pts], dtype=float).reshape(-1, 4)
            history: MidHistory | None = MidHistory(arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3])
        else:
            history = None
        tr = sorted((t for t in b.trades if cs - TRADE_LOOKBACK_S < t[0] <= cs), key=lambda t: t[0])
        trades = (
            TradeWindow(
                np.array([t[0] for t in tr]),
                np.array([t[1] for t in tr]),
                np.array([t[2] for t in tr]),
                np.array([float(t[3]) for t in tr]),
            )
            if tr
            else empty_trades()
        )
        # Bougie en cours, prix de marque et prix d'indice : la DERNIÈRE reçue est postérieure à la
        # coupure sur un flux. On retient la dernière DISPONIBLE à la coupure. En rejeu, rien n'a été
        # ingéré au-delà, donc c'est la dernière tout court : comportement inchangé.
        ib = _dernier_disponible(b.intrabars, cs)
        intrabar: Candle | None = None
        if ib is not None and ib.open_ts.timestamp() <= cs < ib.open_ts.timestamp() + 60:
            intrabar = Candle(
                ib.open_ts,
                ib.available_at,
                ib.open,
                ib.high,
                ib.low,
                ib.close,
                ib.volume,
                ib.volume_quote,
                False,
            )
        # Borne HAUTE sur l'open interest : elle manquait. Seule la borne basse était posée, si bien
        # qu'un relevé postérieur à la coupure entrait dans l'état — et la vérification le rejetait.
        oi_recs = sorted((o for o in b.oi if cs - OI_LOOKBACK_S < o.ts.timestamp() <= cs), key=lambda o: o.ts)
        oi = (
            OiWindow(
                np.array([o.ts.timestamp() for o in oi_recs]), np.array([o.oi_contracts for o in oi_recs])
            )
            if oi_recs
            else None
        )
        return PointInTimeMarketState(
            instrument=inst,
            cutoff_at=cutoff,
            book=book,
            book_history=history,
            trades=trades,
            closed_candles=b.closed_candles(cs),
            intrabar=intrabar,
            mark=_point_prix(_dernier_disponible(b.marks, cs)),
            index=_point_prix(_dernier_disponible(b.indexes, cs)),
            # Le financement était repris en entier : un versement annoncé après la coupure y entrait.
            funding=[f for f in b.funding if f.available_at <= cutoff],
            open_interest=oi,
            announcements=[a for a in announcements if a.available_at <= cutoff],
            announcement_feed_available=announcement_feed_available,
            jev_evaluations=[
                e
                for e in jev_evaluations
                if e.features_committed_at is not None and e.features_committed_at <= cutoff
            ],
            peers={p: self._buf.setdefault(p, _Buffers()).closed_candles(cs) for p in peers if p != inst},
            eligible_instruments=list(eligible),
            base_units_per_contract=b.v,
        )

    def compute(
        self,
        inst: str,
        cutoff: datetime,
        *,
        eligible: Sequence[str] | None = None,
        peers: Sequence[str] | None = None,
        announcements: Sequence[AnnouncementMeta] = (),
        announcement_feed_available: bool = False,
        jev_evaluations: Sequence[JevEvaluation] = (),
        available_at: datetime | None = None,
    ) -> FeatureComputation:
        cutoff = ensure_utc(cutoff)
        if not self.flux_continu and self._last_available_at is not None and self._last_available_at > cutoff:
            raise CausalityError(
                "événement disponible après la coupure déjà ingéré : calcul refusé", cutoff=cutoff.isoformat()
            )
        if self._last_cutoff is not None and cutoff < self._last_cutoff:
            raise CausalityError("les coupures doivent être croissantes", cutoff=cutoff.isoformat())
        self._last_cutoff = cutoff
        insts = list(peers) if peers is not None else self.instruments
        state = self._state(
            inst,
            cutoff,
            list(eligible) if eligible is not None else insts,
            insts,
            announcements,
            announcement_feed_available,
            jev_evaluations,
        )
        return self.engine.compute(state, available_at=available_at)


def run_incremental(
    engine: FeatureEngine,
    events: Iterable[EventEnvelope],
    cutoffs: Sequence[datetime],
    *,
    instruments: Sequence[str] | None = None,
) -> list[FeatureComputation]:
    """Pilote de replay : ingère dans l'ordre ``(available_at, ingest_seq)`` et calcule à chaque coupure."""
    inc = IncrementalFeatureEngine(engine)
    ordered = sorted(events, key=sort_key)
    pending = sorted(ensure_utc(c) for c in cutoffs)
    out: list[FeatureComputation] = []
    i = 0

    def flush(upto: datetime | None) -> None:
        nonlocal i
        while i < len(pending) and (upto is None or pending[i] < upto):
            insts = list(instruments) if instruments is not None else inc.instruments
            for inst in insts:
                out.append(inc.compute(inst, pending[i], eligible=insts, peers=insts))
            i += 1

    for env in ordered:
        flush(env.available_at)
        inc.ingest(env)
    flush(None)
    return out
