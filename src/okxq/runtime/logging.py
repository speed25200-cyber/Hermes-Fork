"""Journaux structurés JSON avec masquage inconditionnel des secrets (§61, T66).

Trois garanties :

1. **Aucun secret ne sort**, quelle que soit la voie d'entrée : valeur d'une variable d'environnement
   sensible, valeur portée par une clé au nom sensible, motif reconnaissable (``Authorization: Bearer``,
   ``VAR_SECRET=valeur``, identifiants dans une URL), texte d'exception, ou journal de la bibliothèque
   standard (uvicorn, httpx). Le masquage s'applique à la sortie, en dernier ressort, sur les chaînes
   déjà rendues : il ne dépend pas de la discipline de l'appelant.
2. **Ce qui doit rester lisible reste lisible** : hashes de payload, identifiants (``intent_id``,
   ``order_id``…), montants. Un masquage trop large rendrait les journaux inutiles pour l'audit, ce qui
   est une autre façon de perdre la trace d'un fill.
3. **Le contexte de corrélation est propagé** : ``trace_id``, ``decision_id``, ``intent_id``,
   ``order_id``, plus ``mode`` et ``software_version`` sur chaque ligne — la chaîne permet de remonter
   d'un fill jusqu'aux données qui ont causé la décision.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TextIO

import structlog

from okxq import __version__

MASK = "***"

#: Variables d'environnement dont la VALEUR est un secret : elle est masquée partout où elle apparaît.
SECRET_ENV_VARS: tuple[str, ...] = (
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_API_PASSPHRASE",
    "TYPESAFE_API_KEY",
    "OPERATOR_AUTH_SECRET",
    "DATABASE_URL",
    "ALERT_WEBHOOK_URL",
    "POSTGRES_PASSWORD",
)

#: Clés dont la valeur est masquée sans la regarder (en-têtes d'authentification, champs de secret).
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|api[_-]?secret|secret|passphrase|password|passwd|token|authorization|"
    r"ok[-_]access[-_](key|sign|passphrase)|cookie|set[-_]cookie|credential|private[_-]?key)",
    re.I,
)

#: Clés explicitement préservées même si un motif ci-dessus les frôle (audit et réconciliation).
_KEEP_KEY_RE = re.compile(
    r"^(payload_hash|intent_hash|allowed_payload_hash|raw_text_hash|schema_hash|config_hash|"
    r"question_hash|dataset_hash|artifact_sha256|checksum|checksum_sha256|"
    r"[a-z_]*_id|[a-z_]*_ids|contracts|price|price_limit|quantity|amount|fee_cashflow)$",
    re.I,
)

#: Motifs masqués à l'intérieur d'une chaîne, quelle que soit la clé qui la porte.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token> / Basic <token>
    (re.compile(r"((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1" + MASK),
    # VAR_SECRET=valeur (fichier .env recopié, ligne de commande, message d'erreur)
    (
        re.compile(
            r"\b([A-Z][A-Z0-9_]*(?:KEY|SECRET|PASSPHRASE|PASSWORD|TOKEN)[A-Z0-9_]*)\s*=\s*([^\s,;'\"]+)"
        ),
        r"\1=" + MASK,
    ),
    # identifiants dans une URL : scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)([^:/@\s]+):([^@/\s]+)@"), r"\1\2:" + MASK + "@"),
)

# Défaut None puis copie à l'écriture : un dictionnaire par défaut serait partagé entre contextes.
_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("okxq_log_context", default=None)


def _current() -> dict[str, str]:
    return _CONTEXT.get() or {}


class SecretMasker:
    """Masque les secrets connus et les motifs sensibles dans les structures journalisées.

    Les valeurs littérales à masquer sont enregistrées explicitement (``register``) ou lues dans
    l'environnement (``load_env``). Une valeur trop courte (< 6 caractères) n'est pas enregistrée comme
    littéral : elle produirait des remplacements parasites partout dans les journaux.
    """

    MIN_LITERAL_LENGTH = 6

    def __init__(self, *, load_environment: bool = False) -> None:
        self._literals: set[str] = set()
        if load_environment:
            self.load_env()

    def register(self, value: str | None) -> None:
        if value and len(value) >= self.MIN_LITERAL_LENGTH:
            self._literals.add(value)
            # Un DSN porte son mot de passe : on enregistre aussi ce fragment, qui peut apparaître seul.
            m = re.match(r"^[a-z][a-z0-9+.-]*://([^:/@\s]+):([^@/\s]+)@", value, re.I)
            if m and len(m.group(2)) >= self.MIN_LITERAL_LENGTH:
                self._literals.add(m.group(2))

    def load_env(self, names: tuple[str, ...] = SECRET_ENV_VARS) -> None:
        for name in names:
            self.register(os.environ.get(name))

    def clear(self) -> None:
        self._literals.clear()

    @property
    def literals(self) -> frozenset[str]:
        return frozenset(self._literals)

    def mask_text(self, text: str) -> str:
        out = text
        # Les littéraux d'abord : ils sont sûrs et ne dépendent d'aucun motif.
        for literal in sorted(self._literals, key=len, reverse=True):
            if literal in out:
                out = out.replace(literal, MASK)
        for pattern, replacement in _PATTERNS:
            out = pattern.sub(replacement, out)
        return out

    def _mask_value(self, key: str | None, value: Any, depth: int) -> Any:
        if depth > 12:  # profondeur bornée : un objet cyclique ne doit pas bloquer un journal
            return MASK if key and self._key_is_secret(key) else "<profondeur max>"
        if key is not None and self._key_is_secret(key):
            return MASK
        if isinstance(value, str):
            return self.mask_text(value)
        if isinstance(value, bytes):
            return MASK
        if isinstance(value, MutableMapping | dict):
            return {k: self._mask_value(str(k), v, depth + 1) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            masked = [self._mask_value(None, v, depth + 1) for v in value]
            return masked if isinstance(value, list) else type(value)(masked)
        return value

    @staticmethod
    def _key_is_secret(key: str) -> bool:
        if _KEEP_KEY_RE.match(key):
            return False
        return bool(_SECRET_KEY_RE.search(key))

    def mask(self, payload: Any) -> Any:
        """Masque récursivement un dictionnaire, une liste ou une chaîne d'événement."""
        return self._mask_value(None, payload, 0)


