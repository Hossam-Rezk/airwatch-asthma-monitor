// app.js - dashboard logic: navigation, Pico connection, live graphs and the AI panel.

const POLL_MS = 1500;
const CHART_POINTS = 120;                 // about 6 minutes at one reading every 3 s
const TABLE_ROWS = 100;

// One graph per sensor value. `band` turns a value into a short description and a colour.
const SENSORS = [
  {
    key: "temperature_c", title: "Temperature", sub: "DHT-22 sensor", color: "#ea580c", decimals: 1, minSpan: 6,
    band: (v) => v >= 38 ? ["Very hot", "DANGER"] : v >= 32 ? ["Hot", "WARNING"] : v <= 10 ? ["Cold", "WARNING"] : ["Comfortable", "SAFE"],
  },
  {
    key: "humidity_pct", title: "Humidity", sub: "DHT-22 sensor · relative humidity", color: "#0284c7", decimals: 1, minSpan: 20,
    band: (v) => v >= 90 ? ["Very humid", "DANGER"] : v >= 80 ? ["Humid", "WARNING"] : v <= 25 ? ["Very dry", "WARNING"] : ["Normal", "SAFE"],
  },
  {
    key: "aqi_proxy", title: "Air Quality", sub: "MQ-135 gas sensor · AQI scale 0–500", color: "#7c3aed", decimals: 0, minSpan: 100,
    band: (v) => v > 300 ? ["Hazardous", "DANGER"] : v > 200 ? ["Very unhealthy", "DANGER"] : v > 150 ? ["Unhealthy", "WARNING"]
      : v > 100 ? ["Unhealthy for sensitive", "WARNING"] : v > 50 ? ["Moderate", "SAFE"] : ["Good", "SAFE"],
  },
  {
    key: "dust_ugm3", title: "Dust Concentration", sub: "Sharp GP2Y1010 · PM2.5 equivalent", color: "#a16207", decimals: 1, minSpan: 40,
    band: (v) => v >= 150.5 ? ["Very unhealthy", "DANGER"] : v >= 55.5 ? ["Unhealthy", "DANGER"] : v >= 35.5 ? ["Unhealthy for sensitive", "WARNING"]
      : v > 12 ? ["Moderate", "SAFE"] : ["Good", "SAFE"],
  },
];

const $ = (id) => document.getElementById(id);
const state = { lastId: 0, points: [], meta: null, status: null, charts: {}, busy: false };

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}
const fmt = (v, d) => (v === null || v === undefined ? "–" : Number(v).toFixed(d));
const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString();
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---------------------------------------------------------------- navigation
function route() {
  const page = (location.hash || "#dashboard").slice(1);
  const known = ["dashboard", "readings", "help"].includes(page) ? page : "dashboard";
  document.querySelectorAll(".page").forEach((s) => (s.hidden = s.id !== `page-${known}`));
  document.querySelectorAll(".nav-links a").forEach((a) => a.classList.toggle("active", a.dataset.page === known));
  if (known === "dashboard") Object.values(state.charts).forEach((c) => c.chart.draw());
}

