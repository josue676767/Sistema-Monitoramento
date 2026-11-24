#!/usr/bin/env python3
"""Mapa de status de conexões a partir do Zabbix (estilo ViaIpê).
 
Somente biblioteca padrão do Python 3.8+. Sem instalação de pacotes.
  MOCK=1 python3 server.py   -> dados simulados
  python3 server.py          -> usa config.json (mock=false para Zabbix real)
"""
import csv, json, math, os, random, ssl, sys, time, threading, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
 
BASE = Path(__file__).parent
FRONT = BASE.parent / "front"
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
if "MOCK" in os.environ:
    CFG["mock"] = os.environ["MOCK"] == "1"
PORT = int(os.environ.get("PORT", CFG.get("port", 8080)))
LOCAIS_PATH = BASE / CFG.get("locais_file", "locais.csv")
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
 
 
ZBX = None if CFG["mock"] else Zabbix(CFG["zabbix"])
ITEMMAP = {}  # hostid -> {metric: (itemid, value_type)}


# ------------------------------------------- locais.csv (coordenadas fora do Zabbix)
_locais = {"mtime": None, "data": {}}


def _num(x):
    try:
        return float(str(x).strip().replace(",", "."))
    except ValueError:
        return None


def load_locais():
    """Lê locais.csv (host;lat;lon;tipo;capacidade_mbps;interface). Recarrega sozinho quando o arquivo muda."""
    try:
        mt = LOCAIS_PATH.stat().st_mtime
    except OSError:
        return {}
    if _locais["mtime"] != mt:
        data = {}
        lines = [l for l in LOCAIS_PATH.read_text(encoding="utf-8-sig").splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if lines:
            delim = ";" if lines[0].count(";") >= lines[0].count(",") else ","
            for row in csv.DictReader(lines, delimiter=delim):
                row = {(k or "").strip().lower(): (x or "").strip() for k, x in row.items()}
                if row.get("host"):
                    data[row["host"].lower()] = row
        _locais.update(mtime=mt, data=data)
    return _locais["data"]


def fetch_hosts():
    """Hosts dos grupos configurados: {hostid: host}, com o nome do grupo em h['_group']."""
    hosts = {}
    groups = ZBX.call("hostgroup.get", {"filter": {"name": CFG["groups"]}, "output": ["groupid", "name"]})
    for g in groups:
        for h in ZBX.call("host.get", {
            "groupids": [g["groupid"]], "monitored_hosts": True,
            "output": ["hostid", "host", "name"],
            "selectInventory": ["location_lat", "location_lon"],
            "selectTags": "extend", "selectInterfaces": ["available"],
        }):
            h["_group"] = g.get("name", "")
            hosts.setdefault(h["hostid"], h)
    return hosts


def write_template():
    """python3 server.py --template : cria/atualiza locais.csv com os hosts do Zabbix (sem apagar o que já foi preenchido)."""
    if ZBX is None:
        sys.exit('Defina "mock": false no config.json para ler os hosts do Zabbix.')
    cols = ["host", "nome", "lat", "lon", "tipo", "capacidade_mbps", "interface"]
    rows = list(load_locais().values())
    known = {k for r_ in rows for k in (r_.get("host", "").lower(), r_.get("nome", "").lower()) if k}
    added = 0
    for h in sorted(fetch_hosts().values(), key=lambda x: x["name"].lower()):
        if h["host"].lower() in known or h["name"].lower() in known:
            continue
        tags = {t["tag"].lower(): t["value"] for t in h.get("tags", [])}
        tipo = tags.get("tipo") or CFG.get("group_tipo", {}).get(h["_group"], "")
        rows.append({"host": h["host"], "nome": h["name"], "tipo": tipo, "capacidade_mbps": tags.get("capacidade_mbps", "")})
        added += 1
    with open(LOCAIS_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, cols, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"{LOCAIS_PATH}: {len(rows)} linhas ({added} novas). Preencha lat e lon (ex.: -2,5530) e salve; o mapa recarrega sozinho.")
 
 
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
    hosts = list(fetch_hosts().values())
    if not hosts:
        return []
    locais = load_locais()
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
        # prioridade: locais.csv > tags do host > (inventário, só coordenadas) > grupo > padrão
        loc = locais.get(h["host"].lower()) or locais.get(h["name"].lower()) or {}
        tipo = loc.get("tipo") or tags.get("tipo") or CFG.get("group_tipo", {}).get(h.get("_group")) or "wan"
        cap = _num(loc.get("capacidade_mbps")) or to_float(tags.get("capacidade_mbps")) or 0
        iface = loc.get("interface") or tags.get("interface")
        inv = h.get("inventory") or {}
        lat, lon = _num(loc.get("lat")), _num(loc.get("lon"))
        if lat is None or lon is None:
            lat = to_float(inv.get("location_lat")) if inv else None
            lon = to_float(inv.get("location_lon")) if inv else None
        if lat is None or lon is None:
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
 
 
# ------------------------------------------------------------------ demonstração
def _make_mock_hosts():
    rnd = random.Random(7)
    hs = []
    states = ["ok"] * 13 + ["warn"] * 4 + ["crit"] * 2 + ["down", "nodata"]
    rnd.shuffle(states)
    for i in range(1, 22):  # unidades do interior
        hs.append(dict(id=f"u{i}", name=f"Unidade Interior {i:02d}", tipo="wan",
                       lat=rnd.uniform(-22, -3), lon=rnd.uniform(-52, -38),
                       cap=rnd.choice([20, 50, 100, 200]), st=states[i - 1]))
    campus = (-15.79, -47.88)
    for i in range(1, 7):
        hs.append(dict(id=f"d{i}", name=f"Distribuição Prédio {i}", tipo="core-dist",
                       lat=campus[0] + rnd.uniform(-.004, .004), lon=campus[1] + rnd.uniform(-.004, .004),
                       cap=10000, st="ok" if i != 3 else "warn"))
    return hs
 
 
MOCK_HOSTS = _make_mock_hosts()
 
 
def _noise(key, bucket):
    return random.Random(f"{key}-{bucket}").random()
 
 
def _mock_values(h, ts):
    wan = h["tipo"] == "wan"
    st = h["st"]
    r = _noise(h["id"], int(ts // 300))
    lat_ok, lat_w, lat_c = ((12, 45, 120) if wan else (0.3, 2, 8))
    base = {"ok": lat_ok, "warn": lat_w, "crit": lat_c, "down": 0, "nodata": 0}[st]
    occ = {"ok": .25, "warn": .75, "crit": .93, "down": 0, "nodata": 0}[st]
    day = 0.6 + 0.4 * math.sin((ts % 86400) / 86400 * 2 * math.pi - 1.5)
    cap_bps = h["cap"] * 1e6
    return {
        "latency": base * (0.85 + 0.3 * r),
        "loss": {"ok": 0.0, "warn": 0.4 if wan else 0.15, "crit": 3.0, "down": 100, "nodata": 0}[st] * (0.7 + 0.6 * r),
        "in_bps": cap_bps * occ * day * (0.8 + 0.4 * r),
        "out_bps": cap_bps * occ * day * 0.35 * (0.8 + 0.4 * r),
    }
 
 
def collect_mock():
    pts = []
    now = time.time()
    for h in MOCK_HOSTS:
        if h["st"] == "nodata":
            vals = {}
        elif h["st"] == "down":
            vals = {"ping": 0}
        else:
            vals = dict(_mock_values(h, now), ping=1)
        probs = []
        if h["st"] == "crit":
            probs = [{"text": "Perda de pacotes alta no enlace", "severity": 4}]
        elif h["st"] == "down":
            probs = [{"text": "Unidade inacessível (ICMP ping)", "severity": 5}]
        elif h["st"] == "warn":
            probs = [{"text": "Ocupação acima de 70%", "severity": 2}]
        pts.append(build_point(h["id"], h["name"], h["lat"], h["lon"], h["tipo"], h["cap"], vals, probs, False))
    return pts
 
 
def history_mock(hostid, t0, t1, n=300):
    h = next((x for x in MOCK_HOSTS if x["id"] == hostid), None)
    if not h or h["st"] == "nodata":
        return {}
    step = max(10, int((t1 - t0) / n))
    out = {"latency": [], "loss": [], "in": [], "out": []}
    for ts in range(int(t0), int(t1), step):
        v = _mock_values(h, ts)
        out["latency"].append([ts, v["latency"]])
        out["loss"].append([ts, v["loss"]])
        out["in"].append([ts, v["in_bps"] / 1e6])
        out["out"].append([ts, v["out_bps"] / 1e6])
    return out
 
 
# ------------------------------------------------------------------ cache + HTTP
_cache = {"t": 0, "data": [], "err": None}
_lock = threading.Lock()
 
 
def get_points():
    with _lock:
        if time.time() - _cache["t"] > CFG.get("cache_seconds", 30):
            try:
                _cache["data"] = collect_mock() if CFG["mock"] else collect_real()
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
                data = history_mock(hostid, t0, t1, n) if CFG["mock"] else history_real(hostid, t0, t1, n)
                self._send(200, data)
            else:
                self._send(404, {"error": "não encontrado"})
        except Exception as e:
            self._send(500, {"error": str(e)})
 
    def log_message(self, *a):
        pass
 
 
if __name__ == "__main__":
    if "--template" in sys.argv:
        write_template()
        sys.exit(0)
    modo = "DEMONSTRAÇÃO (dados simulados)" if CFG["mock"] else "Zabbix " + CFG["zabbix"]["url"]
    print(f"Mapa de conexões em http://localhost:{PORT}  |  modo: {modo}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()