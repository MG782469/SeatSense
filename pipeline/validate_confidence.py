"""Does the confidence grade mean anything? Split-half test.

October is split into odd and even dates. For every route-segment-half-hour we recompute the crowding level
from each half on its own (same scale, bus count and capacity as the full model) and check how often the two
halves agree, separately for cells graded high / medium / low. A useful grade should agree more when it is higher.

Run after build_crowding.py:  python pipeline/validate_confidence.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
from build_crowding import CFG, OUT, load, segment_loads  # noqa: E402


def main() -> None:
    df = load()
    seg = pd.read_parquet(OUT / "segment_crowding.parquet")
    orders = {r: json.loads(o) for r, o in pd.read_csv(OUT / "route_stop_order.csv")[["route", "stops"]].itertuples(index=False)}
    key = ["route", "daytype", "bin", "seg"]
    full = seg[key + ["eticket_per_day", "onboard_mid", "seats", "demand_ratio", "confidence", "level"]].copy()
    for c in ("route", "daytype"):
        full[c] = full[c].astype(str)
    # factor that turns e-tickets per day into riders per bus in the full model (scale / buses)
    full["k"] = np.where(full.eticket_per_day > 0, full.onboard_mid / full.eticket_per_day, 0)
    crush = full.onboard_mid / full.demand_ratio.replace(0, np.nan)

    cuts = CFG["level_cuts"]
    levels = {}
    for name, part in (("odd", df[df.ts.dt.day % 2 == 1]), ("even", df[df.ts.dt.day % 2 == 0])):
        days = part.groupby("daytype")["date"].nunique()
        s = segment_loads(part, orders)
        s["e"] = s.eticket_pax / s.daytype.map(days)
        # same thin-cell pooling as the model (0.25 / 0.5 / 0.25 over neighbouring half-hours)
        s = s.sort_values(["route", "daytype", "seg", "bin"]).reset_index(drop=True)
        g = s.groupby(["route", "daytype", "seg"], sort=False)
        px = g["e"].shift(1).where(g["bin"].shift(1) == s["bin"] - 1, 0.0).fillna(0.0)
        nx = g["e"].shift(-1).where(g["bin"].shift(-1) == s["bin"] + 1, 0.0).fillna(0.0)
        thin = s["n_tickets"] < CFG["conf_med_tickets"]
        s.loc[thin, "e"] = (0.5 * s["e"] + 0.25 * (px + nx))[thin]
        m = full.merge(s[key + ["e"]], on=key, how="left").fillna({"e": 0.0})
        load_ = np.minimum(m.e * m.k, crush.fillna(1e9))
        lv = np.digitize(load_ / m.seats, cuts)
        lv = np.where(m.e * m.k >= 0.95 * crush.fillna(1e9), 3, lv)
        levels[name] = lv
    full["agree"] = levels["odd"] == levels["even"]
    full["near"] = np.abs(levels["odd"] - levels["even"]) <= 1
    day = full[full.bin.between(12, 43)]
    busy = day[day.level.astype(str) != "seats"]  # the answers that change a commuter's choice
    res = {g: {"cells": int(len(x)), "same_level": round(float(x.agree.mean()), 3),
               "within_one_level": round(float(x.near.mean()), 3),
               "busy_cells": int((x.level.astype(str) != "seats").sum()),
               "busy_same_level": round(float(busy[busy.confidence == g].agree.mean()), 3)}
           for g, x in day.groupby("confidence", observed=True)}
    print(json.dumps(res, indent=2))
    summ = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    summ["split_half_agreement_by_confidence"] = res
    (OUT / "summary.json").write_text(json.dumps(summ, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
