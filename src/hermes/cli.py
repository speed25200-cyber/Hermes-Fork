"""Command line: ``hermes <group> <command>``.

Secrets are read from the environment only (``OKX_API_KEY``, ``OKX_API_SECRET``, ``OKX_API_PASSPHRASE``);
they never appear in configuration files, arguments or logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import typer

from hermes.config import HermesConfig, load_config

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Hermes — trading quantitatif sur perpétuels.")
data_app = typer.Typer(no_args_is_help=True, help="Données historiques")
research_app = typer.Typer(no_args_is_help=True, help="Recherche : entraînement walk-forward, évaluation, rapport")
live_app = typer.Typer(no_args_is_help=True, help="Exécution : paper, démo OKX, réel")
model_app = typer.Typer(no_args_is_help=True, help="Gestion des modèles")
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")
app.add_typer(live_app, name="live")
app.add_typer(model_app, name="model")


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _cfg(config: Path | None, overrides: list[str]) -> HermesConfig:
    kv = {}
    for o in overrides:
        k, _, v = o.partition("=")
        try:
            kv[k] = json.loads(v)
        except json.JSONDecodeError:
            kv[k] = v
    return load_config(config, **kv)


ConfigOpt = typer.Option(None, "--config", "-c", help="Fichier YAML de configuration")
SetOpt = typer.Option([], "--set", "-s", help="Surcharge pointée, ex. -s portfolio.vol_target_annual=0.15")


@data_app.command("download")
def data_download(config: Path | None = ConfigOpt, overrides: list[str] = SetOpt) -> None:
    """Télécharge et met en cache le panel (univers point-in-time, sans biais de survie)."""
    _setup_logging()
    from hermes.data.store import load_panel

    cfg = _cfg(config, overrides)
    p = load_panel(cfg.data, cfg.seed)
    typer.echo(f"panel {p.shape[0]} barres x {p.shape[1]} contrats, {p.index[0]} -> {p.index[-1]}")


@research_app.command("run")
def research_run(
    config: Path | None = ConfigOpt,
    overrides: list[str] = SetOpt,
    out: Path = typer.Option(Path("reports/latest"), "--out", "-o"),
    n_null: int = typer.Option(40, help="Répliques du test nul"),
    workers: int = typer.Option(0, help="Processus parallèles (0 = tous les cœurs)"),
    ledger: Path = typer.Option(Path("reports/trials.jsonl"), help="Registre des essais (DSR)"),
    no_model: bool = typer.Option(False, help="Ne pas entraîner le modèle final"),
) -> None:
    """Walk-forward complet + évaluation hors échantillon + porte de promotion + rapport."""
    _setup_logging()
    from hermes.research.run import run_research

    cfg = _cfg(config, overrides)
    ev, _, _ = run_research(cfg, out, n_null=n_null, workers=workers or None, ledger=ledger, save_model=not no_model)
    typer.echo((out / "REPORT.md").read_text())
    typer.echo(f"\nVerdict : {'PROMU' if ev.promoted else 'NON PROMU'}")


@model_app.command("install")
def model_install(
    source: Path = typer.Argument(..., help="Dossier du bundle (ex. reports/latest/model)"),
    target: Path = typer.Option(Path("artifacts/models/champion"), "--to"),
) -> None:
    """Installe un bundle comme modèle de production (le mode réel exige qu'il soit promu)."""
    from hermes.models.bundle import ModelBundle

    b = ModelBundle.load(source)  # verifies hashes
    if target.exists():
        backup = target.with_name(target.name + ".previous")
        shutil.rmtree(backup, ignore_errors=True)
        target.rename(backup)
    shutil.copytree(source, target)
    typer.echo(f"installé : {target} (promu={b.promoted}, entraîné jusqu'au {b.meta.get('train_end')})")


def _okx_client(cfg: HermesConfig, demo: bool):  # type: ignore[no-untyped-def]
    from hermes.execution.okx.client import Credentials, OKXClient

    key, secret, pw = (os.environ.get(k) for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"))
    creds = Credentials(key, secret, pw) if key and secret and pw else None
    return OKXClient(creds, base_url=os.environ.get("OKX_BASE_URL", cfg.execution.okx_base_url), demo=demo)


@live_app.command("run")
def live_run(
    config: Path | None = ConfigOpt,
    overrides: list[str] = SetOpt,
    mode: str = typer.Option("paper", help="paper | demo | live"),
    model: Path = typer.Option(Path("artifacts/models/champion"), help="Bundle du modèle"),
    once: bool = typer.Option(False, help="Un seul cycle puis sortie"),
) -> None:
    """Lance le moteur (une décision par barre)."""
    _setup_logging()
    from hermes.data.live_feed import BinanceLiveFeed
    from hermes.execution.broker import PaperBroker
    from hermes.execution.okx_broker import OKXBroker
    from hermes.live.engine import LiveEngine
    from hermes.live.state import StateStore
    from hermes.models.bundle import ModelBundle

    cfg = _cfg(config, overrides)
    bundle = ModelBundle.load(model)
    # The strategy is the bundle's: its features, labels and portfolio settings win over the local file,
    # except for execution, live and risk settings which belong to the operator.
    cfg = bundle.config.model_copy(update={"execution": cfg.execution, "live": cfg.live, "risk": cfg.risk})
    if mode == "live" and not bundle.promoted and not cfg.live.allow_unpromoted:
        typer.echo("REFUS : ce modèle n'a pas franchi la porte de promotion. Mode réel interdit.", err=True)
        raise typer.Exit(2)
    store = StateStore(cfg.live.state_dir / mode)
    feed = BinanceLiveFeed(cfg.data.bar, cfg.live.history_bars)
    if mode == "paper":
        broker = PaperBroker(
            cfg.live.state_dir / mode / "paper_account.json",
            cfg.live.paper_initial_equity,
            cfg.costs.maker_fee,
            cfg.costs.taker_fee,
            cfg.costs.maker_fill_ratio,
        )
    else:
        client = _okx_client(cfg, demo=(mode == "demo"))
        if client.creds is None:
            typer.echo("OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE manquants", err=True)
            raise typer.Exit(2)
        syms = list(bundle.meta.get("symbols", []))  # type: ignore[arg-type]
        broker = OKXBroker(
            client, cfg.execution, syms, cfg.risk.exchange_leverage, cfg.costs.maker_fee, cfg.costs.taker_fee
        )
    engine = LiveEngine(cfg, bundle, feed, broker, store, mode, model_dir=model)

    async def _main() -> None:
        if once:
            await broker.start()
            d = await engine.step()
            typer.echo(
                json.dumps(
                    {
                        "bar": d.ts,
                        "equity": d.equity,
                        "ic_est": d.ic_est,
                        "risk": d.risk,
                        "targets": {k: round(v, 2) for k, v in d.targets.items()},
                        "notes": d.notes,
                    },
                    indent=1,
                )
            )
            await broker.stop()
        else:
            await engine.run_forever()

    asyncio.run(_main())


@live_app.command("status")
def live_status(state: Path = typer.Option(Path("state/paper"), help="Dossier d'état du mode")) -> None:
    """Affiche l'état publié par le moteur."""
    p = state / "status.json"
    if not p.exists():
        typer.echo("aucun état publié")
        raise typer.Exit(1)
    typer.echo(p.read_text())


@live_app.command("kill")
def live_kill(state_root: Path = typer.Option(Path("state"))) -> None:
    """Arrêt d'urgence : le moteur aplatit le portefeuille au prochain cycle et s'arrête de trader."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "KILL").touch()
    typer.echo(f"interrupteur posé : {state_root / 'KILL'}")


@live_app.command("resume")
def live_resume(state_root: Path = typer.Option(Path("state")), mode: str = typer.Option("paper")) -> None:
    """Lève l'arrêt (fichier KILL et état « halted ») — décision humaine explicite."""
    from hermes.live.state import StateStore

    (state_root / "KILL").unlink(missing_ok=True)
    store = StateStore(state_root / mode)
    rs = store.get("risk_state") or {}
    if isinstance(rs, dict):
        rs.update({"halted": False, "halt_reason": "", "peak_equity": 0.0})
        store.put("risk_state", rs)
    typer.echo("reprise autorisée ; le pic d'équité repart de l'équité actuelle")


@live_app.command("check")
def live_check(
    config: Path | None = ConfigOpt,
    overrides: list[str] = SetOpt,
    demo: bool = typer.Option(True, help="Compte démo OKX"),
) -> None:
    """Vérifie la connexion OKX (horloge, compte, solde, positions) sans passer d'ordre."""
    _setup_logging()
    cfg = _cfg(config, overrides)
    client = _okx_client(cfg, demo)

    async def _main() -> None:
        off = await client.sync_clock()
        typer.echo(f"décalage d'horloge : {off:.0f} ms")
        n = len(await client.instruments())
        typer.echo(f"instruments SWAP : {n}")
        if client.creds is None:
            typer.echo("pas de clés : vérification publique seulement")
            return
        conf = await client.account_config()
        typer.echo(f"compte : acctLv={conf.get('acctLv')} posMode={conf.get('posMode')}")
        bal = await client.balance()
        typer.echo(f"équité totale : {bal.get('totalEq')}")
        pos = await client.positions()
        typer.echo(f"positions ouvertes : {len([p for p in pos if float(p.get('pos') or 0)])}")
        await client.close()

    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    app()
