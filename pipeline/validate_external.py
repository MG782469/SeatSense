"""Check the model's scale against published figures (no on-board counts are public, so this checks the two
numbers the on-board estimate rests on: total riders and buses).

Published figures (Economic Survey of Delhi 2025-26 as reported in the press; see SOURCES):
- daily bus riders: DTC 24.29 lakh (2025-26); DTC + cluster ~40.8 lakh (FY2024-25)
- bus fleet: ~6,100 (March 2026, DTC + Transport Dept.); ~6,300 (April 2026)
Run after build_crowding.py:  python pipeline/validate_external.py
"""
from __future__ import annotations

import json
from pathlib import Path

PROC = Path(__file__).resolve().parents[1] / "data" / "processed"
CFG = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8"))

RIDERS_LAKH = {"DTC 2025-26": 24.29, "DTC + cluster FY2024-25": 40.8}
FLEET = {"March 2026": 6100, "April 2026": 6300}
SOURCES = [
    "https://www.newsbytesapp.com/news/auto/economic-survey-2025-26-finds-delhi-bus-ridership-down-nearly-20/story",
    "https://ackodrive.com/news/delhi-s-dtc-bus-fleet-hits-13-year-low-as-daily-ridership-falls-44/",
    "https://thewire.in/economy/service-sector-transport-water-highlights-of-delhis-economic-survey-2025-26",
    "https://www.electrive.com/2026/04/20/india-delhi-transport-corporation-flags-off-200-electric-buses/",
]


def main() -> None:
    s = json.loads((PROC / "summary.json").read_text(encoding="utf-8"))
    per_day = s["passengers"] / sum(s["days"].values())
    model = {k: round(per_day * CFG[f"scale_{k}"] / 1e5, 1) for k in ("low", "mid", "high")}
    lo, hi = min(RIDERS_LAKH.values()), max(RIDERS_LAKH.values())
    out = {
        "eticket_passengers_per_day": round(per_day),
        "model_daily_riders_lakh": model,
        "published_daily_riders_lakh": RIDERS_LAKH,
        "published_range_inside_model_range": model["low"] <= lo and hi <= model["high"] * 1.05,
        "implied_scale": [round(lo * 1e5 / per_day, 1), round(hi * 1e5 / per_day, 1)],
        "implied_eticket_share_pct": [round(per_day / (hi * 1e5) * 100, 2), round(per_day / (lo * 1e5) * 100, 2)],
        "vehicles_seen_per_weekday": s["vehicles_seen_per_weekday"],
        "published_fleet": FLEET,
        "share_of_fleet_seen_pct": [round(s["vehicles_seen_per_weekday"] / max(FLEET.values()) * 100),
                                    round(s["vehicles_seen_per_weekday"] / min(FLEET.values()) * 100)],
        "sources": SOURCES,
        "note": "Published figures conflict slightly by year and scope (DTC only vs DTC + cluster), so both ends are kept.",
    }
    s["external_check"] = out
    (PROC / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "sources"}, indent=2))


if __name__ == "__main__":
    main()
