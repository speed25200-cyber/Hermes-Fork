"""Univers dynamique point-in-time (§3, T18).

Le but n'est pas de « choisir les vingt plus gros volumes » mais de répondre, à une date donnée et avec
l'information de cette date : sur quels instruments pouvons-nous EXÉCUTER notre taille sans impact
disproportionné, avec assez d'historique et assez de données valides ?

Points de vigilance inscrits dans le code :

- **Unité commune.** ``volCcy24h`` d'OKX est exprimé en devise de BASE : trier dessus compare des BTC à
  des DOGE, c'est-à-dire un nombre de pièces, pas de l'argent échangé. Le critère est donc
  ``volCcy24h × dernier prix`` (notionnel USDT).
- **Hystérésis.** Un instrument entre par un seuil et sort par un seuil plus lâche ; sans cela, un actif
  au bord du critère entre et sort à chaque rafraîchissement, et le portefeuille paie du turnover pour
  du bruit de classement.
- **Sortie ≠ disparition.** Un instrument retiré cesse de recevoir de NOUVELLES augmentations
  d'exposition ; s'il est détenu, il reste géré (reduce-only) et comptabilisé (T18).
- **Biais qualifié.** Si la reconstruction point-in-time n'est pas possible (métadonnées absentes à la
  date), la version d'univers porte un ``bias_note`` et le résultat ne peut pas servir de validation
  générale de l'univers dynamique.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from okxq.config.schema import UniverseCfg
from okxq.data.point_in_time import PointInTimeStore, UniverseVersion
from okxq.domain.clocks import ensure_utc
from okxq.domain.ids import new_id
from okxq.domain.instruments import InstrumentSpec, InstrumentState
from okxq.domain.money import dec

__all__ = ["CandidateMetrics", "UniverseBuilder", "UniverseDecision", "build_universe"]

#: Facteur d'hystérésis : un instrument déjà admis sort à 80 % du seuil d'entrée (volumes, profondeur)
#: et supporte 125 % du spread maximal. Choisi conservateur, documenté, pas ajusté sur un résultat.
HYSTERESIS_KEEP = Decimal("0.8")
HYSTERESIS_SPREAD = Decimal("1.25")


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    """Mesures observées d'un candidat, toutes dans une unité nommée."""

    inst_id: str
    notional_volume_24h_usdt: Decimal | None
    relative_spread: Decimal | None
    depth_notional_usdt: Decimal | None
    history_days: float | None
    missing_fraction: Decimal | None
    executable_notional_usdt: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "notional_volume_24h_usdt": _fmt(self.notional_volume_24h_usdt),
            "relative_spread": _fmt(self.relative_spread),
            "depth_notional_usdt": _fmt(self.depth_notional_usdt),
            "history_days": self.history_days,
            "missing_fraction": _fmt(self.missing_fraction),
            "executable_notional_usdt": _fmt(self.executable_notional_usdt),
        }


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


@dataclass(frozen=True, slots=True)
class UniverseDecision:
    """Verdict par candidat, ADMIS ou NON — un critère qui ne s'explique que sur les refus est à moitié aveugle."""

    inst_id: str
    admitted: bool
    reasons: tuple[str, ...]
    metrics: CandidateMetrics
    incumbent: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "inst_id": self.inst_id,
            "admitted": self.admitted,
            "incumbent": self.incumbent,
            "reasons": list(self.reasons),
            "metrics": self.metrics.as_dict(),
        }


