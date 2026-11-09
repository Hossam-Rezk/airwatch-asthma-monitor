#!/usr/bin/env python3
"""app.py - AI asthma-risk dashboard: web UI, USB link to the Pico and the analysis engine.

    python app.py            # opens http://127.0.0.1:5000 in your browser
    python app.py --demo     # also offers a simulated Pico, for trying it without hardware

Endpoints used by the dashboard (local only by default, see settings.py):
    GET  /api/ports             serial ports, Picos first
    POST /api/connect           {"port": "COM5"} or {} to pick the first Pico found
    POST /api/disconnect
    GET  /api/state?since=N     connection status, latest analysis and readings newer than N
    GET  /api/meta              units and colour bands for the graphs
    GET  /api/readings.csv      everything logged so far
"""
import argparse
import csv
import logging
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import analysis
import settings
from pico_link import PicoLink, find_ports

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
LOG_PATH = BASE / "data" / "readings_log.csv"
LOG_FIELDS = ["time", "station_id", "seq", "temperature_c", "humidity_pct", "dust_ugm3", "aqi_proxy",
              "dht_ok", "mq135_ok", "dust_ok", "risk"]
RANGES = {"temperature_c": (-40, 85), "humidity_pct": (0, 100), "dust_ugm3": (0, 2000), "aqi_proxy": (0, 500)}

log = logging.getLogger("app")


class Store:
    """Recent readings for the graphs, and the latest analysis."""

    def __init__(self, analyzer):
        self.analyzer = analyzer
        self.lock = threading.Lock()
        self.points = deque(maxlen=settings.HISTORY_POINTS)
        self.counter = 0
        self.latest = None

    def add(self, msg):
        """Called by the USB thread for every reading. Returns the risk level for the Pico."""
        ok = msg.get("sensor_ok") if isinstance(msg.get("sensor_ok"), dict) else {}
        reading = {f: analysis.clean_number(msg.get(f), *RANGES[f]) for f in analysis.FEATURES}
        reading["sensor_ok"] = {k: bool(ok.get(k, True)) for k in ("dht", "mq135", "dust")}
        reading["mq135_warming"] = bool(msg.get("mq135_warming"))
        result = self.analyzer.analyze(reading)
        now = time.time()
        with self.lock:
            self.counter += 1
            point = {"id": self.counter, "ts": now, "station_id": str(msg.get("station_id", ""))[:64],
                     "seq": msg.get("seq"), **reading, "risk": result["risk"]}
            self.points.append(point)
            self.latest = dict(result, ts=now, reading=point)
        if settings.LOG_READINGS:
            self._log(point)
        return result["risk"]

    def since(self, after):
        with self.lock:
            return [p for p in self.points if p["id"] > after], self.latest, self.counter

    @staticmethod
    def _log(p):
        try:
            new = not LOG_PATH.exists()
            with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(LOG_FIELDS)
                ok = p["sensor_ok"]
                w.writerow([datetime.fromtimestamp(p["ts"]).isoformat(timespec="seconds"), p["station_id"], p["seq"],
                            p["temperature_c"], p["humidity_pct"], p["dust_ugm3"], p["aqi_proxy"],
                            ok["dht"], ok["mq135"], ok["dust"], p["risk"]])
        except OSError as e:
            log.warning("could not write %s: %s", LOG_PATH, e)


def create_app(demo=False):
    app = Flask(__name__, static_folder=None)
    analyzer = analysis.make_analyzer(settings.USE_RULE_BASED_FALLBACK, BASE / "model" / "model.joblib")
    store = Store(analyzer)
    link = PicoLink(store.add, allow_demo=demo)
    app.config.update(STORE=store, LINK=link)

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        resp = send_from_directory(STATIC, name)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/ports")
    def ports():
        return jsonify(ports=find_ports(include_demo=demo))

    @app.post("/api/connect")
    def connect():
        port = (request.get_json(silent=True) or {}).get("port") or None
        return jsonify(status=link.connect(port))

    @app.post("/api/disconnect")
    def disconnect():
        return jsonify(status=link.disconnect())

    @app.get("/api/state")
    def state():
        after = request.args.get("since", default=0, type=int)
        points, latest, counter = store.since(after)
        if after > counter:                  # the server restarted: send everything
            points, latest, counter = store.since(0)
        return jsonify(status=link.status(), analysis=latest, readings=points, last_id=counter,
                       server_time=time.time())

    @app.get("/api/meta")
    def meta():
        return jsonify(features=analysis.FEATURES, labels=analysis.LABELS, units=analysis.UNITS,
                       high=analysis.HIGH, low={k: v[0] for k, v in analysis.LOW.items()},
                       history_points=settings.HISTORY_POINTS)

    @app.get("/api/readings.csv")
    def readings_csv():
        if not LOG_PATH.exists():
            return "No readings logged yet.", 404
        return send_file(LOG_PATH, mimetype="text/csv", as_attachment=True, download_name="readings_log.csv")

    return app


def main():
    ap = argparse.ArgumentParser(description="AI asthma-risk dashboard")
    ap.add_argument("--demo", action="store_true", help="offer a simulated Pico in the port list")
    ap.add_argument("--port", type=int, default=settings.PORT, help="web server port")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("waitress").setLevel(logging.WARNING)

    app = create_app(demo=args.demo)
    url = "http://{}:{}".format("127.0.0.1" if settings.HOST in ("0.0.0.0", "") else settings.HOST, args.port)
    print("\n  Dashboard running at  {}\n  Press Ctrl+C to stop.\n".format(url), flush=True)
    if settings.OPEN_BROWSER and not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        from waitress import serve
        serve(app, host=settings.HOST, port=args.port, threads=8)
    except ImportError:
        app.run(host=settings.HOST, port=args.port, threaded=True)
    finally:
        app.config["LINK"].disconnect()


if __name__ == "__main__":
    main()
