# Image d'exécution unique de la plateforme (§41, §62). Un seul artefact, plusieurs rôles : le rôle est
# choisi par la commande du service dans compose.yaml, jamais par une variante d'image — deux images
# divergentes finiraient par exécuter deux versions du même code de décision.
#
# POURQUOI multi-étapes : l'étape de construction porte uv, les métadonnées du projet et les sources ;
# l'étape finale ne reçoit QUE l'environnement virtuel résolu et les fichiers réellement lus à
# l'exécution. Ni uv, ni cache de paquets, ni sources non installées n'atteignent l'image livrée.
#
# POURQUOI aucun ARG/ENV de secret ici : §62 interdit d'embarquer des credentials dans une image. Une
# image se pousse, se copie et se partage ; un secret qui y entre est un secret publié. Les clés
# n'existent qu'en variables d'environnement, injectées par service via `env_file` (voir compose.yaml).
#
# POURQUOI aucune activation LIVE : cette image n'offre aucun chemin pour activer LIVE. La commande par
# défaut n'exécute RIEN de marchand (elle affiche l'aide) : un conteneur démarré sans commande explicite
# ne doit jamais se mettre à trader.
#
# Ni BuildKit, ni heredoc, ni montage de cache ne sont requis : l'installateur du VPS doit pouvoir
# construire cette image avec un Docker ancien sans que le déploiement échoue sur une syntaxe récente.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
# uv est épinglé : c'est lui qui applique le verrou. Une version flottante pourrait changer la
# résolution et casser la reproductibilité que uv.lock est censé garantir.
ARG UV_VERSION=0.8.17

# ---------------------------------------------------------------------------------------------------
# Étape 0 — binaire uv épinglé (image officielle « scratch » : elle ne contient que uv et uvx).
# ---------------------------------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------------------------------
# Étape 1 — construction de l'environnement verrouillé
# ---------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

# UV_FROZEN : uv refuse de mettre uv.lock à jour. Si le verrou ne correspond pas à pyproject.toml, le
# build ÉCHOUE au lieu de résoudre silencieusement d'autres versions que celles testées en CI.
# UV_PYTHON_DOWNLOADS=never : on utilise l'interpréteur de l'image de base, pas un binaire téléchargé.
# UV_NO_CACHE : rien à purger ensuite, donc aucun cache inutile ne peut fuir dans une couche.
# UV_COMPILE_BYTECODE : le bytecode est produit MAINTENANT, pendant la construction. Sans lui, chaque
# démarrage de conteneur voudrait l'écrire — impossible sur une racine en lecture seule, et payé en
# latence au premier import. Noter qu'on ne pose surtout pas PYTHONDONTWRITEBYTECODE ici : il
# empêcherait justement cette compilation.
ENV UV_FROZEN=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Deux synchronisations volontairement séparées : les dépendances (qui changent rarement) forment une
# couche réutilisable, le code du projet (qui change à chaque commit) une autre. Un commit applicatif
# ne réinstalle donc pas NumPy, LightGBM et CVXPY.
# README.md est copié parce que pyproject.toml le déclare comme `readme` : sans lui, la construction du
# paquet échoue.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# --no-editable : le paquet est copié dans le site-packages du venv au lieu d'être monté depuis
# /app/src. L'image finale n'a donc pas besoin des sources, et un système de fichiers en lecture seule
# ne casse pas l'import.
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------------------------------
# Étape 2 — image d'exécution
# ---------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# libgomp1 : dépendance système des roues LightGBM (OpenMP). Sans elle l'import échoue à l'exécution
# seulement — donc après le déploiement. ca-certificates : vérification TLS des appels sortants (OKX,
# TypeSafe). Rien d'autre n'est installé : chaque paquet supplémentaire est une surface d'attaque et une
# dépendance à surveiller (§60).
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Utilisateur non-root (§62), UID fixe : les volumes nommés conservent leur propriétaire entre deux
# reconstructions, et un UID stable évite qu'une donnée persistée devienne illisible après mise à jour.
RUN groupadd --gid 10001 okxq \
    && useradd --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin --no-create-home okxq

# PYTHONDONTWRITEBYTECODE : le bytecode est déjà compilé dans le venv (UV_COMPILE_BYTECODE) ; en
# écrire davantage exigerait un système de fichiers inscriptible, que l'on refuse.
# HOME=/tmp : les bibliothèques qui cherchent un répertoire personnel tombent sur le tmpfs monté par
# compose, jamais sur la racine en lecture seule.
# OKXQ_OFFLINE n'est PAS positionné : l'exploitation a besoin du réseau ; ce sont les tests qui sont
# hermétiques.
# `OKXQ_FRONTEND_DIR` : l'image DÉCLARE où elle a mis l'interface, au lieu de laisser le code la
# deviner à partir de sa propre position. Le paquet est installé sous `site-packages/`, d'où remonter
# de trois niveaux désigne `.../lib/python3.12` et non la racine de l'application : l'interface
# n'était jamais trouvée, et l'API répondait du JSON à la racine du site.
#
# Le commentaire est AVANT le bloc, pas à l'intérieur : une ligne de commentaire au milieu d'une
# continuation `\` est un terrain à surprises qu'il n'y a aucune raison de fouler ici.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/tmp \
    OKXQ_FRONTEND_DIR=/app/frontend

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
# Fichiers réellement lus à l'exécution : profils de configuration validés (aucun secret, §63) et
# interface statique servie par l'API (§59). Les sources, tests, docs et scripts de recherche restent
# dehors (voir .dockerignore).
COPY --chown=10001:10001 configs/ /app/configs/
COPY --chown=10001:10001 frontend/ /app/frontend/
# alembic.ini n'est PAS copié, et c'est volontaire : il pointe `script_location` sur `src/`, qui
# n'existe pas dans cette image. Le copier inviterait à lancer `alembic upgrade head` dans le
# conteneur, où il échouerait de façon incompréhensible. Les migrations s'appliquent par
# `okxq db upgrade`, qui les localise DANS le paquet installé — donc sans dépendre de l'arborescence
# du dépôt.

# Répertoires inscriptibles. Ils sont créés ICI, possédés par okxq, pour que les volumes nommés de
# compose héritent du bon propriétaire lors de leur premier remplissage : un bind mount créé par Docker
# appartiendrait à root et un conteneur non-root ne pourrait pas y écrire.
RUN mkdir -p /app/runtime /app/data /app/reports /app/artifacts \
    && chown -R 10001:10001 /app/runtime /app/data /app/reports /app/artifacts

USER 10001:10001

# Sonde par défaut : « le processus répond ». La disponibilité réelle (readiness) dépend du rôle et est
# définie service par service dans compose.yaml — §61 distingue explicitement les deux.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import okxq, sys; sys.exit(0)"]

LABEL org.opencontainers.image.title="okx-quant-jev" \
      org.opencontainers.image.description="Plateforme quantitative OKX + JEV — LIVE désactivé par défaut." \
      org.opencontainers.image.licenses="Proprietary"

# Défaut inoffensif : aucune décision, aucun ordre, aucun appel réseau.
CMD ["okxq", "--help"]
