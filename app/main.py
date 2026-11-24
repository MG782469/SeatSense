"""SeatSense API.

GET /api/stops?q=            stop autocomplete (merged spelling variants)
GET /api/plan                journey options with crowding, per-stop strip and best-time-to-leave
GET /api/route/{route}       segment x time heatmap for operators
GET /api/hotspots            most crowded segments at a time
GET /api/validation          peaks, weekday/weekend, week-to-week stability
Run:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .engine import BIN, LEVEL_LABEL, LEVELS, Engine, option_json

E = Engine()
app = FastAPI(title="SeatSense API", version="1.1",
              description="Crowdedness-aware bus journey planner for Delhi, built on ONDC e-ticket data.")
app.add_middleware(GZipMiddleware, minimum_size=1000)
WEB = Path(__file__).resolve().parents[1] / "web"


def hhmm(s: str) -> float:
    try:
        h, m = s.split(":")
        v = int(h) * 60 + int(m)
        assert 0 <= v < 1440
        return v
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "Time must be HH:MM, e.g. 08:30") from exc


@app.get("/api/stops")
def stops(q: str = Query(..., min_length=1)):
    return {"stops": E.search(q)}


@app.get("/api/plan")
def plan(origin: str, dest: str, depart: str = "08:30", day: str = "weekday",
         comfort: float = Query(1.0, ge=0, le=3)):
    if day not in ("weekday", "weekend"):
        raise HTTPException(400, "day must be weekday or weekend")
    t = hhmm(depart)
    o, d = E.resolve(origin), E.resolve(dest)
    if not o:
        raise HTTPException(404, f"Couldn't find a stop called “{origin}”. Pick one from the suggestions.")
    if not d:
        raise HTTPException(404, f"Couldn't find a stop called “{dest}”. Pick one from the suggestions.")
    if o == d:
        raise HTTPException(400, "Start and destination are the same stop.")
    opts = E.plan_ids(o, d, t, day, comfort)
    if not opts:
        raise HTTPException(404, "No bus option with up to two changes between these stops. Try a nearby, busier stop.")
    fastest = min(op.total for op in opts)
    calmest = min(LEVELS.index(op.level) for op in opts)
    out = []
    for op in opts:
        j = option_json(op)
        j["extra_vs_fastest"] = round(op.total - fastest)
        j["is_fastest"] = round(op.total) == round(fastest)
        j["is_calmest"] = LEVELS.index(op.level) == calmest and calmest < LEVELS.index(max((x.level for x in opts), key=LEVELS.index))
        out.append(j)
    deps = E.departures(o, d, t, day, comfort)
    tip = None
    now = next((x for x in deps if x["shift"] == 0), None)
    if now:
        better = [x for x in deps if x["shift"] > 0 and LEVELS.index(x["level"]) < LEVELS.index(now["level"])]
        if better:
            # calmest ride first, then the smallest delay, then the quickest trip
            b = min(better, key=lambda x: (LEVELS.index(x["level"]), x["shift"], x["total_min"]))
            tip = {**b, "from_level": now["label"], "delta_min": b["total_min"] - now["total_min"]}
    return {"origin": E.group_label[o], "dest": E.group_label[d], "origin_ll": E.ll(o), "dest_ll": E.ll(d),
            "depart": depart, "day": day, "comfort": comfort,
            "options": out, "departures": deps, "tip": tip}


@app.get("/api/route/{route}")
def route(route: str, day: str = "weekday"):
    if route not in E.route_stops:
        raise HTTPException(404, "Unknown route. Pick one from the suggestions, e.g. 717AUP.")
    return E.route_profile(route, day)


@app.get("/api/routes")
def routes(q: str = ""):
    rs = sorted((r for r in E.route_stops if q.upper() in r.upper()), key=lambda r: (not r.upper().startswith(q.upper()), len(r), r))
    return {"routes": rs[:20]}


@app.get("/api/hotspots")
def hotspots(day: str = "weekday", time: str = "08:30", limit: int = Query(15, le=50)):
    b = int(hhmm(time) // BIN)
    return {"time": time, "day": day, "hotspots": E.hotspots(day, b, limit), "routes_over_capacity": E.routes_over_capacity(day, b)}


@app.get("/api/validation")
def validation():
    return {"summary": E.summary, "hourly": E.hourly, "levels": LEVEL_LABEL}


@app.get("/api/health")
def health():
    return {"ok": True, "routes": len(E.route_stops), "stops": len(E.group_members), "stops_on_map": len(E.group_xy)}


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})
