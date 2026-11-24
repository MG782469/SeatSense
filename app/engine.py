"""SeatSense engine: loads pipeline outputs and answers planner / operator queries."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text(encoding="utf-8"))
BIN = CFG["bin_minutes"]
NBINS = 1440 // BIN
LEVELS = ["seats", "few_seats", "standing", "packed"]
LEVEL_LABEL = {"seats": "Seats likely", "few_seats": "Few seats", "standing": "Standing room", "packed": "Packed"}
LEVEL_PENALTY = {"seats": 0.0, "few_seats": 1.0, "standing": 4.0, "packed": 9.0}  # min-equivalent per 10 min
TRANSFER_PENALTY_MIN = 5.0
MAX_HOPS = 60
TT_PRIOR_WEIGHT = 15.0
SPLIT_KM = 2.0          # the same stop name more than this far apart is two different stops  # observations' worth of trust in the prior when a route-hour has little data
WALK_KM = 0.4          # longest walk offered between stops (start, change, end)
WALK_SPEED_KMH = 4.5
WALK_DETOUR = 1.3      # street distance / straight-line distance
CONF = ["low", "medium", "high"]

# ----------------------------------------------------------- stop names
_FIX = [(r"\bsation\b", "station"), (r"\bstn\b", "station"), (r"\bxing\b", "crossing"), (r"\bx-ing\b", "crossing"),
        (r"\bmore\b", "mor"), (r"\(\s*t\.?\s*\)", " "), (r"\bbdr\b", "border"),
        (r"\brd\b", "road"), (r"\bmkt\b", "market"), (r"\bhosp\b", "hospital"), (r"\bsec\b", "sector"), (r"\bph\b", "phase")]
_GENERIC = {"metrostation", "busterminal", "terminal", "crossing", "village", "market", "depot", "busstand", "chowk", "gate"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def canon(s: str) -> str:
    t = " " + str(s).lower() + " "
    for a, b in _FIX:
        t = re.sub(a, b, t)
    return re.sub(r"[^a-z0-9]", "", t)


def aliases(s: str) -> set[str]:
    """Keys under which a stop name can match another: the whole name and each '/'-separated part."""
    keys = {canon(s)}
    parts = [p for p in re.split(r"/|\|", str(s)) if p.strip()]
    if len(parts) > 1:
        for p in parts:
            k = canon(p)
            if len(k) >= 8 and k not in _GENERIC:
                keys.add(k)
    return {k for k in keys if k}


def km(a, b) -> float:
    x = (b[1] - a[1]) * np.cos(np.radians((a[0] + b[0]) / 2)) * 111.32
    y = (b[0] - a[0]) * 110.57
    return float(np.hypot(x, y))


def walk_minutes(d_km: float) -> float:
    return d_km * WALK_DETOUR / WALK_SPEED_KMH * 60


def base(route: str) -> str:
    return re.sub(r"(UP|DOWN)$", "", route)


CUBE = PROC / "cube"
CUBE_FIELDS = ("lvl", "conf", "lo", "mid", "hi", "bus", "dr")


def build_cube(route_stops: dict) -> None:
    """Pack segment_crowding.parquet into one (half-hour x stretch) block per route and day type, saved as
    .npy files the app opens memory-mapped (reading the parquet into a server needs ~1.8 GB; this needs MBs)."""
    cols = ["route", "daytype", "bin", "seg", "level", "confidence", "onboard_lo", "onboard_mid", "onboard_hi",
            "buses_est", "demand_ratio", "bus_type", "seats"]
    seg = pd.read_parquet(PROC / "segment_crowding.parquet", columns=cols)
    def codes(col, cats):  # category codes without building millions of Python strings
        s = seg[col] if isinstance(seg[col].dtype, pd.CategoricalDtype) else seg[col].astype("category")
        return s.cat.set_categories(cats).cat.codes.to_numpy().astype(np.int8)
    lvl_code, conf_code = codes("level", LEVELS), codes("confidence", CONF)
    cube: dict = {}
    route_bus: dict = {}
    rcat = seg.route.astype("category")
    r_names, r_arr = list(rcat.cat.categories.astype(str)), rcat.cat.codes.to_numpy()
    d_names = ["weekday", "weekend"]
    d_arr = seg.daytype.astype("category").cat.set_categories(d_names).cat.codes.to_numpy()
    b_arr, k_arr = seg.bin.to_numpy().astype(np.int16), seg.seg.to_numpy().astype(np.int32)
    num = {c: seg[c].to_numpy().astype(np.float32) for c in ("onboard_lo", "onboard_mid", "onboard_hi", "buses_est", "demand_ratio")}
    order = np.lexsort((d_arr, r_arr))
    bounds = np.flatnonzero(np.r_[True, (r_arr[order][1:] != r_arr[order][:-1]) | (d_arr[order][1:] != d_arr[order][:-1]), True])
    for a, b in zip(bounds[:-1], bounds[1:]):
        idx = order[a:b]
        r, d = r_names[r_arr[idx[0]]], d_names[d_arr[idx[0]]]
        n = max(len(route_stops.get(r, [])) - 1, int(k_arr[idx].max()) + 1)
        c = {"lvl": np.full((NBINS, n), -1, np.int8), "conf": np.zeros((NBINS, n), np.int8)}
        for name in ("lo", "mid", "hi", "bus", "dr"):
            c[name] = np.zeros((NBINS, n), np.float16 if name != "mid" else np.float32)
        bb, kk = b_arr[idx], k_arr[idx]
        c["lvl"][bb, kk] = lvl_code[idx]
        c["conf"][bb, kk] = conf_code[idx]
        c["lo"][bb, kk] = num["onboard_lo"][idx]
        c["mid"][bb, kk] = num["onboard_mid"][idx]
        c["hi"][bb, kk] = num["onboard_hi"][idx]
        c["bus"][bb, kk] = num["buses_est"][idx]
        c["dr"][bb, kk] = num["demand_ratio"][idx]
        cube[(r, d)] = c
        route_bus[r] = (str(seg.bus_type.iat[idx[0]]), int(seg.seats.iat[idx[0]]))

    keys = sorted(cube)
    meta, off = [], 0
    for r, d in keys:
        n = cube[(r, d)]["lvl"].shape[1]
        meta.append([r, d, off, n])
        off += n
    CUBE.mkdir(exist_ok=True)
    for name in CUBE_FIELDS:
        np.save(CUBE / f"{name}.npy", np.concatenate([cube[k][name] for k in keys], axis=1))
    (CUBE / "meta.json").write_text(json.dumps({"blocks": meta, "route_bus": route_bus,
                                                "source_bytes": (PROC / "segment_crowding.parquet").stat().st_size}), encoding="utf-8")


@dataclass
class Leg:
    route: str
    board: str
    alight: str
    i: int
    j: int
    depart_min: float
    wait_min: float
    ride_min: float
    level: str = "seats"
    worst_stretch: str = ""
    confidence: str = "low"
    onboard_range: tuple = (0, 0)
    share_standing: float = 0.0
    penalty: float = 0.0
    strip: list = field(default_factory=list)
    walk_min: float = 0.0     # walk before boarding this bus
    walk_from: str = ""


@dataclass
class Option:
    legs: list = field(default_factory=list)
    walk_end_min: float = 0.0
    walk_end_to: str = ""

    @property
    def total(self) -> float:
        return (sum(l.walk_min + l.wait_min + l.ride_min for l in self.legs) + self.walk_end_min
                + TRANSFER_PENALTY_MIN * (len(self.legs) - 1))

    @property
    def penalty(self) -> float:
        return sum(l.penalty for l in self.legs)

    @property
    def level(self) -> str:
        return max((l.level for l in self.legs), key=LEVELS.index)

    @property
    def share_standing(self) -> float:
        ride = sum(l.ride_min for l in self.legs)
        return sum(l.share_standing * l.ride_min for l in self.legs) / max(ride, 1e-6)


class Engine:
    def __init__(self) -> None:
        orders = pd.read_csv(PROC / "route_stop_order.csv")
        self.route_stops = {r: json.loads(o) for r, o in zip(orders.route, orders.stops)}
        self.order_consistency = dict(zip(orders.route, orders.order_consistency))

        # --- stop groups: union-find over shared alias keys
        names = sorted({s for st in self.route_stops.values() for s in st})
        parent = {n: n for n in names}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            a, b = find(x), find(y)
            if a != b:
                parent[a] = b

        # 1) identical after spelling clean-up ("Sation"/"Station", "(T)", spacing)
        whole: dict[str, str] = {}
        for n in names:
            k = canon(n)
            if k in whole:
                union(n, whole[k])
            else:
                whole[k] = n
        # 2) a composite name "A / B" joins ONE stop whose whole name is A or B (the most used one),
        #    so two different stops are never chained together through composite names
        use: dict[str, int] = {}
        for st in self.route_stops.values():
            for s in st:
                use[s] = use.get(s, 0) + 1
        for n in names:
            cands = sorted(whole[k] for k in aliases(n) - {canon(n)} if k in whole)  # sorted: same result every run
            if cands:
                union(n, max(cands, key=lambda x: use.get(x, 0)))
        name_group = {n: find(n) for n in names}

        # --- coordinates (public GTFS, see pipeline/build_geo.py): needed to tell same-name stops apart
        self.stop_xy: dict = {}
        self.route_shapes: dict = {}
        if (PROC / "stop_coords.csv").exists():
            sc = pd.read_csv(PROC / "stop_coords.csv")
            self.stop_xy = {s: (float(a), float(b)) for s, a, b in zip(sc.stop, sc.lat, sc.lon)}
            self.route_shapes = json.loads((PROC / "route_shapes.json").read_text(encoding="utf-8"))

        # --- a stop is a name group AT A PLACE: "Jain Mandir" near Narela and "Jain Mandir" near Saket
        #     are different stops, so a change of bus can never jump across the city
        items: dict = {}
        for r, st in self.route_stops.items():
            sh = self.route_shapes.get(r) or []
            for i, s in enumerate(st):
                p, tol = (sh[i], 0.0) if i < len(sh) and sh[i] else (None, 0.0)
                if p is None:  # no coordinate: borrow the nearest placed neighbour's, only to tell places apart
                    for off in (1, 2, 3, 4):
                        q = next((sh[j] for j in (i - off, i + off) if 0 <= j < len(sh) and sh[j]), None)
                        if q:
                            p, tol = q, 1.0 * off
                            break
                items.setdefault(name_group[s], []).append((r, i, s, p, tol))
        self.rg = {r: [None] * len(st) for r, st in self.route_stops.items()}   # route position -> stop id
        self.group_xy, members, self.group_of = {}, {}, {}
        cnt: dict[str, int] = {}
        for st in self.route_stops.values():
            for s in st:
                cnt[s] = cnt.get(s, 0) + 1
        self.group_label: dict = {}
        for g, its in items.items():
            cl, loose = [], []
            for it in its:
                if it[3] is None:
                    loose.append(it)
                    continue
                best = None
                for c in cl:
                    d = km((c[0] / c[2], c[1] / c[2]), it[3])
                    if d <= SPLIT_KM + it[4] and (best is None or d < best[0]):
                        best = (d, c)
                if best:
                    c = best[1]
                    c[0] += it[3][0]
                    c[1] += it[3][1]
                    c[2] += 1
                    c[3].append(it)
                else:
                    cl.append([it[3][0], it[3][1], 1, [it]])
            cl.sort(key=lambda c: -len(c[3]))
            if loose:
                if len(cl) <= 1:  # one place only: a stop with no position at all is that place
                    cl = cl or [[0.0, 0.0, 0, []]]
                    cl[0][3].extend(loose)
                else:             # several places and no clue which: each such stop stands alone (no false transfer)
                    cl.extend([0.0, 0.0, 0, [it]] for it in loose)
            names_all = [s for _, _, s, _, _ in its]
            base_label = max(sorted(set(names_all)), key=lambda x: (cnt.get(x, 0), -len(x)))
            for k, c in enumerate(cl):
                gid = g if k == 0 else f"{g}#{k}"
                for r, i, s, _, _ in c[3]:
                    self.rg[r][i] = gid
                    self.group_of.setdefault(s, gid)  # a bare name means its busiest place
                members[gid] = sorted({s for _, _, s, _, _ in c[3]})
                if c[2]:
                    self.group_xy[gid] = (c[0] / c[2], c[1] / c[2])
                label = base_label
                if k > 0:  # the busiest place keeps the plain name; others are told apart by a neighbour
                    nb: dict = {}
                    for r, i, s, _, _ in c[3]:
                        st = self.route_stops[r]
                        for j in (i + 1, i - 1):
                            if 0 <= j < len(st) and name_group[st[j]] != g:
                                nb[st[j]] = nb.get(st[j], 0) + 1
                                break
                    if nb:
                        label = f"{base_label} (near {max(nb, key=nb.get)})"
                self.group_label[gid] = label
        self.split_names = sum(1 for g in items if f"{g}#1" in members)
        self.group_members = members
        self.label_to_gid = {}
        for gid, lab in self.group_label.items():
            self.label_to_gid.setdefault(lab, gid)
        self.group_routes: dict[str, list] = {}
        self.route_group_pos: dict[str, dict] = {}
        for r, gs in self.rg.items():
            pos = {}
            for i, gid in enumerate(gs):
                pos.setdefault(gid, i)
                self.group_routes.setdefault(gid, []).append((r, i))
            self.route_group_pos[r] = pos
        self.group_usage = {g: len({base(r) for r, _ in rs}) for g, rs in self.group_routes.items()}
        # second places of a name used by only one route are usually a coordinate slip: plan with them, don't list them
        self.search_index = [(g, norm(" ".join([self.group_label[g]] + m))) for g, m in members.items()
                             if "#" not in g or self.group_usage.get(g, 0) >= 2]
        self.places_split = {"names_at_several_places": self.split_names,
                             "second_places_used_by_5plus_route_stops": sum(1 for g, rs in self.group_routes.items() if "#" in g and len(rs) >= 5)}

        # --- crowding lookup: memory-mapped (half-hour x stretch) blocks per route and day type
        src = PROC / "segment_crowding.parquet"
        meta = json.loads((CUBE / "meta.json").read_text(encoding="utf-8")) if (CUBE / "meta.json").exists() else {}
        if src.exists() and meta.get("source_bytes") != src.stat().st_size:  # pipeline re-run (git checkout times are not reliable)
            build_cube(self.route_stops)
            meta = json.loads((CUBE / "meta.json").read_text(encoding="utf-8"))
        arr = {name: np.load(CUBE / f"{name}.npy", mmap_mode="r") for name in CUBE_FIELDS}
        self.cube = {(r, d): {name: arr[name][:, off:off + n] for name in CUBE_FIELDS} for r, d, off, n in meta["blocks"]}
        self.route_bus = {r: tuple(v) for r, v in meta["route_bus"].items()}

        tt = pd.read_csv(PROC / "route_hour_traveltime.csv")
        self.tt = {(r, d, int(h)): (m, n) for r, d, h, m, n in tt.itertuples(index=False)}
        self.route_tt = dict(pd.read_csv(PROC / "route_traveltime.csv").itertuples(index=False))
        city = pd.read_csv(PROC / "city_hour_traveltime.csv")
        self.city_tt = {(d, int(h)): m for d, h, m in city.itertuples(index=False)}
        self._city_mean = city.groupby("daytype")["city_min_per_stop"].median().to_dict()
        self.summary = json.loads((PROC / "summary.json").read_text(encoding="utf-8"))

        # --- walking links between nearby stops
        grid: dict = {}
        cell = 0.005
        for g, (la, lo) in self.group_xy.items():
            grid.setdefault((int(la / cell), int(lo / cell)), []).append(g)
        self.nearby: dict = {}
        for g, (la, lo) in self.group_xy.items():
            ci, cj = int(la / cell), int(lo / cell)
            out = []
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for h in grid.get((ci + di, cj + dj), []):
                        if h != g:
                            d = km((la, lo), self.group_xy[h])
                            if d <= WALK_KM:
                                out.append((h, walk_minutes(d)))
            self.nearby[g] = out
        self._cand_cache: dict = {}
        self.hourly = pd.read_csv(PROC / "hourly_profile.csv").to_dict(orient="list")

    # ------------------------------------------------------------ stops
    def resolve(self, name: str) -> str | None:
        if name in self.label_to_gid:
            return self.label_to_gid[name]
        if name in self.group_of:
            return self.group_of[name]
        if name in self.group_members:
            return name
        k = canon(name)
        for g in self.group_members:
            if canon(self.group_label[g]) == k:
                return g
        hits = self.search(name, 1)
        return hits[0]["id"] if hits else None

    def search(self, q: str, limit: int = 8) -> list[dict]:
        k = norm(q)
        if len(k) < 2:
            return []
        hits = [(g, idx) for g, idx in self.search_index if k in idx]
        hits.sort(key=lambda x: (not norm(self.group_label[x[0]]).startswith(k), -self.group_usage.get(x[0], 0)))
        return [{"id": g, "name": self.group_label[g], "routes": self.group_usage.get(g, 0),
                 "aka": [m for m in self.group_members[g] if m != self.group_label[g]][:3],
                 "ll": self.ll(g)} for g, _ in hits[:limit]]

    def ll(self, g: str):
        p = self.group_xy.get(g)
        return [round(p[0], 5), round(p[1], 5)] if p else None

    # ------------------------------------------------------------ timing
    def min_per_stop(self, route: str, day: str, minute: float) -> float:
        """Minutes per stop. A route-hour with few observations is shrunk towards a prior: the route's own
        all-day speed shaped by the city's hour-of-day pattern (so 5 noisy pairs cannot make a bus 2x faster)."""
        h = int(minute // 60) % 24
        city_h = self.city_tt.get((day, h), 2.0)
        city_mean = self._city_mean.get(day, 2.0)
        prior = float(self.route_tt.get(route, city_mean)) * city_h / city_mean
        v = self.tt.get((route, day, h))
        if not v:
            return prior
        m, n = float(v[0]), float(v[1])
        est = (n * m + TT_PRIOR_WEIGHT * prior) / (n + TT_PRIOR_WEIGHT)
        return float(np.clip(est, 0.6 * prior, 1.8 * prior))

    def cell(self, route: str, day: str, b: int, k: int):
        """(level, lo, mid, hi, confidence, buses, demand_ratio) for one stretch and half-hour, or None."""
        c = self.cube.get((route, day))
        if c is None or k >= c["lvl"].shape[1] or c["lvl"][b, k] < 0:
            return None
        return (LEVELS[c["lvl"][b, k]], float(c["lo"][b, k]), float(c["mid"][b, k]), float(c["hi"][b, k]),
                CONF[c["conf"][b, k]], float(c["bus"][b, k]), float(c["dr"][b, k]))

    def headway(self, route: str, day: str, minute: float) -> float:
        c = self.cube.get((route, day))
        b = int(minute // BIN) % NBINS
        seen = c["bus"][b][c["lvl"][b] >= 0] if c is not None else []
        buses = float(np.median(seen)) if len(seen) else 2.0
        trip = len(self.route_stops[route]) * self.min_per_stop(route, day, minute)
        return float(np.clip(trip / max(buses, 1.0), 4.0, 30.0))

    def ride(self, route: str, i: int, j: int, day: str, t0: float) -> Leg:
        wait = self.headway(route, day, t0) / 2
        t = start = t0 + wait
        worst, worst_name, conf_rank, lo, hi, peak = "seats", "", 2, 0.0, 0.0, -1.0
        standing_min = penalty = 0.0
        stops = self.route_stops[route]
        shape = self.route_shapes.get(route) or [None] * len(stops)
        strip = []
        for k in range(i, j):
            dt = self.min_per_stop(route, day, t)
            c = self.cell(route, day, int(t // BIN) % NBINS, k)
            lvl = c[0] if c else "seats"
            mid = float(c[2]) if c else 0.0
            if c:
                conf_rank = min(conf_rank, CONF.index(c[4]))
                if mid > peak:
                    peak, lo, hi = mid, float(c[1]), float(c[3])
            if LEVELS.index(lvl) > LEVELS.index(worst):
                worst, worst_name = lvl, f"{stops[k]} → {stops[k + 1]}"
            if lvl in ("standing", "packed"):
                standing_min += dt
            penalty += LEVEL_PENALTY[lvl] * dt / 10
            strip.append({"stop": stops[k], "level": lvl, "onboard": round(mid), "min": round(t - start, 1), "ll": shape[k]})
            t += dt
        strip.append({"stop": stops[j], "level": None, "onboard": None, "min": round(t - start, 1), "ll": shape[j]})
        ride_min = t - start
        return Leg(route, stops[i], stops[j], i, j, t0, wait, ride_min, worst, worst_name, CONF[conf_rank],
                   (round(lo), round(hi)), round(standing_min / max(ride_min, 1e-6), 2), penalty, strip)

    # ------------------------------------------------------------ planner
    def near(self, g: str) -> list:
        """The stop itself (no walk) plus stops within WALK_KM, with walking minutes."""
        return [(g, 0.0)] + self.nearby.get(g, [])

    def _candidates(self, o: str, d: str):
        """Time-independent route combinations, ranked by a hop-count proxy (2 min per hop)."""
        if (o, d) in self._cand_cache:
            return self._cand_cache[(o, d)]
        H = 2.0  # proxy minutes per hop, for ranking only; exact times come from ride()
        rg, rs = self.rg, self.route_stops
        # egress: every route reaching the destination, or a stop a short walk from it
        dest_pos: dict = {}
        for dg, w in self.near(d):
            for r, k in self.group_routes.get(dg, []):
                if r not in dest_pos or w < dest_pos[r][1]:
                    dest_pos[r] = (k, w, dg)
        origin = self.near(o)
        # 0 changes
        direct = {}
        for og, w0 in origin:
            for r, i in self.group_routes.get(og, []):
                if r in dest_pos:
                    k, w1, dg = dest_pos[r]
                    if 1 <= k - i <= MAX_HOPS:
                        c = (k - i) * H + w0 + w1
                        if r not in direct or c < direct[r][0]:
                            direct[r] = (c, ((r, i, k, w0, og),), w1, dg)
        # first bus: the best way to reach every stop
        first: dict = {}
        for og, w0 in origin:
            for r1, i in self.group_routes.get(og, []):
                st = rs[r1]
                for j in range(i + 2, min(len(st), i + MAX_HOPS)):
                    g = rg[r1][j]
                    c = (j - i) * H + w0
                    if g not in first or c < first[g][0]:
                        first[g] = (c, (r1, i, j, w0, og))
        # board a second bus at g2 after walking there from g
        board: dict = {}
        for g, (c, leg) in first.items():
            for g2, wt in self.near(g):
                c2 = c + wt + TRANSFER_PENALTY_MIN
                if g2 not in board or c2 < board[g2][0]:
                    board[g2] = (c2, leg, wt, g)
        # 1 change
        one = {}
        for r2, (k, w1, dg) in dest_pos.items():
            st = rs[r2]
            for m in range(max(0, k - MAX_HOPS), k - 1):
                b = board.get(rg[r2][m])
                if not b or base(b[1][0]) == base(r2):
                    continue
                c = b[0] + (k - m) * H + w1
                key = (b[1][0], r2)
                if key not in one or c < one[key][0]:
                    one[key] = (c, (b[1], (r2, m, k, b[2], b[3])), w1, dg)
        # 2 changes: the last bus from every stop that reaches the destination
        last: dict = {}
        for r3, (k, w1, dg) in dest_pos.items():
            st = rs[r3]
            for m in range(max(0, k - MAX_HOPS), k - 1):
                g = rg[r3][m]
                c = (k - m) * H + w1
                if g not in last or c < last[g][0]:
                    last[g] = (c, (r3, m, k), w1, dg)
        alight: dict = {}
        for g, (c, leg, w1, dg) in last.items():
            for g2, wt in self.near(g):
                c2 = c + wt + TRANSFER_PENALTY_MIN
                if g2 not in alight or c2 < alight[g2][0]:
                    alight[g2] = (c2, leg, wt, g, w1, dg)
        two = {}
        for rm, st in rs.items():
            best = None  # (cost - H*p, p, board entry)
            for q in range(len(st)):
                g = rg[rm][q]
                a = alight.get(g)
                if best and a and 2 <= q - best[1] <= MAX_HOPS:
                    b = best[2]
                    r1, r3 = b[1][0], a[1][0]
                    if len({base(r1), base(rm), base(r3)}) == 3:
                        c = best[0] + H * q + a[0]
                        key = (r1, rm, r3)
                        if key not in two or c < two[key][0]:
                            mid = (rm, best[1], q, b[2], b[3])
                            last_leg = (r3, a[1][1], a[1][2], a[2], a[3])
                            two[key] = (c, (b[1], mid, last_leg), a[4], a[5])
                b = board.get(g)
                if b and (best is None or b[0] - H * q < best[0]):
                    best = (b[0] - H * q, q, b)
        res = (sorted(direct.values(), key=lambda x: x[0]), sorted(one.values(), key=lambda x: x[0]),
               sorted(two.values(), key=lambda x: x[0]))
        if len(self._cand_cache) > 2000:
            self._cand_cache.clear()
        self._cand_cache[(o, d)] = res
        return res

    def _label(self, name_or_group: str) -> str:
        g = self.group_of.get(name_or_group, name_or_group)
        return self.group_label.get(g, name_or_group)

    def _build(self, plan, w_end: float, dg: str, day: str, t0: float) -> Option:
        legs, t = [], t0
        for idx, (r, i, k, walk, frm) in enumerate(plan):
            t += walk + (TRANSFER_PENALTY_MIN if idx else 0.0)
            leg = self.ride(r, i, k, day, t)
            leg.board, leg.alight = self.group_label[self.rg[r][i]], self.group_label[self.rg[r][k]]
            leg.walk_min = walk
            leg.walk_from = self._label(frm) if walk > 0 else ""
            legs.append(leg)
            t += leg.wait_min + leg.ride_min
        return Option(legs, w_end, self._label(dg) if w_end > 0 else "")

    def plan_ids(self, o: str, d: str, depart_min: float, day: str, comfort: float, limit: int = 4) -> list[Option]:
        if not o or not d or o == d:
            return []
        direct, one, two = self._candidates(o, d)
        options = [self._build(p, w, d, day, depart_min) for _, p, w, dg in direct[:12]]
        options += [self._build(p, w, d, day, depart_min) for _, p, w, dg in one[:25]]
        if options:
            best_t = min(op.total for op in options)
            best_lvl = min((op.level for op in options), key=LEVELS.index)
        else:
            best_t, best_lvl = 1e9, "packed"
        # a second change is only offered when it clearly beats everything with fewer changes
        for _, p, w, dg in two[:12]:
            op = self._build(p, w, d, day, depart_min)
            if op.total < best_t - 5 or LEVELS.index(op.level) < LEVELS.index(best_lvl) or best_t >= 1e9:
                options.append(op)
        directs = [op for op in options if len(op.legs) == 1]
        if directs:
            best_t = min(op.total for op in directs)
            best_lvl = min((op.level for op in directs), key=LEVELS.index)
            # a change of bus is only worth offering if it saves time or gets a better crowding level
            options = directs + [op for op in options if len(op.legs) > 1 and
                                 (op.total < best_t - 2 or LEVELS.index(op.level) < LEVELS.index(best_lvl))]
        options.sort(key=lambda op: (op.total + comfort * op.penalty, op.total, len(op.legs)))
        out, used = [], set()
        for op in options:
            sig = tuple(base(l.route) for l in op.legs)
            if sig in used:
                continue
            used.add(sig)
            out.append(op)
            if len(out) >= limit:
                break
        return out

    def departures(self, o: str, d: str, depart_min: float, day: str, comfort: float) -> list[dict]:
        """Best option for departures from -30 to +90 min in 15-minute steps."""
        out = []
        for shift in range(-30, 91, 15):
            t = depart_min + shift
            if t < 0 or t >= 1440:
                continue
            ops = self.plan_ids(o, d, t, day, comfort, 1)
            if not ops:
                continue
            op = ops[0]
            out.append({"shift": shift, "depart": f"{int(t // 60):02d}:{int(t % 60):02d}", "total_min": round(op.total),
                        "level": op.level, "label": LEVEL_LABEL[op.level], "standing": round(op.share_standing, 2),
                        "routes": [l.route for l in op.legs]})
        return out

    # ------------------------------------------------------------ operator
    def route_profile(self, route: str, day: str) -> dict:
        stops = self.route_stops.get(route, [])
        n = max(len(stops) - 1, 1)
        c = self.cube.get((route, day))
        if c is None:
            heat, lvl, conf, dr = np.zeros((NBINS, n)), np.full((NBINS, n), -1), np.zeros((NBINS, n)), np.zeros((NBINS, n))
        else:
            heat, lvl, conf = c["mid"][:, :n].astype(float), c["lvl"][:, :n].astype(int), c["conf"][:, :n]
            dr = c["dr"][:, :n].astype(float)
        # the busiest half-hour by demand vs capacity (on-board numbers are capped, so many cells tie at the cap)
        trusted = np.where((lvl >= 0) & (conf > 0), dr, 0.0)
        pick = trusted if trusted.max() > 0 else dr
        peak_b, peak_k = np.unravel_index(int(np.argmax(pick)), pick.shape)
        bus_type, seats = self.route_bus.get(route, ("12m", 35))
        return {"route": route, "stops": stops, "heat": np.round(heat, 1).tolist(), "levels": lvl.tolist(), "seats": seats,
                "bus_type": bus_type, "order_consistency": self.order_consistency.get(route),
                "shape": self.route_shapes.get(route),
                "peak": {"time": f"{peak_b * BIN // 60:02d}:{peak_b * BIN % 60:02d}",
                         "stretch": f"{stops[peak_k]} → {stops[peak_k + 1]}" if len(stops) > peak_k + 1 else "",
                         "onboard": round(float(heat[peak_b, peak_k])), "demand_ratio": round(float(dr[peak_b, peak_k]), 2)}}

    def routes_over_capacity(self, day: str, bin_: int) -> int:
        n = 0
        for (r, d), c in self.cube.items():
            if d == day:
                ok = (c["lvl"][bin_] >= 0) & (c["conf"][bin_] > 0)
                n += bool((np.asarray(c["dr"][bin_], dtype=np.float32)[ok] > 1).any())
        return n

    def hotspots(self, day: str, bin_: int, limit: int = 15) -> list[dict]:
        """The worst stretch of each route (one row per route, so the list covers the city)."""
        cand = []
        for (r, d), c in self.cube.items():
            if d != day:
                continue
            ok = (c["lvl"][bin_] >= 0) & (c["conf"][bin_] > 0)
            if not ok.any():
                continue
            dr = np.where(ok, c["dr"][bin_].astype(np.float32), -1)
            k = int(np.argmax(dr))
            if dr[k] >= 0:
                cand.append((float(dr[k]), r, k))
        cand.sort(reverse=True)
        out = []
        for dr, r, k in cand[:limit]:
            lv, lo, mid, hi, cf, _, _ = self.cell(r, day, bin_, k)
            st, sh = self.route_stops[r], self.route_shapes.get(r)
            pt = lambda i: sh[i] if sh and 0 <= i < len(sh) else None  # noqa: E731
            out.append({"route": r, "from": st[k], "to": st[k + 1], "onboard": round(mid), "range": [round(lo), round(hi)],
                        "level": lv, "label": LEVEL_LABEL[lv], "confidence": cf, "demand_ratio": round(dr, 2),
                        "bus_type": self.route_bus.get(r, ("12m", 35))[0], "ll": [pt(k), pt(k + 1)]})
        return out


def option_json(op: Option) -> dict:
    return {
        "total_min": round(op.total), "level": op.level, "label": LEVEL_LABEL[op.level], "transfers": len(op.legs) - 1,
        "share_standing": round(op.share_standing, 2),
        "walk_end_min": round(op.walk_end_min), "walk_end_to": op.walk_end_to,
        "legs": [{"route": l.route, "board": l.board, "alight": l.alight, "stops": l.j - l.i,
                  "walk_min": round(l.walk_min), "walk_from": l.walk_from,
                  "wait_min": round(l.wait_min), "ride_min": round(l.ride_min), "level": l.level,
                  "label": LEVEL_LABEL[l.level], "worst_stretch": l.worst_stretch, "confidence": l.confidence,
                  "onboard_range": list(l.onboard_range), "share_standing": l.share_standing, "strip": l.strip} for l in op.legs],
    }
