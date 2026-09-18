#!/usr/bin/env python3
"""Signe un manifeste d'approbation LIVE (§71.3) avec OPERATOR_AUTH_SECRET lu dans l'environnement.

Usage : OPERATOR_AUTH_SECRET=... python scripts/sign_approval_manifest.py body.json > artifacts/live_approval.json

Le corps doit contenir : account_scope, environment ("LIVE"), code_commit, config_hash, artifact_hashes,
limits (avec max_gross_equity_multiple), issued_at, expires_at, actor, gates {technical, scientific,
operator} chacun avec {"passed": true, "evidence": ...}. Ce script ne vérifie pas les preuves : il lie
cryptographiquement ce que l'opérateur affirme à un secret côté serveur. Sans les preuves, la garde LIVE
refuse quand même ce que les tests critiques n'ont pas validé (voir docs/live_readiness.md).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from okxq.config.live_guard import MANIFEST_FIELDS, ApprovalManifest


def main() -> int:
    if len(sys.argv) != 2:
        print("usage : sign_approval_manifest.py <body.json>", file=sys.stderr)
        return 2
    secret = os.environ.get("OPERATOR_AUTH_SECRET")
    if not secret:
        print("OPERATOR_AUTH_SECRET absent de l'environnement", file=sys.stderr)
        return 1
    body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    missing = [f for f in MANIFEST_FIELDS if f not in body]
    if missing:
        print(f"champs manquants : {missing}", file=sys.stderr)
        return 1
    signature = ApprovalManifest.sign(body, secret)
    json.dump({"body": body, "signature": signature}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
