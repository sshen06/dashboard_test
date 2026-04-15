"""
LoRa GPS Telemetry Dashboard - Dual Node
=========================================
NODE 1: ESP32 debug serial (COM3) — reads GPS/IMU directly
NODE 2: CP2102 receiver (COM4) — receives LoRa packets from ESP32

Usage:
  pip install flask pyserial
  python dashboard_3.py --port COM3 --port2 COM4 --baud 115200 --web-port 8080
  python dashboard_3.py --demo
"""

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

def now_ts():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

MAX_HISTORY   = 200
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
# Parser for RYLR896 +RCV lines
# Creates NODE 1 (transmitter) and NODE 2 (receiver) from each packet
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

    node = {
        'addr': addr, 'ts': ts,
        'rssi': rssi, 'snr': snr,
        'lat': lat, 'lon': lon,
        'sat': sat, 'alt': alt,
        'ax': d.get('ax', 0), 'ay': d.get('ay', 0), 'az': d.get('az', 0),
        'gx': d.get('gx', 0), 'gy': d.get('gy', 0), 'gz': d.get('gz', 0),
    }

    return [node]

# ──────────────────────────────────────────────
# Parser for ESP32 debug serial
# ──────────────────────────────────────────────
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

    if '[LORA RX]' in line:
        rm = LORA_RX_RE.search(line)
        if rm:
            rssi = int(rm.group(2))
            snr  = int(rm.group(3))
            return {
                'addr': 2, 'ts': now_ts(),
                'rssi': rssi, 'snr': snr,
                'lat': None, 'lon': None,
                'sat': 0, 'alt': 0,
                'ax': 0, 'ay': 0, 'az': 0,
                'gx': 0, 'gy': 0, 'gz': 0,
            }

    if all(k in node2_buffer for k in ['lat', 'ax']):
        pkt = {
            'addr': 2,
            'ts':   node2_buffer.get('ts', now_ts()),
            'rssi': 0, 'snr': 0,
            'lat':  node2_buffer.get('lat'),
            'lon':  node2_buffer.get('lon'),
            'sat':  node2_buffer.get('sat', 0),
            'alt':  node2_buffer.get('alt', 0),
            'ax':   node2_buffer.get('ax', 0),
            'ay':   node2_buffer.get('ay', 0),
            'az':   node2_buffer.get('az', 0),
            'gx':   node2_buffer.get('gx', 0),
            'gy':   node2_buffer.get('gy', 0),
            'gz':   node2_buffer.get('gz', 0),
        }
        node2_buffer = {}
        return pkt
    return None

# ──────────────────────────────────────────────
# Ingest
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
            'ts':   packet['ts'],
            'rssi': packet['rssi'],
            'snr':  packet['snr'],
            'addr': addr,
        })

        if packet['lat'] is not None and packet['lon'] is not None:
            positions.append({
                'lat': packet['lat'], 'lon': packet['lon'],
                'alt': packet['alt'], 'ts':  packet['ts'],
                'addr': addr,
            })

        if addr == 1:
            raw_log.appendleft(
                f"[{packet['ts']}] NODE={addr} "
                f"RSSI={packet['rssi']} SNR={packet['snr']} "
                f"LAT={packet['lat']} LON={packet['lon']} "
                f"SAT={int(packet['sat'])} ALT={packet['alt']:.1f}m"
            )

# ──────────────────────────────────────────────
# Serial readers
# ──────────────────────────────────────────────
def serial_reader_node1(port: str, baud: int):
    global serial1_connected
    while True:
        try:
            with serial.Serial(port, baud, timeout=1) as ser:
                serial1_connected = True
                print(f"[NODE1] Connected to {port} @ {baud}")
                while True:
                    line = ser.readline().decode('ascii', errors='ignore').strip()
                    if line:
                        with state_lock:
                            raw_log.appendleft(f"[N1 RAW] {line}")
                        pkts = parse_rylr_line(line)
                        if pkts:
                            for pkt in pkts:
                                ingest(pkt)
        except serial.SerialException as e:
            serial1_connected = False
            print(f"[NODE1] {e} — retrying in 3s")
            time.sleep(3)

