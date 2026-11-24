# SeatSense: 2-minute demo video script

Record at 1366 px wide or larger. Start the app with `uvicorn app.main:app --port 8000` and wait for `/api/health` before you record. You need internet for the map tiles.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:10 | Plan a trip tab, nothing entered | "Delhi's journey planners tell you which bus to take, not whether you'll get a seat. SeatSense adds crowding, built from 2.56 million October e-tickets." |
| 0:10–0:32 | Click **Badarpur → Saket Metro · 8:30** | "The 717A is fastest at 39 minutes, but it's packed: half the ride standing. Route 34 takes 46 minutes and you'll likely get a seat. Seven minutes for a seat is the trade-off commuters actually make. Each leg shows how many people are on board and how confident we are." |
| 0:32–0:45 | Point at the **map**, click the 717A option | "On the map, every stretch is in its crowding colour. Click another option and the map follows." |
| 0:45–1:02 | Click **Shakarpur → Kendriya Terminal · 8:30**, point at the tip and the **Best time to leave** chart | "At 8:30 you would stand all the way. Leave at 9:00 and you get a seat, and you even arrive 3 minutes sooner. The chart shows every departure. Clicking a bar re-plans." |
| 1:02–1:08 | Drag the slider to **Most comfortable** | "The slider trades minutes for comfort." |
| 1:08–1:30 | **For operators** tab, hover the red lines, click one | "For DTC: at 8:30, 164 routes need more space than one bus has. The worst carry over three times a bus's capacity, so people are left behind at stops. Click one for that route's whole day, as a heatmap and on the map." |
| 1:30–2:00 | **How we know** tab | "E-tickets are only 3–4% of riders, so we scale with a range and cap it by bus capacity. Stop order and bus speeds come from the tickets alone, and we checked them against the official GTFS timetable: trip times match to 0.93×. 95% of riders get a medium- or high-confidence answer. Next: live GPS and metro." |

Tips: zoom the browser to 110 % so numbers are readable; keep the mouse still while talking; open the operator tab once before recording so the maps are loaded.
