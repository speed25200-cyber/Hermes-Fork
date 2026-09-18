# Cibles réellement reliées à du code. `make help` les liste.
UV ?= uv
PY := $(UV) run --python .venv/bin/python
export PYTHONDONTWRITEBYTECODE=1
export OKXQ_OFFLINE ?= 1

.PHONY: help setup lint format typecheck test test-unit test-property test-contract-offline test-integration \
        smoke-offline build ui-test security-check clean db-up db-down

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-24s %s\n", $$1, $$2}'

setup: ## Installe l'environnement verrouillé (uv.lock) et les navigateurs de test UI si demandé
	$(UV) sync --python 3.12 --all-groups --extra ui-test --frozen

lint: ## Ruff (lint + format --check)
	$(PY) ruff check src tests scripts
	$(PY) ruff format --check src tests scripts

format: ## Applique ruff format et les corrections automatiques
	$(PY) ruff format src tests scripts
	$(PY) ruff check --fix src tests scripts

typecheck: ## mypy strict sur src/okxq
	$(PY) mypy

test: ## Tests hermétiques (unit + property + contract + e2e + chaos), sans réseau
	$(PY) pytest -q -m "not integration and not connected" --cov=okxq --cov-report=term-missing:skip-covered --cov-report=xml

test-unit: ## Tests unitaires seulement
	$(PY) pytest -q tests/unit

test-property: ## Tests de propriété Hypothesis
	$(PY) pytest -q tests/property -m property

test-contract-offline: ## Contrats fournisseurs sur fixtures (OKX, TypeSafe), sans réseau
	$(PY) pytest -q tests/contract -m "contract and not connected"

test-integration: ## Tests PostgreSQL (OKXQ_TEST_DATABASE_URL requis)
	$(PY) pytest -q tests/integration -m integration

smoke-offline: ## Parcours complet hors ligne : 3 régimes × 2 scénarios + reproductibilité (§68.2)
	$(PY) python scripts/smoke_offline.py

golden: ## Régénère les jeux golden déterministes (graine 25200)
	$(PY) python scripts/build_golden_dataset.py
	$(PY) python scripts/build_golden_dataset.py --all-regimes

build: ## Image Docker multi-stage
	docker build -t okx-quant-jev:local -f infra/Dockerfile .

ui-test: ## Tests frontend (node:test) et smoke Playwright si disponible
	node --test frontend/tests/
	$(PY) pytest -q tests/e2e/test_ui_smoke.py -m e2e

security-check: ## Scan de secrets, dépendances et permissions
	$(PY) python scripts/security_check.py

db-up: ## Démarre PostgreSQL local (compose) pour les tests d'intégration
	docker compose -f compose.yaml up -d postgres

db-down:
	docker compose -f compose.yaml down

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis coverage.xml htmlcov reports/*.json
