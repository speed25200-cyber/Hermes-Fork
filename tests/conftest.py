import numpy as np
import pytest

from hermes.config import load_config
from hermes.data.synthetic import make_synthetic_panel

BPD = 96  # tests run on the default 15-minute timeframe


@pytest.fixture(scope="session")
def small_panel():
    return make_synthetic_panel(n_assets=12, n_bars=BPD * 60, bar="15m", seed=5, signal_strength=1.0)


@pytest.fixture(scope="session")
def cfg_small():
    return load_config(
        None,
        **{
            "data.bar": "15m",
            "data.universe.top_n": 10,
            "data.universe.min_history_days": 3,
            "data.universe.venue": "any",  # synthetic contracts exist on no exchange
            "model.gbm.seeds": [1],
            "model.gbm.n_estimators": 300,
            "model.gbm.learning_rate": 0.08,
            "model.gbm.min_data_in_leaf": 500,
            "validation.min_train_days": 20,
            "validation.test_days": 7,
            "validation.val_days": 5,
            "validation.train_sample_minutes": 30,
        },
    )


@pytest.fixture
def rng():
    return np.random.default_rng(0)