class UniverseBuilder:
    """Construit une version d'univers à un cutoff, depuis un magasin point-in-time."""

    def __init__(self, cfg: UniverseCfg, *, target_order_notional_usdt: Decimal | None = None) -> None:
        self.cfg = cfg
        self.target_order_notional = target_order_notional_usdt

    # --- mesures ----------------------------------------------------------------------------------------

    def measure(
        self, store: PointInTimeStore, inst_id: str, spec: InstrumentSpec, cutoff: datetime
    ) -> CandidateMetrics:
        ticker = store.latest_event("ticker", inst_id, cutoff)
        last_price: Decimal | None = None
        volume_notional: Decimal | None = None
        if ticker is not None:
            last_raw = ticker.payload.get("last")
            base_vol_raw = ticker.payload.get("vol_ccy_24h_base")
            if last_raw:
                last_price = dec(str(last_raw), field="last")
            if last_price and base_vol_raw:
                # volCcy24h est en devise de BASE → notionnel = volume_base × prix.
                volume_notional = dec(str(base_vol_raw), field="vol_ccy_24h_base") * last_price

        book = store.latest_event("book.snapshot", inst_id, cutoff)
        updates = store.events_until("book.update", inst_id, cutoff, limit=1)
        latest_book = max(
            [e for e in ([book] if book else []) + list(updates)],
            key=lambda e: (e.available_at, e.ingest_seq),
            default=None,
        )
        spread: Decimal | None = None
        depth_notional: Decimal | None = None
        if latest_book is not None:
            bids = latest_book.payload.get("bids") or []
            asks = latest_book.payload.get("asks") or []
            if bids and asks:
                best_bid = max(dec(str(b[0])) for b in bids)
                best_ask = min(dec(str(a[0])) for a in asks)
                mid = (best_bid + best_ask) / 2
                if mid > 0 and best_ask > best_bid:
                    spread = (best_ask - best_bid) / mid
                    top_qty = sum(dec(str(b[1])) for b in bids) + sum(dec(str(a[1])) for a in asks)
                    depth_notional = top_qty * spec.base_units_per_contract * mid / 2

        first = None
        for event_type in ("candle.1m", "trade", "book.snapshot"):
            series = store.events_until(event_type, inst_id, cutoff)
            if series:
                first = series[0].available_at if first is None else min(first, series[0].available_at)
        history_days = None if first is None else (ensure_utc(cutoff) - first).total_seconds() / 86_400

        # Fraction manquante : bougies clôturées attendues vs reçues sur la fenêtre observée.
        missing_fraction: Decimal | None = None
        closed = [
            e for e in store.events_until("candle.1m", inst_id, cutoff) if e.payload.get("confirm") == "1"
        ]
        if len(closed) >= 2:
            span_minutes = (closed[-1].available_at - closed[0].available_at).total_seconds() / 60
            expected = int(span_minutes) + 1
            if expected > 0:
                missing = max(0, expected - len(closed))
                missing_fraction = Decimal(missing) / Decimal(expected)

        # Capacité exécutable observable = profondeur notionnelle disponible dans le carnet lu.
        executable = depth_notional
        return CandidateMetrics(
            inst_id=inst_id,
            notional_volume_24h_usdt=volume_notional,
            relative_spread=spread,
            depth_notional_usdt=depth_notional,
            history_days=history_days,
            missing_fraction=missing_fraction,
            executable_notional_usdt=executable,
        )

    # --- décision ---------------------------------------------------------------------------------------

    def judge(self, metrics: CandidateMetrics, spec: InstrumentSpec, *, incumbent: bool) -> UniverseDecision:
        cfg = self.cfg
        keep = HYSTERESIS_KEEP if incumbent else Decimal(1)
        spread_factor = HYSTERESIS_SPREAD if incumbent else Decimal(1)
        reasons: list[str] = []

        if spec.state is not InstrumentState.LIVE:
            reasons.append(f"instrument non négociable (état {spec.state.value})")
        if spec.contract_type != cfg.contract_type or spec.settle_ccy != cfg.settlement_currency:
            reasons.append("contrat hors convention linéaire USDT")

        min_volume = dec(cfg.min_notional_volume_24h_usdt, field="min_volume") * keep
        if metrics.notional_volume_24h_usdt is None:
            reasons.append("volume notionnel inconnu")
        elif metrics.notional_volume_24h_usdt < min_volume:
            reasons.append(
                f"volume notionnel {format(metrics.notional_volume_24h_usdt, 'f')} < {format(min_volume, 'f')} USDT"
            )

        max_spread = dec(str(cfg.max_relative_spread), field="max_spread") * spread_factor
        if metrics.relative_spread is None:
            reasons.append("spread inconnu (carnet absent ou invalide)")
        elif metrics.relative_spread > max_spread:
            reasons.append(
                f"spread relatif {format(metrics.relative_spread, 'f')} > {format(max_spread, 'f')}"
            )

        min_depth = dec(cfg.min_depth_notional_usdt, field="min_depth") * keep
        if metrics.depth_notional_usdt is None:
            reasons.append("profondeur inconnue")
        elif metrics.depth_notional_usdt < min_depth:
            reasons.append(
                f"profondeur {format(metrics.depth_notional_usdt, 'f')} < {format(min_depth, 'f')} USDT"
            )

        if cfg.minimum_history_days > 0:
            if metrics.history_days is None:
                reasons.append("historique disponible inconnu")
            elif metrics.history_days < cfg.minimum_history_days * float(keep):
                reasons.append(
                    f"historique {metrics.history_days:.2f} j < {cfg.minimum_history_days * float(keep):.2f} j"
                )

        if metrics.missing_fraction is not None and metrics.missing_fraction > dec(
            str(cfg.max_missing_fraction), field="max_missing"
        ):
            reasons.append(f"données manquantes {format(metrics.missing_fraction, 'f')} au-delà du seuil")

        if self.target_order_notional is not None and metrics.depth_notional_usdt is not None:
            if metrics.depth_notional_usdt < self.target_order_notional:
                reasons.append("profondeur insuffisante pour exécuter notre taille cible")

        return UniverseDecision(
            inst_id=metrics.inst_id,
            admitted=not reasons,
            reasons=tuple(reasons),
            metrics=metrics,
            incumbent=incumbent,
        )

    def build(
        self,
        store: PointInTimeStore,
        cutoff: datetime,
        *,
        incumbents: Sequence[str] = (),
        held: Sequence[str] = (),
    ) -> tuple[UniverseVersion, list[UniverseDecision]]:
        """Construit la version d'univers au cutoff et rend le verdict de CHAQUE candidat."""
        cutoff = ensure_utc(cutoff, field="cutoff")
        specs = store.instruments_at(cutoff)
        decisions: list[UniverseDecision] = []
        for inst_id, spec in sorted(specs.items()):
            metrics = self.measure(store, inst_id, spec, cutoff)
            decisions.append(self.judge(metrics, spec, incumbent=inst_id in set(incumbents)))

        admitted = [d for d in decisions if d.admitted]
        # Classement par argent échangé (unité commune), volume inconnu en dernier.
        admitted.sort(
            key=lambda d: d.metrics.notional_volume_24h_usdt or Decimal(0),
            reverse=True,
        )
        eligible = [d.inst_id for d in admitted[: self.cfg.maximum_eligible]]
        if len(eligible) > self.cfg.target_size:
            eligible = eligible[: self.cfg.target_size]

        bias: str | None = None
        if not specs:
            bias = "aucune métadonnée d'instrument disponible au cutoff : univers non reconstruit"
        elif len(eligible) < self.cfg.minimum_eligible:
            bias = (
                f"{len(eligible)} instrument(s) admissible(s) < minimum {self.cfg.minimum_eligible} : "
                "aucune décision d'augmentation d'exposition ne doit être prise sur cet univers"
            )

        held_only = tuple(sorted(set(held) - set(eligible)))
        version = UniverseVersion(
            universe_version=new_id("uni"),
            valid_from=cutoff,
            eligible=tuple(eligible),
            held_only=held_only,
            criteria={
                "min_notional_volume_24h_usdt": self.cfg.min_notional_volume_24h_usdt,
                "max_relative_spread": self.cfg.max_relative_spread,
                "min_depth_notional_usdt": self.cfg.min_depth_notional_usdt,
                "minimum_history_days": self.cfg.minimum_history_days,
                "max_missing_fraction": self.cfg.max_missing_fraction,
                "target_size": self.cfg.target_size,
                "hysteresis_keep": format(HYSTERESIS_KEEP, "f"),
                "volume_unit": "notionnel USDT = volCcy24h (base) × dernier prix",
                "candidates": len(decisions),
                "admitted": len(admitted),
            },
            bias_note=bias,
        )
        return version, decisions


