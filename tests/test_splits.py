import numpy as np

from hermes.validation.splits import cpcv, cpcv_paths, walk_forward


def test_walk_forward_purges_label_overlap():
    H = 24
    folds = walk_forward(n_bars=5000, test_bars=500, min_train_bars=1000, horizon=H, embargo=10)
    assert len(folds) >= 7
    for f in folds:
        assert f.train.max() + H < f.test.min()
        assert f.train.max() < f.test.min()
    # Test blocks tile the out-of-sample period without overlap.
    tests = np.concatenate([f.test for f in folds])
    assert len(tests) == len(np.unique(tests))


def test_rolling_window_length():
    folds = walk_forward(n_bars=5000, test_bars=500, min_train_bars=1000, horizon=10, embargo=0, train_bars=1500)
    assert all(len(f.train) <= 1500 for f in folds)


def test_cpcv_purging_and_paths():
    H, E = 20, 5
    folds = cpcv(1200, n_groups=6, k_test=2, horizon=H, embargo=E)
    assert len(folds) == 15
    for f in folds:
        test = set(f.test.tolist())
        for t in f.train:
            # No training label window [t, t+H] may touch a test bar.
            assert not any((t + k) in test for k in range(0, H + 1))
    paths = cpcv_paths(6, 2)
    assert len(paths) == 5
    assert all(len(p) == 6 for p in paths)
