"""Check two model ingredients against the public Delhi bus GTFS schedule (an independent source).

1. Trip time: the model's end-to-end time for a route at an hour (ticket-derived minutes per stop x stops)
   vs the scheduled duration of GTFS trips on the same route starting in that hour.
2. Buses in service: the model's buses per half-hour (distinct vehicles seen issuing tickets)
   vs the number of scheduled GTFS trips on the road at the middle of that half-hour.

The GTFS timetable is the newest public one (calendar 2022-24; the official OTD feed was last updated June 2024 with fewer routes). It is a plan, not observed operations, so this is a sanity check of scale and shape,
not ground truth. Routes are matched by id (GTFS route_long_name == ticket route, e.g. 717AUP).
Run after build_crowding.py and build_traveltime.py:  python pipeline/validate_gtfs.py
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
GTFS = ROOT / "data" / "raw" / "gtfs" / "delhi-bus.zip"


def key(r: str) -> str:
    return re.sub(r"[^A-Z0-9+\-()]", "", str(r).upper())


def secs(s: pd.Series) -> pd.Series:
    p = s.str.split(":", expand=True).astype(int)
    return p[0] * 3600 + p[1] * 60 + p[2]


def main() -> None:
    z = zipfile.ZipFile(GTFS)
    routes = pd.read_csv(z.open("routes.txt"), usecols=["route_id", "route_long_name"])
    trips = pd.read_csv(z.open("trips.txt"), usecols=["route_id", "trip_id"])
    st = pd.read_csv(z.open("stop_times.txt"), usecols=["trip_id", "arrival_time", "stop_sequence"])
    st["t"] = secs(st.arrival_time)
    span = st.groupby("trip_id").t.agg(["min", "max"]).rename(columns={"min": "t0", "max": "t1"})
    span["n_stops"] = st.groupby("trip_id").size()
    trips = trips.join(span, on="trip_id").merge(routes, on="route_id")
    trips["route"] = trips.route_long_name.map(key)
    trips["dur"] = (trips.t1 - trips.t0) / 60

    orders = pd.read_csv(PROC / "route_stop_order.csv")
    nstops = {key(r): len(json.loads(s)) for r, s in zip(orders.route, orders.stops)}
    rname = {key(r): r for r in orders.route}
    trips = trips[trips.route.isin(nstops) & (trips.dur > 5)]

    # ---------------------------------------------------------------- 1. trip time
    rh = pd.read_csv(PROC / "route_hour_traveltime.csv")
    rh = rh[(rh.daytype == "weekday") & (rh.n_obs >= 5)]
    rt = dict(pd.read_csv(PROC / "route_traveltime.csv").itertuples(index=False))
    city = pd.read_csv(PROC / "city_hour_traveltime.csv")
    city = dict(zip(city[city.daytype == "weekday"].hour, city[city.daytype == "weekday"].city_min_per_stop))
    rhd = {(key(r), h): m for r, h, m in zip(rh.route, rh.hour, rh.min_per_stop)}
    trips["hour"] = (trips.t0 // 3600).astype(int) % 24
    g = trips.groupby(["route", "hour"]).dur.median().reset_index()
    g["model"] = [(rhd.get((r, h)) or rt.get(rname[r]) or city.get(h, 2.0)) * (nstops[r] - 1) for r, h in zip(g.route, g.hour)]
    g["ratio"] = g.model / g.dur
    city_cmp = g.groupby("hour").agg(model=("model", "median"), gtfs=("dur", "median"))
    tt = {
        "route_hours_compared": int(len(g)), "routes_compared": int(g.route.nunique()),
        "median_ratio_model_to_schedule": round(float(g.ratio.median()), 2),
        "share_within_25pct": round(float(((g.ratio > 0.75) & (g.ratio < 1.25)).mean()), 3),
        "share_within_50pct": round(float(((g.ratio > 0.5) & (g.ratio < 1.5)).mean()), 3),
        "corr_route_hour": round(float(np.corrcoef(g.model, g.dur)[0, 1]), 3),
        "by_hour": {int(h): {"model": round(float(r.model), 1), "schedule": round(float(r.gtfs), 1)} for h, r in city_cmp.iterrows()},
    }

    # ---------------------------------------------------------------- 2. buses in service
    seg = pd.read_parquet(PROC / "segment_crowding.parquet", columns=["route", "daytype", "bin", "buses_est"])
    seg = seg[seg.daytype == "weekday"]
    model_b = seg.groupby(["route", "bin"], observed=True).buses_est.median().reset_index()
    model_b["route"] = model_b.route.astype(str).map(key)
    mids = np.arange(48) * 1800 + 900
    rows = []
    for r, x in trips.groupby("route"):
        t0, t1 = x.t0.to_numpy() % 86400, x.t1.to_numpy() % 86400
        for b, m in enumerate(mids):
            rows.append((r, b, int(((t0 <= m) & (t1 >= m)).sum())))
    sched = pd.DataFrame(rows, columns=["route", "bin", "sched"])
    cmp_ = model_b.merge(sched, on=["route", "bin"])
    cmp_ = cmp_[(cmp_.sched > 0) & cmp_.bin.between(12, 43)]
    by_route = cmp_.groupby("route")[["buses_est", "sched"]].median()
    rc = cmp_.groupby("route").apply(lambda d: d.buses_est.corr(d.sched) if len(d) > 5 and d.sched.std() > 0 and d.buses_est.std() > 0 else np.nan,
                                     include_groups=False).dropna()
    bus = {
        "route_bins_compared": int(len(cmp_)), "routes_compared": int(cmp_.route.nunique()),
        "median_ratio_model_to_schedule": round(float((cmp_.buses_est / cmp_.sched).median()), 2),
        "corr_across_routes_typical_fleet": round(float(np.corrcoef(by_route.buses_est, by_route.sched)[0, 1]), 3),
        "median_within_route_corr_through_day": round(float(rc.median()), 3),
    }
    out = {"trip_time": tt, "buses_in_service": bus,
           "note": "Latest public GTFS timetable (calendar 2022-24; a plan, not observed runs); tickets Oct 2026. Routes matched by id."}
    s = json.loads((PROC / "summary.json").read_text(encoding="utf-8"))
    s["gtfs_check"] = out
    (PROC / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "by_hour"} if isinstance(v, dict) else v for k, v in out.items()}, indent=2))
    print("by hour (model vs schedule, minutes):", {h: (v["model"], v["schedule"]) for h, v in tt["by_hour"].items() if 5 <= h <= 22})


if __name__ == "__main__":
    main()