_MASKER = SecretMasker()


def get_masker() -> SecretMasker:
    return _MASKER


# --- processeurs structlog ------------------------------------------------------------------------------


def _mask_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    masked = _MASKER.mask(dict(event_dict))
    return masked if isinstance(masked, dict) else dict(event_dict)


def _context_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in _current().items():
        event_dict.setdefault(key, value)
    return event_dict


def _static_fields(mode: str) -> Any:
    def processor(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        event_dict.setdefault("mode", mode)
        event_dict.setdefault("software_version", __version__)
        return event_dict

    return processor


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    mode: str = "PAPER",
    stream: TextIO | None = None,
    capture_stdlib: bool = True,
) -> None:
    """Configure structlog ET la bibliothèque standard vers le même flux masqué.

    ``capture_stdlib`` route uvicorn/httpx/sqlalchemy par le même pipeline : un journal tiers qui
    imprimerait une clé dans un message de format serait sinon le maillon faible.
    """
    _MASKER.load_env()
    target = stream if stream is not None else sys.stdout
    numeric = getattr(logging, level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _context_processor,
        _static_fields(mode),
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _mask_processor,  # dernier avant le rendu : plus rien ne s'ajoute après le masquage
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=target),
        cache_logger_on_first_use=False,
    )

    if capture_stdlib:
        formatter = structlog.stdlib.ProcessorFormatter(processors=[*shared, renderer])
        handler = logging.StreamHandler(target)
        handler.setFormatter(formatter)
        root = logging.getLogger()
        for existing in list(root.handlers):
            root.removeHandler(existing)
        root.addHandler(handler)
        root.setLevel(numeric)
        for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "sqlalchemy.engine"):
            lg = logging.getLogger(noisy)
            lg.handlers = []
            lg.propagate = True
            # Le niveau est remis à NOTSET pour que celui de la racine gouverne. Sans cela, un niveau
            # posé par un tiers (uvicorn applique le sien via dictConfig) écarte silencieusement des
            # enregistrements AVANT le handler masqué : le journal serait muet là où on le croit filtré.
            lg.setLevel(logging.NOTSET)


def get_logger(name: str = "okxq") -> Any:
    return structlog.get_logger(name)


# --- contexte de corrélation ---------------------------------------------------------------------------


def bind_context(**values: str) -> None:
    current = dict(_current())
    current.update({k: str(v) for k, v in values.items() if v is not None})
    _CONTEXT.set(current)


def unbind_context(*keys: str) -> None:
    current = dict(_current())
    for key in keys:
        current.pop(key, None)
    _CONTEXT.set(current)


def clear_context() -> None:
    _CONTEXT.set(None)


def get_context() -> dict[str, str]:
    return dict(_current())


@contextmanager
def context_scope(**values: str) -> Iterator[None]:
    """Lie des identifiants pour la durée d'un bloc, puis restaure l'état antérieur exactement."""
    token = _CONTEXT.set({**_current(), **{k: str(v) for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)
