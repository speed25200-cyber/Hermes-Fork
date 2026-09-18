"""Trois défauts que seul un déploiement réel a montrés (§42, §52, §60).

Les tests hors ligne tournent tous depuis une copie de travail, avec un système de fichiers
inscriptible et un seul processus. Trois hypothèses tacites, et les trois sont fausses en
exploitation : le paquet est installé (pas une copie de travail), la racine du conteneur est en
lecture seule, et cinq processus tournent en même temps sur la même base.

Ces tests reproduisent chacune de ces conditions SANS conteneur.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import okxq
from okxq.runtime.composition import DECIDING_ROLES, PROTECTION_ROLES, ROLES

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


# --- 1. le manifeste doit voyager avec le paquet ------------------------------------------------


def test_the_capability_manifest_lives_inside_the_installed_package() -> None:
    """Le chemin du manifeste ne doit PAS supposer une copie de travail.

    Il était atteint par `Path(__file__).parents[4]` — « quatre niveaux au-dessus, c'est la racine du
    dépôt ». Vrai depuis `src/`, faux depuis `site-packages/` : quatre niveaux au-dessus d'un module
    installé désignent `.../lib/python3.12`. En conteneur le fichier n'était jamais trouvé, le
    collecteur ne pouvait pas découvrir l'univers, et la plateforme restait indéfiniment en
    DATA_STALE. Aucun test ne pouvait le voir tant qu'il s'exécutait depuis une copie de travail.

    La propriété qui tient dans les DEUX dispositions : le manifeste est sous le paquet `okxq`.
    """
    from okxq.exchange.okx.capabilities import DEFAULT_MANIFEST_PATH

    racine_paquet = Path(okxq.__file__).resolve().parent
    assert DEFAULT_MANIFEST_PATH.is_relative_to(racine_paquet), (
        f"{DEFAULT_MANIFEST_PATH} est HORS du paquet {racine_paquet} : introuvable une fois installé"
    )
    assert DEFAULT_MANIFEST_PATH.is_file()
    document = json.loads(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert document["operations"] and document["region_profiles"], "manifeste vide ou tronqué"


def test_the_manifest_is_declared_as_shipped_package_data() -> None:
    """Contre-épreuve : la roue construite doit CONTENIR le fichier.

    Être sous le paquet ne suffit pas si l'outil de construction l'exclut. La configuration
    `[tool.hatch.build.targets.wheel] packages = ["src/okxq"]` embarque tout ce qui vit sous ce
    répertoire ; ce test fixe l'emplacement dont dépend cette règle.
    """
    from okxq.exchange.okx.capabilities import DEFAULT_MANIFEST_PATH

    racine = Path(__file__).resolve().parents[2]
    pyproject = (racine / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/okxq"]' in pyproject
    assert (racine / "src" / "okxq").resolve() in DEFAULT_MANIFEST_PATH.parents


def test_loading_the_manifest_needs_no_argument() -> None:
    """Le collecteur l'appelle sans chemin : c'est ce chemin par défaut qui doit marcher."""
    from okxq.exchange.okx.capabilities import load_manifest

    manifest = load_manifest()
    assert manifest.operations


# --- 2. les alertes doivent être écrites là où tous les rôles peuvent écrire ---------------------


def test_alerts_are_written_under_runtime_not_under_market_data() -> None:
    """`data/` est monté en lecture seule (stratégie) ou pas du tout (risque, gateway, JEV).

    Quatre rôles sur cinq ne pouvaient donc écrire aucune alerte — dont le rôle RISQUE, celui dont
    les alertes comptent le plus. Le système le signalait (« alerte non délivrée ») plutôt que de la
    perdre en silence, mais une alerte signalée non délivrée reste une alerte non délivrée.
    """
    from okxq.runtime.alerts import DEFAULT_ALERT_SINK

    assert DEFAULT_ALERT_SINK.startswith("runtime/"), (
        f"puits d'alertes sous {DEFAULT_ALERT_SINK} : seul `runtime/` est inscriptible par tous les rôles"
    )


def test_every_writer_role_mounts_the_volume_the_alerts_need() -> None:
    """Le lien entre le chemin et le montage est ce qui a lâché : on le vérifie sur `compose.yaml`.

    Vérifier le chemin seul le reproduirait à l'identique si quelqu'un changeait les montages.
    """
    import yaml

    racine = Path(__file__).resolve().parents[2]
    from okxq.runtime.alerts import DEFAULT_ALERT_SINK

    volume_requis = "/app/" + DEFAULT_ALERT_SINK.split("/")[0]
    document = yaml.safe_load((racine / "compose.yaml").read_text(encoding="utf-8"))
    manquants: list[str] = []
    for nom, service in document["services"].items():
        commande = service.get("command") or []
        if not any("run --role" in str(part) for part in commande):
            continue
        montages = service.get("volumes") or []
        inscriptible = any(
            str(m).split(":")[1] == volume_requis and not str(m).endswith(":ro") for m in montages
        )
        if not inscriptible:
            manquants.append(nom)
    assert not manquants, f"rôles sans {volume_requis} inscriptible, donc sans alertes : {manquants}"


# --- 3. un seul décideur, un seul conducteur de la protection ------------------------------------


def test_only_the_strategy_role_decides() -> None:
    """Tous les rôles faisaient tourner la boucle décisionnelle.

    Le collecteur, le risque, le gateway et le worker JEV produisaient chacun leurs décisions sur le
    même compte : quatre boucles concurrentes là où §52 n'en veut qu'une. Visible en exploitation par
    quatre lignes `decision` par minute et des collisions sur l'unicité des instantanés de compte.
    """
    assert DECIDING_ROLES == {"strategy", "all"}
    non_decideurs = set(ROLES) - DECIDING_ROLES
    assert non_decideurs == {"collector", "risk", "gateway", "jev-worker", "api"}


def test_only_one_role_drives_the_protection_and_the_others_can_still_see_it() -> None:
    """Un seul écrivain sur `risk_state`, mais tous les autres doivent RELIRE le halt.

    La version optimiste de la machine d'état interdit deux écrivains. Mais si les autres rôles ne
    relisaient pas, la stratégie déciderait sur le niveau chargé à sa construction — l'état du monde
    au démarrage du processus. Un halt qu'on ne relit pas ne protège que celui qui l'a levé.
    """
    from okxq.risk.kill_switch import KillSwitch

    assert PROTECTION_ROLES == {"risk", "all"}
    assert hasattr(KillSwitch, "refresh"), "sans relecture, le halt du rôle `risk` reste invisible"


def test_a_halt_raised_by_one_process_becomes_visible_to_another(tmp_path: Path) -> None:
    """Deux instances sur le MÊME magasin : celle qui n'écrit pas doit voir le halt de l'autre.

    C'est la situation réelle en conteneur — cinq processus, une base. Sans `refresh`, la seconde
    instance reste sur l'état qu'elle a chargé à sa construction, pour toujours.
    """
    from okxq.config import load_config
    from okxq.domain.clocks import SimulatedClock
    from okxq.risk.budgets import LimitSet
    from okxq.risk.kill_switch import HaltLevel, InMemoryRiskStateStore, KillSwitch

    cfg = load_config(Path(__file__).resolve().parents[2] / "tests/fixtures/configs/smoke.fixture.yaml")
    magasin = InMemoryRiskStateStore()  # un seul magasin, deux instances : la base partagée
    version = LimitSet.from_config(cfg).limits_version
    commun = {
        "account_scope": cfg.account.scope,
        "store": magasin,
        "cfg": cfg.risk,
        "clock": SimulatedClock(T0),
        "limits_version": version,
    }
    conducteur = KillSwitch(**commun)  # le rôle `risk`
    lecteur = KillSwitch(**commun)  # la stratégie
    assert lecteur.level is HaltLevel.NONE

    conducteur.request(HaltLevel.HARD_HALT, "incident de test")
    assert lecteur.level is HaltLevel.NONE, "l'état en mémoire ne change pas tout seul : c'est le piège"
    assert lecteur.refresh().halt_level is HaltLevel.HARD_HALT
    assert lecteur.level is HaltLevel.HARD_HALT


async def test_a_non_deciding_role_still_picks_up_a_halt_raised_elsewhere(tmp_path: Path) -> None:
    """Bout en bout, sur le vrai rappel de frontière : la stratégie doit VOIR le halt du rôle risque.

    Décider est réservé au rôle `strategy` ; regarder la protection ne l'est pas. Le gateway refuse
    une augmentation d'exposition sous halt, et il ne peut le faire que s'il voit le halt. Ce test
    lève un halt par une instance séparée branchée sur la MÊME base — la situation réelle en
    conteneur — puis fait tourner une frontière et vérifie que le processus l'a constaté.
    """
    from okxq.config import load_config
    from okxq.domain.clocks import SimulatedClock
    from okxq.persistence.db import make_engine, make_session_factory
    from okxq.persistence.models import Base
    from okxq.risk.kill_switch import HaltLevel, KillSwitch, SqlRiskStateStore
    from okxq.runtime.composition import _boundary_callback, build_runtime

    cfg = load_config(Path(__file__).resolve().parents[2] / "tests/fixtures/configs/smoke.fixture.yaml")
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    rt = build_runtime(cfg, role="strategy", clock=SimulatedClock(T0), session_factory=factory)
    assert rt.kill_switch.level is HaltLevel.NONE

    # Un AUTRE processus (le rôle `risk`) lève un halt sur la même base.
    autre = KillSwitch(
        account_scope=cfg.account.scope,
        store=SqlRiskStateStore(factory),
        cfg=cfg.risk,
        clock=SimulatedClock(T0),
        limits_version=rt.limits.limits_version,
    )
    autre.request(HaltLevel.HARD_HALT, "incident déclaré par le rôle risque")
    assert rt.kill_switch.level is HaltLevel.NONE, "rien ne change en mémoire tout seul : c'est le piège"

    # La frontière de la stratégie : elle ne conduit pas la protection, mais elle doit la relire.
    await _boundary_callback(rt)(T0, T0)
    assert rt.kill_switch.level is HaltLevel.HARD_HALT, (
        "la stratégie déciderait encore sur le niveau chargé à sa construction"
    )


async def test_a_non_deciding_role_does_not_produce_decisions() -> None:
    """Contre-épreuve : la frontière d'un rôle qui ne décide pas ne rend aucune décision."""
    from okxq.config import load_config
    from okxq.domain.clocks import SimulatedClock
    from okxq.persistence.db import make_engine, make_session_factory
    from okxq.persistence.models import Base
    from okxq.runtime.composition import _boundary_callback, build_runtime

    cfg = load_config(Path(__file__).resolve().parents[2] / "tests/fixtures/configs/smoke.fixture.yaml")
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    rt = build_runtime(
        cfg, role="jev-worker", clock=SimulatedClock(T0), session_factory=make_session_factory(engine)
    )
    # `run_process` ne câble `rt.loop` que pour les rôles décideurs ; il reste donc None ici.
    assert rt.loop is None
    assert await _boundary_callback(rt)(T0, T0) is None


