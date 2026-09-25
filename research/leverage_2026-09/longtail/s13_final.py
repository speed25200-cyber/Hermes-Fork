"""Step 13: compact machine-readable summary of everything reported (all numbers produced by the scripts above)."""
import json
import numpy as np, pandas as pd
from lt_core import *

E = json.load(open(f"{W}/out/eval_main.json"))
D = Data()
from s8_eval import weights_for, sub  # noqa: E402  (re-uses the evaluated configurations)


def n_trades(cfg):
    rows_all, Wt, _, _ = weights_for(cfg)
    ro, wo = sub(rows_all, Wt, OOS0, OOS1)
    held = np.abs(wo) > 0
    ent = (held[1:] & ~held[:-1]).sum() + held[0].sum()
    ext = (~held[1:] & held[:-1]).sum()
    flip = (np.sign(wo[1:]) * np.sign(wo[:-1]) < 0).sum()
    return int(ent + ext + flip), int(len(ro))


out = {"evaluated": {}}
for name, o in E.items():
    cfg = o["cfg"]
    tr, nreb = n_trades(cfg)
    out["evaluated"][name] = dict(
        cfg=cfg, is_sharpe=o["is"]["sharpe"], is_years=o["is"]["years"], oos_sharpe=o["oos"]["sharpe"],
        oos_gross_sharpe=o["oos_gross"]["sharpe"], oos_cagr=o["oos"]["cagr"], oos_vol=o["oos"]["vol"], oos_mdd_close=o["oos"]["mdd"],
        oos_mdd_intrabar_mark=o["oos"]["mdd_ib"], y2025=o["bar1"]["y2025"], y2026=o["bar1"]["y2026"],
        oos_cost_per_year=o["oos"]["cost_frac"], oos_funding_per_year=o["oos"]["fund_frac"], oos_turnover_x=o["oos"]["turn_x"],
        oos_position_entries_exits=tr, oos_rebalances=nreb, bar1=o["bar1"], bar2={k: v for k, v in o["bar2"].items()},
        bar3=o["bar3"], bar4=o["bar4"], corr_with_current_book=o.get("corr_with_book"),
        book_oos_sharpe_same_days=o.get("book_oos_sharpe_same_days"), checks=dict(last_price_extremes=o["oos_last_price_extremes"],
        funding=o["funding_check_2025_01_to_09"]))
out["grid_distribution_taker_prereg"] = pd.read_csv(f"{W}/out/grid_distribution.csv").to_dict("records")
out["grid_distribution_maker_posthoc"] = pd.read_csv(f"{W}/out/grid_maker_distribution.csv").to_dict("records")
out["ic_table_is_oos"] = pd.read_csv(f"{W}/out/ic_table.csv").round(4).to_dict("records")
out["diag_leg_spreads_R24"] = pd.read_csv(f"{W}/out/diag_legs_R24.csv").round(2).to_dict("records")
json.dump(out, open(f"{W}/out/final_summary.json", "w"), indent=1, default=str)
for n, v in out["evaluated"].items():
    print(n, "trades(entries/exits/flips) OOS", v["oos_position_entries_exits"], "rebalances", v["oos_rebalances"])
