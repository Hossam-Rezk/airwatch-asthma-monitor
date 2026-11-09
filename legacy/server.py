#!/usr/bin/env python3
"""server.py - PRJ-004 PC AI server (Flask).

Receives telemetry from the Pico 2 W edge node, runs the Random Forest model, returns the
alert state, logs every report to SQLite and sends SIMULATED SMS / push messages to
subscribed asthma patients.

    python train_model.py        # once, creates model.joblib
    python server.py             # listens on 0.0.0.0:8000

Endpoints (all /api/* need the header  X-API-Key: <key>)
    POST /api/telemetry                    node -> server, returns the alert state
    GET  /api/stations                     latest status of each station
    GET  /api/stations/<id>/history        recent readings (?limit=100, max 1000)
    GET  /api/notifications                recent simulated notifications (?limit=50)
    GET  /health                           no key needed

Settings come from environment variables (defaults in brackets):
    PRJ004_API_KEY [change-me]         must match API_KEY in the Pico's config.py
    PRJ004_PORT [8000]                 PRJ004_HOST [0.0.0.0]
    PRJ004_MODEL [model.joblib]        PRJ004_DB [station_log.db]
    PRJ004_PATIENTS [patients.json]    PRJ004_NOTIFY_LOG [notifications.log]
    PRJ004_NOTIFY_COOLDOWN_MIN [15]    minutes between alerts to the same patient and station
    PRJ004_CONFIRMATIONS [2]           consecutive Danger (or Safe) reports needed before messaging
    PRJ004_STALE_S [30]                a station is "offline" if silent for this long
"""
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
from flask import Flask, jsonify, request

BASE = Path(__file__).resolve().parent


def _path(env, default):
    p = Path(os.getenv(env, default))
    return p if p.is_absolute() else BASE / p


API_KEY = os.getenv("PRJ004_API_KEY", "change-me")
MODEL_PATH = _path("PRJ004_MODEL", "model.joblib")
DB_PATH = _path("PRJ004_DB", "station_log.db")
PATIENTS_PATH = _path("PRJ004_PATIENTS", "patients.json")
NOTIFY_LOG = _path("PRJ004_NOTIFY_LOG", "notifications.log")
COOLDOWN_S = float(os.getenv("PRJ004_NOTIFY_COOLDOWN_MIN", "15")) * 60
CONFIRMATIONS = max(1, int(os.getenv("PRJ004_CONFIRMATIONS", "2")))
STALE_S = float(os.getenv("PRJ004_STALE_S", "30"))
MIN_FEATURES = 2          # fewer usable readings than this -> the model is not trusted

RISKS = ["SAFE", "WARNING", "DANGER"]
LED = {"SAFE": "GREEN", "WARNING": "YELLOW", "DANGER": "RED"}
BUZZER = {"SAFE": "OFF", "WARNING": "SLOW_BEEP", "DANGER": "FAST_PULSE"}
STATION_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

log = logging.getLogger("prj004")
_notify_lock = threading.Lock()


# --------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, received_at TEXT NOT NULL,
    station_id TEXT NOT NULL, seq INTEGER, uptime_s INTEGER,
    temperature_c REAL, humidity_pct REAL, dust_mv INTEGER, dust_ugm3 REAL,
    gas_raw INTEGER, aqi_proxy INTEGER, rssi_dbm INTEGER,
    sensor_ok TEXT, fw TEXT, degraded TEXT,
    risk TEXT NOT NULL, confidence REAL, p_safe REAL, p_warning REAL, p_danger REAL
);
CREATE INDEX IF NOT EXISTS idx_readings_station ON readings (station_id, id);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, received_at TEXT NOT NULL,
    patient_id TEXT NOT NULL, patient_name TEXT, channel TEXT NOT NULL,
    station_id TEXT NOT NULL, kind TEXT NOT NULL, risk TEXT, message TEXT, status TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_lookup ON notifications (patient_id, station_id, kind, ts);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        c.commit()


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- model
class Model:
    def __init__(self, path):
        if not Path(path).exists():
            raise RuntimeError("Model file not found: {}. Run  python train_model.py  first.".format(path))
        bundle = joblib.load(path)
        self.pipeline = bundle["pipeline"]
        self.features = bundle["features"]
        self.classes = bundle["classes"]
        self.meta = {k: bundle.get(k) for k in ("trained_at", "sklearn_version", "n_samples", "synthetic", "metrics")}

    def predict(self, feats):
        row = pd.DataFrame([[feats.get(f) for f in self.features]], columns=self.features, dtype=float)
        proba = self.pipeline.predict_proba(row)[0]
        order = list(self.pipeline.classes_)
        probs = {self.classes[int(c)]: float(p) for c, p in zip(order, proba)}
        best = max(probs, key=probs.get)
        return best, probs


