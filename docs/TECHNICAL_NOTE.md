# SeatSense — Technical note (Problem 2: Crowdedness-aware journey planner)

**One line:** we turn 2.56 million ONDC e-ticket records into an estimate of how many people are on board every stretch of every Delhi bus route, every half hour, with a range and a confidence grade, and put it next to each journey option so a commuter can trade a few minutes for a seat.

## 1. Data and what it can and cannot tell us

| Fact about the data | Consequence | What we do |
|---|---|---|
| 2,559,899 ticket rows (2,559,796 after cleaning), 1–31 Oct 2026, 2,259 route-directions, ~7,100 stop names | Enough for weekly patterns, not seasons | Report weekday vs weekend; check week-to-week stability |
| E-tickets are only **~3–4 % of all bus tickets** (Problem 3 brief) | Counting tickets underestimates load ~25–33× | Scale to all riders with an explicit **range** (×25 / ×33 / ×45) |
| Women ride free (pink pass) and pass holders never buy tickets | Even ×33 can be low | Upper bound raised to ×45; documented in `pipeline/config.json` |
| **48.5 % of rows have no vehicle number** | Cannot rebuild every bus trip | Use the 51.5 % with a vehicle id only where a vehicle is needed (bus counts, travel times) |
| `end_stop_name` is the planned stop | People may get off earlier/later | Treated as alighting stop; noted as a source of error |
| E-ticket share differs by route (new 9 m DEVI e-buses have far higher QR use) | One global scale overstates those routes | **Per-route scale capped by physical capacity** (Section 4) |
| Static GTFS not required | — | Stop order and travel times are **inferred from the tickets themselves** (Sections 2, 5). The public Delhi bus GTFS is used **only for stop coordinates** (map, walking links, telling same-name stops apart; Sections 6–7) **and as an independent check** of trip times and bus counts (Section 9) |

## 2. Stop order without GTFS

Each ticket says "start stop comes before end stop" on that route-direction. For every route we:

1. Score each stop by passengers leaving it minus passengers arriving at it (weighted Borda).
2. Run an insertion local search: move each stop to the position that minimises the number of passengers whose (start, end) order is violated. Prefix sums make each move O(n + degree).

Result: **1,238 routes ordered; median 100 % of passengers consistent with the inferred order** (route-level consistency is reported in the UI). A naive score-only order gave 85 % on route 717A UP; local search brings it to 100 %.

## 3. On-board load per segment

For each route-direction, day type (weekday/weekend) and 30-minute bin:

- every ticket adds `+passengers` at its boarding index and `−passengers` at its alighting index;
- a cumulative sum along the route gives **e-ticket passengers on board each segment**;
- divide by the number of days of that type (22 weekdays, 9 weekend days).

**Buses in service.** A bus issues only ~6 e-tickets a day (median), so in most half-hours a running bus is never seen: counting the vehicles seen in a bin gave 1,875 buses citywide at 08:30 out of 6,023 buses seen per weekday, and **0.33×** the GTFS timetable. Instead, each route's **daily fleet** (distinct vehicles seen on it that day, median over days; a bus that works two routes counts towards each by its share of tickets, so the city total cannot exceed the real fleet) is spread through the day by the **citywide service profile** (share of vehicle-days between their first and last ticket at that half-hour), split over the route's directions, and never below the buses actually seen. Result: 1,002 at 06:00, 3,559 at 08:30, 4,220 at 12:00, 3,729 at 18:30, 1,191 at 22:00; **0.61× the timetable** (Section 9).

`on_board = e-ticket load per day × scale ÷ buses in service`, computed for low / mid / high scale.

## 4. From load to a crowding level a commuter understands

- Bus size: routes starting `D-` are 9 m DEVI e-buses (25 seats, ~45 crush); others 12 m (35 seats, ~70 crush).
- **Capacity-capped scale:** per route, the scale is capped so that the route's weekday p95 load per bus does not exceed crush capacity. This affects **8.2 % of routes**, mostly new 9 m e-bus routes with very high QR use.
- **Demand ratio** = estimated demand ÷ crush capacity. Above 1 means one bus cannot carry everyone: **riders are being left behind**. The displayed on-board number is clipped at capacity; the demand ratio is shown to operators.
- Level from load ÷ seats: < 0.8 *Seats likely*, < 1.0 *Few seats*, < 1.8 *Standing room*, otherwise or at capacity *Packed*.
- **Thin half-hours are pooled** with their neighbours: a cell backed by fewer than 15 tickets is replaced by a 0.25 / 0.5 / 0.25 average over the previous, same and next half-hour, and its ticket count is pooled the same way. Well-observed cells are not touched.
- **Confidence grades the level shown, not just the count.** Ticket counts are roughly Poisson, so the load has a 90 % sampling interval of about ±1.64/√n. *High*: ≥ 60 tickets, or ≥ 15 and the level is the same at both ends of that interval. *Medium*: ≥ 15, or ≥ 5 and the level is stable. Route-order consistency must also be ≥ 0.9 / 0.8.
- Result at weekday peaks (08:00–10:00, 18:00–20:00), **weighted by riders: 56 % seats likely · 10 % few seats · 23 % standing · 11 % packed**: about one rider in three stands. By stretch: 84 % · 5 % · 8 % · 2 % (most stretches are quiet route ends). **5,936 stretch × half-hour cells** have demand above one bus's crush capacity: riders are left behind.

