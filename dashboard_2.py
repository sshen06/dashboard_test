import os
import time
import json
import math
import random
import threading
from collections import deque
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

# =========================
# APP SETUP
# =========================
app = Flask(__name__)

state_lock = threading.Lock()

MAX_HISTORY = 200
MAX_POSITIONS = 500

nodes = {}
imu_history = deque(maxlen=MAX_HISTORY)
rssi_history = deque(maxlen=MAX_HISTORY)
positions = deque(maxlen=MAX_POSITIONS)
raw_log = deque(maxlen=60)

serial1_connected = False
serial2_connected = False


# =========================
# HELPERS
# =========================
def now_ts():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# =========================
# SAFE INGEST
# =========================
def ingest(packet: dict):
    if not packet:
        return

    with state_lock:
        addr = packet.get("addr", 0)
        ts = packet.get("ts", now_ts())

        nodes[addr] = {**packet, "last_seen": time.time()}

        imu_history.append({
            "ts": ts,
            "ax": packet.get("ax", 0),
            "ay": packet.get("ay", 0),
            "az": packet.get("az", 0),
            "gx": packet.get("gx", 0),
            "gy": packet.get("gy", 0),
            "gz": packet.get("gz", 0),
            "addr": addr,
        })

        rssi_history.append({
            "ts": ts,
            "rssi": packet.get("rssi", -100),
            "snr": packet.get("snr", 0),
            "addr": addr,
        })

        lat = packet.get("lat")
        lon = packet.get("lon")

        if lat is not None and lon is not None:
            positions.append({
                "lat": lat,
                "lon": lon,
                "alt": packet.get("alt", 0),
                "ts": ts,
                "addr": addr,
            })

        raw_log.appendleft(f"[{ts}] NODE={addr} RSSI={packet.get('rssi',0)}")


# =========================
# DEMO INJECTOR (RELIABLE)
# =========================
def demo_injector():
    print("[DEMO] Injector started")

    t = 0

    nodes_demo = {
        1: (34.0564, -117.8216),
        2: (34.0580, -117.8210),
        3: (34.0568, -117.8235),
        4: (34.0552, -117.8205),
        5: (34.0575, -117.8225),
    }

    state = {
        addr: {
            "pos": [lat, lon],
            "dir": [random.uniform(-1, 1), random.uniform(-1, 1)]
        }
        for addr, (lat, lon) in nodes_demo.items()
    }

    CENTER_LAT, CENTER_LON = 34.0564, -117.8216
    BOUND = 0.0025

    while True:
        for addr, s in state.items():

            lat, lon = s["pos"]
            dx, dy = s["dir"]

            dx += random.uniform(-0.15, 0.15)
            dy += random.uniform(-0.15, 0.15)

            mag = math.sqrt(dx * dx + dy * dy) + 1e-6
            dx, dy = dx / mag, dy / mag

            speed = 0.00003 + addr * 0.000002

            lat += dx * speed
            lon += dy * speed

            lat += (CENTER_LAT - lat) * 0.0005
            lon += (CENTER_LON - lon) * 0.0005

            lat = max(min(lat, CENTER_LAT + BOUND), CENTER_LAT - BOUND)
            lon = max(min(lon, CENTER_LON + BOUND), CENTER_LON - BOUND)

            s["pos"] = [lat, lon]
            s["dir"] = [dx, dy]

            phase = t * 0.35 + addr

            ax = 0.3 * math.sin(phase)
            ay = 0.3 * math.cos(phase)
            az = 1.0 + 0.1 * math.sin(phase * 2)

            gx = 6 * math.sin(phase) + random.uniform(-0.8, 0.8)
            gy = 6 * math.cos(phase) + random.uniform(-0.8, 0.8)
            gz = 10 * math.sin(t * 0.1 + addr)

            if random.random() < 0.03:
                gz += random.uniform(25, 70)

            dist = math.sqrt((lat - CENTER_LAT) ** 2 + (lon - CENTER_LON) ** 2)

            rssi = -60 - int(dist * 5000)
            snr = 25 - int(dist * 2000)

            packet = {
                "addr": addr,
                "ts": now_ts(),
                "lat": lat,
                "lon": lon,
                "alt": 180,
                "sat": 6,
                "rssi": rssi,
                "snr": snr,
                "ax": ax,
                "ay": ay,
                "az": az,
                "gx": gx,
                "gy": gy,
                "gz": gz,
            }

            ingest(packet)

        t += 1
        time.sleep(1)


# =========================
# START DEMO AUTOMATICALLY (IMPORTANT FOR RENDER + GUNICORN)
# =========================
def start_background():
    thread = threading.Thread(target=demo_injector, daemon=True)
    thread.start()


start_background()


# =========================
# API
# =========================
@app.route("/api/state")
def api_state():
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

        # ensure at least placeholders so UI NEVER shows empty forever
        if not node_list:
            node_list = [
                {"addr": i, "online": False, "rssi": -120, "snr": 0,
                 "lat": None, "lon": None, "sat": 0, "alt": 0,
                 "ax": 0, "ay": 0, "az": 0}
                for i in range(1, 6)
            ]

        return jsonify({
            "nodes": node_list,
            "imu_history": list(imu_history)[-60:],
            "rssi_history": list(rssi_history)[-60:],
            "positions": list(positions),
            "raw_log": list(raw_log)[:20],
        })


# =========================
# FRONTEND
# =========================
HTML = """<YOUR EXISTING HTML HERE (UNCHANGED)>"""

@app.route("/")
def index():
    return render_template_string(HTML)


# =========================
# ENTRYPOINT
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)