def serial_reader_node2(port: str, baud: int):
    global serial2_connected
    while True:
        try:
            with serial.Serial(port, baud, timeout=1) as ser:
                serial2_connected = True
                print(f"[NODE2] Connected to {port} @ {baud}")
                while True:
                    line = ser.readline().decode('ascii', errors='ignore').strip()
                    if line:
                        pkt = parse_esp32_line(line)
                        if pkt:
                            ingest(pkt)
        except serial.SerialException as e:
            serial2_connected = False
            print(f"[NODE2] {e} — retrying in 3s")
            time.sleep(3)

# ──────────────────────────────────────────────
# Demo mode
# ──────────────────────────────────────────────
def demo_injector():
    import random
    import math

    t = 0

    # ── starting positions (5 nodes) ──
    nodes_demo = {
        1: (34.0564, -117.8216),
        2: (34.0580, -117.8210),
        3: (34.0568, -117.8235),
        4: (34.0552, -117.8205),
        5: (34.0575, -117.8225),
    }

    # mutable positions + direction (walking vector)
    node_state = {
        addr: {
            "pos": [lat, lon],
            "dir": [random.uniform(-1, 1), random.uniform(-1, 1)]
        }
        for addr, (lat, lon) in nodes_demo.items()
    }

    CENTER_LAT, CENTER_LON = 34.0564, -117.8216
    BOUND = 0.0025  # ~250m area

    while True:
        for addr, state in node_state.items():

            lat, lon = state["pos"]
            dx, dy = state["dir"]

            # ── smooth "walking" drift (not random jitter) ──
            turn_strength = 0.15

            dx += random.uniform(-turn_strength, turn_strength)
            dy += random.uniform(-turn_strength, turn_strength)

            # normalize direction (keeps constant walking speed)
            mag = math.sqrt(dx*dx + dy*dy) + 1e-6
            dx /= mag
            dy /= mag

            speed = 0.00003 + (addr * 0.000002)

            lat += dx * speed
            lon += dy * speed

            # ── soft boundary (push back toward center) ──
            lat += (CENTER_LAT - lat) * 0.0005
            lon += (CENTER_LON - lon) * 0.0005

            # clamp
            lat = max(min(lat, CENTER_LAT + BOUND), CENTER_LAT - BOUND)
            lon = max(min(lon, CENTER_LON + BOUND), CENTER_LON - BOUND)

            state["pos"] = [lat, lon]
            state["dir"] = [dx, dy]

            # ── realistic motion signal (walking) ──
            phase = t * 0.35 + addr

            ax = 0.3 * math.sin(phase)
            ay = 0.3 * math.cos(phase)
            az = 1.0 + 0.1 * math.sin(phase * 2)

            gx = 6 * math.sin(phase) + random.uniform(-0.8, 0.8)
            gy = 6 * math.cos(phase) + random.uniform(-0.8, 0.8)

            gz = 10 * math.sin(t * 0.1 + addr)
            if random.random() < 0.04:
                gz += random.uniform(25, 70)  # turning event

            # ── signal quality (distance-based realism) ──
            dist_factor = math.sqrt((lat - CENTER_LAT)**2 + (lon - CENTER_LON)**2)

            rssi = -60 - int(dist_factor * 5000) + int(5 * math.sin(t * 0.2 + addr))
            snr  = 25 - int(dist_factor * 2000) + int(3 * math.cos(t * 0.15 + addr))

            payload = {
                "fix": 1,
                "la": round(lat, 6),
                "lo": round(lon, 6),
                "sa": 6,
                "al": 180,
                "ax": round(ax, 3),
                "ay": round(ay, 3),
                "az": round(az, 3),
                "gx": round(gx, 2),
                "gy": round(gy, 2),
                "gz": round(gz, 2),
            }

            payload_str = json.dumps(payload)

            line = f"+RCV={addr},{len(payload_str)},{payload_str},{rssi},{snr}"

            pkts = parse_rylr_line(line)
            if pkts:
                for pkt in pkts:
                    ingest(pkt)

        t += 1
        time.sleep(1)

