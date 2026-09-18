"""Connexion WebSocket publique réelle, seule implémentation concrète de ``WsConnection``.

Le collecteur ne dépend que du protocole ``WsConnection`` : c'est ce qui permet de le tester sans
réseau. Ce module fournit l'implémentation qui parle vraiment à OKX, et elle reste volontairement
mince — reconnexion, backoff, resynchronisation et qualité des carnets sont déjà la responsabilité du
collecteur, les dupliquer ici créerait deux politiques concurrentes.

Trois choix explicites :

- **TLS vérifié, toujours.** Aucun paramètre ne permet de le désactiver. Un flux de marché
  non authentifié est un flux qu'un intermédiaire peut réécrire, et une décision prise sur des prix
  réécrits est pire qu'une décision absente.
- **URL publique seulement.** Le connecteur refuse une URL qui n'est pas ``wss://`` ou qui désigne un
  endpoint privé : un canal privé exige une signature, et il n'a rien à faire dans un collecteur qui
  ne détient aucun secret.
- **Compression désactivée.** ``permessage-deflate`` ajoute une latence variable au décodage, donc du
  bruit dans les horodatages de réception — précisément la mesure dont dépend la causalité.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from typing import Any

import websockets

from okxq.domain.errors import ConfigError
from okxq.runtime.logging import get_logger

__all__ = ["OkxPublicWsConnection", "connect_public", "public_ws_url"]

log = get_logger("okxq.data.ws_client")

#: Fragments qui trahissent un endpoint privé. Le collecteur n'a aucun secret : s'il se connecte là,
#: il ne pourra rien faire d'utile, et l'erreur doit être immédiate plutôt que silencieuse.
_PRIVATE_MARKERS = ("/private", "/v5/user")

#: Taille maximale d'un message accepté. OKX envoie des instantanés de carnet volumineux ; la borne
#: existe pour qu'un flux anormal ne consomme pas toute la mémoire du processus.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def public_ws_url(url: str) -> str:
    """Valide une URL de flux public. Lève plutôt que de « corriger » une URL douteuse."""
    if not url.startswith("wss://"):
        raise ConfigError("un flux de marché doit être en WSS : TLS n'est pas optionnel", url=url)
    lowered = url.lower()
    for marker in _PRIVATE_MARKERS:
        if marker in lowered:
            raise ConfigError(
                "URL d'endpoint privé dans un collecteur public : refus",
                url=url,
                raison="le collecteur ne détient aucun identifiant et ne doit jamais en recevoir",
            )
    return url


class OkxPublicWsConnection:
    """Adaptateur autour de ``websockets`` conforme au protocole attendu par le collecteur."""

    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send(self, text: str) -> None:
        await self._socket.send(text)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        async for message in self._socket:
            # OKX envoie du texte ; un binaire inattendu est décodé au mieux plutôt que de couper le
            # flux, et le collecteur comptera le message comme fautif s'il n'est pas du JSON valide.
            yield message if isinstance(message, str) else message.decode("utf-8", "replace")

    async def close(self) -> None:
        await self._socket.close()


async def connect_public(url: str) -> OkxPublicWsConnection:
    """Ouvre une connexion publique vérifiée. ``WsConnectionFactory`` du collecteur.

    Le contexte TLS est celui du système : il vérifie le certificat et le nom d'hôte. Les valeurs de
    ``ping`` laissent le collecteur détecter une connexion morte sans attendre un timeout TCP.
    """
    validated = public_ws_url(url)
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    log.info("ws_public_connexion", url=validated)
    socket = await websockets.connect(
        validated,
        ssl=context,
        compression=None,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=20,
        ping_timeout=10,
        open_timeout=10,
        close_timeout=5,
    )
    return OkxPublicWsConnection(socket)
