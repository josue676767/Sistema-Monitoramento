const ST = {ok:"Normal", warn:"Degradado", crit:"Crítico", down:"Fora", nodata:"Sem coleta"};
const COLOR = {ok:"#2e9e4f", warn:"#e6b800", crit:"#d93025", down:"#111", nodata:"#1a73e8"};
const ORDER = ["down","crit","warn","nodata","ok"];
const TIPO = {"wan":"WAN (interior)","core-dist":"Core → Distribuição","dist-acesso":"Distribuição → Acesso"};

let points = [], markers = {}, selected = null, hours = 24;
let hidden = new Set(JSON.parse(localStorage.getItem("hidden") || "[]"));

const map = L.map("map", {zoomControl:true}).setView([-14.5,-50], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom:19, attribution:"© OpenStreetMap"}).addTo(map);
const layer = L.layerGroup().addTo(map);

const $ = s => document.querySelector(s);
const fmt = (v, d=1, u="") => v == null ? "—" : v.toFixed(d).replace(".", ",") + u;

function visible(p){
  const q = $("#q").value.trim().toLowerCase();
  return !hidden.has(p.status) && (!q || p.name.toLowerCase().includes(q));
}

function renderChips(){
  const c = {}; points.forEach(p => c[p.status] = (c[p.status]||0)+1);
  $("#chips").innerHTML = ORDER.map(s =>
    `<div class="chip ${hidden.has(s)?"off":""}" data-s="${s}"><span class="dot s-${s}"></span>${ST[s]} <b>${c[s]||0}</b></div>`).join("");
  document.querySelectorAll(".chip").forEach(el => el.onclick = () => {
    const s = el.dataset.s; hidden.has(s) ? hidden.delete(s) : hidden.add(s);
    localStorage.setItem("hidden", JSON.stringify([...hidden])); render();
  });
}

function renderMap(){
  layer.clearLayers(); markers = {};
  const geo = [];
  points.filter(visible).forEach(p => {
    if (p.lat == null || p.lon == null) return;
    const m = L.circleMarker([p.lat,p.lon], {
      radius: p.id === selected ? 12 : 9, color:"#fff", weight:2,
      fillColor: COLOR[p.status], fillOpacity:.95
    }).addTo(layer);
    m.bindTooltip(`${p.name} — ${ST[p.status]}`);
    m.on("click", () => select(p.id));
    markers[p.id] = m; geo.push([p.lat,p.lon]);
  });
  return geo;
}

function renderList(){
  const sorted = points.filter(visible).sort((a,b) =>
    ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || a.name.localeCompare(b.name));
  $("#list").innerHTML = sorted.map(p => `
    <div class="row ${p.id===selected?"sel":""}" data-id="${p.id}">
      <span class="dot s-${p.status}"></span>
      <div>${p.name}<small>${TIPO[p.tipo]||p.tipo}${p.lat==null?" · sem coordenadas":""}</small></div>
    </div>`).join("") || `<div class="row">Nenhum ponto.</div>`;
  document.querySelectorAll(".row[data-id]").forEach(el => el.onclick = () => select(el.dataset.id, true));
}

function render(fit){
  renderChips(); const geo = renderMap(); renderList();
  if (fit && geo.length) map.fitBounds(geo, {padding:[40,40], maxZoom:14});
}

function cls(v, w, c){ return v == null ? "" : v >= c ? "c" : v >= w ? "w" : ""; }