def build_universe(
    cfg: UniverseCfg,
    store: PointInTimeStore,
    cutoff: datetime,
    *,
    incumbents: Sequence[str] = (),
    held: Sequence[str] = (),
    target_order_notional_usdt: Decimal | None = None,
) -> tuple[UniverseVersion, list[UniverseDecision]]:
    return UniverseBuilder(cfg, target_order_notional_usdt=target_order_notional_usdt).build(
        store, cutoff, incumbents=incumbents, held=held
    )


def rotation_report(previous: UniverseVersion | None, current: UniverseVersion) -> dict[str, Any]:
    """Entrées et sorties nommées : un univers qui change en silence rend les trades inexplicables."""
    before = set(previous.eligible) if previous else set()
    after = set(current.eligible)
    return {
        "universe_version": current.universe_version,
        "valid_from": current.valid_from.isoformat(),
        "entered": sorted(after - before),
        "left": sorted(before - after),
        "kept": sorted(after & before),
        "held_only": list(current.held_only),
        "bias_note": current.bias_note,
    }


def refresh_due(previous: UniverseVersion | None, now: datetime, cfg: UniverseCfg) -> bool:
    if previous is None:
        return True
    return ensure_utc(now) - previous.valid_from >= timedelta(seconds=cfg.refresh_interval_seconds)


def held_reduce_only(version: UniverseVersion) -> Mapping[str, str]:
    """Instruments détenus hors univers : gérables en reduce-only seulement, avec le motif."""
    return {
        inst: "sortie d'univers : réduction seule, position toujours comptabilisée"
        for inst in version.held_only
    }