# --------------------------------------------------------------------------- telemetry validation
def _num(d, key, lo, hi, invalid, integer=False):
    """Number in [lo, hi] or None. Bad values are dropped and reported, not fatal."""
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and math.isnan(v)):
        invalid.append(key)
        return None
    if not (lo <= v <= hi):
        invalid.append(key)
        return None
    return int(v) if integer else float(v)


def parse_telemetry(d):
    if not isinstance(d, dict):
        raise ValueError("body must be a JSON object")
    sid = d.get("station_id")
    if not isinstance(sid, str) or not STATION_RE.match(sid):
        raise ValueError("station_id missing or invalid")
    invalid = []
    ok = d.get("sensor_ok") if isinstance(d.get("sensor_ok"), dict) else {}
    t = {
        "station_id": sid,
        "seq": _num(d, "seq", 0, 2 ** 31, invalid, True),
        "uptime_s": _num(d, "uptime_s", 0, 2 ** 31, invalid, True),
        "temperature_c": _num(d, "temperature_c", -40, 85, invalid),
        "humidity_pct": _num(d, "humidity_pct", 0, 100, invalid),
        "dust_mv": _num(d, "dust_mv", 0, 6000, invalid, True),
        "dust_ugm3": _num(d, "dust_ugm3", 0, 2000, invalid),
        "gas_raw": _num(d, "gas_raw", 0, 65535, invalid, True),
        "aqi_proxy": _num(d, "aqi_proxy", 0, 500, invalid, True),
        "rssi_dbm": _num(d, "rssi_dbm", -150, 0, invalid, True),
        "sensor_ok": {k: bool(ok.get(k, True)) for k in ("dht", "mq135", "dust")},
        "fw": str(d.get("fw", ""))[:32],
    }
    return t, invalid


def model_features(t, invalid):
    """The four model inputs, with readings from faulty sensors removed (the model imputes them)."""
    ok = t["sensor_ok"]
    feats = {
        "temperature_c": t["temperature_c"] if ok["dht"] else None,
        "humidity_pct": t["humidity_pct"] if ok["dht"] else None,
        "dust_ugm3": t["dust_ugm3"] if ok["dust"] else None,
        "aqi_proxy": t["aqi_proxy"] if ok["mq135"] else None,
    }
    degraded = [name for name, flag in (("dht", ok["dht"]), ("mq135", ok["mq135"]), ("dust", ok["dust"])) if not flag]
    degraded += ["invalid:" + k for k in invalid]
    return feats, degraded


# --------------------------------------------------------------------------- patients and notifications
SAMPLE_PATIENTS = [
    {"id": "p001", "name": "Sample Patient A", "phone": "+20-000-000-0001", "push_token": "demo-token-001",
     "stations": ["METRO-ST-01"], "channels": ["sms", "push"]},
    {"id": "p002", "name": "Sample Patient B", "phone": "+20-000-000-0002", "push_token": "demo-token-002",
     "stations": ["*"], "channels": ["push"]},
]


def load_patients():
    if not PATIENTS_PATH.exists():
        PATIENTS_PATH.write_text(json.dumps(SAMPLE_PATIENTS, indent=2))
        log.info("created sample patient list %s (fake data, edit it)", PATIENTS_PATH)
    try:
        return json.loads(PATIENTS_PATH.read_text())
    except (OSError, ValueError) as e:
        log.error("cannot read %s: %s", PATIENTS_PATH, e)
        return []


def mask_phone(p):
    p = str(p or "")
    return "***" + p[-4:] if len(p) > 4 else "***"