async def test_an_unchanged_account_is_not_re_inserted_every_minute() -> None:
    """Le journal de PostgreSQL portait une ERROR par frontière, et c'était nous.

    L'instantané de compte était inséré à chaque frontière et la contrainte d'unicité servait de
    test d'existence : sur un compte au repos, PostgreSQL refusait l'insertion toutes les minutes et
    journalisait l'échec. Un journal de base rempli d'erreurs ATTENDUES finit par cacher celles qui
    ne le sont pas — et c'est précisément dans ce journal qu'on cherche quand quelque chose ne va
    pas. La contrainte reste le garde-fou contre une course entre processus ; elle n'est plus le
    moyen normal de savoir si la ligne existe.
    """
    from sqlalchemy import func, select

    from okxq.config import load_config
    from okxq.domain.clocks import SimulatedClock
    from okxq.persistence.db import make_engine, make_session_factory
    from okxq.persistence.models import AccountSnapshot, Base
    from okxq.runtime.composition import advance_unit_value, build_runtime

    cfg = load_config(Path(__file__).resolve().parents[2] / "tests/fixtures/configs/smoke.fixture.yaml")
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    rt = build_runtime(cfg, role="risk", clock=SimulatedClock(T0), session_factory=factory)

    # Dix frontières sans le moindre mouvement comptable.
    valeurs = [advance_unit_value(rt, rt.ledger.equity({}), now=T0) for _ in range(10)]
    assert len(set(valeurs)) == 1, "la valeur de part ne doit pas bouger sur un compte au repos"

    with factory() as session:
        lignes = session.scalar(select(func.count()).select_from(AccountSnapshot))
    assert lignes == 1, f"{lignes} instantanés pour un compte immobile : une insertion par frontière"


