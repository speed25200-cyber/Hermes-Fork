"""Identifiants, hachages et sérialisation canonique.

Les identifiants sont uniques dans notre journal sur toute leur durée de vie (§28) : ils sont générés
une fois, persistés avant envoi, et jamais réutilisés. Le hachage canonique sert à lier une approbation
de risque au payload exact (§43) et à dédupliquer les documents JEV.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, minuscules


def _encode_base32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Identifiant triable par temps (ms) puis aléatoire : ``prefix_<10 car. temps><16 car. aléa>``.

    Le préfixe nomme la nature de l'objet (``int`` intention, ``ord`` ordre, ``dec`` décision...).
    """
    if not prefix or not prefix.isalnum():
        raise ValueError("préfixe d'identifiant invalide")
    ms = int(time.time() * 1000)
    rnd = secrets.randbits(80)
    return f"{prefix}_{_encode_base32(ms, 10)}{_encode_base32(rnd, 16)}"


def canonical_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, set | frozenset):
        return sorted(canonical_default(x) for x in obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"objet non sérialisable canoniquement : {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """JSON canonique : clés triées, pas d'espaces, décimaux en chaînes, dates ISO."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=canonical_default
    )


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def payload_hash(obj: Mapping[str, Any] | Any) -> str:
    """Hash SHA-256 du JSON canonique d'un objet (payload normalisé d'ordre, question JEV, config...)."""
    return sha256_hex(canonical_json(obj))
