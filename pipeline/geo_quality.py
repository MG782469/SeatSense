"""Quality of the stop coordinates along each route (used to compare matching methods).

- placed: share of route stops with a coordinate
- long hops: share of consecutive placed stops more than 3 km apart
- spikes: a stop more than 1.5 km from both neighbours while the neighbours are within 1.5 km of each
  other (the "zig-zag" a misplaced stop draws on the map)
Run:  python pipeline/geo_quality.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

PROC = Path(__file__).resolve().parents[1] / "data" / "processed"


def km(a, b) -> float:
    x = (b[1] - a[1]) * np.cos(np.radians((a[0] + b[0]) / 2)) * 111.32
    return float(np.hypot(x, (b[0] - a[0]) * 110.57))


def quality() -> dict:
    shapes = json.loads((PROC / "route_shapes.json").read_text(encoding="utf-8"))
    import pandas as pd
    orders = pd.read_csv(PROC / "route_stop_order.csv")
    total = sum(len(json.loads(s)) for s in orders.stops)
    placed = hops = long_ = spikes = 0
    for pts in shapes.values():
        q = [p for p in pts if p]
        placed += len(q)
        for a, b in zip(q, q[1:]):
            hops += 1
            long_ += km(a, b) > 3
        for a, b, c in zip(q, q[1:], q[2:]):
            spikes += km(a, b) > 1.5 and km(b, c) > 1.5 and km(a, c) < 1.5
    return {"placed_share": round(placed / total, 3), "long_hop_share": round(long_ / max(hops, 1), 4),
            "spikes": int(spikes), "spike_share": round(spikes / max(hops, 1), 4)}


if __name__ == "__main__":
    q = quality()
    s = json.loads((PROC / "summary.json").read_text(encoding="utf-8"))
    s.setdefault("geo", {})["quality"] = {**q, "before_alignment": {"placed_share": 0.911, "long_hop_share": 0.011, "spikes": 1782}}
    (PROC / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(json.dumps(q, indent=2))
