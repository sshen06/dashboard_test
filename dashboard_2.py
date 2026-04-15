import os
import time
import math
import random
import threading
from collections import deque
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

# =========================
# APP
# =========================
app = Flask(__name__)

state_lock = threading.Lock()

nodes = {}
imu_history = deque(maxlen=200)
rssi_history = deque(maxlen=200)
positions = deque(maxlen=500)
raw_log = deque(maxlen=80)

demo_started = False


# =========================
# TIME
# =========================
def now_ts():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# =========================
# SAFE INGEST
# =========================
def ingest(pkt):
    if not pkt:
        return

    with state_lock:
        addr = pkt.get("addr", 0)

        nodes[addr] = {
            **pkt,
            "last_seen": time.time()
        }

        imu_history.append({
            "ts": pkt.get("ts", now_ts()),
            "ax": pkt.get("ax", 0),
            "ay": pkt.get("ay", 0),
            "az": pkt.get("az", 0),
            "gx": pkt.get("gx", 0),
            "gy": pkt.get("gy", 0),
            "gz": pkt.get("gz", 0),
            "addr": addr
        })

        rssi_history.append({
            "ts": pkt.get("ts", now_ts()),
            "rssi": pkt.get("rssi", -100),
            "snr": pkt.get("snr", 0),
            "addr": addr
        })

        if pkt.get("lat") is not None:
            positions.append({
                "lat": pkt["lat"],
                "lon": pkt["lon"],
                "ts": pkt.get("ts", now_ts()),
                "addr": addr
            })

        raw_log.appendleft(f"[{now_ts()}] NODE {addr} RSSI {pkt.get('rssi',0)}")


# =========================
# DEMO STREAM (ALWAYS RUNNING)
# =========================
def demo_loop():
    print("[DEMO] started")

    t = 0

    base_nodes = {
        1: (34.0564, -117.8216),
        2: (34.0580, -117.8210),
        3: (34.0568, -117.8235),
        4: (34.0552, -117.8205),
        5: (34.0575, -117.8225),
    }

    state = {
        a: {
            "pos": [lat, lon],
            "dir": [random.uniform(-1, 1), random.uniform(-1, 1)]
        }
        for a, (lat, lon) in base_nodes.items()
    }

    center = (34.0564, -117.8216)

    while True:
        for addr, s in state.items():
            lat, lon = s["pos"]
            dx, dy = s["dir"]

            dx += random.uniform(-0.1, 0.1)
            dy += random.uniform(-0.1, 0.1)

            mag = math.sqrt(dx * dx + dy * dy) + 1e-6
            dx, dy = dx / mag, dy / mag

            speed = 0.00003 + addr * 0.000002

            lat += dx * speed
            lon += dy * speed

            lat += (center[0] - lat) * 0.0005
            lon += (center[1] - lon) * 0.0005

            phase = t * 0.3 + addr

            ax = math.sin(phase) * 0.3
            ay = math.cos(phase) * 0.3
            az = 1.0

            dist = math.sqrt((lat-center[0])**2 + (lon-center[1])**2)

            pkt = {
                "addr": addr,
                "ts": now_ts(),
                "lat": lat,
                "lon": lon,
                "alt": 120,
                "sat": 6,
                "rssi": -60 - int(dist * 5000),
                "snr": 20 - int(dist * 2000),
                "ax": ax,
                "ay": ay,
                "az": az,
                "gx": random.uniform(-5, 5),
                "gy": random.uniform(-5, 5),
                "gz": random.uniform(-10, 10),
            }

            ingest(pkt)

        t += 1
        time.sleep(1)


# =========================
# START THREAD SAFELY (WORKS WITH GUNICORN)
# =========================
def start_background():
    global demo_started
    if demo_started:
        return
    demo_started = True

    t = threading.Thread(target=demo_loop, daemon=True)
    t.start()


start_background()


# =========================
# API
# =========================
@app.route("/api/state")
def state():
    with state_lock:
        now = time.time()

        node_list = []
        for addr, n in nodes.items():
            age = now - n.get("last_seen", now)
            node_list.append({
                **n,
                "age_s": round(age, 1),
                "online": age < 60
            })

        # IMPORTANT: never empty UI
        if not node_list:
            node_list = [{
                "addr": i,
                "online": False,
                "rssi": -120,
                "snr": 0,
                "lat": None,
                "lon": None,
                "sat": 0,
                "alt": 0,
                "ax": 0,
                "ay": 0,
                "az": 0
            } for i in range(1, 6)]

        return jsonify({
            "nodes": node_list,
            "imu_history": list(imu_history)[-60:],
            "rssi_history": list(rssi_history)[-60:],
            "positions": list(positions),
            "raw_log": list(raw_log)[:25]
        })


# =========================
# FULL FRONTEND (RESTORED)
# =========================
HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>LoRa Dashboard</title>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>

<style>
body { margin:0; background:#050a0e; color:white; font-family:Arial; }
#map { height:300px; }
.panel { padding:10px; }
.node { padding:8px; border-bottom:1px solid #222; }
</style>
</head>

<body>

<h2 class="panel">LoRa Dashboard (LIVE)</h2>

<div id="nodes"></div>
<div id="map"></div>

<script>
const map = L.map('map').setView([34.0564, -117.8216], 14);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);

let markers = {};

async function update(){
  const res = await fetch('/api/state');
  const d = await res.json();

  document.getElementById("nodes").innerHTML =
    d.nodes.map(n =>
      `<div class="node">
        NODE ${n.addr} | RSSI ${n.rssi} | ${n.online ? "ONLINE" : "OFFLINE"}
      </div>`
    ).join("");

  d.positions.forEach(p => {
    if(!markers[p.addr]){
      markers[p.addr] = L.marker([p.lat, p.lon]).addTo(map);
    } else {
      markers[p.addr].setLatLng([p.lat, p.lon]);
    }
  });
}

setInterval(update, 1000);
update();
</script>

</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML)


# =========================
# RUN (RENDER SAFE)
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
