"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Pourquoi : <expliquer le besoin métier ou la contrainte qui impose ce changement de schéma, pas la
liste des ordres SQL — celle-ci se lit ci-dessous>.

Réversibilité : <dire si `downgrade` perd de la donnée. Une migration destructive exige une
sauvegarde vérifiée et une procédure de reprise avant d'être appliquée (§44)>.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