# ──────────────────────────────────────────────
# REST API
# ──────────────────────────────────────────────
@app.route('/api/state')
def api_state():
    with state_lock:
        now = time.time()
        node_list = []
        for addr, n in sorted(nodes.items()):
            age = now - n['last_seen']
            node_list.append({**n, 'age_s': round(age, 1), 'online': age < 15})

        print(f"[DEBUG] nodes: {list(nodes.keys())}")

        return jsonify({
            'nodes':        node_list,
            'imu_history':  list(imu_history)[-60:],
            'rssi_history': list(rssi_history)[-60:],
            'positions':    list(positions),
            'raw_log':      list(raw_log)[:20],
            'serial_ok':    serial1_connected or serial2_connected,
            'node1_ok':     serial1_connected,
            'node2_ok':     serial2_connected,
        })

# ──────────────────────────────────────────────
# HTML
# ──────────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LoRa Telemetry Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;600;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
  :root {
    --bg:#050a0e; --panel:#0b1520; --border:#0f2840;
    --accent:#00d4ff; --accent2:#ff6b35;
    --green:#39ff14; --red:#ff2d55; --yellow:#ffd60a;
    --text:#c8dde8; --dim:#4a6478;
    --font-mono:'Share Tech Mono',monospace;
    --font-ui:'Exo 2',sans-serif;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--font-ui);min-height:100vh;overflow-x:hidden}
  body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,212,255,.015) 2px,rgba(0,212,255,.015) 4px);pointer-events:none;z-index:9999}
  header{display:flex;align-items:center;justify-content:space-between;padding:14px 28px;border-bottom:1px solid var(--border);background:linear-gradient(90deg,rgba(0,212,255,.06) 0%,transparent 60%);position:sticky;top:0;z-index:100;backdrop-filter:blur(8px)}
  .logo{font-weight:800;font-size:1.3rem;letter-spacing:.12em;color:var(--accent);text-transform:uppercase;display:flex;align-items:center;gap:10px}
  .logo-icon{width:28px;height:28px;border:2px solid var(--accent);border-radius:50%;display:flex;align-items:center;justify-content:center;animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(0,212,255,.5)}50%{box-shadow:0 0 0 8px rgba(0,212,255,0)}}
  .header-right{display:flex;align-items:center;gap:12px;font-family:var(--font-mono);font-size:.75rem}
  .badge{padding:4px 10px;border-radius:3px;font-size:.7rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase}
  .badge.ok{background:rgba(57,255,20,.15);color:var(--green);border:1px solid var(--green)}
  .badge.fail{background:rgba(255,45,85,.15);color:var(--red);border:1px solid var(--red)}
  #clock{color:var(--dim);letter-spacing:.06em}
  .grid{display:grid;grid-template-columns:320px 1fr;grid-template-rows:auto auto 1fr;gap:1px;background:var(--border);min-height:calc(100vh - 57px)}
  .panel{background:var(--panel);padding:18px;position:relative;overflow:hidden}
  .panel::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,var(--accent),transparent);opacity:.4}
  .panel-title{font-size:.65rem;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--accent);margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .panel-title::before{content:'';display:inline-block;width:6px;height:6px;background:var(--accent);clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%)}
  .sidebar{grid-column:1;grid-row:1/-1;display:flex;flex-direction:column;gap:1px;background:var(--border);overflow-y:auto}
  .sidebar>.panel{flex:0 0 auto}
  .sidebar>.panel.grow{flex:1}
  .node-card{border:1px solid var(--border);border-radius:4px;padding:12px;margin-bottom:10px;transition:border-color .3s}
  .node-card.online{border-color:rgba(57,255,20,.35)}
  .node-card.offline{border-color:rgba(255,45,85,.35);opacity:.7}
  .node-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .node-id{font-family:var(--font-mono);font-size:1rem;font-weight:700}
  .node-status{font-size:.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;padding:2px 8px;border-radius:2px}
  .node-status.online{background:rgba(57,255,20,.15);color:var(--green)}
  .node-status.offline{background:rgba(255,45,85,.15);color:var(--red)}
  .node-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px}
  .kv{display:flex;flex-direction:column;gap:1px}
  .kv-label{font-size:.58rem;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)}
  .kv-value{font-family:var(--font-mono);font-size:.82rem;color:var(--text)}
  .kv-value.accent{color:var(--accent)}
  .kv-value.green{color:var(--green)}
  .kv-value.red{color:var(--red)}
  .kv-value.yellow{color:var(--yellow)}
  .rssi-bar-wrap{margin-top:10px}
  .rssi-bar-track{height:4px;background:var(--border);border-radius:2px;overflow:hidden}
  .rssi-bar-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--red),var(--yellow),var(--green));transition:width .5s ease}
  .rssi-labels{display:flex;justify-content:space-between;font-size:.55rem;color:var(--dim);margin-top:2px}
  #map{height:320px;width:100%;border-radius:2px}
  .leaflet-container{background:#060e18!important}
  .charts-row{grid-column:2;display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border)}
  .chart-wrap{position:relative;height:200px}
  #log-list{font-family:var(--font-mono);font-size:.68rem;color:var(--dim);line-height:1.7;list-style:none}
  #log-list li:first-child{color:var(--text)}
  #log-list li{border-bottom:1px solid rgba(15,40,64,.6);padding:2px 0}
  .imu-vector{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
  .imu-axis{flex:1;min-width:70px;background:rgba(0,212,255,.04);border:1px solid var(--border);border-radius:3px;padding:8px;text-align:center}
  .imu-axis-label{font-size:.6rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin-bottom:4px}
  .imu-axis-val{font-family:var(--font-mono);font-size:.9rem;color:var(--accent2)}
  .no-nodes{text-align:center;padding:30px 10px;font-family:var(--font-mono);font-size:.75rem;color:var(--dim);line-height:2}
  .age-badge{font-size:.58rem;color:var(--dim);margin-top:6px;font-family:var(--font-mono)}
  ::-webkit-scrollbar{width:4px}
  ::-webkit-scrollbar-track{background:var(--bg)}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>
<header>
  <div class="logo"><div class="logo-icon">◈</div>LoRa Telemetry</div>
  <div class="header-right">
    <span id="clock">──:──:──</span>
  </div>
</header>

<div class="grid">
  <div class="sidebar">
    <div class="panel">
      <div class="panel-title">Node Status</div>
      <div id="nodes-container">
        <div class="no-nodes">Waiting for packets…<br>──────────<br>No nodes detected</div>
      </div>
    </div>
    <div class="panel grow">
      <div class="panel-title">Serial Log</div>
      <ul id="log-list"><li>Waiting…</li></ul>
    </div>
  </div>

  <div class="panel" style="grid-column:2;grid-row:1;">
    <div class="panel-title">GPS Map</div>
    <div id="map"></div>
  </div>

  <div class="charts-row" style="grid-column:2;grid-row:2;">
    <div class="panel">
      <div class="panel-title">Accelerometer (g)</div>
      <div class="chart-wrap"><canvas id="accel-chart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-title">Gyroscope (°/s)</div>
      <div class="chart-wrap"><canvas id="gyro-chart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-title">RSSI (dBm)</div>
      <div class="chart-wrap"><canvas id="rssi-chart"></canvas></div>
    </div>
    <div class="panel">
      <div class="panel-title">SNR (dB)</div>
      <div class="chart-wrap"><canvas id="snr-chart"></canvas></div>
    </div>
  </div>
</div>

<script>
function updateClock(){const n=new Date();document.getElementById('clock').textContent=n.toUTCString().slice(17,25)+' UTC'}
setInterval(updateClock,1000);updateClock();

const map=L.map('map',{zoomControl:true,attributionControl:false});
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:19,subdomains:'abcd'}).addTo(map);
map.setView([0,0],2);
const nodeColors=['#00d4ff','#ff6b35','#39ff14','#ffd60a','#bf5af2'];
const nodeMarkers={},nodePaths={};
let mapInit=false;
function nodeColor(a){return nodeColors[(a-1)%nodeColors.length]}

