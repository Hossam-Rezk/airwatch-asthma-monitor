#!/usr/bin/env python3
"""send_test.py - posts fake Pico telemetry to the server, so you can test it without the board.

    python send_test.py                              # defaults: http://127.0.0.1:8000, key change-me
    python send_test.py --url http://192.168.1.100:8000 --key my-secret

Sends: a clean-air report, a moderate report, two dangerous reports in a row (the second one
triggers the simulated patient messages), a report with a failed dust sensor, and a report
with a wrong API key. Look at the server console and notifications.log while it runs.
"""
import argparse
import json
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8000")
ap.add_argument("--key", default="change-me")
ap.add_argument("--station", default="METRO-ST-01")
args = ap.parse_args()

seq = 0


def post(label, payload, key=None):
    global seq
    seq += 1
    payload = dict(payload, station_id=args.station, seq=seq, uptime_s=seq * 5, fw="test", rssi_dbm=-60)
    req = urllib.request.Request(args.url + "/api/telemetry", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-API-Key": key or args.key})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print("{:<34} -> {} {}".format(label, r.status, r.read().decode()))
    except urllib.error.HTTPError as e:
        print("{:<34} -> {} {}".format(label, e.code, e.read().decode().strip()))
    except OSError as e:
        print("{:<34} -> cannot reach server: {}".format(label, e))


ok = {"dht": True, "mq135": True, "dust": True}
post("clean air", {"temperature_c": 26, "humidity_pct": 50, "dust_ugm3": 8, "aqi_proxy": 50, "sensor_ok": ok})
post("moderate", {"temperature_c": 30, "humidity_pct": 55, "dust_ugm3": 40, "aqi_proxy": 140, "sensor_ok": ok})
danger = {"temperature_c": 34, "humidity_pct": 20, "dust_ugm3": 120, "aqi_proxy": 220, "sensor_ok": ok}
post("danger (1st report)", danger)
post("danger (2nd report: messages sent)", danger)
post("dust sensor failed (nulls)", {"temperature_c": 26, "humidity_pct": 50, "dust_ugm3": None, "aqi_proxy": 60,
                                    "sensor_ok": {"dht": True, "mq135": True, "dust": False}})
post("wrong API key", {"temperature_c": 26}, key="wrong")
