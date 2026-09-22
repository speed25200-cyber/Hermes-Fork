"""Cross-sectional attention network (optional, requires ``torch``).

Architecture (in the spirit of Kelly-Kuznetsov-Malamud-Xu's *AI asset pricing models* and PatchTST):

1. **Temporal encoder**, shared by all contracts: the last ``seq_len`` bars of features are cut into
   patches of 4 bars, embedded, and passed through a small Transformer encoder; the last token summarises
   each contract's recent history.
2. **Cross-sectional encoder**: a Transformer layer attends *across contracts at the same timestamp*
   (padding masked), so each forecast is conditioned on what the rest of the market is doing.
3. Linear head -> one score per contract.

The loss is the negative per-timestamp Pearson correlation (IC loss) plus a small MSE anchor: the
portfolio monetises ranking quality, not squared error. Early stopping on validation IC; warm-start from
the previous walk-forward fold keeps retraining cheap.

Data layout: the long feature matrix ``X`` (member rows only) plus ``pos[t, s]``, the row of contract ``s``
at bar ``t`` (-1 if it was not a member). A window is gathered on the fly; bars where the contract was not
a member are zero-padded. Nothing dense of size (time x contracts x features) is ever materialised.
"""

from __future__ import annotations

import copy

import numpy as np

from hermes.config import DeepConfig

try:  # pragma: no cover - optional dependency
    import torch
    from torch import nn

    TORCH = True
except Exception:  # pragma: no cover
    TORCH = False

PATCH = 4


def available() -> bool:
    return TORCH


def build_pos(t_pos: np.ndarray, s_pos: np.ndarray, n_bars: int, n_symbols: int) -> np.ndarray:
    pos = np.full((n_bars, n_symbols), -1, dtype=np.int64)
    pos[t_pos, s_pos] = np.arange(len(t_pos))
    return pos


if TORCH:

    class _Net(nn.Module):
        def __init__(self, n_features: int, cfg: DeepConfig):
            super().__init__()
            d = cfg.d_model
            self.n_patches = cfg.seq_len // PATCH
            self.patch = nn.Linear(n_features * PATCH, d)
            self.pos = nn.Parameter(torch.zeros(1, self.n_patches, d))
            t_layer = nn.TransformerEncoderLayer(d, cfg.n_heads, 2 * d, cfg.dropout, batch_first=True, norm_first=True)
            self.temporal = nn.TransformerEncoder(t_layer, cfg.n_layers, enable_nested_tensor=False)
            c_layer = nn.TransformerEncoderLayer(d, cfg.n_heads, 2 * d, cfg.dropout, batch_first=True, norm_first=True)
            self.cross = nn.TransformerEncoder(c_layer, 1, enable_nested_tensor=False)
            self.head = nn.Sequential(nn.LayerNorm(d), nn.Linear(d, 1))

        def forward(self, x: torch.Tensor, member: torch.Tensor) -> torch.Tensor:
            # x: (B, M, L, F); member: (B, M) bool
            B, M, L, F = x.shape
            x = x[:, :, L - self.n_patches * PATCH :, :].reshape(B * M, self.n_patches, PATCH * F)
            h = self.temporal(self.patch(x) + self.pos)[:, -1, :].reshape(B, M, -1)
            pad = ~member
            all_pad = pad.all(dim=1)
            if all_pad.any():
                pad = pad.clone()
                pad[all_pad, 0] = False
            h = self.cross(h, src_key_padding_mask=pad)
            return self.head(h).squeeze(-1)

    def _ic_loss(score: torch.Tensor, y: torch.Tensor, m: torch.Tensor) -> torch.Tensor:
        mf = m.float()
        n = mf.sum(dim=1).clamp(min=1)
        s_mean = (score * mf).sum(1) / n
        y_mean = (y * mf).sum(1) / n
        sc = (score - s_mean[:, None]) * mf
        yc = (y - y_mean[:, None]) * mf
        corr = (sc * yc).sum(1) / (torch.sqrt((sc**2).sum(1) * (yc**2).sum(1)) + 1e-8)
        valid = n >= 3
        ic = corr[valid].mean() if valid.any() else corr.mean() * 0
        mse = (((score - y) ** 2) * mf).sum() / mf.sum().clamp(min=1)
        return -ic + 0.05 * mse


