# main.py - PRJ-004 edge node firmware, USB edition (MicroPython, Raspberry Pi Pico family)
#
# Runs automatically when the Pico is powered. Over the USB cable it:
#   * prints one JSON line per reading:   {"type": "reading", "temperature_c": 26.1, ...}
#   * answers {"cmd": "hello"} with:       {"type": "hello", "station_id": ..., "fw": ...}
#   * accepts {"cmd": "risk", "risk": "DANGER"} from the PC and shows it on the LED and buzzer
# If the PC stops sending risk levels, the Pico falls back to its own thresholds
# (config.LOCAL_THRESHOLDS) and adds a blue flash to the LED so you can tell.
#
# Files to copy to the Pico: main.py, config.py, sensors.py, alerts.py

import asyncio
import json
import select
import sys
from time import ticks_diff, ticks_ms

import config
from alerts import AlertController
from sensors import SensorHub

RISKS = ("SAFE", "WARNING", "DANGER")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")


def local_risk(r):
    """Risk from the fallback thresholds, used only while the PC is silent."""
    th = config.LOCAL_THRESHOLDS
    dust, aqi = r.get("dust_ugm3"), r.get("aqi_proxy")
    temp, hum = r.get("temperature_c"), r.get("humidity_pct")
    ok = r["sensor_ok"]
    if ok["dust"] and dust is not None and dust >= th["dust_danger"]:
        return "DANGER"
    if ok["mq135"] and aqi is not None and aqi >= th["aqi_danger"]:
        return "DANGER"
    if ((ok["dust"] and dust is not None and dust >= th["dust_warn"])
            or (ok["mq135"] and aqi is not None and aqi >= th["aqi_warn"])
            or (ok["dht"] and temp is not None and temp >= th["temp_warn"])
            or (ok["dht"] and hum is not None and hum >= th["humidity_warn"])):
        return "WARNING"
    return "SAFE"


class Node:
    def __init__(self):
        self.hub = SensorHub()
        self.alerts = AlertController()
        self.seq = 0
        self.t0 = ticks_ms()
        self.last_pc = None              # ticks_ms of the last risk level from the PC
        self.wdt = None
        if config.WATCHDOG_ENABLED:
            from machine import WDT
            self.wdt = WDT(timeout=config.WDT_TIMEOUT_MS)

    def hello(self):
        send({"type": "hello", "station_id": config.STATION_ID, "fw": config.FW_VERSION,
              "interval_s": config.SAMPLE_INTERVAL_S})

    def handle(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        cmd = msg.get("cmd")
        if cmd == "hello":
            self.hello()
        elif cmd == "risk" and msg.get("risk") in RISKS:
            self.last_pc = ticks_ms()
            self.alerts.set("online", msg["risk"])

    def pc_online(self):
        return self.last_pc is not None and ticks_diff(ticks_ms(), self.last_pc) < config.PC_SILENT_AFTER_S * 1000

    async def read_usb(self):
        """Collect characters from the USB serial input into lines, without blocking."""
        poller = select.poll()
        poller.register(sys.stdin, select.POLLIN)
        buf = ""
        while True:
            while poller.poll(0):
                ch = sys.stdin.read(1)
                if ch in ("\n", "\r"):
                    if buf:
                        self.handle(buf)
                    buf = ""
                elif len(buf) < 256:
                    buf += ch
            await asyncio.sleep_ms(20)

    async def sample(self):
        self.hello()
        while True:
            started = ticks_ms()
            try:
                warming = not self.hub.mq.warmed_up
                r = await self.hub.read_all()
                self.seq += 1
                r.update({"type": "reading", "station_id": config.STATION_ID, "seq": self.seq,
                          "uptime_s": ticks_diff(ticks_ms(), self.t0) // 1000, "fw": config.FW_VERSION,
                          "mq135_warming": warming})
                send(r)
                if not self.pc_online():
                    self.alerts.set("offline", local_risk(r))
            except Exception as e:           # keep running whatever one cycle does
                send({"type": "error", "message": "{}: {}".format(type(e).__name__, e)})
            if self.wdt:
                self.wdt.feed()
            wait = config.SAMPLE_INTERVAL_S * 1000 - ticks_diff(ticks_ms(), started)
            await asyncio.sleep_ms(max(100, wait))

    async def main(self):
        asyncio.create_task(self.alerts.run())
        asyncio.create_task(self.read_usb())
        await self.sample()


node = Node()
try:
    asyncio.run(node.main())
finally:
    node.alerts.off()
    asyncio.new_event_loop()