function renderPanel(){
  const p = points.find(x => x.id === selected);
  if (!p){ $("#main").classList.remove("open"); return; }
  $("#main").classList.add("open"); map.invalidateSize();
  const t = p.thresholds, occ = Math.max(p.occ_in_pct ?? -1, p.occ_out_pct ?? -1);
  const probs = p.problems.length
    ? `<ul class="probs">${p.problems.map(x=>`<li class="${x.severity>=4?"c":""}">${x.text}</li>`).join("")}</ul>`
    : `<div class="leg">Nenhum problema ativo no Zabbix.</div>`;
  $("#panel").innerHTML = `
    <button class="x" onclick="select(null)">×</button>
    <h2>${p.name}</h2>
    <span class="badge s-${p.status}" ${p.status==="warn"?'style="color:#000"':""}>${ST[p.status]}</span>
    <span class="leg"> &nbsp;${TIPO[p.tipo]||p.tipo}${p.capacity_mbps?` · ${p.capacity_mbps} Mbps`:""}</span>
    <div class="grid">
      <div class="kpi ${cls(p.latency_ms,t.latency_warn,t.latency_crit)}"><b>${fmt(p.latency_ms,1," ms")}</b><span>Latência</span></div>
      <div class="kpi ${cls(p.loss_pct,t.loss_warn,t.loss_crit)}"><b>${fmt(p.loss_pct,2," %")}</b><span>Perda de pacotes</span></div>
      <div class="kpi ${cls(p.occ_in_pct,t.occ_warn,t.occ_crit)}"><b>${fmt(p.in_mbps,1," Mbps")}</b><span>Download${p.occ_in_pct!=null?` (${fmt(p.occ_in_pct,0,"%")})`:""}</span></div>
      <div class="kpi ${cls(p.occ_out_pct,t.occ_warn,t.occ_crit)}"><b>${fmt(p.out_mbps,1," Mbps")}</b><span>Upload${p.occ_out_pct!=null?` (${fmt(p.occ_out_pct,0,"%")})`:""}</span></div>
    </div>
    <div class="leg">Limites: latência ${t.latency_warn}/${t.latency_crit} ms · perda ${t.loss_warn}/${t.loss_crit} % · ocupação ${t.occ_warn}/${t.occ_crit} %</div>
    <h4 style="margin:12px 0 4px">Problemas</h4>${probs}
    <div class="period">${[[1,"1h"],[6,"6h"],[24,"24h"],[168,"7d"],[720,"30d"]].map(([h,l]) =>
      `<button class="${h===hours?"on":""}" onclick="setHours(${h})">${l}</button>`).join("")}</div>
    <div id="charts" class="chart">Carregando gráficos...</div>`;
  loadCharts(p);
}

const CH = [];

// Médias por hora (24h) ou por dia (7d, 30d), no fuso do navegador. 1h e 6h ficam como estão.
function aggMode(){ return hours === 24 ? "hour" : (hours === 168 || hours === 720) ? "day" : null; }
function aggregate(data, mode){
  const b = new Map();
  for (const [t, v] of data){
    const d = new Date(t * 1000);
    const start = (mode === "hour" ? new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours())
                                   : new Date(d.getFullYear(), d.getMonth(), d.getDate())).getTime() / 1000;
    const a = b.get(start) || (b.set(start, {sum: 0, n: 0}), b.get(start));
    a.sum += v; a.n++;
  }
  return [...b].sort((x, y) => x[0] - y[0]).map(([t, a]) => [t, a.sum / a.n]);
}

