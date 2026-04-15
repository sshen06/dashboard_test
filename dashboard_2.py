import os
import re
import threading
import time
import json
import math
from collections import deque
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

# ─────────────────────────────
# STATE
# ─────────────────────────────
state_lock = threading.Lock()

nodes = {}
imu_history = deque(maxlen=200)
rssi_history = deque(maxlen=200)
positions = deque(maxlen=500)
raw_log = deque(maxlen=60)

serial1_connected = False
serial2_connected = False


def now_ts():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


# ─────────────────────────────
# INGEST
# ─────────────────────────────
def ingest(packet):
    with state_lock:
        addr = packet['addr']
        nodes[addr] = {**packet, 'last_seen': time.time()}

        imu_history.append({
            'ts': packet['ts'],
            'ax': packet['ax'], 'ay': packet['ay'], 'az': packet['az'],
            'gx': packet['gx'], 'gy': packet['gy'], 'gz': packet['gz'],
            'addr': addr,
        })

        rssi_history.append({
            'ts': packet['ts'],
            'rssi': packet['rssi'],
            'snr': packet['snr'],
            'addr': addr,
        })

        if packet.get('lat') is not None:
            positions.append({
                'lat': packet['lat'],
                'lon': packet['lon'],
                'alt': packet['alt'],
                'ts': packet['ts'],
                'addr': addr,
            })

        raw_log.appendleft(f"[{packet['ts']}] NODE={addr} RSSI={packet['rssi']}")


# ─────────────────────────────
# DEMO DATA
# ─────────────────────────────
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

    state = {a: {"pos": [lat, lon], "dir": [0.5, 0.5]} for a, (lat, lon) in nodes_demo.items()}

    CENTER_LAT, CENTER_LON = 34.0564, -117.8216

    while True:
        for addr, s in state.items():
            lat, lon = s["pos"]
            dx, dy = s["dir"]

            dx += math.sin(t * 0.1 + addr) * 0.1
            dy += math.cos(t * 0.1 + addr) * 0.1

            mag = math.sqrt(dx*dx + dy*dy) + 1e-6
            dx, dy = dx/mag, dy/mag

            lat += dx * 0.00003
            lon += dy * 0.00003

            lat += (CENTER_LAT - lat) * 0.0005
            lon += (CENTER_LON - lon) * 0.0005

            s["pos"] = [lat, lon]
            s["dir"] = [dx, dy]

            packet = {
                "addr": addr,
                "ts": now_ts(),
                "lat": lat,
                "lon": lon,
                "alt": 180,
                "sat": 6,
                "rssi": -60,
                "snr": 10,
                "ax": 0.2,
                "ay": 0.1,
                "az": 1.0,
                "gx": 0,
                "gy": 0,
                "gz": 0,
            }

            ingest(packet)

        t += 1
        time.sleep(1)


# ─────────────────────────────
# START THREAD (Render safe)
# ─────────────────────────────
_started = False

def start_demo():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=demo_injector, daemon=True).start()

start_demo()


# ─────────────────────────────
# API
# ─────────────────────────────
@app.route('/api/state')
def api_state():
    with state_lock:
        now = time.time()

        node_list = []
        for addr, n in nodes.items():
            age = now - n['last_seen']
            node_list.append({**n, "age_s": round(age, 1), "online": age < 60})

        if not node_list:
            node_list = [{
                "addr": i,
                "online": False,
                "rssi": -120,
                "snr": 0,
                "lat": None,
                "lon": None,
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
            "raw_log": list(raw_log)[:20],
        })


# ─────────────────────────────
# YOUR HTML (UNCHANGED)
# ─────────────────────────────
HTML = r"""
PASTE YOUR ORIGINAL HTML HERE EXACTLY (NO CHANGES AT ALL)
"""


@app.route('/')
def index():
    return render_template_string(HTML)


# ─────────────────────────────
# GUNICORN ENTRYPOINT
# ─────────────────────────────
