"""pico_link.py - the USB serial connection to the Raspberry Pi Pico.

PicoLink runs one background thread that reads JSON lines from the Pico's USB serial port,
hands each reading to a callback, and sends the risk level back so the Pico's LED and buzzer
match the dashboard. If the cable is pulled it keeps trying to reconnect until the user
presses Disconnect.

DEMO_PORT is a built-in simulated Pico (started with  python app.py --demo) for trying the
dashboard without hardware.
"""
import json
import logging
import math
import random
import threading
import time

import serial
from serial.tools import list_ports

import settings

DEMO_PORT = "DEMO"
log = logging.getLogger("pico")


def find_ports(include_demo=False):
    """Serial ports, Picos first. Each: {device, description, is_pico}."""
    ports = []
    for p in list_ports.comports():
        is_pico = p.vid == settings.PICO_USB_VID
        desc = p.description or p.device
        if is_pico and "pico" not in desc.lower():
            desc = "Raspberry Pi Pico ({})".format(p.device)
        ports.append({"device": p.device, "description": desc, "is_pico": is_pico})
    ports.sort(key=lambda p: (not p["is_pico"], p["device"]))
    if include_demo:
        ports.append({"device": DEMO_PORT, "description": "Demo Pico (simulated readings)", "is_pico": False})
    return ports


class DemoSerial:
    """Behaves like a serial port connected to a Pico running main.py."""

    def __init__(self, interval=3.0):
        self.interval = interval
        self.seq = 0
        self.t0 = time.time()
        self.next_at = time.time() + 0.5
        self.out = [self._hello()]
        self.is_open = True

    def _hello(self):
        return {"type": "hello", "station_id": "DEMO-STATION", "fw": "demo", "interval_s": self.interval}

    def _reading(self):
        self.seq += 1
        t = time.time() - self.t0
        # a 4-minute cycle: clean air -> dusty -> polluted -> recovering
        phase = (t % 240) / 240
        bump = max(0.0, math.sin(phase * 2 * math.pi))
        gas_bump = max(0.0, math.sin((phase - 0.15) * 2 * math.pi))
        n = random.gauss
        return {"type": "reading", "station_id": "DEMO-STATION", "seq": self.seq, "uptime_s": int(t), "fw": "demo",
                "temperature_c": round(25 + 6 * bump + n(0, 0.3), 1),
                "humidity_pct": round(52 - 18 * bump + n(0, 1.0), 1),
                "dust_mv": int(700 + 400 * bump),
                "dust_ugm3": round(max(0.0, 8 + 85 * bump ** 2 + n(0, 2.5)), 1),
                "gas_raw": int(9000 + 15000 * gas_bump),
                "aqi_proxy": int(max(0, 35 + 210 * gas_bump ** 2 + n(0, 6))),
                "sensor_ok": {"dht": True, "mq135": True, "dust": True}, "mq135_warming": False}

    def readline(self):
        while not self.out:
            wait = self.next_at - time.time()
            if wait > 0:
                time.sleep(min(wait, 0.5))
                if not self.is_open:
                    return b""
                continue
            self.next_at += self.interval
            self.out.append(self._reading())
        return (json.dumps(self.out.pop(0)) + "\n").encode()

    def write(self, data):
        try:
            if json.loads(data.decode()).get("cmd") == "hello":
                self.out.append(self._hello())
        except ValueError:
            pass

    def close(self):
        self.is_open = False


