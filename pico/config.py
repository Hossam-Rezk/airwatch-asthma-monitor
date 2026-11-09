# config.py - PRJ-004 Asthma Prediction System, edge node (Raspberry Pi Pico / Pico W / Pico 2 / Pico 2 W)
#
# Every tunable value lives here. Edit this file, not the logic in the other modules.
# Hardware reference: Wiring_Diagram.pdf (Phase 2), with the bench changes noted below.

# --------------------------------------------------------------------------- identity
STATION_ID = "METRO-ST-01"
FW_VERSION = "1.0.0"
DEBUG = False                     # True prints extra log lines (the PC ignores them)

# --------------------------------------------------------------------------- USB link to the PC
# The Pico talks to the PC over its USB cable (USB serial). It prints one JSON line per reading
# and listens for the risk level the PC sends back. No Wi-Fi or IP settings are needed.
PC_SILENT_AFTER_S = 12            # no risk update from the PC for this long -> local fallback

# --------------------------------------------------------------------------- pin map (GPIO numbers)
PIN_DHT = 15                      # DHT-22 data, pin 20 (3-pin module, pull-up on the module)
PIN_DUST_ADC = 26                 # Sharp Vo through divider, ADC0, pin 31
PIN_DUST_LED = 16                 # Sharp LED pin, pin 21 (LOW = IR LED on)
PIN_GAS_ADC = 27                  # MQ-135 AO through divider, ADC1, pin 32
PIN_LED_R = 10                    # RGB red,   pin 14
PIN_LED_G = 11                    # RGB green, pin 15
PIN_LED_B = 12                    # RGB blue,  pin 16
PIN_BUZZER = 14                   # 2N3904 base via 880 ohm, pin 19

# --------------------------------------------------------------------------- ADC voltage dividers
# Both 5 V analog sensors (MQ-135 AO, Sharp Vo) feed the Pico through a divider:
#
#     sensor --[ R1 ]--+--[ R2 ]-- GND        ratio = R2 / (R1 + R2)
#                      |
#                    ADC pin
#
# Bench build (5.6 kohm resistors on hand):
#   0.60   R1 = 2 x 5.6k in series = 11.2k, R2 = 3 x 5.6k in series = 16.8k   <- DEFAULT
#          ratio = 16.8 / (11.2 + 16.8) = 0.600; 5.0 V in -> 3.00 V at the ADC.
#   0.667  R1 = 5.6k, R2 = 2 x 5.6k = 11.2k
#          ratio = 11.2 / (5.6 + 11.2) = 0.667; 5.0 V in -> 3.33 V at the ADC,
#          which is slightly ABOVE the 3.3 V supply. Readings saturate near 4.95 V and
#          the pin has almost no margin. Use it only if the sensor output never nears 5 V.
# If you change the resistors, change this number; every voltage in the firmware is
# divided by it.
ADC_DIVIDER_RATIO = 0.60
ADC_VREF = 3.3                    # ADC full-scale voltage (3V3 rail)

# No 100 nF filter capacitor on the MQ-135 line on the bench, so noise is reduced in software:
ADC_OVERSAMPLE = 64               # readings per measurement
ADC_OVERSAMPLE_GAP_US = 150       # pause between readings
ADC_TRIM_FRACTION = 0.10          # drop this fraction of the lowest and highest readings
GAS_EMA_ALPHA = 0.30              # extra smoothing across cycles (1.0 = off, smaller = smoother)

# --------------------------------------------------------------------------- sampling
SAMPLE_INTERVAL_S = 3             # one reading per interval
DHT_MIN_INTERVAL_S = 2.2          # DHT-22 must not be polled faster than every 2 s
DHT_BOOT_DELAY_MS = 2200          # wait after power-up before the first read
DHT_FAIL_LIMIT = 3                # consecutive failed reads before the sensor is flagged bad