// ---------------------------------------------------------------- graphs
function buildCharts() {
  const grid = $("chartGrid");
  for (const s of SENSORS) {
    const card = el("div", "card chart-card");
    const head = el("div", "chart-head");
    const left = el("div");
    left.append(el("div", "chart-title", s.title), el("div", "chart-sub", s.sub));
    const right = el("div", "chart-value");
    const num = el("span", "num", "–");
    const unit = el("span", "unit", state.meta.units[s.key]);
    const chip = el("span", "chip", "No data");
    const line = el("div");
    line.append(num, unit);
    right.append(line, chip);
    head.append(left, right);
    const box = el("div", "chart-box");
    const canvas = el("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${s.title} over time`);
    const empty = el("div", "chart-empty", "Waiting for data. Connect the Pico to start.");
    box.append(canvas, empty);
    card.append(head, box);
    grid.append(card);

    const [warn, danger] = state.meta.high[s.key];
    const lines = [{ y: warn, color: "#d97706" }];
    if (danger !== null) lines.push({ y: danger, color: "#dc2626" });
    const chart = new LineChart(canvas, { color: s.color, unit: state.meta.units[s.key], decimals: s.decimals, lines, minSpan: s.minSpan,
      min: s.key === "temperature_c" ? undefined : 0 });
    state.charts[s.key] = { chart, num, chip, empty, sensor: s };
  }
}

function updateCharts() {
  const pts = state.points.slice(-CHART_POINTS);
  const last = pts[pts.length - 1];
  for (const { chart, num, chip, empty, sensor } of Object.values(state.charts)) {
    chart.setData(pts.map((p) => ({ t: p.ts, v: p[sensor.key] })));
    const v = last ? last[sensor.key] : null;
    empty.hidden = pts.length > 0;
    if (!last) { num.textContent = "–"; chip.textContent = "No data"; chip.className = "chip"; continue; }
    if (v === null || v === undefined) {
      num.textContent = "–";
      chip.textContent = "Sensor unavailable";
      chip.className = "chip";
    } else {
      const [label, level] = sensor.band(v);
      num.textContent = fmt(v, sensor.decimals);
      chip.textContent = label;
      chip.className = `chip ${level}`;
    }
  }
}

// ---------------------------------------------------------------- AI panel
const RISK_TEXT = { SAFE: "SAFE", WARNING: "WARNING", DANGER: "DANGER", UNKNOWN: "NO DATA" };

function updateAnalysis(a) {
  const card = $("aiCard");
  if (!a) return;
  card.dataset.risk = a.risk;
  $("riskLevel").textContent = RISK_TEXT[a.risk] || a.risk;
  $("aiHeadline").textContent = a.headline;
  $("aiUpdated").textContent = `Updated ${timeOf(a.ts)}`;
  const intro = $("aiIntro");
  intro.textContent = a.risk === "SAFE" ? "All four readings are within comfortable limits for people with asthma."
    : a.risk === "UNKNOWN" ? "The analysis needs at least two working sensors." : "Main factors right now:";

  const fl = $("factorList");
  fl.replaceChildren(...a.factors.map((f) => el("li", f.level, f.text)));
  const box = $("adviceBox");
  box.hidden = !a.advice.length;
  $("adviceList").replaceChildren(...a.advice.map((t) => el("li", "", t)));
  $("issueList").replaceChildren(...a.sensor_issues.map((t) => el("li", "", t)));
}

function clearAnalysis() {
  $("aiCard").dataset.risk = "NONE";
  $("riskLevel").textContent = "–";
  $("aiHeadline").textContent = "Waiting for sensor data";
  $("aiIntro").textContent = "Connect the Pico to start the live analysis of temperature, humidity, air quality and dust.";
  $("aiUpdated").textContent = "";
  $("factorList").replaceChildren();
  $("adviceBox").hidden = true;
  $("issueList").replaceChildren();
}

// ---------------------------------------------------------------- readings table
function updateTable() {
  const rows = state.points.slice(-TABLE_ROWS).reverse();
  const body = $("readingsBody");
  if (!rows.length) return;
  body.replaceChildren(...rows.map((p) => {
    const tr = el("tr");
    const risk = el("span", `chip ${p.risk}`, RISK_TEXT[p.risk] || p.risk);
    const cells = [timeOf(p.ts), `${fmt(p.temperature_c, 1)} °C`, `${fmt(p.humidity_pct, 1)} %`,
      `${fmt(p.aqi_proxy, 0)} AQI`, `${fmt(p.dust_ugm3, 1)} µg/m³`];
    cells.forEach((c) => tr.append(el("td", "", c)));
    const td = el("td");
    td.append(risk);
    tr.append(td);
    return tr;
  }));
}

// ---------------------------------------------------------------- connection
const STATE_TEXT = { connected: "Connected", connecting: "Connecting", reconnecting: "Reconnecting", disconnected: "Disconnected", stale: "No data" };

function updateStatus(s) {
  state.status = s;
  const stale = s.state === "connected" && /no readings/i.test(s.message);
  const shown = stale ? "stale" : s.state;
  const pill = $("connPill");
  pill.textContent = STATE_TEXT[shown];
  pill.className = `pill ${shown}`;
  $("connMessage").textContent = s.message;
  const nav = $("navStatus");
  nav.dataset.state = shown;
  nav.querySelector(".label").textContent = s.state === "disconnected" ? "Pico not connected"
    : `${STATE_TEXT[shown]}${s.port ? " · " + s.port : ""}`;
  $("factStation").textContent = s.device && s.device.station_id ? s.device.station_id : "–";
  $("factLast").textContent = s.last_data_age_s === null ? "–" : s.last_data_age_s < 2 ? "just now" : `${Math.round(s.last_data_age_s)} s ago`;

  const btn = $("connectBtn");
  const active = s.state !== "disconnected";
  if (!state.busy) {
    btn.textContent = active ? "Disconnect" : "Connect";
    btn.classList.toggle("is-disconnect", active);
  }
  const sel = $("portSelect");
  sel.disabled = active;
  if (active && s.port && sel.value !== s.port) {
    if (![...sel.options].some((o) => o.value === s.port)) sel.append(new Option(s.port, s.port));
    sel.value = s.port;
  }
  $("refreshPorts").disabled = active;
}

async function loadPorts() {
  const sel = $("portSelect");
  const previous = sel.value;
  try {
    const { ports } = await api("/api/ports");
    sel.replaceChildren();
    if (!ports.length) {
      sel.append(new Option("No USB devices found. Plug in the Pico.", ""));
      return;
    }
    const picos = ports.filter((p) => p.is_pico);
    if (!picos.length) sel.append(new Option("Auto-detect Pico", ""));
    for (const p of ports) sel.append(new Option(p.is_pico ? `${p.description} ✓` : `${p.description}`, p.device));
    const keep = ports.find((p) => p.device === previous);
    sel.value = keep ? previous : picos.length ? picos[0].device : sel.options[0].value;
  } catch (e) {
    sel.replaceChildren(new Option("Cannot reach the dashboard server", ""));
  }
}

async function toggleConnection() {
  const btn = $("connectBtn");
  const active = state.status && state.status.state !== "disconnected";
  state.busy = true;
  btn.disabled = true;
  btn.textContent = active ? "Disconnecting…" : "Connecting…";
  try {
    const { status } = active
      ? await api("/api/disconnect", { method: "POST" })
      : await api("/api/connect", { method: "POST", body: JSON.stringify({ port: $("portSelect").value || null }) });
    state.busy = false;
    updateStatus(status);
    if (status.state === "disconnected") loadPorts();
  } catch (e) {
    state.busy = false;
    $("connMessage").textContent = "The dashboard server is not responding. Is app.py still running?";
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- polling
async function poll() {
  try {
    const data = await api(`/api/state?since=${state.lastId}`);
    if (data.last_id < state.lastId) state.points = [];       // server restarted
    if (data.readings.length) {
      state.points.push(...data.readings);
      const max = (state.meta && state.meta.history_points) || 720;
      if (state.points.length > max) state.points.splice(0, state.points.length - max);
      updateCharts();
      updateTable();
    }
    state.lastId = data.last_id;
    updateStatus(data.status);
    if (data.analysis) updateAnalysis(data.analysis); else clearAnalysis();
  } catch (e) {
    const nav = $("navStatus");
    nav.dataset.state = "disconnected";
    nav.querySelector(".label").textContent = "Server offline";
    $("connMessage").textContent = "Lost contact with the dashboard server. Start it again with the start script.";
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

// ---------------------------------------------------------------- start
async function init() {
  window.addEventListener("hashchange", route);
  route();
  $("connectBtn").addEventListener("click", toggleConnection);
  $("refreshPorts").addEventListener("click", loadPorts);
  try {
    state.meta = await api("/api/meta");
  } catch (e) {
    state.meta = { units: { temperature_c: "°C", humidity_pct: "%", aqi_proxy: "AQI", dust_ugm3: "µg/m³" },
      high: { temperature_c: [32, 38], humidity_pct: [80, 90], aqi_proxy: [101, 201], dust_ugm3: [35.5, 55.5] }, history_points: 720 };
  }
  buildCharts();
  await loadPorts();
  poll();
}
init();