## 5. Travel times from ticket timestamps

A ticket is issued when the passenger boards, so for one bus on one day the timestamps at successive boarding stops trace the bus along its route. From 387,575 consecutive same-bus ticket pairs we take minutes per stop-hop, filtered (1–15 hops apart, ≤ 60 min, 0.3–10 min/hop), and keep the median by route, day type and hour. City-wide weekday medians: **1.4 min/stop at 5–6 am, 1.7 at 8 am, 2.5 at 6–7 pm**, the evening peak Delhi commuters know. A route-hour with few observations is **shrunk towards a prior**: the route's own all-day speed shaped by the city's hour-of-day pattern, weighted as if it were 15 observations, and kept within 0.6–1.8× that prior. Without this, 5 noisy pairs made route 34 look twice as fast at 10 am.

## 6. Stop names

The same stop is spelled differently across routes ("Nehru Place (T.)", "Nehru Place / Paras Cinema", "Saket Metro Sation"). Names are normalised (case, punctuation, terminal suffixes, common misspellings) and grouped with union–find: identical normalised names merge, and a composite "A / B" name joins the single most-used stop whose whole name is A or B, so two different stops are never chained together. **6,724 names → 4,959 stop groups.** Search shows the other spellings ("also known as").

**A name is not a place.** "Jain Mandir" is a stop near Narela *and* a different stop near Saket, 25 km apart. Treating them as one let the planner change buses across the city. So a stop is a name group **at a place**: route stops with the same name more than 2 km apart become separate stops (the busiest keeps the plain name, others read e.g. "Jain Mandir (near Ahinsa Sathal)"). 2,735 names are split this way (254 clearly real second places used by ≥ 5 route stops; most others are single route stops with an uncertain position, kept apart to rule out false transfers and hidden from search). A route stop with no coordinate borrows its nearest placed neighbour's position for this; with no clue at all, and a name used in several places, it stays its own stop. **12,696 stop places in all.**

## 7. Stop locations and the map

Coordinates come from the latest public Delhi bus timetable (GTFS). The official Open Transit Data feed was last updated in June 2024 and covers 543 routes, so we use the broader Mobility Database / Transitland archive (2,964 routes, calendar 2022–24); no newer public timetable exists. GTFS route names use the same ids as the tickets (`717AUP`), so **914 of 1,238 routes are matched by id**, and the ticket route's stop order is **aligned with the timetable's stop order** (an order-preserving alignment over fuzzy name similarity, either direction: "Vayusena Bad" pairs with "Vayusenabad (Tigri)", "Jain Mandir" never with "Shiv Mandir"). Otherwise the name is matched anywhere in the city (exactly, or fuzzily among timetable stops within 2.5 km of a matched neighbour), and when several stops share a name the one nearest the route's matched neighbours is taken. Every match must be a plausible distance from its matched neighbours on the route (≤ 1.6 km per stop + 1.5 km); generic names such as "Check Post" that land in the wrong part of Delhi are rejected. A stop more than 1.5 km from both neighbours while they are close to each other (a zig-zag on the map) is dropped. Short gaps (≤ 4 stops) are filled between neighbours. Result: **93 % of route stops placed** (91 % before alignment), 0.7 % of hops over 3 km, and **232 zig-zags, down from 1,782**. Known stops check out: Saket Metro, Badarpur Border, Kashmere Gate ISBT and Laxmi Nagar Metro within 0.1 km. `pipeline/build_geo.py`, `pipeline/geo_quality.py`. `pipeline/build_geo.py`.

## 8. Journey planner

