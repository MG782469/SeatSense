"""Travel time per stop-hop from the tickets themselves.

A ticket is issued when the passenger boards, so for one bus on one day the
ticket timestamps at successive boarding stops trace the bus along its route.
We reconstruct those traces (rows that carry a vehicle id, ~51%), measure
minutes per stop-hop, and summarise by route, day type and hour.

Run after build_crowding.py:  python pipeline/build_traveltime.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "October-Delhi.csv"
OUT = ROOT / "data" / "processed"


def main() -> None:
    orders = pd.read_csv(OUT / "route_stop_order.csv")
    rank = {r: {s: i for i, s in enumerate(json.loads(o))} for r, o in zip(orders.route, orders.stops)}

    df = pd.read_csv(RAW, dtype=str, usecols=["route", "vehicle_number", "start_stop_name", "created_at"])
    df = df[df.vehicle_number.notna() & ~df.vehicle_number.isin(["NULL", ""]) & df.route.isin(rank.keys())]
    df["ts"] = pd.to_datetime(df.created_at.str.slice(0, 19))
    df["idx"] = [rank[r].get(s.strip(), -1) for r, s in zip(df.route, df.start_stop_name)]
    df = df[df.idx >= 0].sort_values(["vehicle_number", "route", "ts"])

    # consecutive tickets of the same bus on the same route
    g = df.groupby(["vehicle_number", "route"], sort=False)
    d_idx = g.idx.diff()
    d_min = g.ts.diff().dt.total_seconds() / 60
    hops = pd.DataFrame({"route": df.route, "ts": df.ts, "d_idx": d_idx, "d_min": d_min})
    # same trip, moving forward, plausible gap
    hops = hops[(hops.d_idx >= 1) & (hops.d_idx <= 15) & (hops.d_min > 0) & (hops.d_min <= 60)]
    hops["mph"] = hops.d_min / hops.d_idx  # minutes per stop-hop
    hops = hops[(hops.mph >= 0.3) & (hops.mph <= 10)]
    hops["hour"] = hops.ts.dt.hour
    hops["daytype"] = np.where(hops.ts.dt.dayofweek >= 5, "weekend", "weekday")

    tt = hops.groupby(["route", "daytype", "hour"]).mph.agg(["median", "count"]).reset_index()
    tt.columns = ["route", "daytype", "hour", "min_per_stop", "n_obs"]
    city = hops.groupby(["daytype", "hour"]).mph.median().rename("city_min_per_stop").reset_index()
    route_all = hops.groupby("route").mph.median().rename("route_min_per_stop").reset_index()
    tt.to_csv(OUT / "route_hour_traveltime.csv", index=False)
    city.to_csv(OUT / "city_hour_traveltime.csv", index=False)
    route_all.to_csv(OUT / "route_traveltime.csv", index=False)
    print(f"hops used: {len(hops):,}; routes with times: {tt.route.nunique()}")
    print(city[city.daytype == "weekday"].round(2).to_string(index=False))


if __name__ == "__main__":
    main()
