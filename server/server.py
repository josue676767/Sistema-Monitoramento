
import json, math, os, random, ssl, time, threading, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
 
BASE = Path(__file__).parent
FRONT = BASE.parent / "front"
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
if "MOCK" in os.environ:
    CFG["mock"] = os.environ["MOCK"] == "1"
PORT = int(os.environ.get("PORT", CFG.get("port", 8080)))
LAT_FACTOR = CFG.get("latency_factor", 1000)
 
 
# ---------------------------------------------------------------- Zabbix API
class Zabbix:
    def __init__(self, c):
        self.url = c["url"]
        self.token = os.environ.get("ZABBIX_TOKEN") or c.get("token", "")
        self.body_auth = c.get("auth_in_body", False)
        self.ctx = None if c.get("verify_tls", True) else ssl._create_unverified_context()
        self.n = 0
 
    def call(self, method, params):
        self.n += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params, "id": self.n}
        headers = {"Content-Type": "application/json-rpc"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
            if self.body_auth:
                body["auth"] = self.token
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
            out = json.load(r)
        if "error" in out:
            e = out["error"]
            raise RuntimeError(f"Zabbix {method}: {e.get('message')} {e.get('data', '')}")
        return out["result"]
 
 
ZBX = Zabbix(CFG["zabbix"])
ITEMMAP = {}  # hostid -> {metric: (itemid, value_type)}
 

# ------------------------------------------------------------ regras de status
def classify(key):
    k = key.lower()
    if any(x in k for x in ("error", "discard", "dropped")):
        return None
    for name, pref in sorted(CFG["metrics"].items(), key=lambda kv: -len(kv[1])):
        if key.startswith(pref):
            return name
    return None
 
 
def level(value, warn, crit):
    if value is None:
        return 0
    return 2 if value >= crit else 1 if value >= warn else 0
 
 
def build_point(pid, name, lat, lon, tipo, cap, vals, problems, down_flag):
    """vals: dict com ping, latency(ms), loss(%), in_bps, out_bps (None = sem dado)."""
    th = CFG["thresholds"].get(tipo) or CFG["thresholds"]["wan"]
    occ_in = occ_out = None
    if cap and vals.get("in_bps") is not None:
        occ_in = vals["in_bps"] / (cap * 1e6) * 100
    if cap and vals.get("out_bps") is not None:
        occ_out = vals["out_bps"] / (cap * 1e6) * 100
    occ = max([o for o in (occ_in, occ_out) if o is not None], default=None)
 
    has_data = any(vals.get(k) is not None for k in ("ping", "latency", "loss", "in_bps", "out_bps"))
    sev = max([p["severity"] for p in problems], default=0)
    ps = CFG["problem_severity"]
 
    if vals.get("ping") == 0 or (down_flag and not has_data):
        status = "down"
    elif not has_data:
        status = "nodata"
    else:
        lv = max(
            level(vals.get("latency"), th["latency_warn"], th["latency_crit"]),
            level(vals.get("loss"), th["loss_warn"], th["loss_crit"]),
            level(occ, th["occ_warn"], th["occ_crit"]),
            2 if sev >= ps["crit"] else 1 if sev >= ps["warn"] else 0,
        )
        status = ("ok", "warn", "crit")[lv]
 
    return {
        "id": pid, "name": name, "lat": lat, "lon": lon, "tipo": tipo,
        "capacity_mbps": cap or None, "status": status,
        "latency_ms": vals.get("latency"), "loss_pct": vals.get("loss"),
        "in_mbps": None if vals.get("in_bps") is None else vals["in_bps"] / 1e6,
        "out_mbps": None if vals.get("out_bps") is None else vals["out_bps"] / 1e6,
        "occ_in_pct": occ_in, "occ_out_pct": occ_out,
        "problems": problems, "thresholds": th,
    }
 
 
# ------------------------------------------------------------------ Zabbix real
def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
 
 
def collect_real():
    gids = [g["groupid"] for g in ZBX.call("hostgroup.get", {"filter": {"name": CFG["groups"]}, "output": ["groupid"]})]
    hosts = ZBX.call("host.get", {
        "groupids": gids, "monitored_hosts": True,
        "output": ["hostid", "name"],
        "selectInventory": ["location_lat", "location_lon"],
        "selectTags": "extend", "selectInterfaces": ["available"],
    })
    if not hosts:
        return []
    hids = [h["hostid"] for h in hosts]
    items = ZBX.call("item.get", {
        "hostids": hids, "monitored": True,
        "output": ["itemid", "hostid", "key_", "lastvalue", "lastclock", "value_type"],
        "search": {"key_": list(CFG["metrics"].values())}, "searchByAny": True,
    })
    trigs = ZBX.call("trigger.get", {
        "hostids": hids, "only_true": True, "monitored": True, "skipDependent": True,
        "output": ["triggerid", "description", "priority"],
        "selectHosts": ["hostid"], "expandDescription": True,
    })
    probs = {}
    for t in trigs:
        for h in t["hosts"]:
            probs.setdefault(h["hostid"], []).append({"text": t["description"], "severity": int(t["priority"])})
 
    now = time.time()
    stale = CFG.get("stale_minutes", 15) * 60
    by_host = {}
    for it in items:
        by_host.setdefault(it["hostid"], []).append(it)
 
    points = []
    for h in hosts:
        tags = {t["tag"].lower(): t["value"] for t in h.get("tags", [])}
        tipo = tags.get("tipo", "wan")
        cap = to_float(tags.get("capacidade_mbps")) or 0
        iface = tags.get("interface")
        inv = h.get("inventory") or {}
        lat = to_float(inv.get("location_lat")) if inv else None
        lon = to_float(inv.get("location_lon")) if inv else None
        if lat is None:
            lat, lon = to_float(tags.get("lat")), to_float(tags.get("lon"))
 
        best = {}  # metric -> (valor, itemid, value_type)
        for it in by_host.get(h["hostid"], []):
            m = classify(it["key_"])
            if not m:
                continue
            if m in ("in", "out") and iface and iface not in it["key_"]:
                continue
            if it["lastvalue"] == "" or now - int(it["lastclock"] or 0) > stale:
                continue
            v = to_float(it["lastvalue"])
            if v is None:
                continue
            if m == "latency":
                v *= LAT_FACTOR
            if m not in best or v > best[m][0]:
                best[m] = (v, it["itemid"], it["value_type"])
 
        ITEMMAP[h["hostid"]] = {m: (b[1], b[2]) for m, b in best.items()}
        vals = {
            "ping": best["ping"][0] if "ping" in best else None,
            "latency": best["latency"][0] if "latency" in best else None,
            "loss": best["loss"][0] if "loss" in best else None,
            "in_bps": best["in"][0] if "in" in best else None,
            "out_bps": best["out"][0] if "out" in best else None,
        }
        down_flag = any(i.get("available") == "2" for i in h.get("interfaces", []))
        points.append(build_point(h["hostid"], h["name"], lat, lon, tipo, cap, vals,
                                  sorted(probs.get(h["hostid"], []), key=lambda p: -p["severity"]), down_flag))
    return points
 
 
def downsample(ser, n=300):
    if len(ser) <= n:
        return ser
    step = math.ceil(len(ser) / n)
    out = []
    for i in range(0, len(ser), step):
        ch = ser[i:i + step]
        out.append([ch[len(ch) // 2][0], sum(v for _, v in ch) / len(ch)])
    return out
 
 
def history_real(hostid, t0, t1, n=300):
    if hostid not in ITEMMAP:
        get_points()
    span = t1 - t0
    out = {}
    for metric, (itemid, vt) in ITEMMAP.get(hostid, {}).items():
        if metric == "ping":
            continue
        if span <= 48 * 3600:
            rows = ZBX.call("history.get", {
                "itemids": [itemid], "history": int(vt), "time_from": int(t0), "time_till": int(t1),
                "output": ["clock", "value"], "sortfield": "clock", "sortorder": "ASC", "limit": 20000})
            ser = [[int(r["clock"]), float(r["value"])] for r in rows]
        else:
            rows = ZBX.call("trend.get", {"itemids": [itemid], "time_from": int(t0), "time_till": int(t1), "output": ["clock", "value_avg"]})
            ser = [[int(r["clock"]), float(r["value_avg"])] for r in rows]
        f = LAT_FACTOR if metric == "latency" else 1e-6 if metric in ("in", "out") else 1
        out[metric] = downsample([[t, v * f] for t, v in ser], n)
    return out
 

 
 
# ------------------------------------------------------------------ cache + HTTP
_cache = {"t": 0, "data": [], "err": None}
_lock = threading.Lock()
 
 
def get_points():
    with _lock:
        if time.time() - _cache["t"] > CFG.get("cache_seconds", 30):
            try:
                _cache["data"] = collect_real()
                _cache["err"] = None
            except Exception as e:  # mantém último dado bom
                _cache["err"] = str(e)
            _cache["t"] = time.time()
        return _cache["data"]
 
 
STATIC = {
    "/app.js":    ("app.js",    "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
 
 
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)
 
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, (FRONT / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path in STATIC:
                f, ct = STATIC[u.path]
                self._send(200, (FRONT / f).read_bytes(), ct)
            elif u.path == "/api/points":
                pts = get_points()
                self._send(200, {"mock": CFG["mock"], "updated": _cache["t"], "error": _cache["err"], "points": pts})
            elif u.path == "/api/history":
                hostid = q.get("id", [""])[0]
                now = time.time()
                if "from" in q:  # intervalo exato (usado pelo zoom)
                    t0 = float(q["from"][0]); t1 = min(float(q.get("to", [now])[0]), now)
                else:
                    t1 = now; t0 = now - min(float(q.get("hours", ["24"])[0]), 24 * 366) * 3600
                n = max(50, min(int(q.get("n", ["300"])[0]), 1500))
                data = history_real(hostid, t0, t1, n)
                self._send(200, data)
            else:
                self._send(404, {"error": "não encontrado"})
        except Exception as e:
            self._send(500, {"error": str(e)})
 
    def log_message(self, *a):
        pass
 
 
if __name__ == "__main__":
    modo = "Zabbix " + CFG["zabbix"]["url"]
    print(f"Mapa de conexões em http://localhost:{PORT}  |  modo: {modo}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()