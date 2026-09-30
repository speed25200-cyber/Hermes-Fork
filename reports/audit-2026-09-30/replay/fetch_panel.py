import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from hermes.config import HermesConfig
from hermes.data.binance_archive import BinanceArchive
from hermes.data.panel import Panel, clean_panel, resample_panel
from hermes.data.store import trim_to_funding, with_execution_fields

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
root = Path("artifacts/diagnostic_2026")
artifact = Path("artifacts/research_books/reports/2026-09-24-research_30m_xl_lb_sres_books-35950527756")
score = pd.read_parquet(artifact / "walkforward/score.parquet")
cfg = HermesConfig.model_validate_json((artifact / "model/config.json").read_text())
symbols = list(score.columns)
start, end = date(2025, 7, 1), date(2026, 8, 31)
manifest = {
    "declared_at": datetime.now(UTC).isoformat(),
    "purpose": "Fixed OOF differential diagnostic; no training, tuning or promotion; not a fresh holdout",
    "evaluation_start": "2026-01-01T00:00:00+00:00",
    "evaluation_end": str(score.index[-1]),
    "warmup_start": str(start),
    "source": "Binance public monthly futures archives; OKX listing calendar",
    "artifact_zip_sha256": "adb69d3e7225e08f03cf853f51d3299daf0f3e4031966a537ef097cec1ac123c",
    "artifact_run": "speed25200-cyber/Hermes/actions/runs/35950527756",
    "baseline_commit": "22facd44104a7994cc3365f0696898da4c98d789",
    "corrected_commit": "b6b52d32109147535204c5d2cda9a76ccec14bcd",
    "symbols": symbols,
    "config_sha256": hashlib.sha256((artifact / "model/config.json").read_bytes()).hexdigest(),
    "oof_sha256": hashlib.sha256((artifact / "walkforward/score.parquet").read_bytes()).hexdigest(),
    "limits": [
        "Original historical selection universe; not new discovery or new independent holdout",
        "Truncated warmup and flat portfolio at evaluation start; "
        "not exact recreation of original full-history account",
        "Funding and bars from Binance; actual OKX execution costs not observed",
        "Legacy OOF artifact lacks source-and-data training provenance; used explicitly as diagnostic input only",
    ],
}
mp = root / "declaration.json"
if not mp.exists():
    mp.write_text(json.dumps(manifest, indent=2) + "\n")
frames = {}
cache = root / "data"


# Independent symbol downloads, each limited to six monthly requests.
def one(sym):
    p = cache / "parsed/15m" / f"{sym}.parquet"
    if p.exists():
        return sym, pd.read_parquet(p)
    archive = BinanceArchive(cache, workers=6)
    try:
        return sym, archive.symbol_frame(sym, "15m", start, end, True, False)
    finally:
        archive.client.close()


with ThreadPoolExecutor(4) as pool:
    futures = {pool.submit(one, s): s for s in symbols}
    for i, future in enumerate(as_completed(futures), 1):
        sym, frame = future.result()
        if len(frame):
            frames[sym] = frame.loc[str(start) : str(end)].astype("float32")
        logging.info("Fetched %d/%d %s rows=%d", i, len(symbols), sym, len(frame))
panel = Panel.from_long(frames, "15m")
panel.meta["source"] = "binance_archive"
panel = with_execution_fields(panel, cfg.data.model_copy(update={"cache_dir": cache}))
panel = resample_panel(panel, "30m")
panel = trim_to_funding(clean_panel(panel))
if panel.index[-1] > score.index[-1]:
    panel = panel.iloc(slice(0, panel.index.searchsorted(score.index[-1], side="right")))
panel.save(root / "panel")
logging.info("Saved panel shape=%s %s..%s", panel.shape, panel.index[0], panel.index[-1])
