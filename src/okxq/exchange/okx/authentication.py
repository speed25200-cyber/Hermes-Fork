"""Signature des requêtes OKX v5 (REST et WebSocket).

Formule officielle (docs-v5, section « Authentication », vérifiée le 2026-09-18) :

    prehash = timestamp + method + requestPath + body
    sign    = Base64( HMAC-SHA256( secretKey, prehash ) )

- ``timestamp`` REST : ISO 8601 UTC en millisecondes, ex. ``2020-12-08T09:08:57.715Z`` ;
- ``method`` : ``GET``/``POST`` en majuscules ;
- ``requestPath`` : chemin AVEC la chaîne de requête (``/api/v5/account/balance?ccy=USDT``) ;
- ``body`` : la chaîne JSON exacte envoyée (vide pour GET) — on signe les OCTETS envoyés, jamais une
  re-sérialisation ;
- en-têtes : ``OK-ACCESS-KEY``, ``OK-ACCESS-SIGN``, ``OK-ACCESS-TIMESTAMP``, ``OK-ACCESS-PASSPHRASE`` ;
  ``x-simulated-trading: 1`` en DEMO ;
- login WebSocket : ``timestamp`` en secondes epoch (chaîne), ``sign`` sur ``timestamp + "GET" +
  "/users/self/verify"``.

Les vecteurs des tests sont CONSTRUITS par cette formule (ils ne proviennent pas d'OKX).
"""

from __future__ import annotations

import base64
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from okxq.domain.errors import ConfigError

__all__ = [
    "DEMO_HEADER",
    "WS_LOGIN_PATH",
    "OkxCredentials",
    "load_credentials_from_env",
    "okx_timestamp",
    "prehash",
    "rest_headers",
    "sign",
    "ws_login_args",
]

DEMO_HEADER = ("x-simulated-trading", "1")
WS_LOGIN_PATH = "/users/self/verify"


@dataclass(frozen=True, slots=True)
class OkxCredentials:
    """Clés lues depuis l'environnement du seul gateway ; jamais journalisées."""

    api_key: str
    api_secret: str
    passphrase: str

    def __post_init__(self) -> None:
        if not (self.api_key and self.api_secret and self.passphrase):
            raise ConfigError("identifiants OKX incomplets")

    def __repr__(self) -> str:  # pragma: no cover - masquage
        return f"OkxCredentials(api_key={self.api_key[:4]}…, api_secret=***, passphrase=***)"


def load_credentials_from_env(env: Mapping[str, str] | None = None) -> OkxCredentials:
    src = os.environ if env is None else env
    try:
        return OkxCredentials(
            api_key=src.get("OKX_API_KEY", ""),
            api_secret=src.get("OKX_API_SECRET", ""),
            passphrase=src.get("OKX_API_PASSPHRASE", ""),
        )
    except ConfigError as exc:
        raise ConfigError(
            "OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE absents de l'environnement du gateway"
        ) from exc


def okx_timestamp(now: datetime) -> str:
    """ISO 8601 UTC avec millisecondes et suffixe ``Z``."""
    if now.tzinfo is None:
        raise ConfigError("horodatage naïf refusé pour la signature")
    now = now.astimezone(UTC)
    return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"


def prehash(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        raise ConfigError("méthode HTTP inattendue", method=method)
    if not request_path.startswith("/"):
        raise ConfigError("requestPath doit commencer par '/'", path=request_path)
    return f"{timestamp}{method}{request_path}{body}"


def sign(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), "sha256").digest()
    return base64.b64encode(digest).decode("ascii")


def rest_headers(
    creds: OkxCredentials,
    *,
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
    demo: bool,
) -> dict[str, str]:
    headers = {
        "OK-ACCESS-KEY": creds.api_key,
        "OK-ACCESS-SIGN": sign(creds.api_secret, prehash(timestamp, method, request_path, body)),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": creds.passphrase,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if demo:
        headers[DEMO_HEADER[0]] = DEMO_HEADER[1]
    return headers


def ws_login_args(creds: OkxCredentials, *, epoch_seconds: int) -> dict[str, str]:
    ts = str(int(epoch_seconds))
    return {
        "apiKey": creds.api_key,
        "passphrase": creds.passphrase,
        "timestamp": ts,
        "sign": sign(creds.api_secret, prehash(ts, "GET", WS_LOGIN_PATH, "")),
    }