#: Cas réel : cette clé dérivée est apparue en clair dans un journal de conteneur, puis dans la page
#: publique du workflow qui recopiait ce journal.
LIGNE_FUITEE = (
    "GET /api/v1/system/status?key=4b3833147cdc4dcde7f9b42988e2d5d17363e83b1fd5986a9da4ec16ec96d3f5"
    ' HTTP/1.1" 200'
)


def test_a_credential_in_a_query_string_is_masked_in_logs() -> None:
    """Le journal d'accès écrit l'URL complète : le masquage doit couvrir la chaîne de requête.

    Le masquage par valeur LITTÉRALE ne pouvait pas attraper celle-ci : la clé d'accès est dérivée
    du secret par HMAC, elle n'est la valeur d'aucune variable d'environnement. Seul un motif sur la
    forme `?key=…` la couvre.
    """
    from okxq.runtime.logging import get_masker

    masque = get_masker().mask({"event": LIGNE_FUITEE})["event"]
    assert "4b3833147cdc" not in masque, "la clé est encore lisible dans le journal"
    assert "key=***" in masque


def test_masking_a_query_string_does_not_swallow_ordinary_parameters() -> None:
    """Contre-épreuve : un masquage trop large rendrait les journaux inutilisables."""
    from okxq.runtime.logging import get_masker

    masque = get_masker().mask({"event": "GET /api/v1/decisions?limit=50&inst_id=BTC-USDT-SWAP"})["event"]
    assert "limit=50" in masque and "BTC-USDT-SWAP" in masque


