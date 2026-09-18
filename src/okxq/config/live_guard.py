"""Garde LIVE (§1, §71) : aucune activation sans manifeste d'approbation vérifié.

Le manifeste est un fichier JSON signé HMAC-SHA256 par ``OPERATOR_AUTH_SECRET`` (côté serveur, jamais
dans Git). Il lie compte, environnement, commit, hashes de configuration et d'artefacts, limites,
date d'expiration et acteur. Une modification de l'un de ces éléments invalide la signature.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from okxq.config.modes import Mode
from okxq.config.schema import AppConfig
from okxq.domain.clocks import ensure_utc
from okxq.domain.errors import LiveGuardError
from okxq.domain.ids import canonical_json, sha256_hex

REQUIRED_GATES = ("technical", "scientific", "operator")
MANIFEST_FIELDS = (
    "account_scope",
    "environment",
    "code_commit",
    "config_hash",
    "artifact_hashes",
    "limits",
    "issued_at",
    "expires_at",
    "actor",
    "gates",
)


@dataclass(frozen=True, slots=True)
class ApprovalManifest:
    body: dict[str, Any]
    signature: str

    @classmethod
    def load(cls, path: Path) -> ApprovalManifest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LiveGuardError(f"manifeste d'approbation illisible : {exc}", path=str(path)) from exc
        if not isinstance(raw, dict) or "body" not in raw or "signature" not in raw:
            raise LiveGuardError("manifeste sans body/signature", path=str(path))
        return cls(body=dict(raw["body"]), signature=str(raw["signature"]))

    def canonical(self) -> str:
        return canonical_json(self.body)

    @staticmethod
    def sign(body: dict[str, Any], secret: str) -> str:
        return hmac.new(secret.encode("utf-8"), canonical_json(body).encode("utf-8"), "sha256").hexdigest()

    def verify_signature(self, secret: str) -> bool:
        return hmac.compare_digest(self.sign(self.body, secret), self.signature)


def verify_live_authorization(
    cfg: AppConfig,
    *,
    manifest_path: Path | None,
    operator_secret: str | None,
    now: datetime,
    code_commit: str | None,
) -> list[str]:
    """Retourne la liste des preuves vérifiées ; lève LiveGuardError sinon. Jamais de contournement."""
    if cfg.project.mode is not Mode.LIVE:
        return ["mode non LIVE : garde non concernée"]
    if not cfg.project.live_enabled:
        raise LiveGuardError("profil LIVE désactivé (live_enabled=false)")
    if not operator_secret:
        raise LiveGuardError("OPERATOR_AUTH_SECRET absent : impossible de vérifier un manifeste")
    if manifest_path is None or not manifest_path.exists():
        raise LiveGuardError("manifeste d'approbation LIVE absent (§71.3)")
    manifest = ApprovalManifest.load(manifest_path)
    missing = [f for f in MANIFEST_FIELDS if f not in manifest.body]
    if missing:
        raise LiveGuardError("manifeste incomplet", missing=missing)
    if not manifest.verify_signature(operator_secret):
        raise LiveGuardError("signature du manifeste invalide")
    body = manifest.body
    now = ensure_utc(now)
    expires = ensure_utc(datetime.fromisoformat(str(body["expires_at"])), field="expires_at")
    if expires <= now:
        raise LiveGuardError("manifeste expiré", expires_at=expires.isoformat())
    if body["environment"] != "LIVE":
        raise LiveGuardError("manifeste émis pour un autre environnement", environment=body["environment"])
    if body["account_scope"] != cfg.account.scope:
        raise LiveGuardError("manifeste émis pour un autre compte")
    if cfg.config_hash and body["config_hash"] != cfg.config_hash:
        raise LiveGuardError("le hash de configuration approuvé diffère de la configuration chargée")
    if code_commit and body["code_commit"] != code_commit:
        raise LiveGuardError("le commit approuvé diffère du code déployé", approved=body["code_commit"])
    gates = body.get("gates", {})
    not_passed = [
        g for g in REQUIRED_GATES if not (isinstance(gates, dict) and gates.get(g, {}).get("passed") is True)
    ]
    if not_passed:
        raise LiveGuardError("gates non franchis", gates=not_passed)
    limits = body.get("limits", {})
    if not isinstance(limits, dict) or "max_gross_equity_multiple" not in limits:
        raise LiveGuardError("limites propres au capital absentes du manifeste")
    if float(limits["max_gross_equity_multiple"]) < cfg.risk.max_gross_equity_multiple:
        raise LiveGuardError("la configuration dépasse les limites approuvées")
    return [
        "signature HMAC vérifiée",
        f"expire le {expires.isoformat()}",
        "gates technical/scientific/operator marqués franchis",
        f"empreinte manifeste {sha256_hex(manifest.canonical())[:16]}",
    ]


@dataclass(frozen=True, slots=True)
class LiveAuthorization:
    """Preuve VÉRIFIÉE d'autorisation LIVE, exigée par l'adaptateur avant toute connexion privée (T64).

    Ne se construit que via :func:`authorize_live` : elle porte les preuves rendues par
    :func:`verify_live_authorization`, le compte, le hash de configuration et l'heure de vérification.
    """

    account_scope: str
    config_hash: str | None
    code_commit: str | None
    verified_at: datetime
    proofs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.proofs or not any("HMAC" in p for p in self.proofs):
            raise LiveGuardError("autorisation LIVE sans preuve de signature vérifiée")

    def covers(self, cfg: AppConfig) -> bool:
        return (
            cfg.project.mode is Mode.LIVE
            and cfg.project.live_enabled
            and self.account_scope == cfg.account.scope
            and (cfg.config_hash is None or self.config_hash == cfg.config_hash)
        )


def authorize_live(
    cfg: AppConfig,
    *,
    manifest_path: Path | None,
    operator_secret: str | None,
    now: datetime,
    code_commit: str | None,
) -> LiveAuthorization:
    """Vérifie le manifeste et rend l'objet d'autorisation ; lève ``LiveGuardError`` sinon."""
    if cfg.project.mode is not Mode.LIVE:
        raise LiveGuardError("authorize_live appelé hors mode LIVE", mode=cfg.project.mode.value)
    proofs = verify_live_authorization(
        cfg, manifest_path=manifest_path, operator_secret=operator_secret, now=now, code_commit=code_commit
    )
    return LiveAuthorization(
        account_scope=cfg.account.scope,
        config_hash=cfg.config_hash,
        code_commit=code_commit,
        verified_at=ensure_utc(now),
        proofs=tuple(proofs),
    )
