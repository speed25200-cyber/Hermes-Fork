"""Aucun score sur un signal planté ne peut autoriser une promotion économique."""

import pytest

from hermes.config import with_overrides
from hermes.data.synthetic import make_synthetic_panel
from hermes.research.evaluate import promotion_decision


@pytest.mark.parametrize("passed", [7, 8, 9])
@pytest.mark.parametrize("source", ["synthetic", "binance_archive"])
def test_synthetic_panel_cannot_promote_even_if_every_criterion_passes(cfg_small, passed, source):
    panel = make_synthetic_panel(n_assets=2, n_bars=48, bar="15m")
    cfg = with_overrides(cfg_small, {"data.source": source})
    promoted, eligibility = promotion_decision(passed, cfg, panel)
    assert not promoted
    assert eligibility == {"eligible": False, "reason": "synthetic_data", "source": source, "synthetic": True}


def test_synthetic_configuration_cannot_promote_when_panel_flag_is_missing(cfg_small):
    panel = make_synthetic_panel(n_assets=2, n_bars=48, bar="15m")
    panel.meta.clear()
    cfg = with_overrides(cfg_small, {"data.source": "synthetic"})
    assert promotion_decision(9, cfg, panel)[0] is False


@pytest.mark.parametrize(("passed", "expected"), [(6, False), (7, True), (8, True), (9, True)])
def test_market_data_keeps_the_existing_seven_of_nine_policy(cfg_small, passed, expected):
    # Aucun rendement n'est évalué ici : seul le verdict de la porte reçoit un panneau sans marqueur synthétique.
    panel = make_synthetic_panel(n_assets=2, n_bars=48, bar="15m")
    panel.meta.clear()
    promoted, eligibility = promotion_decision(passed, cfg_small, panel)
    assert promoted is expected
    assert eligibility["eligible"] is True and eligibility["reason"] is None
