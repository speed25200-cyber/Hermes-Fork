"""Un seul writer d'exécution (§52.3, D7) : bail en base avec heartbeat et token de fencing monotone.

- ``runtime_leases`` porte le titulaire, l'expiration et un ``fencing_token`` qui n'augmente qu'à chaque
  NOUVELLE acquisition ; un ancien titulaire conserve un token périmé que la base refuse ;
- une instance standby (``FOLLOWER``) n'a aucun chemin d'envoi : le gateway exige ``assert_leader`` ;
- ``assert_leader`` relit la base DANS la transaction d'envoi : split-brain, perte de base ou expiration
  de bail bloquent tout envoi (T46, T47) ;
- une perte de base (exception à l'heartbeat) fait passer localement en ``LOST`` : aucun envoi tant que le
  bail n'a pas été réacquis.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

import structlog

from okxq.domain.clocks import Clock
from okxq.domain.errors import LeadershipError
from okxq.persistence.repositories import UnitOfWork, UnitOfWorkFactory

__all__ = ["DEFAULT_LEASE_NAME", "LeadershipState", "LeaseManager"]

log = structlog.get_logger(__name__)

DEFAULT_LEASE_NAME = "execution-writer"


class LeadershipState(StrEnum):
    FOLLOWER = "FOLLOWER"
    LEADER = "LEADER"
    LOST = "LOST"


class LeaseManager:
    """Gestion du bail d'un processus. Toutes les décisions passent par la base, jamais par l'horloge seule."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        holder_id: str,
        clock: Clock,
        ttl_ms: int,
        heartbeat_ms: int,
        lease_name: str = DEFAULT_LEASE_NAME,
    ) -> None:
        if heartbeat_ms * 2 > ttl_ms:
            raise ValueError("heartbeat_ms doit être au plus la moitié de ttl_ms")
        self._uow_factory = uow_factory
        self.holder_id = holder_id
        self._clock = clock
        self.ttl = timedelta(milliseconds=ttl_ms)
        self.heartbeat_interval = timedelta(milliseconds=heartbeat_ms)
        self.lease_name = lease_name
        self._token: int | None = None
        self._state = LeadershipState.FOLLOWER
        self._local_expires_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None

    # --- lecture -----------------------------------------------------------------------------------------

    @property
    def state(self) -> LeadershipState:
        return self._state

    @property
    def fencing_token(self) -> int | None:
        return self._token

    @property
    def is_leader(self) -> bool:
        """Connaissance LOCALE (pré-filtre) ; la vérité est ``assert_leader`` en base au point d'envoi."""
        if self._state is not LeadershipState.LEADER or self._token is None or self._local_expires_at is None:
            return False
        return self._clock.now_utc() < self._local_expires_at

    def heartbeat_due(self) -> bool:
        if self._last_heartbeat_at is None:
            return True
        return self._clock.now_utc() - self._last_heartbeat_at >= self.heartbeat_interval

    # --- transitions ---------------------------------------------------------------------------------------

    def try_acquire(self) -> bool:
        now = self._clock.now_utc()
        try:
            with self._uow_factory.transaction() as uow:
                token = uow.leases.try_acquire(
                    self.lease_name, holder=self.holder_id, now=now, ttl_seconds=self.ttl.total_seconds()
                )
        except Exception as exc:  # perte de base : on ne devient pas leader sur une supposition
            self._lose("database_error", exc)
            return False
        if token is None:
            self._state = LeadershipState.FOLLOWER
            self._token = None
            self._local_expires_at = None
            return False
        self._token = token
        self._state = LeadershipState.LEADER
        self._local_expires_at = now + self.ttl
        self._last_heartbeat_at = now
        log.info("leadership_acquired", holder=self.holder_id, token=token, lease=self.lease_name)
        return True

    def heartbeat(self) -> bool:
        """Prolonge le bail. False (et état LOST) si la base le refuse ou est injoignable."""
        if self._token is None or self._state is not LeadershipState.LEADER:
            return False
        now = self._clock.now_utc()
        try:
            with self._uow_factory.transaction() as uow:
                token = uow.leases.heartbeat(
                    self.lease_name,
                    holder=self.holder_id,
                    token=self._token,
                    now=now,
                    ttl_seconds=self.ttl.total_seconds(),
                )
        except Exception as exc:
            self._lose("database_error", exc)
            return False
        if token is None:
            self._lose("lease_lost", None)
            return False
        self._local_expires_at = now + self.ttl
        self._last_heartbeat_at = now
        return True

    def release(self) -> None:
        if self._token is not None:
            try:
                with self._uow_factory.transaction() as uow:
                    uow.leases.release(self.lease_name, holder=self.holder_id, token=self._token)
            except Exception as exc:  # pragma: no cover - dépend de la base
                log.warning("leadership_release_failed", error=str(exc))
        self._token = None
        self._state = LeadershipState.FOLLOWER
        self._local_expires_at = None

    def _lose(self, reason: str, exc: Exception | None) -> None:
        log.warning(
            "leadership_lost", holder=self.holder_id, reason=reason, error=None if exc is None else str(exc)
        )
        self._state = LeadershipState.LOST
        self._local_expires_at = None

    # --- point unique de vérification à l'envoi ------------------------------------------------------------

    def assert_leader(self, uow: UnitOfWork) -> int:
        """Vérifie EN BASE, dans la transaction de l'appelant, que le bail est encore le nôtre.

        Retourne le token de fencing à joindre à l'envoi. Lève ``LeadershipError`` sinon.
        """
        if self._state is not LeadershipState.LEADER or self._token is None:
            raise LeadershipError("instance non leader : aucun chemin d'envoi", holder=self.holder_id)
        now = self._clock.now_utc()
        if self._local_expires_at is not None and now >= self._local_expires_at:
            self._lose("local_expiry", None)
            raise LeadershipError("bail expiré localement", holder=self.holder_id)
        try:
            current = uow.leases.is_current(
                self.lease_name, holder=self.holder_id, token=self._token, now=now
            )
        except Exception as exc:
            self._lose("database_error", exc)
            raise LeadershipError("base injoignable : envoi refusé", holder=self.holder_id) from exc
        if not current:
            self._lose("fencing_mismatch", None)
            raise LeadershipError(
                "bail tenu par une autre instance ou token périmé : envoi refusé",
                holder=self.holder_id,
                token=self._token,
            )
        return self._token
