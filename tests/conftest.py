import numpy as np
import pytest

from hermes.config import load_config
from hermes.data.synthetic import make_synthetic_panel


@pytest.fixture(scope="session")
def small_panel():
    return make_synthetic_panel(n_assets=12, n_bars=24 * 120, seed=5, signal_strength=1.0)


@pytest.fixture(scope="session")
def cfg_small():
    return load_config(
        None,
        **{
            "data.universe.top_n": 10,
            "data.universe.min_history_days": 5,
            "model.gbm.seeds": [1],
            "model.gbm.n_estimators": 300,
            "model.gbm.learning_rate": 0.08,
            "model.gbm.min_data_in_leaf": 500,
            "validation.min_train_bars": 24 * 60,
            "validation.test_bars": 24 * 20,
            "validation.val_bars": 24 * 15,
        },
    )


@pytest.fixture
def rng():
    return np.random.default_rng(0)