class DeepModel:
    name = "deep"

    def __init__(self, cfg: DeepConfig, max_members: int = 64):
        if not TORCH:
            raise RuntimeError("torch is not installed: pip install 'hermes[deep]'")
        self.cfg = cfg
        self.max_members = max_members
        self.net: _Net | None = None
        self.mu: np.ndarray | None = None
        self.sd: np.ndarray | None = None
        self.val_ic = float("nan")

    # -- batching -------------------------------------------------------------------------------------------
    def _norm_rows(self, X: np.ndarray, rows: np.ndarray) -> np.ndarray:
        assert self.mu is not None and self.sd is not None
        out = np.zeros((len(rows), X.shape[1]), dtype=np.float32)
        ok = rows >= 0
        if ok.any():
            Z = (X[rows[ok]] - self.mu) / self.sd
            out[ok] = np.clip(np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0), -5, 5)
        return out

    def _batch(
        self, X: np.ndarray, pos: np.ndarray, t_idx: np.ndarray, y: np.ndarray | None = None
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor | None, list[np.ndarray]]:
        L = self.cfg.seq_len
        M = self.max_members
        B = len(t_idx)
        members = [np.nonzero(pos[t] >= 0)[0][:M] for t in t_idx]
        win_rows = np.full((B, M, L), -1, dtype=np.int64)
        mem = np.zeros((B, M), dtype=bool)
        for b, (t, ms) in enumerate(zip(t_idx, members)):
            k = len(ms)
            mem[b, :k] = True
            lo = max(0, t - L + 1)
            block = pos[lo : t + 1, ms].T  # (k, <=L)
            win_rows[b, :k, L - block.shape[1] :] = block
        feats = self._norm_rows(X, win_rows.reshape(-1)).reshape(B, M, L, X.shape[1])
        yt = None
        if y is not None:
            cur = win_rows[:, :, -1]  # the row of each member at the decision bar
            yv = np.where(cur >= 0, y[np.maximum(cur, 0)], np.nan)
            ok = mem & np.isfinite(yv)
            mem = ok
            yt = torch.from_numpy(np.nan_to_num(yv).astype(np.float32))
        return torch.from_numpy(feats), torch.from_numpy(mem), yt, members

    # -- training -------------------------------------------------------------------------------------------
    def fit(
        self,
        X: np.ndarray,
        pos: np.ndarray,
        y: np.ndarray,
        train_t: np.ndarray,
        val_t: np.ndarray | None = None,
        warm_start: DeepModel | None = None,
    ) -> DeepModel:
        cfg = self.cfg
        torch.manual_seed(cfg.seed)
        rng = np.random.default_rng(cfg.seed)
        train_t = train_t[train_t >= cfg.seq_len - 1]
        rows = pos[train_t].reshape(-1)
        rows = rows[rows >= 0]
        sample = rows if len(rows) <= 200_000 else rng.choice(rows, 200_000, replace=False)
        self.mu = np.nan_to_num(np.nanmean(X[sample], axis=0)).astype(np.float32)
        sd = np.nanstd(X[sample], axis=0)
        self.sd = np.where(np.isfinite(sd) & (sd > 1e-9), sd, 1.0).astype(np.float32)
        if warm_start is not None and warm_start.net is not None:
            self.net = copy.deepcopy(warm_start.net)
            epochs = max(2, cfg.epochs // 3)
        else:
            self.net = _Net(X.shape[1], cfg)
            epochs = cfg.epochs
        opt = torch.optim.AdamW(self.net.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
        best_state, best_ic, bad = None, -np.inf, 0
        for _ in range(epochs):
            self.net.train()
            order = rng.permutation(train_t)
            for s in range(0, len(order), cfg.batch_timestamps):
                xb, mb, yb, _ = self._batch(X, pos, order[s : s + cfg.batch_timestamps], y)
                loss = _ic_loss(self.net(xb, mb), yb, mb)
                opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.net.parameters(), 1.0)
                opt.step()
            if val_t is not None and len(val_t):
                ic = self.evaluate_ic(X, pos, y, val_t)
                if ic > best_ic:
                    best_ic, best_state, bad = ic, copy.deepcopy(self.net.state_dict()), 0
                else:
                    bad += 1
                    if bad >= cfg.patience:
                        break
        if best_state is not None:
            self.net.load_state_dict(best_state)
            self.val_ic = float(best_ic)
        return self

    def predict(self, X: np.ndarray, pos: np.ndarray, t_idx: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Scores for the members of each bar in ``t_idx``: (rows into X, scores, bar of each row)."""
        assert self.net is not None
        self.net.eval()
        out_rows, out_scores, out_bars = [], [], []
        with torch.no_grad():
            for s in range(0, len(t_idx), 256):
                idx = t_idx[s : s + 256]
                xb, mb, _, members = self._batch(X, pos, idx)
                sc = self.net(xb, mb).numpy()
                for b, (t, ms) in enumerate(zip(idx, members)):
                    out_rows.append(pos[t, ms])
                    out_scores.append(sc[b, : len(ms)])
                    out_bars.append(np.full(len(ms), t))
        if not out_rows:
            return np.zeros(0, dtype=np.int64), np.zeros(0), np.zeros(0, dtype=np.int64)
        return np.concatenate(out_rows), np.concatenate(out_scores).astype(np.float64), np.concatenate(out_bars)

    def evaluate_ic(self, X: np.ndarray, pos: np.ndarray, y: np.ndarray, t_idx: np.ndarray) -> float:
        from hermes.models.base import mean_group_corr

        rows, sc, bars = self.predict(X, pos, t_idx[t_idx >= self.cfg.seq_len - 1][::2])
        yy = y[rows] if len(rows) else np.zeros(0)
        ok = np.isfinite(yy)
        return mean_group_corr(sc[ok], yy[ok], bars[ok]) if ok.any() else 0.0