- Origin / destination by stop (autocomplete with spelling variants), departure time, day type. Every trip has a shareable link (`?from=…&to=…&t=…&day=…`).
- Direct, one-change and **two-change** options. A second change is only offered when it beats every option with fewer changes by more than 5 minutes or gives a calmer ride.
- **Walking links:** up to 400 m at the start, at a change and at the end (4.5 km/h, ×1.3 for streets), so a trip can use a stop across the road or round the corner.
- Over 400 random stop pairs (fixed seed, `pipeline/evaluate_planner.py`), trips with at least one option rose from **68 % to 100 %** with walking links and second changes. Door-to-door straight-line speed: median **12.9 km/h** (p90 18.9), realistic for Delhi buses; planning takes ~0.059 s.
- Wait = half the headway (route trip time ÷ buses in service, clamped 4–30 min); ride = Σ minutes per hop at the hour the bus is there; transfer penalty 5 min.
- Crowding is looked up for **each segment at the half-hour the bus actually reaches it**, so a long ride can start empty and end packed.
- Ranking: `time + comfort weight × crowding penalty` (slider from *Fastest* to *Most comfortable*).
- **Best time to leave:** re-plans every 15 min from 30 min earlier to 90 min later and charts trip time and crowding for each; the tip suggests the calmest later departure (ties: the soonest).
- Transfers are only kept when they save more than 2 minutes or give a calmer ride than the best direct bus.
- **Operators:** the most overloaded routes at a chosen half-hour, one row per route at its worst stretch, on a citywide map (164 routes over capacity at weekday 08:30, 37 at 18:30); per route, a stretch × half-hour heatmap and crowding map, with the busiest half-hour picked by demand vs capacity (loads shown are capped at the bus's capacity, so many cells tie).

## 9. Validation

| Check | Result |
|---|---|
| Week-to-week stability, weekday e-ticket volume by route × half-hour | **r = 0.934, 0.939, 0.943** for consecutive weeks |
| Peaks | Weekday peaks 8–10 am and 5–8 pm; Sunday lowest (see Validation tab) |
| Direction flip | Morning loads build towards central/metro hubs: 717A UP is busiest at 07:00 into Saket Metro (122 % of capacity), then empties |
| Confidence | cells: high 32 % · medium 39 % · low 29 %; **weighted by riders: high 66 % · medium 29 % · low 5 %** |
| Does the grade mean anything? (split-half: odd vs even days) | on busy stretches the two halves give the same level 57 % (high) · 34 % (medium) · 16 % (low); within one level 99 % · 96 % · 100 % |
| Trip time vs GTFS timetable (independent; the latest public timetable, a plan rather than observed runs) | ours 0.93× the timetable, median over 12,087 route-hours; 88 % within ±50 %. Ours slows in the evening peak (1.04× at 6–7 pm), the timetable is flat all day |
| Buses on the road vs GTFS timetable | ours 0.61× the timetable (was 0.33× before the fleet method); correlation of typical fleet across 902 routes 0.672 |
| Order consistency | median 100 % of passengers |
| Journey planner, 400 random stop pairs | 100 % found; door-to-door median 12.9 km/h, 0.5 % faster than 30 km/h (was 5 % before stops were split by place) |
| Scale vs published figures (Economic Survey of Delhi 2025-26, press reports) | our range gives 22.2–40.0 lakh riders a day; published 24.29 lakh (DTC 2025-26) and 40.8 lakh (DTC + cluster FY2024-25): inside our range (implied e-ticket share 2.18–3.66 %). 6,023 buses seen per weekday vs a published fleet of 6,100–6,300 (96–99 %) |
| Automated tests | 18 end-to-end API tests (`pytest`), including "no cross-city transfer" and "memory under 400 MB" |

## 10. Where estimates are least reliable

- Absolute on-board numbers depend on the scale and on the bus count. Both are checked against published totals (riders, fleet) and the timetable, but **no public source counts people on board**, so loads are shown as ranges; **relative** patterns (which stretch, which half-hour) are robust.
- Night bins and low-volume routes (flagged *low confidence*; only 5 % of rider-trips fall in these).
- Stops placed between neighbours rather than matched (approximate on the map); 324 routes not in the timetable are placed by name only; 7 % of route stops have no position.
- The timetable is a plan (calendar 2022–24); it is the newest public one, and is used only for positions and sanity checks.
- Routes with very high e-ticket share (scale capped), and stop names that differ beyond what normalisation catches.
- Planned vs actual alighting stop; free (pink pass) riders whose patterns may differ from ticket buyers.

## 11. Engineering

- The crowding model (2.4 M stretch × half-hour cells) is packed into one array block per route and day type and **memory-mapped**: the API runs in ~160 MB (a plain Python lookup needed 1.8 GB), starts in ~3 s, and answers a plan in ~0.06 s.
- FastAPI + a single static HTML page (no build step); Leaflet map with OpenStreetMap tiles; Dockerfile and a Render blueprint (`render.yaml`) for a free deployment.
- Every assumption is in `pipeline/config.json`; every number in this note is regenerated by the pipeline into `summary.json` and shown in the app's **How we know** tab.

## 12. What is next (Phase 2)

Join the bus GPS feed to replace ticket-derived travel times and confirm stop positions, and carry the same load engine into Problem 5 (metro + bus crowding from AFC taps) and Problem 3 (OD matrix from the same tickets).
