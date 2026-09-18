"""Manifeste de capacités OKX (``infra/capability_manifest.json``) et profil par configuration (§46).

Le profil dérivé d'une configuration fixe : le profil régional (domaines REST/WS), le canal de carnet et sa
profondeur, les canaux publics souscrits, et la liste des features REFUSÉES parce qu'elles exigent plus de
profondeur que le canal disponible. Le domaine vient de ``account.region_profile`` (ou de la variable
``OKX_ACCOUNT_REGION_PROFILE`` quand la config dit ``from_env``) : jamais d'un choix automatique visant à
contourner une restriction régionale ou de type de compte.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from okxq.config.schema import AppConfig
from okxq.domain.errors import ConfigError
from okxq.exchange.okx.mappings import PRIVATE_CHANNELS, is_business_channel

DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[4] / "infra" / "capability_manifest.json"
REGION_ENV_VAR = "OKX_ACCOUNT_REGION_PROFILE"

#: canaux publics souscrits par défaut (en plus du canal de carnet configuré)
DEFAULT_PUBLIC_CHANNELS: tuple[str, ...] = (
    "trades",
    "candle1m",
    "mark-price",
    "index-tickers",
    "funding-rate",
    "open-interest",
    "tickers",
)


@dataclass(frozen=True, slots=True)
class RegionProfile:
    name: str
    rest_base_url: str
    ws_public_url: str
    ws_business_url: str
    simulated_trading: bool
    availability: str
    verified_at: str

    def ws_url(self, endpoint: str) -> str:
        if endpoint == "public":
            return self.ws_public_url
        if endpoint == "business":
            return self.ws_business_url
        raise ConfigError(f"endpoint WebSocket inconnu : {endpoint!r}")


@dataclass(frozen=True, slots=True)
class RateLimit:
    scope: str
    requests: int
    per_seconds: float


@dataclass(frozen=True, slots=True)
class Operation:
    key: str
    kind: str  # rest | ws
    method: str | None
    path: str | None
    channel: str | None
    endpoint: str | None
    environment: tuple[str, ...]
    permission: str
    rate_limit: RateLimit
    pagination: Mapping[str, Any] | None
    idempotent: bool
    errors: tuple[str, ...]
    reference: str
    verified_at: str
    availability: Mapping[str, Any]
    fixture: str | None
    depth: int | None = None
    sequenced: bool | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    reference: str
    verified_at: str
    region_profiles: Mapping[str, RegionProfile]
    operations: Mapping[str, Operation]
    features: Mapping[str, int]  # feature → profondeur minimale requise
    path: str

    def rest_operation(self, path: str) -> Operation:
        for op in self.operations.values():
            if op.kind == "rest" and op.path == path:
                return op
        raise ConfigError(f"endpoint REST absent du manifeste de capacités : {path}")

    def ws_operation(self, channel: str) -> Operation:
        for op in self.operations.values():
            if op.kind == "ws" and op.channel == channel:
                return op
        raise ConfigError(f"canal WebSocket absent du manifeste de capacités : {channel}")


def _parse_operation(key: str, raw: Mapping[str, Any]) -> Operation:
    rl = raw.get("rate_limit") or {}
    return Operation(
        key=key,
        kind=str(raw["kind"]),
        method=raw.get("method"),
        path=raw.get("path"),
        channel=raw.get("channel"),
        endpoint=raw.get("endpoint"),
        environment=tuple(raw.get("environment", ())),
        permission=str(raw.get("permission", "none")),
        rate_limit=RateLimit(
            str(rl.get("scope", "ip")), int(rl.get("requests", 1)), float(rl.get("per_seconds", 1))
        ),
        pagination=raw.get("pagination"),
        idempotent=bool(raw.get("idempotent", True)),
        errors=tuple(str(e) for e in raw.get("errors", ())),
        reference=str(raw["reference"]),
        verified_at=str(raw["verified_at"]),
        availability=raw.get("availability") or {},
        fixture=raw.get("fixture"),
        depth=raw.get("depth"),
        sequenced=raw.get("sequenced"),
        notes=str(raw.get("notes", "")),
    )


def load_manifest(path: Path | str | None = None) -> CapabilityManifest:
    p = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
    if not p.exists():
        raise ConfigError(f"manifeste de capacités introuvable : {p}")
    raw: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    profiles = {
        name: RegionProfile(
            name=name,
            rest_base_url=str(v["rest_base_url"]),
            ws_public_url=str(v["ws_public_url"]),
            ws_business_url=str(v["ws_business_url"]),
            simulated_trading=bool(v.get("simulated_trading", False)),
            availability=str(v.get("availability", "")),
            verified_at=str(v.get("verified_at", raw.get("verified_at", ""))),
        )
        for name, v in raw["region_profiles"].items()
    }
    operations = {key: _parse_operation(key, v) for key, v in raw["operations"].items()}
    features = {name: int(v["min_depth"]) for name, v in raw.get("features", {}).items()}
    for op in operations.values():
        if op.kind == "ws" and op.channel in PRIVATE_CHANNELS:
            raise ConfigError(f"le manifeste public ne peut pas déclarer un canal privé : {op.channel}")
    return CapabilityManifest(
        reference=str(raw["reference"]),
        verified_at=str(raw["verified_at"]),
        region_profiles=profiles,
        operations=operations,
        features=features,
        path=str(p),
    )


def resolve_region_profile(
    cfg: AppConfig, manifest: CapabilityManifest, *, env: Mapping[str, str] | None = None
) -> RegionProfile:
    """``unspecified`` → ``default`` ; ``from_env`` → variable d'environnement obligatoire ; sinon nom direct."""
    name = cfg.account.region_profile
    if name == "from_env":
        source = os.environ if env is None else env
        name = source.get(REGION_ENV_VAR, "").strip()
        if not name:
            raise ConfigError(
                f"account.region_profile=from_env exige la variable {REGION_ENV_VAR} (profil de compte de l'opérateur)"
            )
    if name == "unspecified":
        name = "default"
    profile = manifest.region_profiles.get(name)
    if profile is None:
        raise ConfigError(
            f"profil régional inconnu : {name!r} (profils : {', '.join(sorted(manifest.region_profiles))})"
        )
    if cfg.mode.value == "DEMO" and not profile.simulated_trading:
        raise ConfigError("le mode DEMO exige un profil de démonstration (simulated_trading)")
    if cfg.mode.value == "LIVE" and profile.simulated_trading:
        raise ConfigError("le mode LIVE ne peut pas utiliser un profil de démonstration")
    return profile


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    region: RegionProfile
    book_channel: str
    book_depth: int
    book_sequenced: bool
    channels: tuple[str, ...]
    available_features: frozenset[str]
    forbidden_features: Mapping[str, str]
    manifest: CapabilityManifest
    environment: str
    ws_endpoints: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # endpoint → canaux

    def require_feature(self, name: str) -> None:
        if name in self.forbidden_features:
            raise ConfigError(f"feature {name!r} interdite : {self.forbidden_features[name]}")
        if name not in self.available_features:
            raise ConfigError(f"feature {name!r} inconnue du manifeste de capacités")

    def ws_url_for(self, channel: str) -> str:
        return self.region.ws_url("business" if is_business_channel(channel) else "public")