function chart(title, series, unit, ulabel){
  const raw = series, agg = aggMode();
  if (agg) series = series.map(s => Object.assign({}, s, {data: aggregate(s.data, agg)}));
  const W=360,H=110,pl=34,pr=6,pt=8,pb=16;
  const all = series.flatMap(s => s.data);
  if (!all.length) return `<h4>${title}</h4><svg></svg>`;
  const x0 = Math.min(...all.map(d=>d[0])), x1 = Math.max(...all.map(d=>d[0]));
  let y1 = Math.max(...all.map(d=>d[1]), 1e-9) * 1.1;
  const X = t => pl + (t-x0)/Math.max(x1-x0,1)*(W-pl-pr);
  const Y = v => H-pb - v/y1*(H-pb-pt);
  const idx = CH.push({title, series, raw, agg, x0, x1, y1, W, H, pl, pr, pt, pb, ulabel: ulabel||""}) - 1;
  const paths = series.map(s => `<polyline fill="none" stroke="${s.color}" stroke-width="1.5" points="${
    s.data.map(d => X(d[0]).toFixed(1)+","+Y(d[1]).toFixed(1)).join(" ")}"/>`).join("");
  const fmtT = t => { const d = new Date(t*1000); return hours>48 ? d.toLocaleDateString("pt-BR",{day:"2-digit",month:"2-digit"}) : d.toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit"}); };
  const dots = agg ? series.map(s => s.data.map(d => `<circle cx="${X(d[0]).toFixed(1)}" cy="${Y(d[1]).toFixed(1)}" r="2.2" fill="${s.color}"/>`).join("")).join("") : "";
  const leg = series.length>1 ? series.map(s=>`<span style="color:${s.color}">■ ${s.name}</span>`).join(" &nbsp;") : "";
  return `<h4><span>${title}</span><span style="font-weight:400">${leg}</span><button class="zoom" data-c="${idx}" title="Abrir em janela maior, com zoom">⤢ Ampliar</button></h4>
  <div class="cw" data-c="${idx}"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line x1="${pl}" x2="${W-pr}" y1="${Y(0)}" y2="${Y(0)}" stroke="#ccd"/>
    <line x1="${pl}" x2="${W-pr}" y1="${Y(y1/1.1)}" y2="${Y(y1/1.1)}" stroke="#eef" />
    <text x="2" y="${Y(y1/1.1)+4}" font-size="9" fill="#667">${(y1/1.1).toFixed(y1>20?0:1)}${unit}</text>
    <text x="2" y="${Y(0)}" font-size="9" fill="#667">0</text>
    <text x="${pl}" y="${H-3}" font-size="9" fill="#667">${fmtT(x0)}</text>
    <text x="${W-pr}" y="${H-3}" font-size="9" fill="#667" text-anchor="end">${fmtT(x1)}</text>
    ${paths}${dots}
    <line class="cur" x1="0" x2="0" y1="${pt}" y2="${H-pb}" stroke="#667" stroke-width="1" stroke-dasharray="3 2" vector-effect="non-scaling-stroke" style="display:none"/>
  </svg><div class="tip" style="display:none"></div></div>`;
}

async function loadCharts(p){
  try{
    const h = await (await fetch(`/api/history?id=${encodeURIComponent(p.id)}&hours=${hours}`)).json();
    if (selected !== p.id) return;
    if (h.error) throw new Error(h.error);
    if (!Object.keys(h).length){ $("#charts").textContent = "Sem histórico para este ponto."; return; }
    CH.length = 0;
    $("#charts").innerHTML =
      chart("Latência (ms)", [{key:"latency",name:"latência",color:"#1a73e8",data:h.latency||[]}], "", "ms") +
      chart("Perda de pacotes (%)", [{key:"loss",name:"perda",color:"#d93025",data:h.loss||[]}], "", "%") +
      chart("Tráfego (Mbps)", [{key:"in",name:"download",color:"#2e9e4f",data:h.in||[]},{key:"out",name:"upload",color:"#e68a00",data:h.out||[]}], "", "Mbps");
    bindTips();
    document.querySelectorAll("#charts .zoom").forEach(b => b.onclick = () => openBig(CH[+b.dataset.c]));
  }catch(e){ $("#charts").textContent = "Erro ao carregar histórico: " + e.message; }
}

function bindTips(){
  const fmtTip = (t, agg) => {
    const d = new Date(t*1000);
    const hm = x => x.toLocaleTimeString("pt-BR", {hour:"2-digit", minute:"2-digit"});
    const dm = d.toLocaleDateString("pt-BR", {day:"2-digit", month:"2-digit"});
    if (agg === "hour") return `${dm} ${hm(d)}–${hm(new Date((t + 3600) * 1000))} · média`;
    if (agg === "day") return `${dm} · média do dia`;
    return hours > 24 ? dm + " " + hm(d) : hm(d);
  };
  const num = v => v.toFixed(2).replace(".", ",");
  document.querySelectorAll("#charts .cw").forEach(box => {
    const c = CH[+box.dataset.c];
    const svg = box.querySelector("svg"), cur = box.querySelector(".cur"), tip = box.querySelector(".tip");
    const dots = c.series.map(s => {
      const d = document.createElement("div"); d.className = "pt"; d.style.background = s.color; d.style.display = "none";
      box.appendChild(d); return d;
    });
    box.addEventListener("dblclick", () => openBig(c));
    const hide = () => { cur.style.display = "none"; tip.style.display = "none"; dots.forEach(d => d.style.display = "none"); };
    box.addEventListener("mouseleave", hide);
    box.addEventListener("mousemove", e => {
      const r = svg.getBoundingClientRect();
      const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      const sx = f * c.W;
      if (sx < c.pl || sx > c.W - c.pr) return hide();
      const t = c.x0 + (sx - c.pl) / (c.W - c.pl - c.pr) * (c.x1 - c.x0);
      const near = c.series.map(s => s.data.reduce((b, d) => (!b || Math.abs(d[0]-t) < Math.abs(b[0]-t)) ? d : b, null));
      const ref = near.find(Boolean);
      if (!ref) return hide();
      const px = c.pl + (ref[0] - c.x0) / Math.max(c.x1 - c.x0, 1) * (c.W - c.pl - c.pr);
      cur.setAttribute("x1", px); cur.setAttribute("x2", px); cur.style.display = "";
      let html = `<b>${fmtTip(ref[0], c.agg)}</b>`;
      near.forEach((b, i) => {
        if (!b){ dots[i].style.display = "none"; return; }
        const y = c.H - c.pb - b[1] / c.y1 * (c.H - c.pb - c.pt);
        dots[i].style.display = "";
        dots[i].style.left = (px / c.W * 100) + "%";
        dots[i].style.top = (y / c.H * 100) + "%";
        const s = c.series[i];
        html += `<br>${c.series.length > 1 ? `<span style="color:${s.color}">■</span> ${s.name}: ` : ""}${num(b[1])} ${c.ulabel}`;
      });
      tip.innerHTML = html;
      tip.style.display = "";
      tip.style.left = (px / c.W * 100) + "%";
      tip.style.transform = f > 0.55 ? "translateX(calc(-100% - 10px))" : "translateX(10px)";
    });
  });
}

/* ===== janela ampliada com zoom ===== */
let BIG = null;
const MINSPAN = 60;
const T_STEPS = [10,30,60,120,300,600,900,1800,3600,7200,10800,21600,43200,86400,172800,604800];
const num2 = v => v.toLocaleString("pt-BR", {minimumFractionDigits:2, maximumFractionDigits:2});
const fmtFull = (t, span) => new Date(t*1000).toLocaleString("pt-BR",
  Object.assign({day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"}, span < 3600 ? {second:"2-digit"} : {}));

function niceTicks(lo, hi, n){
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag;
  const step = (r < 1.5 ? 1 : r < 3 ? 2 : r < 7 ? 5 : 10) * mag, t = [];
  for (let v = Math.floor(lo/step)*step; v <= Math.ceil(hi/step)*step + step/2; v += step) t.push(+v.toFixed(10));
  return t;
}
function timeTicks(a, b, maxN){
  const span = b - a;
  const step = T_STEPS.find(s => span / s <= maxN) || T_STEPS[T_STEPS.length - 1];
  const off = new Date(a*1000).getTimezoneOffset() * 60, ticks = [];
  for (let t = Math.ceil((a - off) / step) * step + off; t <= b; t += step) ticks.push(t);
  return {ticks, step};
}
function fmtAxis(t, step, span){
  const d = new Date(t*1000);
  const hm = d.toLocaleTimeString("pt-BR", Object.assign({hour:"2-digit", minute:"2-digit"}, step < 60 ? {second:"2-digit"} : {}));
  const dm = d.toLocaleDateString("pt-BR", {day:"2-digit", month:"2-digit"});
  return step >= 86400 ? dm : span > 86400 ? dm + " " + hm : hm;
}

function ensureBig(){
  if ($("#big")) return;
  const el = document.createElement("div");
  el.id = "big";
  el.innerHTML = `<div class="bd">
    <div class="bh"><b id="bt"></b><span id="bl"></span><span class="spacer"></span>
      <span class="leg" id="brange"></span>
      <button id="breset" style="visibility:hidden">Restaurar</button>
      <button id="bx" class="x2" title="Fechar (Esc)">×</button></div>
    <div id="bw"><svg id="bs"></svg><div id="btip"></div></div>
    <div class="bf leg">Arraste para ampliar · roda do mouse = zoom · Shift + arrastar = mover · duplo clique = restaurar · Esc = fechar</div>
  </div>`;
  document.body.appendChild(el);
  el.addEventListener("mousedown", e => { if (e.target === el) closeBig(); });
  $("#bx").onclick = closeBig;
  $("#breset").onclick = resetView;
  document.addEventListener("keydown", e => { if (e.key === "Escape" && BIG) closeBig(); });
  window.addEventListener("resize", () => BIG && drawBig());

  const svg = $("#bs");
  const xOf = e => e.clientX - svg.getBoundingClientRect().left;
  const tAt = x => { const g = BIG.g; return g.v0 + (x - g.m.l) / (g.W - g.m.l - g.m.r) * g.span; };

  svg.addEventListener("mousedown", e => {
    if (!BIG || !BIG.g || e.button !== 0) return;
    const x = xOf(e);
    BIG.drag = {x0: x, x, pan: e.shiftKey, v: BIG.view.slice()};
    hideHover(); e.preventDefault();
  });
  svg.addEventListener("mousemove", e => {
    const B = BIG; if (!B || !B.g) return;
    const x = xOf(e), g = B.g;
    if (B.drag){
      B.drag.x = x;
      if (B.drag.pan){
        const w = B.drag.v[1] - B.drag.v[0];
        const dt = (B.drag.x0 - x) / (g.W - g.m.l - g.m.r) * w;
        setView(B.drag.v[0] + dt, B.drag.v[1] + dt);
      } else {
        const a = Math.max(Math.min(B.drag.x0, x), g.m.l), b = Math.min(Math.max(B.drag.x0, x), g.W - g.m.r);
        const r = $("#bsel"); r.setAttribute("x", a); r.setAttribute("width", Math.max(0, b - a)); r.style.display = "";
      }
      return;
    }
    hover(x);
  });
  svg.addEventListener("mouseleave", () => { if (BIG && !BIG.drag) hideHover(); });
  window.addEventListener("mouseup", () => {
    const B = BIG; if (!B || !B.drag) return;
    const d = B.drag; B.drag = null;
    if (d.pan) return;
    if (Math.abs(d.x - d.x0) > 8) setView(tAt(Math.min(d.x0, d.x)), tAt(Math.max(d.x0, d.x)));
    else { const r = $("#bsel"); if (r) r.style.display = "none"; }
  });
  svg.addEventListener("dblclick", resetView);
  svg.addEventListener("wheel", e => {
    const B = BIG; if (!B || !B.g) return;
    e.preventDefault();
    const g = B.g, t = tAt(xOf(e)), full = B.full[1] - B.full[0];
    const ns = Math.min(full, Math.max(MINSPAN, g.span * (e.deltaY < 0 ? 0.75 : 1 / 0.75)));
    const f = (t - g.v0) / g.span;
    setView(t - f * ns, t - f * ns + ns);
  }, {passive: false});
}

function openBig(c){
  const src = (c.raw || c.series).map(s => s.data);
  const all = src.flat();
  if (!all.length) return;
  ensureBig();
  const t0 = Math.min(...all.map(d => d[0])), t1 = Math.max(...all.map(d => d[0]));
  const base = src;
  BIG = {c, base, data: base, full: [t0, t1], view: [t0, t1], tok: 0, timer: null, drag: null, pid: selected};
  $("#bt").textContent = c.title;
  $("#bl").innerHTML = c.series.length > 1 ? c.series.map(s => `<span style="color:${s.color}">■ ${s.name}</span>`).join(" &nbsp;") : "";
  $("#big").classList.add("on");
  document.body.style.overflow = "hidden";
  drawBig();
}
function closeBig(){
  if (BIG && BIG.timer) clearTimeout(BIG.timer);
  BIG = null; $("#big").classList.remove("on"); document.body.style.overflow = "";
}
function resetView(){
  const B = BIG; if (!B) return;
  B.view = B.full.slice(); B.data = B.base; B.tok++;
  if (B.timer) clearTimeout(B.timer);
  drawBig();
}
function setView(a, b){
  const B = BIG, [f0, f1] = B.full, w = Math.max(MINSPAN, b - a);
  a = Math.max(f0, Math.min(a, f1 - w)); b = a + w;
  B.view = [a, b];
  drawBig(); scheduleFetch();
}
function scheduleFetch(){
  const B = BIG; if (B.timer) clearTimeout(B.timer);
  B.timer = setTimeout(fetchDetail, 250);
}
async function fetchDetail(){
  const B = BIG; if (!B) return;
  const [a, b] = B.view, zoomed = a > B.full[0] + 0.5 || b < B.full[1] - 0.5;
  if (!zoomed){ B.data = B.base; drawBig(); return; }
  const tok = ++B.tok, pad = (b - a) * 0.1;
  try {
    const h = await (await fetch(`/api/history?id=${encodeURIComponent(B.pid)}&from=${Math.floor(a - pad)}&to=${Math.ceil(b + pad)}&n=800`)).json();
    if (BIG !== B || tok !== B.tok || h.error) return;
    B.data = B.c.series.map((s, i) => (h[s.key] && h[s.key].length) ? h[s.key] : B.base[i]);
    drawBig();
  } catch (e) { /* mantém os dados atuais */ }
}

function drawBig(){
  const B = BIG; if (!B) return;
  const wrap = $("#bw"), svg = $("#bs");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  if (!W || !H) return;
  const m = {l:58, r:18, t:14, b:32};
  const [v0, v1] = B.view, span = v1 - v0;
  const zoomed = v0 > B.full[0] + 0.5 || v1 < B.full[1] - 0.5;
  const vals = B.data.flatMap(d => d.filter(p => p[0] >= v0 && p[0] <= v1).map(p => p[1]));
  let lo = 0, hi = vals.length ? Math.max(...vals) : 1;
  if (zoomed && vals.length){
    const mn = Math.min(...vals), pad = (hi - mn) * 0.15 || hi * 0.1 || 1;
    lo = Math.max(0, mn - pad); hi = hi + pad;
  } else hi = hi * 1.1;
  if (!(hi > lo)) hi = lo + 1;
  const yt = niceTicks(lo, hi, 5), y0 = yt[0], y1 = yt[yt.length - 1];
  const X = t => m.l + (t - v0) / span * (W - m.l - m.r);
  const Y = v => H - m.b - (v - y0) / (y1 - y0) * (H - m.b - m.t);
  const {ticks, step} = timeTicks(v0, v1, Math.max(2, Math.floor((W - m.l - m.r) / 110)));
  const fy = v => v.toLocaleString("pt-BR", {maximumFractionDigits: 3});

  let s = `<defs><clipPath id="bclip"><rect x="${m.l}" y="${m.t}" width="${W-m.l-m.r}" height="${H-m.t-m.b}"/></clipPath></defs>`;
  yt.forEach(v => {
    s += `<line x1="${m.l}" x2="${W-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v === 0 ? "#aab" : "#e6e9ee"}"/>` +
         `<text x="${m.l-6}" y="${Y(v)+4}" font-size="11" fill="#667" text-anchor="end">${fy(v)}</text>`;
  });
  ticks.forEach(t => {
    s += `<line x1="${X(t)}" x2="${X(t)}" y1="${m.t}" y2="${H-m.b}" stroke="#f0f2f5"/>` +
         `<text x="${X(t)}" y="${H-10}" font-size="11" fill="#667" text-anchor="middle">${fmtAxis(t, step, span)}</text>`;
  });
  s += `<g clip-path="url(#bclip)">`;
  B.data.forEach((d, i) => {
    const col = B.c.series[i].color;
    s += `<polyline fill="none" stroke="${col}" stroke-width="1.8" stroke-linejoin="round" points="${d.map(p => X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join(" ")}"/>`;
    const inView = d.filter(p => p[0] >= v0 && p[0] <= v1);
    if (inView.length <= 80) inView.forEach(p => { s += `<circle cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="2.6" fill="${col}"/>`; });
  });
  s += `</g><g id="bhov"></g><rect id="bsel" y="${m.t}" height="${H-m.t-m.b}" fill="rgba(26,115,232,.15)" stroke="#1a73e8" style="display:none"/>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = s;
  B.g = {W, H, m, v0, v1, span, X, Y};
  $("#breset").style.visibility = zoomed ? "visible" : "hidden";
  $("#brange").textContent = fmtFull(v0, span) + " → " + fmtFull(v1, span);
}

function hideHover(){
  const g = $("#bhov"); if (g) g.innerHTML = "";
  const t = $("#btip"); if (t) t.style.display = "none";
}
function hover(x){
  const B = BIG, g = B.g;
  if (x < g.m.l || x > g.W - g.m.r){ hideHover(); return; }
  const t = g.v0 + (x - g.m.l) / (g.W - g.m.l - g.m.r) * g.span;
  const near = B.data.map(d => d.reduce((b, p) => (!b || Math.abs(p[0] - t) < Math.abs(b[0] - t)) ? p : b, null));
  const ref = near.find(Boolean);
  if (!ref){ hideHover(); return; }
  const px = g.X(ref[0]), ser = B.c.series;
  let s = `<line x1="${px}" x2="${px}" y1="${g.m.t}" y2="${g.H-g.m.b}" stroke="#667" stroke-dasharray="4 3"/>`;
  let html = `<b>${fmtFull(ref[0], g.span)}</b>`;
  near.forEach((p, i) => {
    if (!p) return;
    s += `<circle cx="${px}" cy="${g.Y(p[1])}" r="5" fill="${ser[i].color}" stroke="#fff" stroke-width="2"/>`;
    html += `<br>${ser.length > 1 ? `<span style="color:${ser[i].color}">■</span> ${ser[i].name}: ` : ""}${num2(p[1])} ${B.c.ulabel}`;
  });
  $("#bhov").innerHTML = s;
  const tip = $("#btip"); tip.innerHTML = html; tip.style.display = "block";
  const right = px > g.W * 0.6;
  tip.style.left = right ? "auto" : (px + 14) + "px";
  tip.style.right = right ? (g.W - px + 14) + "px" : "auto";
  tip.style.top = (g.m.t + 6) + "px";
}

function setHours(h){ hours = h; renderPanel(); }

function select(id, zoom){
  selected = id; render();
  if (id){
    const p = points.find(x => x.id === id);
    if (zoom && p && p.lat != null) map.setView([p.lat,p.lon], Math.max(map.getZoom(), 10));
    renderPanel();
  } else { renderPanel(); }
}

async function load(first){
  try{
    const r = await (await fetch("/api/points")).json();
    points = r.points;
    $("#flags").innerHTML = (r.mock ? '<span class="demo">dados simulados</span> ' : "") +
      (r.error ? `<span class="err" title="${r.error.replace(/"/g,"'")}">erro no Zabbix (mostrando último dado)</span>` : "");
    $("#upd").textContent = "Atualizado " + new Date(r.updated*1000).toLocaleTimeString("pt-BR");
    render(first);
    if (selected) renderPanel();
  }catch(e){ $("#upd").textContent = "Falha ao carregar: " + e.message; }
}
$("#q").oninput = () => render();
load(true); setInterval(() => load(false), 60000);