"""SeatSense crowding pipeline.

Raw ONDC e-ticket records -> per-segment on-board load and crowding level,
by route-direction, day type (weekday / weekend) and 30-minute time bin,
with an explicit confidence grade.

Run:  python pipeline/build_crowding.py
Outputs go to data/processed/.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "October-Delhi.csv"
OUT = ROOT / "data" / "processed"
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text(encoding="utf-8"))

BIN_MIN = CFG["bin_minutes"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 1. load
def load() -> pd.DataFrame:
    df = pd.read_csv(RAW, dtype=str)
    df["pax"] = pd.to_numeric(df["passenger_count"], errors="coerce").fillna(1).clip(1, 6).astype(int)
    df["fare"] = pd.to_numeric(df["amount"], errors="coerce")
    ts = pd.to_datetime(df["created_at"].str.slice(0, 19), errors="coerce")  # already IST
    df["ts"] = ts
    df["date"] = ts.dt.date
    df["dow"] = ts.dt.dayofweek
    df["daytype"] = np.where(df["dow"] >= 5, "weekend", "weekday")
    df["bin"] = (ts.dt.hour * 60 + ts.dt.minute) // BIN_MIN
    df["week"] = ts.dt.isocalendar().week.astype(int)
    df["vehicle"] = df["vehicle_number"].where(~df["vehicle_number"].isin(["NULL", ""]))
    df["ac"] = df["is_ac"].eq("True")
    m = df["route"].str.extract(r"^(?P<base>.*?)(?P<dir>UP|DOWN)$")
    df["route_base"] = m["base"].fillna(df["route"])
    df["dir"] = m["dir"].fillna("NA")
    for c in ("start_stop_name", "end_stop_name"):
        df[c] = df[c].str.strip()
    df = df.dropna(subset=["ts"])
    df = df[df["start_stop_name"] != df["end_stop_name"]]
    return df


# ------------------------------------------------- 2. stop order per route
def stop_order(g: pd.DataFrame) -> tuple[list[str], float]:
    """Order the stops of one route-direction using only ticket precedence.

    Every ticket says 'start comes before end'. Start from a weighted Borda score
    (passengers leaving a stop minus passengers arriving at it), then run an
    insertion local search: move each stop to the position that minimises the
    passengers whose (start, end) order is violated. O(n + degree) per move via
    prefix sums. Returns the order and the share of passengers consistent with it.
    """
    pairs = g.groupby(["start_stop_name", "end_stop_name"])["pax"].sum().reset_index()
    stops = list(pd.unique(pd.concat([pairs.start_stop_name, pairs.end_stop_name])))
    idx = {s: i for i, s in enumerate(stops)}
    n = len(stops)
    a = pairs.start_stop_name.map(idx).to_numpy()
    b = pairs.end_stop_name.map(idx).to_numpy()
    w = pairs.pax.to_numpy().astype(float)
    score = np.zeros(n)
    np.add.at(score, a, w)
    np.add.at(score, b, -w)
    order = list(np.argsort(-score))
    out_nb = [[] for _ in range(n)]   # stop -> (partner that must come after, weight)
    in_nb = [[] for _ in range(n)]    # stop -> (partner that must come before, weight)
    for x, y, ww in zip(a, b, w):
        if x == y:
            continue
        out_nb[x].append((y, ww))
        in_nb[y].append((x, ww))
    for _ in range(8):
        improved = False
        for s in list(order):
            rest = [t for t in order if t != s]
            pos = {t: i for i, t in enumerate(rest)}
            m = len(rest)
            # cost[p] = violated weight if s is inserted before rest[p] (p in 0..m)
            cost = np.zeros(m + 2)
            for t, ww in out_nb[s]:          # t must be after s: violated if p > pos[t]
                cost[pos[t] + 1] += ww
            for t, ww in in_nb[s]:           # t must be before s: violated if p <= pos[t]
                cost[0] += ww
                cost[pos[t] + 1] -= ww
            cost = np.cumsum(cost)[: m + 1]
            cur = order.index(s)
            best = int(np.argmin(cost))
            if cost[best] + 1e-9 < cost[cur]:
                rest.insert(best, s)
                order = rest
                improved = True
        if not improved:
            break
    names = [stops[i] for i in order]
    rank = {s: i for i, s in enumerate(names)}
    ok = pairs.start_stop_name.map(rank) < pairs.end_stop_name.map(rank)
    consistency = float((pairs.pax * ok).sum() / pairs.pax.sum())
    return names, consistency


# ------------------------------------------------------------ 3. load
def segment_loads(df: pd.DataFrame, orders: dict) -> pd.DataFrame:
    """On-board e-ticket passengers per segment, via +board/-alight cumulative sums."""
    rows = []
    for route, g in df.groupby("route", sort=False):
        order = orders.get(route)
        if not order:
            continue
        rank = {s: i for i, s in enumerate(order)}
        i = g.start_stop_name.map(rank)
        j = g.end_stop_name.map(rank)
        ok = i.notna() & j.notna() & (i < j)
        g = g[ok].assign(i=i[ok].astype(int), j=j[ok].astype(int))
        if g.empty:
            continue
        nseg = len(order) - 1
        for (dt, b), h in g.groupby(["daytype", "bin"]):
            diff = np.zeros(nseg + 1)
            np.add.at(diff, h.i.to_numpy(), h.pax.to_numpy())
            np.add.at(diff, h.j.to_numpy(), -h.pax.to_numpy())
            load = np.cumsum(diff)[:nseg]
            board = np.bincount(h.i, weights=h.pax, minlength=nseg + 1)[:nseg]
            tickets = np.zeros(nseg)
            # tickets contributing to each segment (for confidence)
            cov = np.zeros(nseg + 1)
            np.add.at(cov, h.i.to_numpy(), 1)
            np.add.at(cov, h.j.to_numpy(), -1)
            tickets = np.cumsum(cov)[:nseg]
            for k in range(nseg):
                if load[k] <= 0:
                    continue
                rows.append((route, dt, int(b), k, order[k], order[k + 1], float(load[k]), float(board[k]), int(tickets[k])))
    return pd.DataFrame(rows, columns=["route", "daytype", "bin", "seg", "from_stop", "to_stop", "eticket_pax", "boardings", "n_tickets"])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    log("loading tickets")
    df = load()
    log(f"{len(df):,} tickets after cleaning")

    days = df.groupby("daytype")["date"].nunique().to_dict()
    log(f"days in data: {days}")

    # --- stop order per route-direction
    log("inferring stop order per route (no GTFS needed)")
    orders, cons = {}, {}
    min_t = CFG["min_tickets_per_route"]
    for route, g in df.groupby("route"):
        if len(g) < min_t:
            continue
        o, c = stop_order(g)
        orders[route], cons[route] = o, c
    log(f"ordered {len(orders)} routes; median consistency {np.median(list(cons.values())):.3f}")

    # --- buses running: distinct vehicles per route, date, bin (lower bound, ~51% of rows carry a vehicle)
    veh = df.dropna(subset=["vehicle"]).groupby(["route", "daytype", "bin", "date"])["vehicle"].nunique()
    veh = veh.groupby(["route", "daytype", "bin"]).mean().rename("buses_seen").reset_index()

    # --- segment loads
    log("computing segment loads")
    seg = segment_loads(df, orders)
    seg["days"] = seg["daytype"].map(days)
    seg["eticket_per_day"] = seg["eticket_pax"] / seg["days"]

    # --- thin half-hours borrow evidence from the neighbouring bins (kernel 0.25 / 0.5 / 0.25).
    # The estimate AND the ticket count are pooled together; well-observed cells are left as they are.
    seg = seg.sort_values(["route", "daytype", "seg", "bin"]).reset_index(drop=True)
    g = seg.groupby(["route", "daytype", "seg"], sort=False)
    nb = {}
    for d, sh in (("p", 1), ("n", -1)):
        same = g["bin"].shift(sh) == seg["bin"] - sh  # neighbour bin exists (missing bin = nobody on board)
        nb[d + "x"] = g["eticket_per_day"].shift(sh).where(same, 0.0).fillna(0.0)
        nb[d + "t"] = g["n_tickets"].shift(sh).where(same, 0).fillna(0)
    thin = seg["n_tickets"] < CFG["conf_med_tickets"]
    seg["smoothed"] = thin
    seg.loc[thin, "eticket_per_day"] = (0.5 * seg["eticket_per_day"] + 0.25 * (nb["px"] + nb["nx"]))[thin]
    seg["n_eff"] = np.where(thin, seg["n_tickets"] + 0.5 * (nb["pt"] + nb["nt"]), seg["n_tickets"])
    seg = seg.merge(veh, on=["route", "daytype", "bin"], how="left")

    # --- scale e-ticket sample to all riders (documented, configurable)
    s_lo, s_mid, s_hi = CFG["scale_low"], CFG["scale_mid"], CFG["scale_high"]
    # buses in service per bin = distinct vehicle ids seen issuing tickets in that bin.
    # A bus issues many tickets, so almost every running bus shows up at least once even
    # though only ~51% of rows carry an id: do NOT divide by the row share (that double-counts
    # buses and halves the load). Bins with no id at all fall back to a default.
    veh_share = df["vehicle"].notna().mean()
    seg["buses_est"] = seg["buses_seen"].fillna(CFG["default_buses_per_bin"]).clip(lower=1)
    # A bus issues only ~6 e-tickets a day, so in most half-hours a running bus is never seen.
    # Better: the route's daily fleet (distinct vehicles seen on it that day, median over days of that type),
    # spread through the day by the citywide service profile (share of vehicle-days between their first and
    # last ticket at that half-hour), split over the route's directions. Checked against the GTFS timetable
    # in validate_gtfs.py.
    v = df.dropna(subset=["vehicle"])
    # a bus that works two routes in a day counts towards each in proportion to its tickets,
    # so the citywide total can never exceed the buses actually seen that day
    share = v.groupby(["vehicle", "date", "daytype", "route_base"]).size()
    share = share / share.groupby(["vehicle", "date"]).transform("sum")
    fleet = (share.groupby(["route_base", "daytype", "date"]).sum()
             .groupby(["route_base", "daytype"]).median().rename("fleet"))
    span = v.groupby(["vehicle", "date", "daytype"])["bin"].agg(["min", "max"]).reset_index()
    prof = {}
    for dt, s in span.groupby("daytype"):
        b = np.arange(1440 // BIN_MIN)
        active = ((s["min"].to_numpy()[:, None] <= b) & (s["max"].to_numpy()[:, None] >= b)).sum(axis=0)
        prof[dt] = active / len(s)
    ndir = df.groupby("route_base")["dir"].nunique().clip(lower=1)
    rb = seg["route"].map(df.drop_duplicates("route").set_index("route")["route_base"])
    fl = pd.MultiIndex.from_arrays([rb, seg["daytype"]]).map(fleet.to_dict().get).astype(float)
    p = np.array([prof[d][b] for d, b in zip(seg["daytype"], seg["bin"])])
    seg["buses_fleet"] = fl * p / rb.map(ndir).to_numpy()
    seg["buses_est"] = np.maximum(seg["buses_est"], np.nan_to_num(seg["buses_fleet"], nan=0.0)).clip(lower=1)
    # bus size per route: 9 m DEVI e-buses vs 12 m standard
    nine = seg["route"].str.match(CFG["nine_metre_route_regex"])
    seats = np.where(nine, CFG["bus_9m"]["seats"], CFG["bus_12m"]["seats"])
    crush = np.where(nine, CFG["bus_9m"]["crush"], CFG["bus_12m"]["crush"])
    seg["bus_type"] = np.where(nine, "9m", "12m")
    seg["seats"] = seats
    # per-route scale: global e-ticket scale, capped so the route's weekday p95 load
    # per bus stays within crush capacity (e-ticket share is far higher on some routes)
    per_bus = seg["eticket_per_day"] / seg["buses_est"]
    p95 = per_bus[seg.daytype == "weekday"].groupby(seg["route"]).quantile(CFG["route_cap_quantile"])
    cap = pd.Series(crush, index=seg.index).groupby(seg["route"]).first()
    route_scale_max = (cap / p95.reindex(cap.index)).replace([np.inf, -np.inf], np.nan)
    seg["route_scale_max"] = seg["route"].map(route_scale_max).fillna(s_hi)
    for tag, s in (("lo", s_lo), ("mid", s_mid), ("hi", s_hi)):
        scale = np.minimum(s, seg["route_scale_max"] * (1.15 if tag == "hi" else 1.0))
        seg[f"onboard_{tag}"] = seg["eticket_per_day"] * scale / seg["buses_est"]
    seg["scale_capped"] = seg["route_scale_max"] < s_mid
    # demand beyond what one bus can physically carry => riders left behind at stops
    seg["demand_ratio"] = (seg["onboard_mid"] / crush).round(2)
    for tag in ("lo", "mid", "hi"):
        seg[f"onboard_{tag}"] = np.minimum(seg[f"onboard_{tag}"], crush)

    # --- capacity and crowding level
    seg["load_factor"] = seg["onboard_mid"] / seg["seats"]
    cuts = CFG["level_cuts"]  # load / seats
    seg["level"] = pd.cut(seg["load_factor"], [-1, cuts[0], cuts[1], cuts[2], 1e9],
                          labels=["seats", "few_seats", "standing", "packed"]).astype(str)
    seg.loc[seg["demand_ratio"] >= 0.95, "level"] = "packed"  # at or beyond crush capacity

    # --- relative crowding index (scale-free): load vs this route's own weekday peak
    peak = seg[seg.daytype == "weekday"].groupby("route")["eticket_per_day"].max().rename("route_peak")
    seg = seg.merge(peak, on="route", how="left")
    seg["rci"] = (seg["eticket_per_day"] / seg["route_peak"]).clip(0, 1.5).round(3)

    # --- confidence in the crowding LEVEL shown: ticket counts are ~Poisson, so the load has a 90 %
    # sampling interval of about +-1.64/sqrt(n). If the level is the same at both ends of that interval,
    # the answer the commuter sees does not depend on sampling noise. Route-order consistency also counts.
    seg["order_consistency"] = seg["route"].map(cons).round(3)
    n = seg["n_eff"].clip(lower=1)
    rel = 1.64 / np.sqrt(n)

    def lvl(lf):
        return np.digitize(lf, cuts)  # 0 seats .. 3 packed

    lf = seg["load_factor"].to_numpy()
    at_cap = seg["demand_ratio"].to_numpy() >= 0.95
    stable = (lvl(lf * (1 - rel)) == lvl(lf * (1 + rel))) | (at_cap & (seg["demand_ratio"] * (1 - rel) >= 0.95))
    oc = seg.order_consistency
    seg["level_stable"] = stable
    seg["confidence"] = np.select(
        [((n >= CFG["conf_high_tickets"]) | (stable & (n >= CFG["conf_med_tickets"]))) & (oc >= 0.9),
         ((n >= CFG["conf_med_tickets"]) | (stable & (n >= CFG["conf_min_tickets"]))) & (oc >= 0.8)],
        ["high", "medium"], default="low")

    seg["ac_share"] = seg["route"].map(df.groupby("route")["ac"].mean()).round(2)
    keep = ["route", "daytype", "bin", "seg", "from_stop", "to_stop", "eticket_per_day", "boardings", "n_tickets",
            "buses_est", "onboard_lo", "onboard_mid", "onboard_hi", "demand_ratio", "level", "confidence", "rci",
            "bus_type", "seats", "order_consistency", "scale_capped"]
    out = seg[keep].copy()
    for c in ("route", "daytype", "from_stop", "to_stop", "level", "confidence", "bus_type"):
        out[c] = out[c].astype("category")
    for c in ("eticket_per_day", "boardings", "buses_est", "onboard_lo", "onboard_mid", "onboard_hi", "demand_ratio", "rci", "order_consistency"):
        out[c] = out[c].astype("float32")
    out.to_parquet(OUT / "segment_crowding.parquet", index=False)
    pd.DataFrame([{"route": r, "order_consistency": round(cons[r], 4), "stops": json.dumps(o, ensure_ascii=False)}
                  for r, o in orders.items()]).to_csv(OUT / "route_stop_order.csv", index=False)

    # --- validation inputs: week-to-week stability, peaks, weekday vs weekend
    hourly = df.groupby([df.ts.dt.hour.rename("hour"), "daytype"])["pax"].sum().unstack().fillna(0)
    hourly = (hourly / pd.Series(days)).round(1)
    hourly.to_csv(OUT / "hourly_profile.csv")
    wk = df[df.daytype == "weekday"].groupby(["route", "bin", "week"])["pax"].sum().unstack("week")
    full = [w for w in wk.columns if df[df.week == w]["date"].nunique() >= 5]
    stab = {}
    if len(full) >= 2:
        pairs = [(full[i], full[i + 1]) for i in range(len(full) - 1)]
        stab = {f"{a}-{b}": round(float(wk[[a, b]].dropna().corr().iloc[0, 1]), 3) for a, b in pairs}
    summary = {
        "tickets": int(len(df)), "passengers": int(df.pax.sum()), "routes_ordered": len(orders),
        "median_order_consistency": round(float(np.median(list(cons.values()))), 3),
        "days": days, "vehicle_id_share": round(float(veh_share), 3),
        "week_to_week_corr_route_bin": stab,
        "segments": int(len(seg)), "confidence_share": seg["confidence"].value_counts(normalize=True).round(3).to_dict(),
        "confidence_share_rider_weighted": (seg.groupby("confidence")["eticket_per_day"].sum() / seg["eticket_per_day"].sum()).round(3).to_dict(),
        "confidence_share_daytime_6_22": seg[seg.bin.between(12, 43)]["confidence"].value_counts(normalize=True).round(3).to_dict(),
        "smoothed_cells_share": round(float(seg["smoothed"].mean()), 3),
        "scale": {"low": s_lo, "mid": s_mid, "high": s_hi},
        "routes_scale_capped_share": round(float(seg.groupby("route")["scale_capped"].first().mean()), 3),
        "segments_demand_over_capacity_weekday_peak": int(((seg.daytype == "weekday") & seg.bin.isin([16, 17, 18, 19, 36, 37, 38, 39]) & (seg.demand_ratio > 1)).sum()),
        "level_share_weekday_peak": seg[(seg.daytype == "weekday") & seg.bin.isin([16, 17, 18, 19, 36, 37, 38, 39])]["level"].value_counts(normalize=True).round(3).to_dict(),
        "level_share_weekday_peak_rider_weighted": (lambda p: (p.groupby("level")["eticket_per_day"].sum() / p["eticket_per_day"].sum()).round(3).to_dict())(
            seg[(seg.daytype == "weekday") & seg.bin.isin([16, 17, 18, 19, 36, 37, 38, 39])]),
        "buses_in_service_citywide_weekday": {f"{b // 2:02d}:{30 * (b % 2):02d}": int(round(seg[(seg.daytype == "weekday") & (seg.bin == b)].groupby("route")["buses_est"].first().sum()))
                                              for b in (12, 17, 24, 37, 44)},
        "vehicles_seen_per_weekday": int(df[df.daytype == "weekday"].dropna(subset=["vehicle"]).groupby("date")["vehicle"].nunique().median()),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