def profile_for(
    cfg: AppConfig,
    *,
    manifest: CapabilityManifest | None = None,
    env: Mapping[str, str] | None = None,
    extra_channels: tuple[str, ...] = (),
) -> CapabilityProfile:
    """Profil de capacités d'une configuration ; refuse ce que le canal de carnet ne peut pas fournir."""
    manifest = manifest or load_manifest()
    region = resolve_region_profile(cfg, manifest, env=env)
    environment = "demo" if region.simulated_trading else "production"
    book_channel = cfg.market_data.orderbook_channel
    book_op = manifest.ws_operation(book_channel)
    if environment not in book_op.environment:
        raise ConfigError(f"canal {book_channel} indisponible dans l'environnement {environment}")
    depth = int(book_op.depth or 0)
    available: set[str] = set()
    forbidden: dict[str, str] = {}
    for feature, min_depth in manifest.features.items():
        if min_depth > depth:
            forbidden[feature] = (
                f"exige {min_depth} niveaux, le canal {book_channel} n'en fournit que {depth}"
            )
        else:
            available.add(feature)
    channels: list[str] = [book_channel]
    for ch in (*DEFAULT_PUBLIC_CHANNELS, *extra_channels):
        if ch in PRIVATE_CHANNELS:
            raise ConfigError(f"canal privé refusé dans le collecteur public : {ch}")
        op = manifest.ws_operation(ch)
        if environment not in op.environment:
            raise ConfigError(f"canal {ch} indisponible dans l'environnement {environment}")
        if ch not in channels:
            channels.append(ch)
    endpoints: dict[str, list[str]] = {}
    for ch in channels:
        endpoints.setdefault("business" if is_business_channel(ch) else "public", []).append(ch)
    return CapabilityProfile(
        region=region,
        book_channel=book_channel,
        book_depth=depth,
        book_sequenced=bool(book_op.sequenced),
        channels=tuple(channels),
        available_features=frozenset(available),
        forbidden_features=forbidden,
        manifest=manifest,
        environment=environment,
        ws_endpoints={k: tuple(v) for k, v in endpoints.items()},
    )
