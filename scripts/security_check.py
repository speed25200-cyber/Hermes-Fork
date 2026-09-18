#!/usr/bin/env python3
"""``make security-check`` : contrôles de sécurité bloquants sur le dépôt (§60, principes 8 à 10).

Ce script REFUSE (code de sortie 1) quatre familles de régressions, choisies parce qu'elles sont
silencieuses : rien ne casse, les tests passent, et le dommage n'apparaît qu'après la publication.

1. ``secret_committe``    — une valeur ressemblant à un identifiant réel affectée à un nom de variable
                            de déploiement (OKX_API_*, TYPESAFE_API_KEY, OPERATOR_AUTH_SECRET,
                            POSTGRES_PASSWORD…), un mot de passe intégré à une URL de connexion, ou un
                            jeton reconnaissable à sa forme (bloc de clé privée, style ``sk-``, JWT).
2. ``env_suivi_par_git``  — un ``*.env`` non ``*.example`` suivi par git, ou n'importe quel fichier de
                            ``env/`` (emplacement des secrets par service) qui ne soit pas un modèle.
3. ``live_active``        — une configuration activant LIVE. LIVE est désactivé par défaut et n'a aucun
                            contournement : son activation exige un manifeste d'approbation signé
                            vérifié par le code (§71), jamais un drapeau dans un fichier.
4. ``separation_secrets`` — un service de ``compose.yaml`` recevant des identifiants qui ne lui
                            appartiennent pas : les clés OKX n'existent que dans ``gateway``, la clé
                            TypeSafe que dans ``jev-worker``, la clé opérateur que dans ``api``.

POURQUOI LA DÉTECTION EST NOMMÉE PLUTÔT QU'ENTROPIQUE : un dépôt quantitatif est rempli de chaînes à
forte entropie parfaitement légitimes — empreintes de ``uv.lock``, hachés de configuration, clés
d'exécution, identifiants d'ordre. Une règle fondée sur l'entropie produirait des centaines de faux
positifs, et un contrôle que l'on apprend à ignorer ne protège plus rien. On cible donc ce qui compte :
une valeur affectée à un nom de secret DE DÉPLOIEMENT, un mot de passe dans une URL, et les formats de
jeton non ambigus.

POURQUOI UNE LISTE DE VALEURS DE REMPLACEMENT : les modèles (``*.env.example``) et les tests de
rédaction ont BESOIN de chaînes en forme de secret. Une valeur qui s'annonce elle-même comme fausse
(``remplacer-par-…``, ``$VARIABLE``, ``…``, ``SHOULDNOTLEAK``) n'est pas un secret. C'est une
reconnaissance de forme, pas une exemption de fichier : AUCUN chemin n'est mis sur liste blanche, donc
un vrai secret déposé dans un test ou dans un modèle reste signalé.

``compose.yaml`` est analysé APRÈS interprétation YAML : un ancrage partagé qui porterait un secret est
développé par le parseur dans chaque service, donc détecté — exactement le cas que §60 qualifie de
violation (« un secret injecté globalement dans tous les conteneurs »).

Usage : ``python scripts/security_check.py [--root .] [--json]`` — code 0 si aucune anomalie.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

# ── Noms de variables réellement utilisés pour déployer la plateforme. La liste est EXACTE : un nom
#    générique comme « SECRET » ou « KEY » provoquerait des faux positifs sur des constantes de test et
#    des clés de dictionnaire, et c'est ainsi qu'un contrôle finit par être désactivé.
SECRET_VAR_NAMES: tuple[str, ...] = (
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_API_PASSPHRASE",
    "TYPESAFE_API_KEY",
    "OPERATOR_AUTH_SECRET",
    "POSTGRES_PASSWORD",
    "PGPASSWORD",
    "OKXQ_BACKUP_PASSPHRASE",
)

# ── Propriétaire unique de chaque secret (§60). Tout autre service qui le reçoit est une anomalie.
SERVICE_OWNER: dict[str, str] = {
    "OKX_API_KEY": "gateway",
    "OKX_API_SECRET": "gateway",
    "OKX_API_PASSPHRASE": "gateway",
    "OKX_ACCOUNT_REGION_PROFILE": "gateway",
    "TYPESAFE_API_KEY": "jev-worker",
    "OPERATOR_AUTH_SECRET": "api",
}

# ── Marqueurs qui déclarent une valeur comme fausse. Comparaison en majuscules.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "REMPLACER",
    "CHANGEME",
    "CHANGE-ME",
    "CHANGE_ME",
    "A-REMPLIR",
    "EXAMPLE",
    "EXEMPLE",
    "PLACEHOLDER",
    "FAKE",
    "FACTICE",
    "DUMMY",
    "SAMPLE",
    "TEST",
    "TODO",
    "XXXX",
    "VOTRE",
    "YOUR",
    "SHOULDNOT",
    "NOTREAL",
    "NOT-REAL",
    "REDACTED",
    "MASQUE",
    # Une valeur qui se NOMME « mot de passe » est de la documentation ou une donnée de test, pas un
    # identifiant : les vrais secrets d'OKX et de TypeSafe sont hexadécimaux, base64 ou UUID.
    "PASSWORD",
    "PASSWD",
    "MOTDEPASSE",
    "MOT-DE-PASSE",
    "PASSPHRASE",
    # Idem pour une valeur qui contient le mot « secret ». Les identifiants réels en jeu ici sont
    # hexadécimaux (secret OKX : 32 caractères hex), UUID (clé OKX) ou base64 : aucun ne contient un
    # mot français ou anglais. Les fixtures de masquage, elles, en contiennent presque toujours un.
    "SECRET",
    "...",
    "<",
    "$",
    # Une accolade signale une interpolation ou un gabarit (f-string, Jinja, compose) : le vrai secret
    # est ailleurs, et c'est ailleurs qu'il faut le chercher.
    "{",
    "%",
    "{{",
)

# ── Formats de jeton non ambigus : reconnaissables sans connaître le nom de la variable.
PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bloc de clé privée", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("jeton style sk-", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("clé AWS", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jeton GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("jeton Slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)

# ── Mot de passe intégré à une URL de connexion (``schema://utilisateur:motdepasse@hote``).
URL_CREDENTIAL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:@/\"']+:(?P<pw>[^\s:@/\"']{6,})@")

# ── Clés de configuration qui activeraient LIVE. Le préfixe ``okxq_`` est retiré avant comparaison.
LIVE_KEYS: frozenset[str] = frozenset(
    {"live_enabled", "allow_live", "enable_live", "force_live", "live_trading", "live_orders"}
)

TRUTHY: frozenset[str] = frozenset({"true", "1", "yes", "y", "on", "oui", "enabled", "live"})

# ── Variables d'environnement qui activeraient LIVE, hors YAML (scripts, workflows, fichiers .env).
LIVE_ASSIGNMENT = re.compile(
    r"\b(?P<name>OKXQ_LIVE_ENABLED|LIVE_ENABLED|OKXQ_ALLOW_LIVE|ALLOW_LIVE|OKXQ_FORCE_LIVE)\b"
    r"\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9_]*)"
)

# ── Répertoires ignorés quand git n'est pas disponible pour établir l'inventaire.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        "htmlcov",
        "backups",
        "data",
        "logs",
        "runtime",
        "dist",
        "build",
    }
)

# ── Extensions binaires : les lire en texte ne produirait que du bruit.
BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".xz",
        ".zst",
        ".parquet",
        ".duckdb",
        ".so",
        ".dylib",
        ".dll",
        ".pyc",
        ".dump",
        ".enc",
    }
)

MAX_FILE_BYTES = 4_000_000
EXAMPLE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template", ".dist", ".j2")
YAML_SUFFIXES: tuple[str, ...] = (".yaml", ".yml")


@dataclass(frozen=True)
class Finding:
    """Une anomalie bloquante. ``line`` vaut 0 quand elle ne se rattache pas à une ligne précise."""

    check: str
    path: str
    line: int
    detail: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.check}] {where} — {self.detail}"


@dataclass(frozen=True)
class Inventory:
    """Fichiers à contrôler. ``from_git`` dit si l'inventaire vient réellement de l'index git."""

    root: Path
    files: tuple[Path, ...]
    from_git: bool


def build_inventory(root: Path) -> Inventory:
    """Inventaire des fichiers, via l'index git quand c'est possible.

    La question posée par le principe 9 est « qu'est-ce qui est publiable », pas « qu'est-ce qui traîne
    sur ce disque ». On prend donc les fichiers suivis (``--cached``) ET les fichiers non suivis mais
    non ignorés (``--others --exclude-standard``) : ces derniers sont à un ``git add .`` de la
    publication, ce qui en fait exactement le même risque. Les fichiers réellement ignorés par
    ``.gitignore`` — un vrai ``.env`` local — sont exclus : les signaler serait du bruit, puisque git
    ne peut pas les publier.

    Hors dépôt git (tests, archive extraite), on parcourt l'arborescence, et le rapport le signale —
    pour ne pas laisser croire à une vérification de l'index qui n'a pas eu lieu.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0 and completed.stdout.strip():
        names = [name for name in completed.stdout.split("\0") if name]
        return Inventory(root=root, files=tuple(Path(name) for name in names), from_git=True)
    return Inventory(root=root, files=tuple(_walk(root)), from_git=False)