class Notifier:
    """SIMULATED delivery: writes to the console and notifications.log.

    To go live, replace `deliver()` with a real provider (for example Twilio for SMS and
    Firebase Cloud Messaging for push). Keep provider keys in environment variables, never
    in this file or in the Pico firmware.
    """

    def deliver(self, patient, channel, message):
        dest = mask_phone(patient.get("phone")) if channel == "sms" else "push:" + str(patient.get("push_token", ""))[:6] + "..."
        line = {"time": iso(), "channel": channel, "to": patient.get("name"), "dest": dest, "message": message}
        with open(NOTIFY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        log.info("[SIMULATED %s -> %s %s] %s", channel.upper(), patient.get("name"), dest, message)
        return "simulated"


notifier = Notifier()


def alert_text(station, feats):
    dust = feats.get("dust_ugm3")
    extra = " (dust about {:.0f} ug/m3)".format(dust) if dust is not None else ""
    return ("PRJ-004 alert: air quality at {} is a HIGH asthma-trigger risk{}. Consider delaying travel "
            "through this station and follow your asthma action plan.").format(station, extra)


def clear_text(station):
    return "PRJ-004: air quality at {} has returned to normal.".format(station)


def _recent_risks(conn, station_id, n):
    rows = conn.execute("SELECT risk FROM readings WHERE station_id=? ORDER BY id DESC LIMIT ?", (station_id, n)).fetchall()
    return [r["risk"] for r in rows]


def _last_ts(conn, patient_id, station_id, kind):
    r = conn.execute("SELECT MAX(ts) AS t FROM notifications WHERE patient_id=? AND station_id=? AND kind=?",
                     (patient_id, station_id, kind)).fetchone()
    return r["t"]


def process_notifications(station_id, risk, feats):
    """Send Danger alerts (after CONFIRMATIONS in a row, with a cooldown) and one all-clear."""
    if risk not in ("DANGER", "SAFE"):
        return
    with _notify_lock, closing(db()) as conn:
        recent = _recent_risks(conn, station_id, CONFIRMATIONS)
        if len(recent) < CONFIRMATIONS or any(r != risk for r in recent):
            return
        now = time.time()
        for p in load_patients():
            subs = p.get("stations", [])
            if station_id not in subs and "*" not in subs:
                continue
            last_alert = _last_ts(conn, p["id"], station_id, "ALERT")
            last_clear = _last_ts(conn, p["id"], station_id, "CLEAR")
            if risk == "DANGER":
                if last_alert is not None and now - last_alert < COOLDOWN_S:
                    continue                       # alerted this patient about this station recently
                kind, text = "ALERT", alert_text(station_id, feats)
            else:
                if last_alert is None or (last_clear is not None and last_clear >= last_alert):
                    continue
                kind, text = "CLEAR", clear_text(station_id)
            for ch in p.get("channels", ["push"]):
                status = notifier.deliver(p, ch, text)
                conn.execute("INSERT INTO notifications (ts, received_at, patient_id, patient_name, channel, station_id,"
                             " kind, risk, message, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (now, iso(now), p["id"], p.get("name"), ch, station_id, kind, risk, text, status))
        conn.commit()


def _notify_async(station_id, risk, feats):
    def run():
        try:
            process_notifications(station_id, risk, feats)
        except Exception:
            log.exception("notification processing failed")
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- app
def create_app(model=None):
    app = Flask(__name__)
    app.json.sort_keys = False
    init_db()
    app.config["MODEL"] = model if model is not None else Model(MODEL_PATH)
    load_patients()

    @app.before_request
    def check_key():
        if request.path.startswith("/api/"):
            sent = request.headers.get("X-API-Key", "")
            if not secrets.compare_digest(sent.encode(), API_KEY.encode()):
                return jsonify(error="invalid or missing X-API-Key"), 401

    @app.get("/")
    def index():
        return jsonify(service="PRJ-004 asthma prediction server",
                       endpoints=["POST /api/telemetry", "GET /api/stations", "GET /api/stations/<id>/history",
                                  "GET /api/notifications", "GET /health"])

    @app.get("/health")
    def health():
        m = app.config["MODEL"]
        try:
            with closing(db()) as c:
                c.execute("SELECT 1").fetchone()
            db_ok = True
        except sqlite3.Error:
            db_ok = False
        return jsonify(status="ok" if db_ok else "degraded", model=m.meta, database=db_ok, time=iso())

    @app.post("/api/telemetry")
    def telemetry():
        body = request.get_json(silent=True)
        try:
            t, invalid = parse_telemetry(body)
        except ValueError as e:
            return jsonify(error=str(e)), 422
        feats, degraded = model_features(t, invalid)
        usable = sum(v is not None for v in feats.values())
        m = app.config["MODEL"]
        if usable < MIN_FEATURES:
            # the station cannot vouch for the air: say WARNING (not SAFE) and flag the outage
            risk, probs = "WARNING", {"SAFE": 0.0, "WARNING": 1.0, "DANGER": 0.0}
            confidence = 0.0
            degraded.append("insufficient_data")
        else:
            risk, probs = m.predict(feats)
            confidence = probs[risk]
        now = time.time()
        with closing(db()) as c:
            c.execute(
                "INSERT INTO readings (ts, received_at, station_id, seq, uptime_s, temperature_c, humidity_pct, dust_mv,"
                " dust_ugm3, gas_raw, aqi_proxy, rssi_dbm, sensor_ok, fw, degraded, risk, confidence, p_safe, p_warning,"
                " p_danger) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, iso(now), t["station_id"], t["seq"], t["uptime_s"], t["temperature_c"], t["humidity_pct"],
                 t["dust_mv"], t["dust_ugm3"], t["gas_raw"], t["aqi_proxy"], t["rssi_dbm"], json.dumps(t["sensor_ok"]),
                 t["fw"], json.dumps(degraded), risk, round(confidence, 4), probs["SAFE"], probs["WARNING"], probs["DANGER"]))
            c.commit()
        log.info("%s seq=%s T=%s RH=%s dust=%s aqi=%s -> %s (%.2f) degraded=%s", t["station_id"], t["seq"],
                 t["temperature_c"], t["humidity_pct"], t["dust_ugm3"], t["aqi_proxy"], risk, confidence, degraded)
        if usable >= MIN_FEATURES:
            _notify_async(t["station_id"], risk, feats)
        return jsonify(risk=risk, risk_level=RISKS.index(risk), confidence=round(confidence, 3), led=LED[risk],
                       buzzer=BUZZER[risk], server_time=iso(now), degraded=degraded)

    def _row(r):
        d = dict(r)
        for k in ("sensor_ok", "degraded"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    @app.get("/api/stations")
    def stations():
        with closing(db()) as c:
            rows = c.execute("SELECT r.* FROM readings r JOIN (SELECT station_id, MAX(id) AS mid FROM readings "
                             "GROUP BY station_id) m ON r.id = m.mid ORDER BY r.station_id").fetchall()
        now = time.time()
        out = []
        for r in rows:
            d = _row(r)
            d["age_s"] = round(now - d["ts"], 1)
            d["online"] = d["age_s"] <= STALE_S
            out.append(d)
        return jsonify(stations=out)

    @app.get("/api/stations/<station_id>/history")
    def history(station_id):
        limit = max(1, min(request.args.get("limit", default=100, type=int), 1000))
        with closing(db()) as c:
            rows = c.execute("SELECT * FROM readings WHERE station_id=? ORDER BY id DESC LIMIT ?",
                             (station_id, limit)).fetchall()
        return jsonify(station_id=station_id, count=len(rows), readings=[_row(r) for r in rows])

    @app.get("/api/notifications")
    def notifications():
        limit = max(1, min(request.args.get("limit", default=50, type=int), 500))
        with closing(db()) as c:
            rows = c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return jsonify(count=len(rows), notifications=[dict(r) for r in rows])

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if API_KEY == "change-me":
        log.warning("PRJ004_API_KEY is the default 'change-me'; set a real key on both sides")
    app = create_app()
    host, port = os.getenv("PRJ004_HOST", "0.0.0.0"), int(os.getenv("PRJ004_PORT", "8000"))
    log.info("listening on http://%s:%s  (model %s)", host, port, MODEL_PATH.name)
    try:
        from waitress import serve          # pip install waitress: a proper production server
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        log.info("waitress not installed: using Flask's built-in server (fine for a prototype)")
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
