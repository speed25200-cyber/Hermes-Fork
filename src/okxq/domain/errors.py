"""Hiérarchie d'erreurs du domaine.

Chaque erreur porte un ``code`` stable (utilisé dans les journaux et l'API) et un message en français.
"""

from __future__ import annotations


class OkxqError(Exception):
    """Base de toutes les erreurs du projet."""

    code: str = "OKXQ_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None, **context: object) -> None:
        super().__init__(message or self.code)
        if code is not None:
            self.code = code
        self.context: dict[str, object] = dict(context)

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        return f"[{self.code}] {base}" if base else self.code


class ConfigError(OkxqError):
    code = "CONFIG_INVALID"


class LiveGuardError(OkxqError):
    """LIVE demandé sans les preuves et autorisations requises (§71)."""

    code = "LIVE_NOT_AUTHORIZED"


class UnitError(OkxqError):
    """Mélange d'unités, de devises ou d'instruments incompatibles."""

    code = "UNIT_MISMATCH"


class UnsupportedInstrumentError(OkxqError):
    """Contrat inverse, option, produit à échéance ou devise non supportée (§45, T02)."""

    code = "INSTRUMENT_UNSUPPORTED"


class RoundingError(OkxqError):
    """Arrondi impossible sans franchir une limite préapprouvée (T03, T40)."""

    code = "ROUNDING_REJECTED"


class TimestampError(OkxqError):
    """Horodatage naïf, futur, ou violation du contrat de causalité (§34)."""

    code = "TIMESTAMP_INVALID"


class CausalityError(TimestampError):
    code = "CAUSALITY_VIOLATION"


class DataQualityError(OkxqError):
    code = "DATA_INVALID"


class BookInvalidError(DataQualityError):
    code = "BOOK_INVALID"


class SequenceGapError(BookInvalidError):
    code = "BOOK_SEQUENCE_GAP"


class RiskRejectedError(OkxqError):
    code = "RISK_REJECTED"


class ApprovalError(OkxqError):
    """Approbation absente, expirée ou ne correspondant pas au payload (T48, T49)."""

    code = "APPROVAL_INVALID"


class OrderStateError(OkxqError):
    code = "ORDER_STATE_INVALID"


class IdempotencyError(OkxqError):
    """Réutilisation d'un identifiant terminal ou double application (T34, T36)."""

    code = "IDEMPOTENCY_VIOLATION"


class LeadershipError(OkxqError):
    """Le processus n'est pas (ou plus) l'unique writer autorisé (§52.3, T46, T47)."""

    code = "NOT_LEADER"


class ExchangeError(OkxqError):
    code = "EXCHANGE_ERROR"


class ExchangeAmbiguousError(ExchangeError):
    """Réponse ambiguë (timeout, ordre introuvable immédiatement) : réconcilier avant tout retry."""

    code = "EXCHANGE_AMBIGUOUS"


class JevError(OkxqError):
    code = "JEV_ERROR"


class JevContractError(JevError):
    """Réponse JEV invalide : probabilités hors [0,1], somme incohérente, version inattendue (T52, T53)."""

    code = "JEV_CONTRACT_INVALID"


class SsrfBlockedError(OkxqError):
    code = "SSRF_BLOCKED"


class SolverError(OkxqError):
    code = "SOLVER_FAILED"


class LedgerError(OkxqError):
    code = "LEDGER_INVALID"


class ReconciliationError(OkxqError):
    code = "RECONCILIATION_MISMATCH"
