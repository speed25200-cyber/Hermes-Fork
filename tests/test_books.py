"""1/N books (``portfolio.books``): sub-books on equal capital shares, risk and stops on the sum, netted trades."""

import numpy as np
import pandas as pd
import pytest

from hermes.backtest.engine import BookSignals, run_backtest
from hermes.config import BookSetting, load_config
from hermes.data.synthetic import make_synthetic_panel
from hermes.research.dataset import build_dataset
from hermes.research.evaluate import book_signal, make_signal, single_book


@pytest.fixture(scope="module")
def market():
    cfg = load_config(
        None,
        **{
            "data.bar": "1h",
            "data.universe.top_n": 8,
            "data.universe.min_history_days": 3,
            "data.universe.venue": "any",
            "risk.stop_loss_daily_sigmas": 3.0,  # tight stops so that the test exercises stop exits
        },
    )
    panel = make_synthetic_panel(n_assets=10, n_bars=24 * 200, bar="1h", seed=31, signal_strength=1.0)
    ds = build_dataset(panel, cfg)
    rng = np.random.default_rng(0)
    oos = ds.mask.index[24 * 150]
    tgt = ds.targets.primary
    score = (tgt + rng.normal(0, 3, tgt.shape)).where(ds.mask).where(ds.mask.index.to_series() >= oos, axis=0)
    return cfg, ds, score, pd.Series(0.03, index=ds.mask.index), oos


def test_two_identical_sub_books_are_the_single_book(market):
    cfg, ds, score, prior, oos = market
    sig = make_signal(score, ds, prior, cfg)
    single = run_backtest(ds.panel, ds.mask, ds.feats.aux, sig, cfg, start=oos)
    twin = BookSignals([(cfg.portfolio, sig), (cfg.portfolio, sig)])
    both = run_backtest(ds.panel, ds.mask, ds.feats.aux, twin, cfg, start=oos)
    assert single.stats["turnover"].sum() > 0 and single.stats["stops"].sum() > 0
    np.testing.assert_allclose(both.returns.to_numpy(), single.returns.to_numpy(), atol=1e-10)
    np.testing.assert_allclose(both.stats["gross"].to_numpy(), single.stats["gross"].to_numpy(), atol=1e-9)


def test_one_over_n_book_nets_its_trades(market):
    cfg, ds, score, prior, oos = market
    H = sorted(ds.targets.residual)
    settings = [
        BookSetting(holding_horizon=H[0], cost_aversion=0.5),
        BookSetting(holding_horizon=H[-1], cost_aversion=2.0),
    ]
    multi = cfg.model_copy(update={"portfolio": cfg.portfolio.model_copy(update={"books": tuple(settings)})})
    sig = book_signal(score, ds, prior, multi)
    assert isinstance(sig, BookSignals) and [p.holding_horizon for p, _ in sig.members] == [H[0], H[-1]]
    book = run_backtest(ds.panel, ds.mask, ds.feats.aux, sig, multi, start=oos)
    alone = []
    for s in settings:
        c = single_book(multi, holding_horizon=s.holding_horizon, cost_aversion=s.cost_aversion)
        assert not c.portfolio.books
        alone.append(run_backtest(ds.panel, ds.mask, ds.feats.aux, book_signal(score, ds, prior, c), c, start=oos))
    # Netting: the combined book trades (and pays) less than the two books run side by side on half the capital.
    sep_turnover = 0.5 * sum(a.stats["turnover"].sum() for a in alone)
    assert 0 < book.stats["turnover"].sum() < sep_turnover
    fees = lambda b: b.stats[["fees", "spread", "impact"]].sum().sum()  # noqa: E731
    assert fees(book) < 0.5 * sum(fees(a) for a in alone)
    # Its P&L before costs is close to the average of the two books (same positions, up to the overlay).
    gross = book.stats["gross_pnl"].sum()
    mean_gross = 0.5 * sum(a.stats["gross_pnl"].sum() for a in alone)
    assert abs(gross - mean_gross) < 0.25 * abs(mean_gross) + 0.01


def test_single_book_configs_keep_their_hash():
    from hermes.research.run import config_hash

    cfg = load_config(None)
    one = (BookSetting(holding_horizon=4, cost_aversion=1),)
    multi = cfg.model_copy(update={"portfolio": cfg.portfolio.model_copy(update={"books": one})})
    import hashlib
    import json

    d = json.loads(cfg.model_dump_json())
    d["data"].pop("cache_dir", None)
    d["data"].pop("download_workers", None)
    d["data"].pop("end", None)
    d.pop("live", None)
    d.pop("execution", None)
    d["validation"].pop("n_trials", None)
    d["portfolio"].pop("books", None)
    assert config_hash(cfg) == hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]
    assert config_hash(multi) != config_hash(cfg)