class PicoLink:
    def __init__(self, on_reading, allow_demo=False):
        self.on_reading = on_reading
        self.allow_demo = allow_demo
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._ser = None
        self.state = "disconnected"          # disconnected | connecting | connected | reconnecting
        self.port = None
        self.message = "Not connected. Plug in the Pico and press Connect."
        self.device = None                   # from the Pico's hello message
        self.last_data = None
        self._connected_at = None

    # ------------------------------------------------------------------ public
    def status(self):
        with self._lock:
            s = {"state": self.state, "port": self.port, "message": self.message,
                 "device": self.device, "last_data_age_s": None}
            if self.last_data:
                s["last_data_age_s"] = round(time.time() - self.last_data, 1)
            if self.state == "connected":
                since = self.last_data or self._connected_at
                if since and time.time() - since > settings.NO_DATA_WARNING_S:
                    s["message"] = ("Connected to {}, but no readings are arriving. Make sure main.py, config.py, "
                                    "sensors.py and alerts.py are on the Pico, then unplug and replug it.").format(self.port)
            return s

    def connect(self, port=None):
        self.disconnect()
        if not port:
            picos = [p for p in find_ports() if p["is_pico"]]
            if not picos:
                with self._lock:
                    self.state, self.message = "disconnected", (
                        "No Raspberry Pi Pico found. Check the USB cable (it must be a data cable, not "
                        "charge-only) and that the Pico has MicroPython installed.")
                return self.status()
            port = picos[0]["device"]
        if port == DEMO_PORT and not self.allow_demo:
            port = None
            with self._lock:
                self.state, self.message = "disconnected", "Unknown port."
            return self.status()
        with self._lock:
            self.state, self.port, self.device, self.last_data = "connecting", port, None, None
            self.message = "Connecting to {} ...".format(port)
            self._connected_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(port,), daemon=True)
        self._thread.start()
        # wait briefly so the first status the user sees is meaningful
        for _ in range(20):
            if self.state != "connecting":
                break
            time.sleep(0.05)
        return self.status()

    def disconnect(self):
        self._stop.set()
        ser = self._ser
        if ser is not None:
            try:
                ser.close()
            except Exception:                 # noqa: BLE001
                pass
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._thread = None
        with self._lock:
            was = self.port
            self.state, self.port, self.device, self.last_data = "disconnected", None, None, None
            self.message = "Disconnected from {}.".format(was) if was else "Not connected."
        return self.status()

    def send_risk(self, risk):
        if risk in ("SAFE", "WARNING", "DANGER"):
            self._write({"cmd": "risk", "risk": risk})

    # ------------------------------------------------------------------ internals
    def _write(self, msg):
        ser = self._ser
        if ser is None:
            return
        try:
            ser.write((json.dumps(msg) + "\n").encode())
        except Exception as e:                # noqa: BLE001 - the reader thread handles the outage
            log.debug("write failed: %s", e)

    def _open(self, port):
        if port == DEMO_PORT:
            return DemoSerial()
        ser = serial.Serial(port, settings.BAUD_RATE, timeout=1, write_timeout=1)
        ser.reset_input_buffer()
        return ser

    def _run(self, port):
        first = True
        while not self._stop.is_set():
            try:
                self._ser = self._open(port)
            except serial.SerialException as e:
                text = str(e)
                if first:
                    with self._lock:
                        self.state = "disconnected"
                        self.port = None
                        if "denied" in text.lower() or "busy" in text.lower() or "PermissionError" in text:
                            self.message = ("{} is in use by another program (often Thonny). Close it and press "
                                            "Connect again.").format(port)
                        else:
                            self.message = "Could not open {}: {}".format(port, text)
                    log.warning("open %s failed: %s", port, text)
                    return
                self._stop.wait(settings.RECONNECT_EVERY_S)
                continue
            first = False
            with self._lock:
                self.state = "connected"
                self.message = "Connected to {}.".format(port)
                self._connected_at = time.time()
            log.info("connected to %s", port)
            self._write({"cmd": "hello"})
            try:
                self._read_loop()
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                # TypeError/AttributeError: pyserial internals after close() from another thread
                if self._stop.is_set():
                    break
                log.warning("lost %s: %s", port, e)
                with self._lock:
                    self.state = "reconnecting"
                    self.message = "Lost the connection to {}. Reconnecting automatically ...".format(port)
            finally:
                try:
                    self._ser.close()
                except Exception:             # noqa: BLE001
                    pass
                self._ser = None
            self._stop.wait(settings.RECONNECT_EVERY_S)

    def _read_loop(self):
        while not self._stop.is_set():
            raw = self._ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue                       # MicroPython banner, debug prints, REPL echo
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "hello":
                with self._lock:
                    self.device = {"station_id": msg.get("station_id"), "fw": msg.get("fw")}
                    self.message = "Connected to {} ({}).".format(msg.get("station_id", "Pico"), self.port)
            elif kind == "reading":
                with self._lock:
                    self.last_data = time.time()
                    if self.device is None:
                        self.device = {"station_id": msg.get("station_id"), "fw": msg.get("fw")}
                try:
                    risk = self.on_reading(msg)
                except Exception:              # noqa: BLE001 - a bad reading must not drop the link
                    log.exception("could not process a reading")
                    continue
                self.send_risk(risk)
            elif kind == "error":
                log.warning("Pico reports: %s", msg.get("message"))
