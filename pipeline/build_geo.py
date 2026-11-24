"""Attach coordinates to ticket stop names using the public Delhi bus GTFS (stops, routes, trips, stop_times).

Matching, most to least reliable:
  1. route  - the ticket route exists in GTFS (route_long_name, e.g. 717AUP); the stop name matches a stop
              on that GTFS route's own stop sequence (spelling-normalised, or one '/' part of the name)
  2. global - the name matches a GTFS stop anywhere; if several stops share the name, the one closest
              to the neighbouring matched stops on this route is used
  3. interp - unmatched stops are placed between their matched neighbours on the route (shown as approximate)

Outputs data/processed/stop_coords.csv (stop name -> lat, lon, method) and route_shapes.json (route -> [[lat, lon], ...]).
Input: data/raw/gtfs/delhi-bus.zip
Run:   python pipeline/build_geo.py
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

from difflib import SequenceMatcher
from functools import lru_cache

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engine import aliases, canon  # noqa: E402  (same normalisation as the app)

PROC = ROOT / "data" / "processed"
GTFS = ROOT / "data" / "raw" / "gtfs" / "delhi-bus.zip"
KM_PER_STOP = 1.6  # a bus rarely covers more than this per stop on a Delhi city route
SLACK_KM = 1.5
MAX_GAP = 4       # never bridge more than this many unmatched stops
CELL = 0.025      # ~2.5 km grid for nearby-stop lookups
MIN_SIM = 0.78    # fuzzy name similarity needed to pair a ticket stop with a GTFS stop
SPIKE_KM = 1.5    # see "drop spikes"
FUZZY_KM = 2.5    # fuzzy global matches must lie this close to a matched neighbour


@lru_cache(maxsize=None)
def _keys(a: str) -> tuple:
    return canon(a), frozenset(aliases(a))


@lru_cache(maxsize=2_000_000)
def sim(a: str, b: str) -> float:
    """Name similarity in [0, 1]: 1 for the same normalised name or a shared '/' part, else fuzzy ratio."""
    (x, ka), (y, kb) = _keys(a), _keys(b)
    if x == y or ka & kb:
        return 1.0
    if not x or not y:
        return 0.0
    if len(x) >= 6 and len(y) >= 6 and (x in y or y in x):
        return 0.9
    if min(len(x), len(y)) / max(len(x), len(y)) < MIN_SIM - 0.1:
        return 0.0
    m = SequenceMatcher(None, x, y, autojunk=False)
    if m.real_quick_ratio() < MIN_SIM or m.quick_ratio() < MIN_SIM:
        return 0.0
    return m.ratio()


def align(ticket: list, gtfs: list) -> tuple[list, float]:
    """Order-preserving alignment (like Needleman-Wunsch without gap costs): pick ticket->GTFS stop pairs,
    in the same order on both, that maximise the summed name similarity. Pairs below MIN_SIM are not used."""
    n, m = len(ticket), len(gtfs)
    M = np.array([[sim(a, b) for b in gtfs] for a in ticket]) if n and m else np.zeros((n, m))
    M = np.where(M >= MIN_SIM, M, -1e9)
    S = np.zeros((n + 1, m + 1))
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            S[i, j] = max(S[i - 1, j], S[i, j - 1], S[i - 1, j - 1] + M[i - 1, j - 1])
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        if S[i, j] == S[i - 1, j]:
            i -= 1
        elif S[i, j] == S[i, j - 1]:
            j -= 1
        else:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
    return pairs[::-1], float(S[n, m])


def km(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    x = (lo2 - lo1) * np.cos(np.radians((la1 + la2) / 2)) * 111.32
    y = (la2 - la1) * 110.57
    return float(np.hypot(x, y))


def main() -> None:
    z = zipfile.ZipFile(GTFS)
    stops = pd.read_csv(z.open("stops.txt"), usecols=["stop_id", "stop_name", "stop_lat", "stop_lon"])
    routes = pd.read_csv(z.open("routes.txt"), usecols=["route_id", "route_long_name"])
    trips = pd.read_csv(z.open("trips.txt"), usecols=["route_id", "trip_id"])
    st = pd.read_csv(z.open("stop_times.txt"), usecols=["trip_id", "stop_id", "stop_sequence"])
    pos = {r.stop_id: (float(r.stop_lat), float(r.stop_lon)) for r in stops.itertuples()}
    sname = dict(zip(stops.stop_id, stops.stop_name))

    # one representative (longest) trip per GTFS route
    n = st.groupby("trip_id").size()
    trips["n"] = trips.trip_id.map(n).fillna(0)
    rep = trips.sort_values("n").drop_duplicates("route_id", keep="last")
    seq = st[st.trip_id.isin(rep.trip_id)].sort_values(["trip_id", "stop_sequence"])
    trip_stops = seq.groupby("trip_id").stop_id.apply(list).to_dict()
    gtfs_route_stops: dict[str, list] = {}
    for rid, tid in zip(rep.route_id, rep.trip_id):
        name = routes.loc[routes.route_id == rid, "route_long_name"].iloc[0]
        gtfs_route_stops.setdefault(canon(name), trip_stops.get(tid, []))

    # global index: normalised key -> list of coordinates
    glob: dict[str, list] = {}
    for sid, nm in sname.items():
        for k in aliases(nm):
            glob.setdefault(k, []).append(pos[sid])

    grid: dict = {}
    for sid, p in pos.items():
        grid.setdefault((int(p[0] / CELL), int(p[1] / CELL)), []).append(sid)

    orders = pd.read_csv(PROC / "route_stop_order.csv")
    route_stops = {r: json.loads(o) for r, o in zip(orders.route, orders.stops)}

    votes: dict[str, list] = {}     # stop name -> [(lat, lon, method)]
    shapes: dict[str, list] = {}
    stats = {"route": 0, "global": 0, "interp": 0, "none": 0}
    routes_in_gtfs = 0
    for r, names in route_stops.items():
        gseq = gtfs_route_stops.get(canon(r)) or gtfs_route_stops.get(canon(r) + "2") or gtfs_route_stops.get(canon(r) + "1")
        routes_in_gtfs += bool(gseq)
        coord: list = [None] * len(names)
        meth: list = [None] * len(names)
        if gseq:
            # align the ticket route's stop order with the GTFS route's stop order (either direction)
            gnames = [sname[s] for s in gseq]
            fwd, bwd = align(names, gnames), align(names, gnames[::-1])
            best, gs = (fwd, gseq) if fwd[1] >= bwd[1] else (bwd, gseq[::-1])
            for i, j in best[0]:
                coord[i], meth[i] = pos[gs[j]], "route"
        def keys(nm):
            return [canon(nm)] + sorted(aliases(nm) - {canon(nm)})

        def gcands(nm):
            for k in keys(nm):
                if glob.get(k):
                    return glob[k]
            return []

        def plausible(i, c, strict=False):
            """c is plausible if it is a sensible distance from the nearest matched stop on each side."""
            ok, seen = True, 0
            for step in (-1, 1):
                j = i + step
                while 0 <= j < len(coord) and coord[j] is None and abs(j - i) <= MAX_GAP:
                    j += step
                if 0 <= j < len(coord) and coord[j] is not None:
                    seen += 1
                    if km(c, coord[j]) > KM_PER_STOP * abs(j - i) + SLACK_KM:
                        ok = False
            return ok and (seen > 0 or not strict)

        # unambiguous global matches (one place in the city carries this name)
        for i, nm in enumerate(names):
            if coord[i] is None:
                cs = gcands(nm)
                if cs and max(km(cs[0], c) for c in cs) < 1.0:
                    coord[i], meth[i] = cs[0], "global"
        # drop outliers: global first, then route matches, until stable
        for kind in ("global", "route"):
            for _ in range(3):
                bad = [i for i, c in enumerate(coord) if c and meth[i] == kind and not plausible(i, c)]
                for i in bad:
                    coord[i], meth[i] = None, None
                if not bad:
                    break
        # ambiguous global names: the candidate nearest the route's matched neighbours, if plausible
        for i, nm in enumerate(names):
            if coord[i] is None:
                cs = gcands(nm)
                near = [coord[j] for j in range(max(0, i - MAX_GAP), min(len(coord), i + MAX_GAP + 1)) if coord[j]]
                if cs and near:
                    best = min(cs, key=lambda c: min(km(c, a) for a in near))
                    if plausible(i, best, strict=True):
                        coord[i], meth[i] = best, "global"
        # fuzzy names near the route ("Vayusena Bad" ~ "Vayusenabad"): only GTFS stops close to a matched neighbour
        for i, nm in enumerate(names):
            if coord[i] is None:
                near = [coord[j] for j in range(max(0, i - 2), min(len(coord), i + 3)) if coord[j]]
                if not near:
                    continue
                cand = []
                for a in near:
                    for sid in grid.get((int(a[0] / CELL), int(a[1] / CELL)), []) + [x for di in (-1, 0, 1) for dj in (-1, 0, 1) if (di, dj) != (0, 0)
                                                                                    for x in grid.get((int(a[0] / CELL) + di, int(a[1] / CELL) + dj), [])]:
                        if km(pos[sid], a) <= FUZZY_KM:
                            s = sim(nm, sname[sid])
                            if s >= MIN_SIM:
                                cand.append((s, -km(pos[sid], a), sid))
                if cand:
                    c = pos[max(cand)[2]]
                    if plausible(i, c, strict=True):
                        coord[i], meth[i] = c, "global"
        # drop spikes: a stop far from both matched neighbours while those neighbours are close to each other
        #   is a misplaced match (it draws a zig-zag); interpolation below puts it between its neighbours
        for _ in range(3):
            idx = [i for i, c in enumerate(coord) if c]
            bad = [b for a, b, c in zip(idx, idx[1:], idx[2:])
                   if km(coord[a], coord[b]) > SPIKE_KM and km(coord[b], coord[c]) > SPIKE_KM and km(coord[a], coord[c]) < SPIKE_KM]
            for i in bad:
                coord[i], meth[i] = None, None
            if not bad:
                break
        # interpolate short gaps between plausible anchors
        idx = [i for i, c in enumerate(coord) if c]
        for a, b in zip(idx, idx[1:]):
            if b - a - 1 > MAX_GAP or km(coord[a], coord[b]) > KM_PER_STOP * (b - a) + SLACK_KM:
                continue
            for i in range(a + 1, b):
                f = (i - a) / (b - a)
                coord[i] = (coord[a][0] + f * (coord[b][0] - coord[a][0]), coord[a][1] + f * (coord[b][1] - coord[a][1]))
                meth[i] = "interp"
        for nm, c, m in zip(names, coord, meth):
            stats[m or "none"] += 1
            if c:
                votes.setdefault(nm, []).append((c[0], c[1], m))
        if sum(c is not None for c in coord) >= 2:
            shapes[r] = [[round(c[0], 5), round(c[1], 5)] if c else None for c in coord]

    rank = {"route": 0, "global": 1, "interp": 2}
    rows = []
    for nm, vs in votes.items():
        best = min(rank[v[2]] for v in vs)
        pts = np.array([(v[0], v[1]) for v in vs if rank[v[2]] == best])
        lat, lon = np.median(pts, axis=0)
        rows.append({"stop": nm, "lat": round(float(lat), 6), "lon": round(float(lon), 6),
                     "method": [k for k, v in rank.items() if v == best][0]})
    out = pd.DataFrame(rows).sort_values("stop")
    out.to_csv(PROC / "stop_coords.csv", index=False)
    (PROC / "route_shapes.json").write_text(json.dumps(shapes, separators=(",", ":")), encoding="utf-8")

    total = sum(stats.values())
    names_all = {s for v in route_stops.values() for s in v}
    summary = {
        "routes_found_in_gtfs": routes_in_gtfs, "routes": len(route_stops),
        "route_stop_slots": total, **{f"slots_{k}": v for k, v in stats.items()},
        "stop_names_with_coords": len(out), "stop_names": len(names_all),
        "method_share": out.method.value_counts(normalize=True).round(3).to_dict(),
    }
    s = json.loads((PROC / "summary.json").read_text(encoding="utf-8"))
    s["geo"] = summary
    (PROC / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