def test_the_status_script_does_not_put_the_key_in_a_url() -> None:
    """Masquer limite les dégâts ; ne pas mettre le justificatif dans l'URL les évite.

    Une URL traverse le journal d'accès du serveur, les journaux de proxy, l'historique du
    navigateur et l'en-tête Referer. Le script d'état interroge donc l'API par l'en-tête
    `Authorization`.
    """
    texte = (Path(__file__).resolve().parents[2] / "deploy" / "etat.sh").read_text(encoding="utf-8")
    assert "Authorization: Bearer" in texte
    assert "status?key=" not in texte, "le script remet la clé dans l'URL"


def test_changing_an_env_file_recreates_the_container_instead_of_restarting_it() -> None:
    """`docker compose restart` ne relit PAS le fichier d'environnement.

    Un fichier `env_file` est lu à la CRÉATION du conteneur, pas à son redémarrage. Les trois
    étapes qui posent une clé — OKX, TypeSafe, et la rotation de la clé opérateur — utilisaient
    `restart` : le fichier changeait, le conteneur gardait l'ancienne valeur. La rotation semblait
    réussir et l'ancienne clé restait valide.

    C'est le pire mode d'échec pour une révocation : on se croit protégé. Il a été trouvé parce que
    le script d'état a reçu un `403` avec la clé dérivée du NOUVEAU secret — le diagnostic a fait
    son travail.
    """
    import yaml

    racine = Path(__file__).resolve().parents[2]
    texte = (racine / ".github" / "workflows" / "vps-status.yml").read_text(encoding="utf-8")
    document = yaml.safe_load(texte)
    assert document["jobs"]["status"]["steps"], "workflow vide : ce test ne vérifierait rien"

    assert "docker compose restart" not in texte, (
        "un `restart` après avoir écrit un fichier d'environnement ne prend pas la nouvelle valeur"
    )
    assert texte.count("--force-recreate") >= 3, "les trois poses de clé doivent recréer leur service"


def test_the_rotation_verifies_the_new_key_instead_of_assuming_it() -> None:
    """Une révocation doit être CONSTATÉE, pas supposée.

    Sans ce contrôle, l'étape se terminait en vert alors que rien n'avait été révoqué.
    """
    racine = Path(__file__).resolve().parents[2]
    texte = (racine / ".github" / "workflows" / "vps-status.yml").read_text(encoding="utf-8")
    debut = texte.index("Tourner la clé opérateur")
    etape = texte[debut : debut + 3000]
    assert "Authorization: Bearer" in etape, "la vérification doit présenter la nouvelle clé"
    assert 'if [ "$CODE" != "200" ]' in etape, "l'étape doit ÉCHOUER si la nouvelle clé est refusée"
