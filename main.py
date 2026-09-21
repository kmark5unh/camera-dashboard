import os, sqlite3, requests, cv2
import numpy as np
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler
import urllib3
urllib3.disable_warnings()

app = FastAPI()
DB_FILE = "/data/cameras.db"
SERVERS = [s.strip() for s in os.getenv("EXACQ_SERVER", "").split(",") if s.strip()]
USER = os.getenv("EXACQ_USER", "")
PASS = os.getenv("EXACQ_PASS", "")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS cameras 
                 (id TEXT PRIMARY KEY, name TEXT, server TEXT, status TEXT, is_blurry INTEGER DEFAULT 0, disconnect_count INTEGER DEFAULT 0, last_updated TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS maintenance_logs 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT, action_taken TEXT, resolved_at TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

def sync_cameras():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now()
    for srv in SERVERS:
        try:
            res = requests.get(f"{srv}/api/cameras", auth=(USER, PASS), verify=False, timeout=10)
            if res.status_code == 200:
                for cam in res.json():
                    cid = f"{srv}_{cam.get('id')}"
                    cname = cam.get("name", f"Cam {cam.get('id')}")
                    new_status = "UP" if cam.get("online") else "DOWN"
                    c.execute("SELECT status, disconnect_count FROM cameras WHERE id = ?", (cid,))
                    row = c.fetchone()
                    if row:
                        old_status, dcount = row
                        if old_status == "UP" and new_status == "DOWN": dcount += 1
                        c.execute("UPDATE cameras SET status=?, disconnect_count=?, last_updated=? WHERE id=?", (new_status, dcount, now, cid))
                    else:
                        c.execute("INSERT INTO cameras (id, name, server, status, disconnect_count, last_updated) VALUES (?,?,?,?,?,?)",
                                  (cid, cname, srv, new_status, 1 if new_status=="DOWN" else 0, now))
        except Exception: pass
    conn.commit()
    conn.close()

def check_blur():
    thresh = float(os.getenv("BLUR_THRESHOLD", "100.0"))
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, server FROM cameras WHERE status = 'UP'")
    cams = c.fetchall()
    for cid, srv in cams:
        raw_id = cid.split("_")[-1]
        try:
            res = requests.get(f"{srv}/api/cameras/{raw_id}/image", auth=(USER, PASS), verify=False, timeout=10)
            if res.status_code == 200:
                arr = np.frombuffer(res.content, np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
                score = cv2.Laplacian(img, cv2.CV_64F).var() if img is not None else 999.0
                is_blurry = 1 if score < thresh else 0
                c.execute("UPDATE cameras SET is_blurry = ? WHERE id = ?", (is_blurry, cid))
        except Exception: pass
    conn.commit()
    conn.close()

sched = BackgroundScheduler()
sched.add_job(sync_cameras, 'interval', minutes=5)
sched.add_job(check_blur, 'interval', hours=6)
sched.start()

@app.get("/api/summary")
def get_summary():
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM cameras WHERE status='UP'"); up = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cameras WHERE status='DOWN'"); down = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cameras WHERE disconnect_count >= 5"); flap = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cameras WHERE is_blurry = 1"); blur = c.fetchone()[0]
    conn.close()
    return {"up": up, "down": down, "flapping": flap, "blurry": blur}

@app.get("/api/cameras/{filter_type}")
def get_cams(filter_type: str):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    if filter_type == "up": c.execute("SELECT id, name, server, status, disconnect_count, is_blurry FROM cameras WHERE status='UP'")
    elif filter_type == "down": c.execute("SELECT id, name, server, status, disconnect_count, is_blurry FROM cameras WHERE status='DOWN'")
    elif filter_type == "flapping": c.execute("SELECT id, name, server, status, disconnect_count, is_blurry FROM cameras WHERE disconnect_count >= 5")
    elif filter_type == "blurry": c.execute("SELECT id, name, server, status, disconnect_count, is_blurry FROM cameras WHERE is_blurry = 1")
    else: c.execute("SELECT id, name, server, status, disconnect_count, is_blurry FROM cameras")
    rows = c.fetchall(); conn.close()
    return [{"id": r[0], "name": r[1], "server": r[2], "status": r[3], "disconnects": r[4], "blurry": r[5]} for r in rows]

@app.post("/api/log-fix")
def log_fix(p: dict):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute("INSERT INTO maintenance_logs (camera_id, action_taken, resolved_at) VALUES (?,?,?)", (p['camera_id'], p['action'], datetime.now()))
    c.execute("UPDATE cameras SET disconnect_count=0, status='UP', is_blurry=0 WHERE id=?", (p['camera_id'],))
    conn.commit(); conn.close()
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def ui():
    return '''<!DOCTYPE html><html><head><title>Camera Dashboard</title><style>
    body{font-family:sans-serif;margin:30px;background:#f4f6f8}.cards{display:flex;gap:15px;margin-bottom:20px}
    .card{background:#fff;padding:20px;border-radius:8px;flex:1;cursor:pointer;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.1)}
    .green{color:#2e7d32}.red{color:#c62828}.orange{color:#ef6c00}.purple{color:#7b1fa2}
    table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
    th,td{padding:10px 15px;text-align:left;border-bottom:1px solid #ddd}th{background:#333;color:#fff}
    button{background:#0066cc;color:#fff;border:none;padding:5px 10px;border-radius:4px;cursor:pointer}
    </style></head><body>
    <h2>University Camera Dashboard</h2>
    <div class="cards">
      <div class="card" onclick="loadList('up')"><h3>Up</h3><div class="green" id="c-up" style="font-size:32px;font-weight:bold">-</div></div>
      <div class="card" onclick="loadList('down')"><h3>Down</h3><div class="red" id="c-down" style="font-size:32px;font-weight:bold">-</div></div>
      <div class="card" onclick="loadList('flapping')"><h3>Frequent Disconnects</h3><div class="orange" id="c-flap" style="font-size:32px;font-weight:bold">-</div></div>
      <div class="card" onclick="loadList('blurry')"><h3>Blurry</h3><div class="purple" id="c-blur" style="font-size:32px;font-weight:bold">-</div></div>
    </div>
    <h3 id="t">Select a card above</h3>
    <table><thead><tr><th>Name</th><th>Server</th><th>Status</th><th>Disconnects</th><th>Blurry?</th><th>Action</th></tr></thead><tbody id="tb"></tbody></table>
    <script>
      async function u(){let r=await fetch('/api/summary');let d=await r.json();document.getElementById('c-up').innerText=d.up;document.getElementById('c-down').innerText=d.down;document.getElementById('c-flap').innerText=d.flapping;document.getElementById('c-blur').innerText=d.blurry}
      async function loadList(t){document.getElementById('t').innerText="Showing: "+t.toUpperCase();let r=await fetch('/api/cameras/'+t);let d=await r.json();let b=document.getElementById('tb');b.innerHTML='';d.forEach(c=>{b.innerHTML+=`<tr><td>${c.name}</td><td>${c.server}</td><td><b>${c.status}</b></td><td>${c.disconnects}</td><td>${c.blurry?'Yes':'No'}</td><td><button onclick="fix('${c.id}')">Log Fix</button></td></tr>`})}
      async function fix(id){let a=prompt("What fix was applied?");if(a){await fetch('/api/log-fix',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera_id:id,action:a})});u();loadList('down')}}
      u();
    </script></body></html>'''

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
