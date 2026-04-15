import argparse
import re
import threading
import time
import json
import math
from collections import deque
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify
import serial
import random
import os

def now_ts():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

MAX_HISTORY = 200
MAX_POSITIONS = 500

app = Flask(__name__)

state_lock = threading.Lock()
nodes = {}
imu_history  = deque(maxlen=MAX_HISTORY)
rssi_history = deque(maxlen=MAX_HISTORY)
positions    = deque(maxlen=MAX_POSITIONS)
raw_log      = deque(maxlen=60)

serial1_connected = False
serial2_connected = False

# ──────────────────────────────────────────────
# PARSERS (UNCHANGED)
# ──────────────────────────────────────────────
RCV_RE = re.compile(r'\+RCV=(\d+),(\d+),(.*),(-?\d+),(-?\d+)')

def parse_rylr_line(line: str):
    m = RCV_RE.match(line.strip())
    if not m:
        return None
    payload = m.group(3)
    rssi    = int(m.group(4))
    snr     = int(m.group(5))

    try:
        d = json.loads(payload)
    except json.JSONDecodeError:
        return None

    ts  = now_ts()
    fix = d.get('fix')
    lat = d.get('la') if fix else None
    lon = d.get('lo') if fix else None
    sat = d.get('sa', 0)
    alt = d.get('al', 0)

    addr = int(m.group(1))

    return [{
        'addr': addr,
        'ts': ts,
        'rssi': rssi,
        'snr': snr,
        'lat': lat,
        'lon': lon,
        'sat': sat,
        'alt': alt,
        'ax': d.get('ax', 0),
        'ay': d.get('ay', 0),
        'az': d.get('az', 0),
        'gx': d.get('gx', 0),
        'gy': d.get('gy', 0),
        'gz': d.get('gz', 0),
    }]

GPS_RE = re.compile(r'\[GPS\] FIX:(\d) SAT:(\d+) LAT:([\d\.\-]+) LON:([\d\.\-]+) ALT:([\d\.\-]+)')
IMU_RE = re.compile(r'\[IMU\] AX:([\d\.\-]+) AY:([\d\.\-]+) AZ:([\d\.\-]+) GX:([\d\.\-]+) GY:([\d\.\-]+) GZ:([\d\.\-]+)')
LORA_RX_RE = re.compile(r'\+RCV=(\d+),\d+,.*,(-?\d+),(-?\d+)')

node2_buffer = {}

def parse_esp32_line(line: str):
    global node2_buffer

    gm = GPS_RE.search(line)
    if gm:
        fix = int(gm.group(1))
        node2_buffer['fix'] = fix
        node2_buffer['sat'] = float(gm.group(2))
        node2_buffer['lat'] = float(gm.group(3)) if fix else None
        node2_buffer['lon'] = float(gm.group(4)) if fix else None
        node2_buffer['alt'] = float(gm.group(5))
        node2_buffer['ts']  = now_ts()

    im = IMU_RE.search(line)
    if im:
        node2_buffer['ax'] = float(im.group(1))
        node2_buffer['ay'] = float(im.group(2))
        node2_buffer['az'] = float(im.group(3))
        node2_buffer['gx'] = float(im.group(4))
        node2_buffer['gy'] = float(im.group(5))
        node2_buffer['gz'] = float(im.group(6))

    if all(k in node2_buffer for k in ['lat', 'ax']):
        pkt = {
            'addr': 2,
            'ts': node2_buffer.get('ts', now_ts()),
            'rssi': 0,
            'snr': 0,
            'lat': node2_buffer.get('lat'),
            'lon': node2_buffer.get('lon'),
            'sat': node2_buffer.get('sat', 0),
            'alt': node2_buffer.get('alt', 0),
            'ax': node2_buffer.get('ax', 0),
            'ay': node2_buffer.get('ay', 0),
            'az': node2_buffer.get('az', 0),
            'gx': node2_buffer.get('gx', 0),
            'gy': node2_buffer.get('gy', 0),
            'gz': node2_buffer.get('gz', 0),
        }
        node2_buffer = {}
        return pkt

    return None

# ──────────────────────────────────────────────
# INGEST (UNCHANGED LOGIC)
# ──────────────────────────────────────────────
def ingest(packet: dict):
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

        if packet['lat'] is not None and packet['lon'] is not None:
            positions.append({
                'lat': packet['lat'],
                'lon': packet['lon'],
                'alt': packet['alt'],
                'ts': packet['ts'],
                'addr': addr,
            })

        raw_log.appendleft(f"[{packet['ts']}] NODE={addr} RSSI={packet['rssi']}")

# ──────────────────────────────────────────────
# DEMO INJECTOR (UNCHANGED)
# ──────────────────────────────────────────────
def demo_injector():
    import random
    import math

    t = 0
    nodes_demo = {
        1: (34.0564, -117.8216),
        2: (34.0580, -117.8210),
        3: (34.0568, -117.8235),
        4: (34.0552, -117.8205),
        5: (34.0575, -117.8225),
    }

    node_state = {
        a: {"pos": [lat, lon], "dir": [random.uniform(-1,1), random.uniform(-1,1)]}
        for a,(lat,lon) in nodes_demo.items()
    }

    CENTER_LAT, CENTER_LON = 34.0564, -117.8216
    BOUND = 0.0025

    while True:
        for addr, state in node_state.items():
            lat, lon = state["pos"]
            dx, dy = state["dir"]

            dx += random.uniform(-0.15, 0.15)
            dy += random.uniform(-0.15, 0.15)

            mag = math.sqrt(dx*dx + dy*dy) + 1e-6
            dx, dy = dx/mag, dy/mag

            speed = 0.00003 + addr*0.000002

            lat += dx * speed
            lon += dy * speed

            lat = max(min(lat, CENTER_LAT+BOUND), CENTER_LAT-BOUND)
            lon = max(min(lon, CENTER_LON+BOUND), CENTER_LON-BOUND)

            state["pos"] = [lat, lon]
            state["dir"] = [dx, dy]

            packet = {
                "addr": addr,
                "ts": now_ts(),
                "lat": lat,
                "lon": lon,
                "alt": 180,
                "sat": 6,
                "rssi": -60,
                "snr": 20,
                "ax": 0.1,
                "ay": 0.2,
                "az": 1.0,
                "gx": 1,
                "gy": 1,
                "gz": 1,
            }

            ingest(packet)

        t += 1
        time.sleep(1)

# ──────────────────────────────────────────────
# START THREAD (WORKS ON RENDER + GUNICORN)
# ──────────────────────────────────────────────
threading.Thread(target=demo_injector, daemon=True).start()

# ──────────────────────────────────────────────
# API
# ──────────────────────────────────────────────
@app.route('/api/state')
def api_state():
    with state_lock:
        now = time.time()
        node_list = [
            {**n, 'age_s': round(now - n['last_seen'], 1)}
            for n in nodes.values()
        ]

        return jsonify({
            'nodes': node_list,
            'imu_history': list(imu_history)[-60:],
            'rssi_history': list(rssi_history)[-60:],
            'positions': list(positions),
            'raw_log': list(raw_log)[:20],
        })

# ──────────────────────────────────────────────
# HTML (UNCHANGED)
# ──────────────────────────────────────────────
HTML = r"""<YOUR HTML HERE EXACTLY SAME AS YOU SENT>"""

@app.route('/')
def index():
    return render_template_string(HTML)

# ──────────────────────────────────────────────
# RENDER / GUNICORN COMPATIBLE ENTRYPOINT
# ──────────────────────────────────────────────
def main():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

if __name__ == '__main__':
    main()
