# sensors.py - sensor drivers for the PRJ-004 edge node (MicroPython, Pico 2 W)
#
#   AdcInput          oversampled ADC channel with divider-ratio scaling
#   DHT22Sensor       temperature / humidity (rate-limited, tolerant of failed reads)
#   MQ135Sensor       gas level with warm-up tracking, smoothing and a 0-500 index
#   SharpDustSensor   GP2Y1010AU0F with microsecond-timed LED pulsing and ADC sampling
#   SensorHub         reads everything and returns one telemetry-ready dictionary

import asyncio
import dht
from machine import ADC, Pin, disable_irq, enable_irq
from time import ticks_add, ticks_diff, ticks_ms, ticks_us, sleep_us

import config


def _log(msg):
    if config.DEBUG:
        print("[sensors]", msg)


def trimmed_mean(values, frac):
    """Mean after discarding the lowest and highest `frac` of the values."""
    n = len(values)
    if n == 0:
        return 0.0
    ordered = sorted(values)
    k = int(n * frac)
    if n - 2 * k < 1:
        k = 0
    kept = ordered[k:n - k]
    return sum(kept) / len(kept)


# --------------------------------------------------------------------------- ADC
class AdcInput:
    """One ADC pin behind a resistor divider.

    raw (0..65535) -> volts at the pin -> volts at the sensor (divided by the ratio).
    """

    def __init__(self, gpio, ratio=None, vref=None):
        self._adc = ADC(Pin(gpio))
        self.ratio = config.ADC_DIVIDER_RATIO if ratio is None else ratio
        self.vref = config.ADC_VREF if vref is None else vref
        if not 0.0 < self.ratio <= 1.0:
            raise ValueError("ADC_DIVIDER_RATIO must be in (0, 1]")

    def read_raw_once(self):
        return self._adc.read_u16()

    def read_raw_avg(self, n=None):
        n = config.ADC_OVERSAMPLE if n is None else n
        buf = [0] * n
        gap = config.ADC_OVERSAMPLE_GAP_US
        for i in range(n):
            buf[i] = self._adc.read_u16()
            sleep_us(gap)
        return trimmed_mean(buf, config.ADC_TRIM_FRACTION)

    def raw_to_pin_volts(self, raw):
        return raw * self.vref / 65535.0

    def raw_to_sensor_volts(self, raw):
        return self.raw_to_pin_volts(raw) / self.ratio


# --------------------------------------------------------------------------- DHT-22
class DHT22Sensor:
    """DHT-22 on one GPIO. Call `await poll()` once per cycle."""

    def __init__(self, gpio=None):
        self._dev = dht.DHT22(Pin(config.PIN_DHT if gpio is None else gpio))
        self._ready_at = ticks_add(ticks_ms(), config.DHT_BOOT_DELAY_MS)
        self._last_poll = None
        self._fails = 0
        self.temperature = None
        self.humidity = None

    @property
    def ok(self):
        return self.temperature is not None and self._fails < config.DHT_FAIL_LIMIT

    async def poll(self):
        # honour the boot delay and the minimum interval between reads
        wait = ticks_diff(self._ready_at, ticks_ms())
        if wait > 0:
            await asyncio.sleep_ms(wait)
        if self._last_poll is not None:
            gap = int(config.DHT_MIN_INTERVAL_S * 1000) - ticks_diff(ticks_ms(), self._last_poll)
            if gap > 0:
                await asyncio.sleep_ms(gap)
        self._last_poll = ticks_ms()
        try:
            self._dev.measure()
            t = self._dev.temperature()
            h = self._dev.humidity()
            if not (-40.0 <= t <= 80.0 and 0.0 <= h <= 100.0):
                raise ValueError("out of range")
            self.temperature = t
            self.humidity = h
            self._fails = 0
        except (OSError, ValueError) as e:
            self._fails += 1
            _log("DHT-22 read failed ({}): {}".format(self._fails, e))
        return self.ok


# --------------------------------------------------------------------------- MQ-135
class MQ135Sensor:
    """MQ-135 analog output. Relative index only; see config.GAS_* for calibration."""

    def __init__(self, gpio=None):
        self._in = AdcInput(config.PIN_GAS_ADC if gpio is None else gpio)
        self._t0 = ticks_ms()
        self._ema_raw = None

    @property
    def warmed_up(self):
        return ticks_diff(ticks_ms(), self._t0) >= config.MQ_WARMUP_S * 1000

    def read(self):
        raw = self._in.read_raw_avg()
        a = config.GAS_EMA_ALPHA
        if self._ema_raw is None or a >= 1.0:
            self._ema_raw = raw
        else:
            self._ema_raw = a * raw + (1.0 - a) * self._ema_raw
        smooth = self._ema_raw
        volts = self._in.raw_to_sensor_volts(smooth)
        # a pin stuck at 0 or at full scale means a wiring fault, not a measurement
        plausible = 50 < raw < 65000
        ok = self.warmed_up and plausible
        aqi = None
        if ok:
            span = config.GAS_FULL_SCALE_V - config.GAS_CLEAN_V
            index = (volts - config.GAS_CLEAN_V) / span * 500.0
            aqi = int(max(0, min(500, index)))
        return {"gas_raw": int(smooth), "gas_v": volts, "aqi_proxy": aqi, "ok": ok}

    def measure_clean_air_volts(self, n=20):
        """REPL helper: average sensor-side volts over n readings, for GAS_CLEAN_V."""
        total = 0.0
        for _ in range(n):
            total += self._in.raw_to_sensor_volts(self._in.read_raw_avg())
        return total / n