function updateMap(positions){
  if(!positions.length)return;
  const byAddr={};
  positions.forEach(p=>{if(!byAddr[p.addr])byAddr[p.addr]=[];byAddr[p.addr].push([p.lat,p.lon])});
  Object.entries(byAddr).forEach(([a,pts])=>{
    const addr=parseInt(a),col=nodeColor(addr);
    if(!nodePaths[addr])nodePaths[addr]=L.polyline(pts,{color:col,weight:2,opacity:.6}).addTo(map);
    else nodePaths[addr].setLatLngs(pts);
    const last=pts[pts.length-1];
    const icon=L.divIcon({html:`<div style="width:14px;height:14px;border-radius:50%;background:${col};border:2px solid #fff;box-shadow:0 0 8px ${col};"></div>`,iconSize:[14,14],iconAnchor:[7,7],className:''});
    if(!nodeMarkers[addr])nodeMarkers[addr]=L.marker(last,{icon}).addTo(map);
    else{nodeMarkers[addr].setLatLng(last);nodeMarkers[addr].setIcon(icon)}
  });
  if(!mapInit&&positions.length>0){mapInit=true;const l=positions[positions.length-1];map.setView([l.lat,l.lon],16)}
}

const chartCfg=ds=>({type:'line',data:{labels:[],datasets:ds},options:{animation:false,responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{legend:{labels:{color:'#4a6478',font:{size:10}}}},scales:{x:{display:false},y:{grid:{color:'rgba(15,40,64,.8)'},ticks:{color:'#4a6478',font:{size:9}}}}}});
const mkDs=(label,color)=>({label,data:[],borderColor:color,backgroundColor:color+'18',borderWidth:1.5,pointRadius:0,fill:true,tension:.35});
const accelChart=new Chart(document.getElementById('accel-chart'),chartCfg([mkDs('AX','#00d4ff'),mkDs('AY','#ff6b35'),mkDs('AZ','#39ff14')]));
const gyroChart =new Chart(document.getElementById('gyro-chart'), chartCfg([mkDs('GX','#00d4ff'),mkDs('GY','#ff6b35'),mkDs('GZ','#39ff14')]));
const rssiChart = new Chart(document.getElementById('rssi-chart'),
  chartCfg([
    mkDs('N1','#00d4ff'),
    mkDs('N2','#ff6b35'),
    mkDs('N3','#39ff14'),
    mkDs('N4','#ffd60a'),
    mkDs('N5','#bf5af2')
  ])
);

