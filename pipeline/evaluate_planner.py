"""Planner check over random stop pairs (fixed seed, so it is reproducible).

Reports: share of pairs with at least one option, with and without walking links / a second change;
how often walks and second changes are used; door-to-door straight-line speed (a teleporting transfer
or a too-fast travel time would show up as implausible speeds); time per plan.
Run after the other pipeline steps:  python pipeline/evaluate_planner.py
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.engine import PROC, Engine, km  # noqa: E402

N_PAIRS, SEED, DEPART, DAY = 400, 7, 510, "weekday"


def main() -> None:
    E = Engine()
    rng = random.Random(SEED)
    stops = sorted(g for g, u in E.group_usage.items() if u >= 3 and g in E.group_xy)
    pairs = [tuple(rng.sample(stops, 2)) for _ in range(N_PAIRS)]

    found = walk = two = 0
    speeds = []
    t0 = time.time()
    for a, b in pairs:
        ops = E.plan_ids(a, b, DEPART, DAY, 1.0)
        if not ops:
            continue
        found += 1
        walk += any(op.walk_end_min > 0 or any(l.walk_min > 0 for l in op.legs) for op in ops)
        two += any(len(op.legs) == 3 for op in ops)
        speeds.append(km(E.group_xy[a], E.group_xy[b]) / max(ops[0].total, 1) * 60)
    per_plan = (time.time() - t0) / len(pairs)

    # baseline: no walking links, no second change
    E.nearby = {g: [] for g in E.nearby}
    E._cand_cache = {}
    orig = E._candidates
    E._candidates = lambda o, d: orig(o, d)[:2] + ([],)
    base = sum(1 for a, b in pairs if E.plan_ids(a, b, DEPART, DAY, 1.0))

    sp = np.array(speeds)
    out = {
        "pairs": N_PAIRS, "seed": SEED, "depart": "08:30", "day": DAY,
        "found_share": round(found / N_PAIRS, 3), "found_share_without_walks_or_second_change": round(base / N_PAIRS, 3),
        "share_using_a_walk": round(walk / max(found, 1), 3), "share_offering_two_changes": round(two / max(found, 1), 3),
        "door_to_door_kmh_median": round(float(np.median(sp)), 1), "door_to_door_kmh_p90": round(float(np.percentile(sp, 90)), 1),
        "share_faster_than_30kmh": round(float((sp > 30).mean()), 3), "seconds_per_plan": round(per_plan, 3),
        "stop_places": len(E.group_members), **E.places_split,
    }
    s = json.loads((PROC / "summary.json").read_text(encoding="utf-8"))
    s["planner_check"] = out
    (PROC / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
