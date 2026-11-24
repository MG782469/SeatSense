"""End-to-end API tests on the committed pipeline outputs.  Run:  pytest -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.engine import LEVELS, km  # noqa: E402
from app.main import E, app  # noqa: E402

client = TestClient(app)


def plan(origin, dest, depart="08:30", day="weekday", comfort=1):
    return client.get("/api/plan", params={"origin": origin, "dest": dest, "depart": depart, "day": day, "comfort": comfort})


def test_health():
    j = client.get("/api/health").json()
    assert j["ok"] and j["routes"] > 1000 and j["stops_on_map"] > 0.9 * j["stops"]


def test_index_served_without_cache():
    r = client.get("/")
    assert r.status_code == 200 and "SeatSense" in r.text and r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("o,d", [("Badarpur Border (T)", "Saket Metro Station"),
                                 ("Kashmere Gate City Bus Terminal", "Nehru Place"),
                                 ("Shakarpur (Vikas Marg)", "Kendriya Terminal (Church Road)")])
def test_plan_examples(o, d):
    r = plan(o, d)
    assert r.status_code == 200
    j = r.json()
    assert 1 <= len(j["options"]) <= 4 and j["origin_ll"] and j["dest_ll"]
    for op in j["options"]:
        assert op["level"] in LEVELS and op["total_min"] > 5
        assert op["transfers"] == len(op["legs"]) - 1 <= 2
        for leg in op["legs"]:
            lo, hi = leg["onboard_range"]
            assert 0 <= lo <= hi <= 70  # never above a 12 m bus's crush load
            assert leg["confidence"] in ("low", "medium", "high")
            mins = [s["min"] for s in leg["strip"]]
            assert mins == sorted(mins)  # times along the ride only go forward
    assert len(j["departures"]) >= 5


def test_options_sorted_and_flags():
    j = plan("Badarpur Border (T)", "Saket Metro Station").json()
    fastest = min(op["total_min"] for op in j["options"])
    assert any(op["is_fastest"] for op in j["options"])
    assert all(op["extra_vs_fastest"] == op["total_min"] - fastest for op in j["options"])


def test_comfort_slider_prefers_calmer_rides():
    fast = plan("Badarpur Border (T)", "Saket Metro Station", comfort=0).json()["options"][0]
    calm = plan("Badarpur Border (T)", "Saket Metro Station", comfort=3).json()["options"][0]
    assert LEVELS.index(calm["level"]) <= LEVELS.index(fast["level"])
    assert fast["total_min"] <= calm["total_min"]


def test_leave_later_tip_is_calmer():
    j = plan("Shakarpur (Vikas Marg)", "Kendriya Terminal (Church Road)").json()
    assert j["tip"] is not None
    now = next(x for x in j["departures"] if x["shift"] == 0)
    assert LEVELS.index(j["tip"]["level"]) < LEVELS.index(now["level"])


def test_no_cross_city_teleport():
    """At every change of bus you get on where you got off, or within a short walk; never across the city."""
    pairs = [("Narela Terminal", "Saket Metro Station"), ("Badarpur Border (T)", "Kashmere Gate City Bus Terminal"),
             ("Dwarka Sec 21 Metro Station", "Anand Vihar Isbt Main Road"), ("Ganesh Nagar West", "Saket Metro Station")]

    for o, d in pairs:
        for op in E.plan_ids(E.resolve(o), E.resolve(d), 510, "weekday", 1.0):
            for prev, leg in zip(op.legs, op.legs[1:]):
                a, b = E.rg[prev.route][prev.j], E.rg[leg.route][leg.i]
                if a != b and a in E.group_xy and b in E.group_xy:
                    assert km(E.group_xy[a], E.group_xy[b]) <= 0.45, (o, d, prev.route, leg.route)

                elif a != b:
                    assert leg.walk_min > 0 or a == b


@pytest.mark.parametrize("params,code", [({"origin": "zzqx", "dest": "Saket Metro Station"}, 404),
                                         ({"origin": "Saket Metro Station", "dest": "Saket Metro Station"}, 400),
                                         ({"origin": "Saket Metro Station", "dest": "Nehru Place", "depart": "25:00"}, 400),
                                         ({"origin": "Saket Metro Station", "dest": "Nehru Place", "day": "sunday"}, 400)])
def test_plan_errors(params, code):
    r = client.get("/api/plan", params=params)
    assert r.status_code == code and r.json()["detail"]


def test_stop_search_has_spelling_variants():
    s = client.get("/api/stops", params={"q": "saket metro"}).json()["stops"]
    assert s and s[0]["name"] == "Saket Metro Station" and s[0]["ll"]


def test_route_profile():
    j = client.get("/api/route/717AUP").json()
    n = len(j["stops"]) - 1
    assert len(j["heat"]) == 48 and len(j["heat"][0]) == n and len(j["shape"]) == n + 1
    assert j["peak"]["demand_ratio"] > 0
    assert client.get("/api/route/NOPE").status_code == 404


def test_hotspots_sorted_by_demand():
    h = client.get("/api/hotspots", params={"time": "08:30"}).json()["hotspots"]
    assert h and all(a["demand_ratio"] >= b["demand_ratio"] for a, b in zip(h, h[1:]))
    assert all(x["confidence"] != "low" for x in h)


def test_validation_summary():
    s = client.get("/api/validation").json()["summary"]
    for k in ("week_to_week_corr_route_bin", "confidence_share_rider_weighted", "split_half_agreement_by_confidence",
              "gtfs_check", "geo", "planner_check"):
        assert k in s, k


def test_engine_memory_is_small():
    """The crowding data is memory-mapped, so the app fits a 512 MB container."""
    psutil = pytest.importorskip("psutil")
    assert psutil.Process().memory_info().rss < 400e6
    assert len(E.cube) > 2000
