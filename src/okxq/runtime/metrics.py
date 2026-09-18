"""Métriques Prometheus (§61) : noms exacts, types explicites, labels à cardinalité BORNÉE.

Deux règles qui ont un coût si on les oublie : un label non borné (identifiant d'ordre, instrument
arbitraire, code d'erreur fournisseur) fait exploser la série temporelle et finit par tuer la collecte ;
et un secret ou un texte privé dans un label le publie à tout lecteur des métriques. ``bounded_label``
ramène donc toute valeur inconnue à ``other``.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

#: Noms exigés par le cahier des charges (§61), dans l'ordre du document.
METRIC_NAMES: tuple[str, ...] = (
    "market_data_age_seconds",
    "private_stream_health",
    "clock_offset_ms",
    "book_sequence_gaps_total",
    "book_invalid_total",
    "data_drops_total",
    "feature_compute_ms",
    "inference_ms",
    "optimizer_ms",
    "decision_deadline_miss_total",
    "order_ack_ms",
    "unknown_orders_total",
    "reconciliation_gap_usdt",
    "fill_ratio",
    "execution_shortfall_bps",
    "realized_fees_usdt",
    "portfolio_gross",
    "portfolio_net",
    "beta_exposure",
    "margin_utilization",
    "drawdown",
    "daily_loss",
    "halt_state",
    "unprotected_position_notional",
    "jev_latency_ms",
    "jev_error_total",
    "jev_cache_hit_ratio",
    "jev_input_tokens",
    "outbox_depth",
    "disk_free_bytes",
    "db_write_failures",
    "restart_count",
)

#: Valeurs autorisées par nom de label. Tout le reste devient ``other``.
ALLOWED_LABELS: dict[str, frozenset[str]] = {
    "channel": frozenset(
        {
            "books",
            "books5",
            "books-l2-tbt",
            "books50-l2-tbt",
            "bbo-tbt",
            "trades",
            "candle",
            "mark-price",
            "index-tickers",
            "funding-rate",
            "open-interest",
            "instruments",
            "orders",
            "positions",
            "account",
            "algo",
            "rest",
        }
    ),
    "reason": frozenset(
        {
            "gap",
            "reset",
            "crossed",
            "negative_qty",
            "nan",
            "stale",
            "checksum",
            "unknown_state",
            "timeout",
            "rejected",
            "http",
            "business",
            "deadline",
            "budget",
            "circuit",
            "other",
        }
    ),
    "kind": frozenset(
        {
            "book",
            "trade",
            "candle",
            "funding",
            "open_interest",
            "mark_price",
            "index_price",
            "instrument",
            "order",
            "fill",
            "position",
            "balance",
            "protection",
        }
    ),
    "side": frozenset({"buy", "sell"}),
    "liquidity": frozenset({"maker", "taker", "unknown"}),
    "stage": frozenset(
        {"features", "inference", "optimizer", "risk", "execution", "snapshot", "gate", "intents"}
    ),
    "factor": frozenset({"btc", "eth"}),
    "mode": frozenset({"RESEARCH", "PAPER", "SHADOW", "DEMO", "LIVE"}),
    "role": frozenset({"collector", "strategy", "risk", "gateway", "jev-worker", "api", "all"}),
}

#: Encodage numérique de l'état de halt (une jauge ne porte pas de texte).
HALT_STATE_CODES: dict[str, int] = {
    "NONE": 0,
    "ENTRIES_HALTED": 1,
    "SOFT_HALT": 1,
    "HARD_HALT": 2,
    "EMERGENCY_FLATTEN": 4,
}
HALT_STATE_UNKNOWN = 3

_MS_BUCKETS = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0)


def bounded_label(name: str, value: str | None) -> str:
    """Ramène une valeur de label à l'ensemble autorisé pour ce nom, sinon ``other``."""
    allowed = ALLOWED_LABELS.get(name)
    if allowed is None or value is None:
        return "other"
    return value if value in allowed else "other"