# --------------------------------------------------------------------------- Sharp GP2Y1010AU0F
class SharpDustSensor:
    """Optical dust sensor.

    Each pulse: LED pin LOW (IR LED on) -> wait 280 us -> read ADC -> wait until 320 us
    -> LED pin HIGH (off). Pulses repeat every 10 ms. The 320 us timed section runs with
    interrupts disabled; the 9.7 ms gap between pulses is spent in asyncio.sleep_ms so
    other tasks keep running.
    """

    def __init__(self, adc_gpio=None, led_gpio=None):
        self._led = Pin(config.PIN_DUST_LED if led_gpio is None else led_gpio, Pin.OUT, value=1)
        self._in = AdcInput(config.PIN_DUST_ADC if adc_gpio is None else adc_gpio)
        self._adc = self._in._adc
        self._ema_v = None

    def _one_pulse(self):
        adc = self._adc
        led = self._led
        delay_us = config.DUST_SAMPLE_DELAY_US
        width_us = config.DUST_PULSE_WIDTH_US
        irq = disable_irq()
        try:
            led.value(0)
            t0 = ticks_us()
            while ticks_diff(ticks_us(), t0) < delay_us:
                pass
            raw = adc.read_u16()
            while ticks_diff(ticks_us(), t0) < width_us:
                pass
        finally:
            led.value(1)
            enable_irq(irq)
        return raw

    async def measure(self):
        """Run one burst of pulses; returns a dict with volts, mV and ug/m3."""
        n = config.DUST_PULSES
        period = config.DUST_PULSE_PERIOD_US
        samples = []
        t_next = ticks_us()
        try:
            for _ in range(n):
                samples.append(self._one_pulse())
                t_next = ticks_add(t_next, period)
                wait_us = ticks_diff(t_next, ticks_us())
                if wait_us < 0:
                    t_next = ticks_us()          # fell behind; do not try to catch up
                elif wait_us >= 2000:
                    await asyncio.sleep_ms(wait_us // 1000 - 1)
        finally:
            self._led.value(1)                   # IR LED always ends up off
        raw = trimmed_mean(samples, config.ADC_TRIM_FRACTION)
        volts = self._in.raw_to_sensor_volts(raw)
        # Damping for a noisy output: smooth across cycles, then ignore the first
        # DUST_DEADBAND_V above the baseline. dust_v / dust_mv stay un-smoothed on purpose,
        # so the test scripts and the server log show what the sensor really did.
        alpha = getattr(config, "DUST_EMA_ALPHA", 0.30)
        if self._ema_v is None or alpha >= 1.0:
            self._ema_v = volts
        else:
            self._ema_v = alpha * volts + (1.0 - alpha) * self._ema_v
        deadband = getattr(config, "DUST_DEADBAND_V", 0.10)
        ugm3 = (self._ema_v - config.DUST_BASELINE_V - deadband) * config.DUST_SENSITIVITY_UG_PER_V
        ugm3 = max(0.0, min(config.DUST_MAX_UGM3, ugm3))
        # A reading stuck near 0 V means the IR LED is not lighting (supply, LED pin or
        # ground wiring), so it must not be reported as a valid "clean air" value.
        min_ok_v = getattr(config, "DUST_MIN_OK_V", 0.10)
        ok = (min_ok_v < volts < 3.5) and (50 < raw < 65000)
        return {"dust_raw": int(raw), "dust_v": volts, "dust_mv": int(volts * 1000),
                "dust_ugm3": ugm3, "ok": ok}


# --------------------------------------------------------------------------- hub
class SensorHub:
    """Owns all sensors and returns one dictionary per sampling cycle."""

    def __init__(self):
        self.dht = DHT22Sensor()
        self.mq = MQ135Sensor()
        self.dust = SharpDustSensor()

    async def read_all(self):
        dht_ok = await self.dht.poll()
        gas = self.mq.read()
        dust = await self.dust.measure()
        return {
            "temperature_c": round(self.dht.temperature, 1) if dht_ok else None,
            "humidity_pct": round(self.dht.humidity, 1) if dht_ok else None,
            "dust_mv": dust["dust_mv"],
            "dust_ugm3": round(dust["dust_ugm3"], 1),
            "gas_raw": gas["gas_raw"],
            "aqi_proxy": gas["aqi_proxy"],
            "sensor_ok": {"dht": dht_ok, "mq135": gas["ok"], "dust": dust["ok"]},
        }