def _walk(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        yield relative


def read_source(path: Path) -> str | None:
    """Contenu texte d'un fichier, ou ``None`` s'il est binaire, trop gros ou illisible."""
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_placeholder(value: str) -> bool:
    """Vrai si la valeur s'annonce elle-même comme fausse.

    Une valeur vide, très courte, uniformément répétée (``aaaaaaaa``) ou porteuse d'un marqueur
    explicite n'est pas un secret. C'est la VALEUR qui décide, jamais le chemin du fichier : un vrai
    secret déposé dans un test ou dans un modèle reste donc signalé.
    """
    bare = value.strip().strip("\"'")
    if len(bare) < 8:
        return True
    upper = bare.upper()
    if any(marker in upper for marker in PLACEHOLDER_MARKERS):
        return True
    return len(set(bare)) <= 2


def _assignment_pattern() -> re.Pattern[str]:
    names = "|".join(re.escape(name) for name in SECRET_VAR_NAMES)
    # La valeur s'arrête au premier blanc, guillemet ou début de commentaire : c'est la délimitation
    # commune à un shell, à un fichier .env et à un YAML sur une seule ligne. La barre oblique inverse
    # arrête aussi la capture, car un « \\n » écrit dans du code source n'est pas un blanc : sans cette
    # borne, `OKX_API_KEY=\\nAUTRE_VAR=` serait lu comme une valeur de vingt caractères. Aucun secret
    # d'OKX ou de TypeSafe ne contient d'antislash (hexadécimal, UUID ou base64).
    return re.compile(rf"\b(?P<name>{names})\b\s*[:=]\s*[\"']?(?P<value>[^\s\"'#\\]*)", re.IGNORECASE)


SECRET_ASSIGNMENT = _assignment_pattern()


def scan_line(path: str, number: int, line: str) -> list[Finding]:
    """Applique les trois règles de détection de secret à une seule ligne."""
    findings: list[Finding] = []
    for match in SECRET_ASSIGNMENT.finditer(line):
        value = match.group("value")
        if is_placeholder(value):
            continue
        name = match.group("name").upper()
        findings.append(
            Finding(
                check="secret_committe",
                path=path,
                line=number,
                detail=f"{name} reçoit une valeur qui ressemble à un identifiant réel "
                f"({len(value)} caractères). Un secret ne vit jamais dans le dépôt (principe 9) : "
                "le révoquer, puis le reposer dans le fichier d'environnement du seul service concerné.",
            )
        )
    for match in URL_CREDENTIAL.finditer(line):
        if is_placeholder(match.group("pw")):
            continue
        findings.append(
            Finding(
                check="secret_committe",
                path=path,
                line=number,
                detail="mot de passe intégré à une URL de connexion : à déplacer dans le fichier "
                "d'environnement du seul service concerné.",
            )
        )
    for label, pattern in PROVIDER_PATTERNS:
        found = pattern.search(line)
        if found is None or is_placeholder(found.group(0)):
            continue
        findings.append(
            Finding(
                check="secret_committe",
                path=path,
                line=number,
                detail=f"{label} reconnaissable à sa forme : révoquer la clé, puis la retirer de "
                "l'historique (la supprimer du dernier commit ne suffit pas).",
            )
        )
    return findings


def check_committed_secrets(inventory: Inventory) -> list[Finding]:
    """Cherche des identifiants réels dans tous les fichiers texte inventoriés."""
    findings: list[Finding] = []
    for relative in inventory.files:
        content = read_source(inventory.root / relative)
        if content is None:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            findings.extend(scan_line(str(relative), number, line))
    return findings


def _is_example(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in EXAMPLE_SUFFIXES)


def check_env_files(inventory: Inventory) -> list[Finding]:
    """Refuse tout fichier d'environnement réel suivi par git.

    Deux formes sont refusées : un ``*.env`` (ou ``.env.production``) qui n'est pas un modèle, et
    n'importe quel fichier du répertoire ``env/``, emplacement des secrets par service sur le serveur.
    Ce second cas compte : ``.gitignore`` couvre ``.env*`` mais PAS ``env/``, donc rien n'empêcherait
    un ``git add env/gateway.env`` — rien, sauf ce contrôle.
    """
    findings: list[Finding] = []
    origin = "suivi par git" if inventory.from_git else "présent dans l'arborescence"
    for relative in inventory.files:
        name = relative.name
        if _is_example(name):
            continue
        in_env_dir = bool(relative.parts) and relative.parts[0] == "env"
        is_env_file = name == ".env" or name.endswith(".env") or name.startswith(".env.")
        if not (in_env_dir or is_env_file):
            continue
        findings.append(
            Finding(
                check="env_suivi_par_git",
                path=str(relative),
                line=0,
                detail=f"fichier d'environnement {origin} : seuls les modèles (*.env.example) sont "
                "versionnés. Le retirer de l'index et révoquer ce qu'il contenait.",
            )
        )
    return findings


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY
    return False


def _walk_yaml(node: object, path: str = "") -> Iterator[tuple[str, str, object]]:
    """Parcours récursif : rend (chemin complet, nom de clé, valeur) pour chaque entrée de mapping."""
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            full = f"{path}.{name}" if path else name
            yield full, name, value
            yield from _walk_yaml(value, full)
    elif isinstance(node, list):
        for index, element in enumerate(node):
            yield from _walk_yaml(element, f"{path}[{index}]")


def check_live_disabled(inventory: Inventory) -> list[Finding]:
    """Refuse toute configuration activant LIVE (§1, §71).

    Le contrôle est STRUCTUREL : on cherche une clé d'activation portant une valeur vraie, pas le mot
    « live » dans un texte. Sinon ``configs/live.disabled.yaml``, la route ``/health/live`` et les
    commentaires qui expliquent que LIVE est désactivé seraient tous signalés — et un contrôle bruyant
    finit par être contourné.
    """
    findings: list[Finding] = []
    for relative in inventory.files:
        if relative.suffix.lower() not in YAML_SUFFIXES:
            continue
        content = read_source(inventory.root / relative)
        if content is None:
            continue
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            findings.append(
                Finding(
                    check="live_active",
                    path=str(relative),
                    line=0,
                    detail=f"YAML illisible, donc non contrôlable : {str(exc).splitlines()[0]}",
                )
            )
            continue
        for full, name, value in _walk_yaml(document):
            if name.lower().removeprefix("okxq_") in LIVE_KEYS and _truthy(value):
                findings.append(
                    Finding(
                        check="live_active",
                        path=str(relative),
                        line=0,
                        detail=f"« {full}: {value!r} » activerait LIVE. LIVE est désactivé par défaut "
                        "et son activation exige un manifeste d'approbation signé vérifié par le code "
                        "(§71) ; aucun fichier ne peut l'activer.",
                    )
                )
    findings.extend(_check_live_env_assignments(inventory))
    return findings


def _check_live_env_assignments(inventory: Inventory) -> list[Finding]:
    """Même interdiction, côté variables d'environnement (compose, scripts, workflows)."""
    findings: list[Finding] = []
    for relative in inventory.files:
        content = read_source(inventory.root / relative)
        if content is None:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            for match in LIVE_ASSIGNMENT.finditer(line):
                if not _truthy(match.group("value")):
                    continue
                findings.append(
                    Finding(
                        check="live_active",
                        path=str(relative),
                        line=number,
                        detail=f"{match.group('name')} est mis à une valeur vraie : aucun fichier "
                        "d'infrastructure ne doit activer LIVE (§71).",
                    )
                )
    return findings


def _env_names_from_text(content: str) -> set[str]:
    """Noms de variables déclarés par un fichier de type ``.env`` (les valeurs ne sont pas lues)."""
    names: set[str] = set()
    for line in content.splitlines():
        bare = line.strip()
        if not bare or bare.startswith("#"):
            continue
        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", bare)
        if match is not None:
            names.add(match.group(1).upper())
    return names


def _env_file_paths(entry: object) -> list[str]:
    """Normalise ``env_file``, qui accepte une chaîne, une liste, ou une liste de mappings."""
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        path = entry.get("path")
        return [str(path)] if isinstance(path, str) else []
    if isinstance(entry, list):
        paths: list[str] = []
        for element in entry:
            paths.extend(_env_file_paths(element))
        return paths
    return []


def _environment_names(entry: object) -> set[str]:
    """Noms déclarés par ``environment``, en forme mapping comme en forme liste ``CLE=valeur``."""
    names: set[str] = set()
    if isinstance(entry, dict):
        names.update(str(key).upper() for key in entry)
    elif isinstance(entry, list):
        for element in entry:
            if isinstance(element, str):
                names.add(element.split("=", 1)[0].strip().upper())
    return names


def _resolve_env_file(root: Path, path: str) -> tuple[Path | None, set[str]]:
    """Trouve le fichier d'environnement, ou à défaut son modèle versionné, et rend ses noms.

    Sur un poste de développement et en intégration continue, les vrais ``env/*.env`` n'existent pas :
    c'est le modèle ``infra/env/<nom>.example`` qui déclare les variables du service, et c'est donc lui
    qui répond à la question « ce service reçoit-il les clés OKX ? ».
    """
    candidates = [
        root / path,
        Path(f"{root / path}.example"),
        root / "infra" / "env" / f"{Path(path).name}.example",
    ]
    for candidate in candidates:
        if candidate.is_file():
            content = read_source(candidate)
            if content is not None:
                return candidate, _env_names_from_text(content)
    return None, set()


def check_compose_separation(root: Path, compose_name: str = "compose.yaml") -> list[Finding]:
    """Vérifie que chaque secret n'atteint que le service qui en est propriétaire (§60).

    L'analyse porte sur le YAML INTERPRÉTÉ : les ancrages y sont déjà développés, donc un bloc partagé
    portant un secret apparaît dans chaque service et se fait prendre. Pour chaque service, les noms
    proviennent de son ``environment`` et des fichiers listés dans son ``env_file`` (ou de leurs
    modèles). Référencer un fichier global comme ``.env``, qui déclare tous les secrets, échoue donc
    pour tous les services sauf, au mieux, un seul.
    """
    compose = root / compose_name
    if not compose.is_file():
        return [_coverage_gap(compose_name, "fichier absent")]
    content = read_source(compose)
    if content is None:
        return [_coverage_gap(compose_name, "fichier illisible")]
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return [_coverage_gap(compose_name, f"YAML illisible ({str(exc).splitlines()[0]})")]
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        return [_coverage_gap(compose_name, "aucun service déclaré")]

    findings: list[Finding] = []
    for raw_name, definition in services.items():
        service = str(raw_name)
        if not isinstance(definition, dict):
            continue
        names = _environment_names(definition.get("environment"))
        sources: dict[str, str] = dict.fromkeys(names, "bloc environment")
        for path in _env_file_paths(definition.get("env_file")):
            resolved, declared = _resolve_env_file(root, path)
            for name in declared:
                sources.setdefault(name, str(resolved) if resolved is not None else path)
            names.update(declared)
        for variable, owner in SERVICE_OWNER.items():
            if variable not in names or service == owner:
                continue
            findings.append(
                Finding(
                    check="separation_secrets",
                    path=f"{compose_name}#services.{service}",
                    line=0,
                    detail=f"{variable} atteint le service « {service} » alors qu'elle appartient au "
                    f"seul service « {owner} » (source : {sources.get(variable, 'inconnue')}). §60 : "
                    "un secret injecté globalement dans tous les conteneurs viole cette séparation.",
                )
            )
    return findings


def _coverage_gap(path: str, reason: str) -> Finding:
    return Finding(
        check="separation_secrets",
        path=path,
        line=0,
        detail=f"{reason} : la séparation des secrets par service n'a pas pu être vérifiée. "
        "Un contrôle qui ne trouve rien à contrôler n'est pas un contrôle.",
    )


def check_coverage(inventory: Inventory) -> list[Finding]:
    """Contrôle du contrôle : un scan qui ne lit rien passerait indéfiniment.

    Ce garde-fou existe parce qu'un ``security-check`` vert est censé vouloir dire quelque chose. Si
    l'inventaire est vide, ou si aucun modèle d'environnement n'est trouvé, le vert serait un artefact
    de configuration et non une garantie.
    """
    findings: list[Finding] = []
    if len(inventory.files) < 5:
        findings.append(
            Finding(
                check="couverture",
                path=".",
                line=0,
                detail=f"{len(inventory.files)} fichier(s) inventorié(s) : trop peu pour que le "
                "résultat signifie quoi que ce soit.",
            )
        )
    if not any(name.name.endswith(".env.example") for name in inventory.files):
        findings.append(
            Finding(
                check="couverture",
                path="infra/env",
                line=0,
                detail="aucun modèle *.env.example trouvé : la séparation des secrets par service "
                "n'est alors décrite nulle part.",
            )
        )
    return findings


def run_all_checks(root: Path) -> tuple[list[Finding], Inventory]:
    """Exécute tous les contrôles et rend les anomalies avec l'inventaire réellement parcouru."""
    inventory = build_inventory(root)
    findings: list[Finding] = []
    findings.extend(check_coverage(inventory))
    findings.extend(check_committed_secrets(inventory))
    findings.extend(check_env_files(inventory))
    findings.extend(check_live_disabled(inventory))
    findings.extend(check_compose_separation(root))
    return findings, inventory


def _report(findings: Sequence[Finding], inventory: Inventory) -> None:
    yaml_count = sum(1 for f in inventory.files if f.suffix.lower() in YAML_SUFFIXES)
    origin = "index git" if inventory.from_git else "arborescence (git indisponible)"
    print("=== contrôles de sécurité (§60) ===")
    print(f"  inventaire : {len(inventory.files)} fichiers depuis l'{origin}, dont {yaml_count} YAML")
    print("  contrôles  : couverture, secret_committe, env_suivi_par_git, live_active, separation_secrets")
    if not findings:
        print("  résultat   : AUCUNE ANOMALIE")
        return
    print(f"  résultat   : {len(findings)} ANOMALIE(S) — livraison refusée")
    for finding in findings:
        print(f"    - {finding.render()}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contrôles de sécurité bloquants (§60).")
    parser.add_argument("--root", default=".", help="Racine du dépôt à contrôler.")
    parser.add_argument("--json", action="store_true", help="Sortie JSON pour l'intégration continue.")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings, inventory = run_all_checks(root)
    if args.json:
        payload = {
            "ok": not findings,
            "racine": str(root),
            "fichiers_inventories": len(inventory.files),
            "inventaire_git": inventory.from_git,
            "anomalies": [
                {"check": f.check, "path": f.path, "line": f.line, "detail": f.detail} for f in findings
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _report(findings, inventory)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
