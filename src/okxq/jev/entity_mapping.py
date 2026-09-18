"""Registre d'entités point-in-time et mapping document → actifs (§50.2, T54).

Principes :
- un ticker seul n'est PAS une identité suffisante : sans nom canonique, alias de projet ou contexte de
  réseau, aucun mapping n'est inventé ; la qualité (``ticker_only``, ``ambiguous``, ``none``) est explicite ;
- le registre est daté (``valid_from``/``valid_to``) : un renommage crée un nouvel enregistrement et l'ancien
  symbole devient un alias du nouveau à partir de la date de bascule ;
- un document peut concerner plusieurs actifs (événement multi-actifs) : un mapping par actif reconnu ;
- la sélection des documents antérieurs comparables (question ``novelty_vs_prior``) est point-in-time
  (``received_at`` strictement antérieur) et dédupliquée par similarité calibrée (Jaccard sur shingles),
  jamais par simple égalité de titre.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import JevError
from okxq.domain.events import AssetMapping, SourceDocument
from okxq.jev.schemas import parse_json_strict

CONFIDENCE_CANONICAL_NAME = 0.95
CONFIDENCE_PROJECT_ALIAS = 0.85
CONFIDENCE_TICKER_WITH_CONTEXT = 0.70
NEAR_DUPLICATE_JACCARD = 0.80
DEFAULT_PRIOR_WINDOW = timedelta(days=7)
DEFAULT_MAX_PRIOR = 5


class MappingQuality(StrEnum):
    OK = "ok"
    AMBIGUOUS = "ambiguous"
    TICKER_ONLY = "ticker_only"
    NONE = "none"


class EntityRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    inst_id: str = Field(min_length=1, max_length=64)
    canonical_name: str = Field(min_length=2, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    aliases: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None

    def active_at(self, as_of: datetime) -> bool:
        if as_of < self.valid_from:
            return False
        return self.valid_to is None or as_of < self.valid_to


class EntityRegistryFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    records: list[EntityRecord] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class MappingResult:
    mappings: list[AssetMapping]
    quality: MappingQuality
    candidates: list[str]
    notes: list[str]
    mapping_version: str


def _word_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w$])" + re.escape(term) + r"(?![\w])", re.IGNORECASE)


def _ticker_pattern(symbol: str) -> re.Pattern[str]:
    # Ticker : en majuscules exactes ou précédé de « $ » ; « sol » en minuscules n'est pas un ticker.
    return re.compile(r"(?<![\w])\$?" + re.escape(symbol.upper()) + r"(?![\w])")


class EntityRegistry:
    """Registre point-in-time des actifs connus."""

    def __init__(self, records: Sequence[EntityRecord], *, version: str) -> None:
        if not records:
            raise JevError("registre d'entités vide", code="ENTITY_REGISTRY_INVALID")
        seen: set[tuple[str, datetime]] = set()
        for r in records:
            key = (r.inst_id, ensure_utc(r.valid_from))
            if key in seen:
                raise JevError(
                    f"enregistrement dupliqué : {r.inst_id}@{r.valid_from}", code="ENTITY_REGISTRY_INVALID"
                )
            seen.add(key)
            if r.valid_to is not None and r.valid_to <= r.valid_from:
                raise JevError(f"valid_to <= valid_from pour {r.inst_id}", code="ENTITY_REGISTRY_INVALID")
        self._records = tuple(records)
        self.version = version

    @classmethod
    def load(cls, path: str | Path) -> EntityRegistry:
        p = Path(path)
        try:
            data = EntityRegistryFile.model_validate(parse_json_strict(p.read_bytes()))
        except FileNotFoundError as exc:
            raise JevError(f"registre d'entités introuvable : {p}", code="ENTITY_REGISTRY_MISSING") from exc
        except ValidationError as exc:
            raise JevError(f"registre d'entités invalide : {exc}", code="ENTITY_REGISTRY_INVALID") from exc
        return cls(data.records, version=data.version)

    @property
    def records(self) -> tuple[EntityRecord, ...]:
        return self._records

    def active(self, as_of: datetime) -> list[EntityRecord]:
        as_of = ensure_utc(as_of, field="as_of")
        return [r for r in self._records if r.active_at(as_of)]

    def resolve(self, *, title: str, text: str, as_of: datetime) -> MappingResult:
        """Mappe un document vers les actifs du registre actifs à ``as_of``."""
        corpus = f"{title}\n{text}"
        active = self.active(as_of)
        mapped: dict[str, AssetMapping] = {}
        notes: list[str] = []
        ticker_hits: dict[str, list[EntityRecord]] = {}
        for record in active:
            method, confidence = self._match(record, corpus, title)
            if method is not None and confidence is not None:
                mapped[record.inst_id] = AssetMapping(
                    inst_id=record.inst_id,
                    canonical_name=record.canonical_name,
                    symbol=record.symbol,
                    mapping_version=self.version,
                    confidence=confidence,
                    method=method,
                )
            elif _ticker_pattern(record.symbol).search(corpus):
                ticker_hits.setdefault(record.symbol.upper(), []).append(record)
        candidates = sorted({r.inst_id for hits in ticker_hits.values() for r in hits} - set(mapped))
        if mapped:
            quality = MappingQuality.OK
            if candidates:
                notes.append(f"tickers non résolus ignorés : {candidates}")
        elif any(len(hits) > 1 for hits in ticker_hits.values()):
            quality = MappingQuality.AMBIGUOUS
            notes.append(
                "ticker partagé par plusieurs actifs sans nom canonique ni contexte : mapping non inventé"
            )
        elif ticker_hits:
            quality = MappingQuality.TICKER_ONLY
            notes.append("ticker seul sans nom canonique ni contexte : identité insuffisante")
        else:
            quality = MappingQuality.NONE
        ordered = sorted(mapped.values(), key=lambda m: (-m.confidence, m.inst_id))
        return MappingResult(
            mappings=ordered,
            quality=quality,
            candidates=candidates,
            notes=notes,
            mapping_version=self.version,
        )

    @staticmethod
    def _match(record: EntityRecord, corpus: str, title: str) -> tuple[str | None, float | None]:
        if _word_pattern(record.canonical_name).search(corpus):
            bonus = 0.03 if _word_pattern(record.canonical_name).search(title) else 0.0
            return "canonical_name", min(1.0, CONFIDENCE_CANONICAL_NAME + bonus)
        for alias in (*record.aliases, *record.project_ids):
            if len(alias) >= 3 and _word_pattern(alias).search(corpus):
                return "project_alias", CONFIDENCE_PROJECT_ALIAS
        if _ticker_pattern(record.symbol).search(corpus):
            for network in record.networks:
                if _word_pattern(network).search(corpus):
                    return "ticker_with_context", CONFIDENCE_TICKER_WITH_CONTEXT
        return None, None


# --- documents antérieurs comparables ----------------------------------------------------------------

_TOKEN = re.compile(r"\w+", re.UNICODE)


def word_shingles(text: str, k: int = 3) -> frozenset[str]:
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    if len(tokens) < k:
        return frozenset({" ".join(tokens)}) if tokens else frozenset()
    return frozenset(" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


@dataclass(frozen=True, slots=True)
class PriorSelection:
    priors: list[SourceDocument]
    considered: int
    dropped_near_duplicates: int
    notes: list[str] = field(default_factory=list)

    def as_request_pairs(self) -> list[tuple[str, str]]:
        return [(d.title, d.text) for d in self.priors]


def select_prior_documents(
    document: SourceDocument,
    candidates: Iterable[SourceDocument],
    *,
    inst_id: str,
    window: timedelta = DEFAULT_PRIOR_WINDOW,
    max_prior: int = DEFAULT_MAX_PRIOR,
    near_duplicate_threshold: float = NEAR_DUPLICATE_JACCARD,
) -> PriorSelection:
    """Documents antérieurs comparables pour ``novelty_vs_prior``.

    Point-in-time : seuls les documents REÇUS strictement avant ``document.received_at`` (et dans la fenêtre)
    sont admissibles — un document publié avant mais reçu après n'existait pas pour le système. Une version
    antérieure du même document est admissible. Les quasi-doublons (Jaccard ≥ seuil) sont fusionnés ; deux
    événements distincts au même titre ne le sont jamais.
    """
    horizon = document.received_at - window
    admissible: list[SourceDocument] = []
    for cand in candidates:
        if cand.document_id == document.document_id and cand.version >= document.version:
            continue
        if cand.received_at >= document.received_at or cand.received_at < horizon:
            continue
        if (
            not any(m.inst_id == inst_id for m in cand.asset_mapping)
            and cand.document_id != document.document_id
        ):
            continue
        admissible.append(cand)
    admissible.sort(key=lambda d: (d.received_at, d.document_id, d.version))
    kept: list[SourceDocument] = []
    kept_shingles: list[frozenset[str]] = []
    dropped = 0
    for cand in admissible:
        sh = word_shingles(cand.title + " " + cand.text)
        if any(jaccard(sh, other) >= near_duplicate_threshold for other in kept_shingles):
            dropped += 1
            continue
        kept.append(cand)
        kept_shingles.append(sh)
    priors = kept[-max_prior:] if max_prior > 0 else []
    notes = []
    if dropped:
        notes.append(f"{dropped} quasi-doublon(s) fusionné(s) (Jaccard ≥ {near_duplicate_threshold})")
    return PriorSelection(
        priors=priors, considered=len(admissible), dropped_near_duplicates=dropped, notes=notes
    )