const snrChart = new Chart(document.getElementById('snr-chart'),
  chartCfg([
    mkDs('N1','#00d4ff'),
    mkDs('N2','#ff6b35'),
    mkDs('N3','#39ff14'),
    mkDs('N4','#ffd60a'),
    mkDs('N5','#bf5af2')
  ])
);

function pushToChart(chart, newLabel, ...newPoints){
  const maxPoints = 60;

  chart.data.labels.push(newLabel);

  newPoints.forEach((point, i) => {
    chart.data.datasets[i].data.push(point);
  });

  // keep only last N points (scrolling effect)
  if (chart.data.labels.length > maxPoints) {
    chart.data.labels.shift();
    chart.data.datasets.forEach(ds => ds.data.shift());
  }

  chart.update('none');
}

function rssiPct(r){return Math.min(100,Math.max(0,((r+120)/80)*100))}
function rssiClass(r){return r>-70?'green':r>-90?'yellow':'red'}

function renderNodes(nodes){
  const el=document.getElementById('nodes-container');
  if(!nodes.length){
    el.innerHTML='<div class="no-nodes">Waiting for packets…<br>──────────<br>No nodes detected</div>';
    return;
  }
  el.innerHTML=nodes.map(n=>{
    const cls=n.online?'online':'offline';
    const col=nodeColor(n.addr);
    const pct=rssiPct(n.rssi);
    const rCls=rssiClass(n.rssi);
    const lat=n.lat!=null?n.lat.toFixed(6):'──';
    const lon=n.lon!=null?n.lon.toFixed(6):'──';
    const alt=n.alt!=null?n.alt.toFixed(1)+' m':'──';
    const sat=n.sat!=null?Math.round(n.sat):'──';
    const satCls=n.sat>=4?'green':'red';
    return `<div class="node-card ${cls}">
      <div class="node-header">
        <span class="node-id" style="color:${col}">NODE ${n.addr}</span>
        <span class="node-status ${cls}">${n.online?'ONLINE':'OFFLINE'}</span>
      </div>
      <div class="node-grid">
        <div class="kv"><span class="kv-label">Latitude</span><span class="kv-value accent">${lat}</span></div>
        <div class="kv"><span class="kv-label">Longitude</span><span class="kv-value accent">${lon}</span></div>
        <div class="kv"><span class="kv-label">Altitude</span><span class="kv-value">${alt}</span></div>
        <div class="kv"><span class="kv-label">Satellites</span><span class="kv-value ${satCls}">${sat}</span></div>
        <div class="kv"><span class="kv-label">RSSI</span><span class="kv-value ${rCls}">${n.rssi} dBm</span></div>
        <div class="kv"><span class="kv-label">SNR</span><span class="kv-value">${n.snr} dB</span></div>
      </div>
      <div class="rssi-bar-wrap">
        <div class="rssi-bar-track"><div class="rssi-bar-fill" style="width:${pct}%"></div></div>
        <div class="rssi-labels"><span>−120</span><span>Signal Strength</span><span>−40</span></div>
      </div>
      <div class="imu-vector">
        <div class="imu-axis"><div class="imu-axis-label">AX</div><div class="imu-axis-val">${n.ax.toFixed(2)}</div></div>
        <div class="imu-axis"><div class="imu-axis-label">AY</div><div class="imu-axis-val">${n.ay.toFixed(2)}</div></div>
        <div class="imu-axis"><div class="imu-axis-label">AZ</div><div class="imu-axis-val">${n.az.toFixed(2)}</div></div>
      </div>
      <div class="age-badge">Last packet: ${n.age_s}s ago · ${n.ts}</div>
    </div>`;
  }).join('');
}

