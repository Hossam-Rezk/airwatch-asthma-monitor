// charts.js - a small, dependency-free live line chart (canvas). Works offline.
//
//   const c = new LineChart(canvas, { color: "#2563eb", unit: "°C", lines: [{ y: 32, color: "#d97706" }] });
//   c.setData([{ t: 1700000000, v: 24.1 }, ...]);   // t in seconds, v may be null (gap)

class LineChart {
  constructor(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.opts = Object.assign({ color: "#2563eb", unit: "", decimals: 1, lines: [] }, opts);
    this.data = [];
    this.hover = null;
    this.tip = document.createElement("div");
    this.tip.className = "chart-tip";
    this.tip.hidden = true;
    canvas.parentElement.appendChild(this.tip);
    new ResizeObserver(() => this.draw()).observe(canvas);
    canvas.addEventListener("mousemove", (e) => this.onMove(e));
    canvas.addEventListener("mouseleave", () => { this.hover = null; this.tip.hidden = true; this.draw(); });
  }

  setData(points) {
    this.data = points;
    this.draw();
  }

  static niceStep(range, count) {
    const raw = range / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }

  layout() {
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    const vals = this.data.map((p) => p.v).filter((v) => v !== null && v !== undefined);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (!vals.length) { lo = 0; hi = 1; }
    // show a threshold line when it is close to the data, so the graph says "how far from the limit"
    for (const l of this.opts.lines) {
      if (l.y >= lo - (hi - lo) * 0.6 - 2 && l.y <= hi + (hi - lo) * 0.6 + 2) { lo = Math.min(lo, l.y); hi = Math.max(hi, l.y); }
    }
    // keep a minimum visible range so sensor noise does not look like big swings
    const minSpan = this.opts.minSpan || 2;
    if (hi - lo < minSpan) { const mid = (hi + lo) / 2; lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
    const step = LineChart.niceStep(hi - lo, 4);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    if (this.opts.min !== undefined) lo = Math.max(lo, this.opts.min);
    const ts = this.data.map((p) => p.t);
    const t0 = ts.length ? ts[0] : 0, t1 = ts.length ? ts[ts.length - 1] : 1;
    const pad = { l: 44, r: 12, t: 10, b: 24 };
    const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
    return {
      w, h, pad, lo, hi, step, t0, t1,
      x: (t) => pad.l + (t1 > t0 ? (t - t0) / (t1 - t0) : 1) * pw,
      y: (v) => pad.t + (1 - (v - lo) / (hi - lo)) * ph,
    };
  }

  draw() {
    const c = this.canvas, ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (!w || !h) return;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!this.data.length) return;
    const L = this.layout();
    this.L = L;
    ctx.font = "11px Segoe UI, system-ui, sans-serif";

    // grid + y labels
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let v = L.lo; v <= L.hi + L.step / 2; v += L.step) {
      const y = Math.round(L.y(v)) + 0.5;
      ctx.strokeStyle = "#eef1f5";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(L.pad.l, y); ctx.lineTo(L.w - L.pad.r, y); ctx.stroke();
      ctx.fillStyle = "#8a94a3";
      ctx.fillText(+v.toFixed(2), L.pad.l - 8, y);
    }

    // x labels (clock time)
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const span = L.t1 - L.t0;
    const ticks = span > 0 ? (L.w < 420 ? 2 : 3) : 1;
    for (let i = 0; i < ticks; i++) {
      const t = ticks === 1 ? L.t1 : L.t0 + (span * (i + 0.5)) / ticks;
      ctx.fillStyle = "#8a94a3";
      ctx.fillText(new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }), L.x(t), L.h - L.pad.b + 7);
    }

    // threshold lines
    for (const l of this.opts.lines) {
      if (l.y < L.lo || l.y > L.hi) continue;
      const y = Math.round(L.y(l.y)) + 0.5;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = l.color;
      ctx.globalAlpha = 0.7;
      ctx.beginPath(); ctx.moveTo(L.pad.l, y); ctx.lineTo(L.w - L.pad.r, y); ctx.stroke();
      ctx.restore();
    }

    // area + line, broken where values are missing
    const segs = [];
    let cur = [];
    for (const p of this.data) {
      if (p.v === null || p.v === undefined) { if (cur.length) segs.push(cur); cur = []; }
      else cur.push(p);
    }
    if (cur.length) segs.push(cur);
    const color = this.opts.color;
    const grad = ctx.createLinearGradient(0, L.pad.t, 0, L.h - L.pad.b);
    grad.addColorStop(0, color + "33");
    grad.addColorStop(1, color + "00");
    for (const s of segs) {
      ctx.beginPath();
      s.forEach((p, i) => (i ? ctx.lineTo(L.x(p.t), L.y(p.v)) : ctx.moveTo(L.x(p.t), L.y(p.v))));
      ctx.lineTo(L.x(s[s.length - 1].t), L.h - L.pad.b);
      ctx.lineTo(L.x(s[0].t), L.h - L.pad.b);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.beginPath();
      s.forEach((p, i) => (i ? ctx.lineTo(L.x(p.t), L.y(p.v)) : ctx.moveTo(L.x(p.t), L.y(p.v))));
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.stroke();
    }

    // latest point, or the hovered one
    const mark = this.hover || [...this.data].reverse().find((p) => p.v !== null && p.v !== undefined);
    if (mark && mark.v !== null) {
      ctx.beginPath();
      ctx.arc(L.x(mark.t), L.y(mark.v), 4, 0, Math.PI * 2);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = color;
      ctx.stroke();
    }
  }

  onMove(e) {
    if (!this.L || !this.data.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    let best = null, bestD = Infinity;
    for (const p of this.data) {
      if (p.v === null || p.v === undefined) continue;
      const d = Math.abs(this.L.x(p.t) - mx);
      if (d < bestD) { bestD = d; best = p; }
    }
    if (!best) return;
    this.hover = best;
    this.tip.hidden = false;
    this.tip.style.left = this.L.x(best.t) + "px";
    this.tip.style.top = this.L.y(best.v) + "px";
    this.tip.textContent = `${best.v.toFixed(this.opts.decimals)} ${this.opts.unit} · ${new Date(best.t * 1000).toLocaleTimeString()}`;
    this.draw();
  }
}
