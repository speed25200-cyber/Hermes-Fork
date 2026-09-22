import numpy as np

from hermes.config import UniverseConfig
from hermes.data.universe import base_asset, is_excluded, universe_mask


def test_universe_respects_top_n_and_history(small_panel):
    cfg = UniverseConfig(top_n=5, min_history_days=10)
    m = universe_mask(small_panel, cfg, 24)
    assert m.sum(axis=1).max() <= 5
    close = small_panel["close"]
    first = close.notna().idxmax()
    for sym in m.columns:
        members = m.index[m[sym]]
        if len(members):
            assert (members[0] - first[sym]).total_seconds() >= 10 * 86400 - 3600


def test_delisted_contract_leaves(small_panel):
    m = universe_mask(small_panel, UniverseConfig(top_n=12, min_history_days=1), 24)
    dead = small_panel["close"].isna() & small_panel["close"].ffill().notna()
    assert not (m & dead).any().any()


def test_exclusions():
    u = UniverseConfig()
    assert is_excluded("USDCUSDT", u) and is_excluded("XAUUSDT", u) and is_excluded("TSLAUSDT", u)
    assert not is_excluded("BTCUSDT", u) and not is_excluded("1000PEPEUSDT", u)
    assert base_asset("1000PEPEUSDT") == "PEPE" and base_asset("1INCHUSDT") == "1INCH"
    assert np.all([not is_excluded(s, u) for s in ("SYRUPUSDT", "JUPUSDT", "SUPERUSDT")])


def test_clean_panel_fills_only_short_interior_gaps(small_panel):
    from hermes.data.panel import clean_panel

    p = small_panel.subset(["BTCUSDT", "ETHUSDT"])
    close = p["close"].copy()
    close.iloc[100:102, 0] = np.nan  # 2-bar interior gap -> filled
    close.iloc[200:210, 1] = np.nan  # 10-bar gap -> 3 filled, 7 left missing
    fields = dict(p.fields)
    fields["close"] = close
    q = clean_panel(type(p)(fields, bar=p.bar))
    assert q["close"].iloc[100:102, 0].notna().all()
    assert (q["quote_volume"].iloc[100:102, 0] == 0).all()
    assert q["close"].iloc[200:203, 1].notna().all() and q["close"].iloc[203:210, 1].isna().all()