async function poll(){
  try{
    const r=await fetch('/api/state');
    const d=await r.json();
    renderNodes(d.nodes);
    updateMap(d.positions);
    const ih = d.imu_history;

    if (ih.length > 0) {
      const p = ih[ih.length - 1];   // latest point
      const t = p.ts.slice(11,19);

      pushToChart(accelChart, t, p.ax, p.ay, p.az);
      pushToChart(gyroChart,  t, p.gx, p.gy, p.gz);
    }
    const rh=d.rssi_history;
    const rts=rh.map(p=>p.ts.slice(11,19));
    if (rh.length > 0) {
      const p = rh[rh.length - 1];
      const t = p.ts.slice(11,19);

      const rssiVals = [null, null, null, null, null];
      const snrVals  = [null, null, null, null, null];

    if (p.addr >= 1 && p.addr <= 5) {
      rssiVals[p.addr - 1] = p.rssi;
      snrVals[p.addr - 1]  = p.snr;
    }

    pushToChart(rssiChart, t, ...rssiVals);
    pushToChart(snrChart,  t, ...snrVals);
  }

    document.getElementById('log-list').innerHTML=d.raw_log.map(l=>`<li>${l}</li>`).join('');
  }catch(e){console.warn('poll error',e)}
  setTimeout(poll,1000);
}
poll();
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)

# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────
import os
import threading

def start_app():
    print("[DEMO] Starting demo injector")

    t = threading.Thread(target=demo_injector, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Gunicorn / Render entry point
def boot_demo():
    print("[DEMO] Starting demo injector")
    threading.Thread(target=demo_injector, daemon=True).start()

boot_demo()