class Metrics:
    """Toutes les métriques d'un processus, sur un registre isolé (plusieurs instances cohabitent en tests)."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        r = self.registry

        # --- données de marché
        self.market_data_age_seconds = Gauge(
            "market_data_age_seconds",
            "Âge de la donnée de marché la plus récente, en secondes",
            ["channel"],
            registry=r,
        )
        self.private_stream_health = Gauge(
            "private_stream_health",
            "Santé du flux privé : 1 connecté et réconcilié, 0,5 connecté seul, 0 rompu",
            registry=r,
        )
        self.clock_offset_ms = Gauge(
            "clock_offset_ms",
            "Écart mesuré entre notre horloge et celle de l'exchange, en millisecondes",
            registry=r,
        )
        self.book_sequence_gaps_total = Counter(
            "book_sequence_gaps_total", "Trous de séquence détectés dans un carnet", ["channel"], registry=r
        )
        self.book_invalid_total = Counter(
            "book_invalid_total",
            "Carnets invalidés (trou, croisement, quantité non finie)",
            ["reason"],
            registry=r,
        )
        self.data_drops_total = Counter(
            "data_drops_total", "Messages abandonnés par saturation d'une file bornée", ["kind"], registry=r
        )

        # --- boucle décisionnelle
        self.feature_compute_ms = Histogram(
            "feature_compute_ms",
            "Durée du calcul des features, en millisecondes",
            buckets=_MS_BUCKETS,
            registry=r,
        )
        self.inference_ms = Histogram(
            "inference_ms",
            "Durée de l'inférence du modèle, en millisecondes",
            buckets=_MS_BUCKETS,
            registry=r,
        )
        self.optimizer_ms = Histogram(
            "optimizer_ms",
            "Durée de résolution de l'optimiseur, en millisecondes",
            buckets=_MS_BUCKETS,
            registry=r,
        )
        self.decision_deadline_miss_total = Counter(
            "decision_deadline_miss_total", "Décisions terminées après leur deadline", registry=r
        )

        # --- exécution
        self.order_ack_ms = Histogram(
            "order_ack_ms",
            "Délai entre l'envoi d'un ordre et son accusé de réception",
            buckets=_MS_BUCKETS,
            registry=r,
        )
        self.unknown_orders_total = Gauge(
            "unknown_orders_total",
            "Ordres actuellement en état UNKNOWN (exposition réservée pessimiste)",
            registry=r,
        )
        self.reconciliation_gap_usdt = Gauge(
            "reconciliation_gap_usdt", "Écart de réconciliation financière non expliqué, en USDT", registry=r
        )
        self.fill_ratio = Gauge(
            "fill_ratio", "Fraction des intentions effectivement exécutées", ["liquidity"], registry=r
        )
        self.execution_shortfall_bps = Gauge(
            "execution_shortfall_bps", "Implementation shortfall mesuré, en points de base", registry=r
        )
        self.realized_fees_usdt = Gauge(
            "realized_fees_usdt",
            "Commissions réalisées cumulées, en USDT (négatif = rebate ; jauge car un Counter interdit le signe)",
            ["liquidity"],
            registry=r,
        )

        # --- portefeuille et risque
        self.portfolio_gross = Gauge(
            "portfolio_gross", "Exposition brute en multiple de l'equity", registry=r
        )
        self.portfolio_net = Gauge(
            "portfolio_net", "Exposition nette signée en multiple de l'equity", registry=r
        )
        self.beta_exposure = Gauge(
            "beta_exposure", "Exposition au facteur, en multiple de l'equity", ["factor"], registry=r
        )
        self.margin_utilization = Gauge(
            "margin_utilization", "Utilisation de la capacité de marge, fraction de 0 à 1", registry=r
        )
        self.drawdown = Gauge(
            "drawdown", "Drawdown courant depuis le high-water mark unitisé, fraction", registry=r
        )
        self.daily_loss = Gauge(
            "daily_loss", "Perte réalisée du jour UTC, fraction de l'equity de référence", registry=r
        )
        self.halt_state = Gauge(
            "halt_state",
            "État de halt encodé : 0 aucun, 1 entrées bloquées, 2 dur, 3 inconnu, 4 sortie d'urgence",
            registry=r,
        )
        self.unprotected_position_notional = Gauge(
            "unprotected_position_notional",
            "Notionnel des positions sans protection CONFIRMÉE côté exchange, en USDT",
            registry=r,
        )

        # --- JEV
        self.jev_latency_ms = Histogram(
            "jev_latency_ms", "Latence des appels JEV, en millisecondes", buckets=_MS_BUCKETS, registry=r
        )
        self.jev_error_total = Counter("jev_error_total", "Erreurs JEV par motif", ["reason"], registry=r)
        self.jev_cache_hit_ratio = Gauge(
            "jev_cache_hit_ratio", "Taux de succès du cache sémantique JEV", registry=r
        )
        self.jev_input_tokens = Gauge(
            "jev_input_tokens",
            "Tokens d'entrée facturés par JEV, cumulés (jauge pour conserver le nom exact du cahier des charges)",
            registry=r,
        )

        # --- exploitation
        self.outbox_depth = Gauge("outbox_depth", "Nombre d'entrées d'outbox en attente", registry=r)
        self.disk_free_bytes = Gauge(
            "disk_free_bytes", "Espace disque libre du volume de données, en octets", registry=r
        )
        self.db_write_failures = Gauge(
            "db_write_failures", "Échecs d'écriture en base depuis le démarrage", registry=r
        )
        self.restart_count = Gauge(
            "restart_count", "Nombre de redémarrages observés du processus", registry=r
        )

    # --- aides ------------------------------------------------------------------------------------------

    def set_halt_state(self, level: str) -> None:
        """Encode l'état de halt. Un niveau inconnu vaut 3 : « inconnu » n'est pas « aucun »."""
        self.halt_state.set(HALT_STATE_CODES.get(str(level).upper(), HALT_STATE_UNKNOWN))

    def set_private_stream(self, *, connected: bool, reconciled: bool) -> None:
        self.private_stream_health.set(1.0 if (connected and reconciled) else (0.5 if connected else 0.0))

    def observe_stage_ms(self, stage: str, milliseconds: float) -> None:
        histogram = {
            "features": self.feature_compute_ms,
            "inference": self.inference_ms,
            "optimizer": self.optimizer_ms,
        }.get(stage)
        if histogram is not None:
            histogram.observe(milliseconds)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def collected_names(self) -> frozenset[str]:
        """Noms tels qu'ils apparaissent dans l'exposition (suffixe ``_total`` conservé)."""
        names: set[str] = set()
        for line in self.render().decode().splitlines():
            if line.startswith("# TYPE "):
                names.add(line.split()[2])
        return frozenset(names)

    def snapshot(self) -> dict[str, Any]:
        """Vue lisible pour l'API et les journaux (pas de scraping nécessaire pour un diagnostic)."""
        out: dict[str, Any] = {}
        for metric in self.registry.collect():
            for sample in metric.samples:
                key = sample.name + (
                    "{" + ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items())) + "}"
                    if sample.labels
                    else ""
                )
                out[key] = sample.value
        return out
