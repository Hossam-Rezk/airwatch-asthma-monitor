# alerts.py - RGB LED and buzzer patterns for the PRJ-004 edge node (MicroPython)
#
#   AlertController   one asyncio task that plays the LED and buzzer patterns from config.py
#
# Modes:
#   connecting   blue blink (start-up, before the first risk level is known)
#   online       risk colour and buzzer pattern sent by the PC
#   offline      local risk colour with a short blue flash (the PC is not answering)

import asyncio
from machine import PWM, Pin
from time import ticks_diff, ticks_ms

import config


class AlertController:
    def __init__(self):
        self._pwm = [PWM(Pin(p)) for p in (config.PIN_LED_R, config.PIN_LED_G, config.PIN_LED_B)]
        for p in self._pwm:
            p.freq(config.LED_PWM_FREQ_HZ)
        self._buzzer = Pin(config.PIN_BUZZER, Pin.OUT, value=0)
        self.mode = "connecting"
        self.risk = None

    def set(self, mode, risk=None):
        self.mode = mode
        if risk is not None:
            self.risk = risk

    def _colour(self, name):
        r, g, b = config.COLORS.get(name, (0.0, 0.0, 0.0))
        for pwm, duty in zip(self._pwm, (r, g, b)):
            duty = max(0.0, min(1.0, duty * config.LED_BRIGHTNESS))
            if config.LED_COMMON_ANODE:
                duty = 1.0 - duty
            pwm.duty_u16(int(duty * 65535))

    def _led_pattern(self):
        if self.mode == "connecting" or self.risk is None:
            return config.CONNECTING_PATTERN
        colour = config.RISK_LED.get(self.risk, "YELLOW")
        if self.mode == "offline":
            return [(colour, config.OFFLINE_STEADY_MS), ("BLUE", config.OFFLINE_FLASH_MS)]
        return config.LED_PATTERNS.get(colour, [(colour, 1000)])

    def _buzzer_pattern(self):
        if not config.BUZZER_ENABLED or self.risk is None or self.mode == "connecting":
            return config.BUZZER_PATTERNS["OFF"]
        return config.BUZZER_PATTERNS.get(config.RISK_BUZZER.get(self.risk, "OFF"), config.BUZZER_PATTERNS["OFF"])

    @staticmethod
    def _step(pattern, t):
        """The pattern entry that is active `t` ms into the repeating pattern."""
        total = sum(ms for _, ms in pattern) or 1
        t %= total
        for value, ms in pattern:
            if t < ms:
                return value
            t -= ms
        return pattern[-1][0]

    async def run(self):
        t0 = ticks_ms()
        while True:
            t = ticks_diff(ticks_ms(), t0)
            self._colour(self._step(self._led_pattern(), t))
            self._buzzer.value(1 if self._step(self._buzzer_pattern(), t) else 0)
            await asyncio.sleep_ms(config.ALERT_TICK_MS)

    def off(self):
        self._colour("OFF")
        self._buzzer.value(0)