# --------------------------------------------------------------------------- Sharp GP2Y1010AU0F
DUST_PULSES = 100                 # pulses averaged per report (100 x 10 ms = about 1 s)
DUST_PULSE_PERIOD_US = 10000      # datasheet: 10 ms between pulses
DUST_PULSE_WIDTH_US = 320         # datasheet: IR LED on for 0.32 ms
DUST_SAMPLE_DELAY_US = 280        # datasheet: read the output 0.28 ms after the LED turns on
# Calibration (sensor-side volts, i.e. after un-dividing). The conversion is
#   dust_ugm3 = max(0, (Vo - DUST_BASELINE_V) * DUST_SENSITIVITY_UG_PER_V)
# Datasheet sensitivity is 0.5 V per 0.1 mg/m3 = 200 ug/m3 per volt.
# Measure DUST_BASELINE_V in clean air (see SharpDustSensor.measure in sensors.py, or the
# README step "calibrate"). 0.60 is only a starting guess; sensors vary roughly 0.5 - 1.2 V.
DUST_BASELINE_V = 0.60
DUST_SENSITIVITY_UG_PER_V = 200.0
DUST_MAX_UGM3 = 1000.0
DUST_MIN_OK_V = 0.10              # below this the sensor is reported not-ok (IR LED not lighting)
# Damping for a noisy sensor (both are optional; the code falls back to these defaults):
DUST_EMA_ALPHA = 0.30             # 1.0 = no smoothing; smaller = smoother but slower to react
DUST_DEADBAND_V = 0.10            # volts above the baseline that are ignored (0.10 V = 20 ug/m3)

# --------------------------------------------------------------------------- MQ-135
MQ_WARMUP_S = 30                  # readings are flagged not-ready until this has passed
# aqi_proxy = 0..500 index: 0 at GAS_CLEAN_V, 500 at GAS_FULL_SCALE_V (sensor-side volts).
# These are placeholders. Measure the clean-air voltage on your own module after it has
# been powered for a long time and set GAS_CLEAN_V; the index is relative, not a regulatory AQI.
GAS_CLEAN_V = 0.40
GAS_FULL_SCALE_V = 2.50

# --------------------------------------------------------------------------- RGB LED (common-cathode)
LED_COMMON_ANODE = False          # confirmed common-cathode, longest leg to GND
LED_PWM_FREQ_HZ = 1000
LED_BRIGHTNESS = 1.0              # global scale, 0.0 - 1.0
# Channel mix (red, green, blue) as 0.0 - 1.0 duty fractions.
# With 220 ohm on every channel red carries about 6 mA and green/blue only 1-2 mA,
# so yellow is mixed with LESS red than green to look yellow instead of orange.
# Tune the middle number of YELLOW if it still looks orange (lower red) or green (raise red).
COLORS = {
    "OFF":    (0.0, 0.0, 0.0),
    "GREEN":  (0.0, 1.0, 0.0),
    "YELLOW": (0.30, 1.0, 0.0),
    "RED":    (1.0, 0.0, 0.0),
    "BLUE":   (0.0, 0.0, 1.0),
}
# LED patterns: list of (colour name, milliseconds), repeated forever.
LED_PATTERNS = {
    "GREEN":  [("GREEN", 1000)],
    "YELLOW": [("YELLOW", 1000)],
    "RED":    [("RED", 1000)],
}
CONNECTING_PATTERN = [("BLUE", 500), ("OFF", 500)]
OFFLINE_STEADY_MS = 1500          # offline: local risk colour for this long ...
OFFLINE_FLASH_MS = 300            # ... then a blue flash

# --------------------------------------------------------------------------- buzzer (active, on/off only)
BUZZER_ENABLED = True
# Patterns: list of (1 = on / 0 = off, milliseconds), repeated forever.
BUZZER_PATTERNS = {
    "OFF":        [(0, 1000)],
    "SLOW_BEEP":  [(1, 150), (0, 2850)],
    "FAST_PULSE": [(1, 120), (0, 120)],
}
RISK_LED = {"SAFE": "GREEN", "WARNING": "YELLOW", "DANGER": "RED"}
RISK_BUZZER = {"SAFE": "OFF", "WARNING": "SLOW_BEEP", "DANGER": "FAST_PULSE"}
ALERT_TICK_MS = 20                # alert controller refresh interval

# --------------------------------------------------------------------------- fault handling
WATCHDOG_ENABLED = False          # True reboots the Pico if the firmware hangs. Leave False while
                                  # using Thonny: once on, stopping the script makes the Pico reboot
WDT_TIMEOUT_MS = 8000             # RP2 hardware limit is about 8388 ms

# --------------------------------------------------------------------------- local fallback thresholds
# Used only while the PC is not connected or not answering. Engineering assumptions, not clinical limits.
# The dust numbers follow the US EPA PM2.5 breakpoints for sensitive groups (35.5 and 55.5).
LOCAL_THRESHOLDS = {
    "dust_warn": 35.5,            # ug/m3 (PM2.5-equivalent estimate)
    "dust_danger": 55.5,
    "aqi_warn": 101,              # aqi_proxy (same bands as the PC rules)
    "aqi_danger": 201,
    "temp_warn": 38.0,            # deg C
    "humidity_warn": 90.0,        # percent
}
