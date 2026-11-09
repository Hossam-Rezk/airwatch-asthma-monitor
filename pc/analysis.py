"""analysis.py - turns one set of sensor readings into a risk level and plain-language warnings.

Two engines produce exactly the same output format, so the dashboard cannot tell them apart:

    ModelEngine   the trained classifier (model/model.joblib) plus a "what is driving it" check
    RuleEngine    fixed thresholds, fully deterministic, no model needed

settings.USE_RULE_BASED_FALLBACK picks one (see make_analyzer at the bottom).
"""
import logging
import math
from pathlib import Path

FEATURES = ["temperature_c", "humidity_pct", "dust_ugm3", "aqi_proxy"]
CLASSES = ["SAFE", "WARNING", "DANGER"]
LEVEL = {name: i for i, name in enumerate(CLASSES)}
MIN_FEATURES = 2            # fewer usable readings than this -> no verdict about the air

LABELS = {"temperature_c": "Temperature", "humidity_pct": "Humidity", "dust_ugm3": "Dust", "aqi_proxy": "Air quality"}
UNITS = {"temperature_c": "°C", "humidity_pct": "%", "dust_ugm3": "µg/m³", "aqi_proxy": "AQI"}
SENSOR_OF = {"temperature_c": "dht", "humidity_pct": "dht", "dust_ugm3": "dust", "aqi_proxy": "mq135"}
SENSOR_NAMES = {"dht": "Temperature/humidity sensor (DHT-22)", "mq135": "Air-quality sensor (MQ-135)",
                "dust": "Dust sensor (Sharp GP2Y1010)"}

# Bands used by the rules and by the dashboard colours: (WARNING from, DANGER from).
# Dust follows the US EPA PM2.5 breakpoints for sensitive groups (35.5 and 55.5 ug/m3); the
# air-quality index follows the EPA AQI bands (101+ unhealthy for sensitive groups, 201+ very
# unhealthy). Temperature and humidity are common asthma-trigger comfort limits.
HIGH = {"temperature_c": (32.0, 38.0), "humidity_pct": (80.0, 90.0), "dust_ugm3": (35.5, 55.5), "aqi_proxy": (101, 201)}
LOW = {"temperature_c": (10.0, None), "humidity_pct": (25.0, None)}     # cold or very dry air
CLEAN = {"temperature_c": 24.0, "humidity_pct": 50.0, "dust_ugm3": 8.0, "aqi_proxy": 30.0}

HEADLINE = {
    "SAFE": "Air conditions look safe",
    "WARNING": "Possible asthma triggers detected",
    "DANGER": "High asthma-trigger risk",
    "UNKNOWN": "Not enough sensor data",
}
ADVICE = {
    "SAFE": ["No action needed. Keep your reliever inhaler with you as usual."],
    "WARNING": ["Sensitive people should limit time in this area.",
                "Keep your reliever inhaler close and watch for symptoms."],
    "DANGER": ["Avoid this area or leave it if you can.",
               "Follow your asthma action plan and use your reliever inhaler if symptoms start.",
               "Improve ventilation or use an air purifier if you must stay."],
    "UNKNOWN": ["Check the sensor wiring on the Pico. Warnings resume when readings are back."],
}

log = logging.getLogger("analysis")


def fmt(feature, value):
    return "{:g} {}".format(round(value, 1), UNITS[feature])


def usable_features(reading):
    """The four inputs, with values from sensors that report a fault set to None."""
    ok = reading.get("sensor_ok") or {}
    feats = {}
    for f in FEATURES:
        v = reading.get(f)
        feats[f] = v if (v is not None and ok.get(SENSOR_OF[f], True)) else None
    return feats


def sensor_issues(reading):
    ok = reading.get("sensor_ok") or {}
    out = []
    for s in ("dht", "mq135", "dust"):
        if ok.get(s, True):
            continue
        if s == "mq135" and reading.get("mq135_warming"):
            out.append(SENSOR_NAMES[s] + " is warming up (about 30 s after the Pico starts).")
        else:
            out.append(SENSOR_NAMES[s] + " is not giving valid readings. Check its wiring.")
    return out


def result(risk, factors, issues):
    key = risk if risk in HEADLINE else "UNKNOWN"
    return {"risk": risk, "headline": HEADLINE[key], "advice": ADVICE[key],
            "factors": factors, "sensor_issues": issues}


def no_data_result(issues):
    return result("UNKNOWN", [], issues or ["Waiting for valid sensor readings."])


# --------------------------------------------------------------------------- rule engine
class RuleEngine:
    """Deterministic thresholds. Same inputs -> same output, always."""

    def analyze(self, reading):
        feats = usable_features(reading)
        issues = sensor_issues(reading)
        if sum(v is not None for v in feats.values()) < MIN_FEATURES:
            return no_data_result(issues)
        factors = []
        for f, v in feats.items():
            if v is None:
                continue
            lvl, text = self._check(f, v)
            if lvl:
                factors.append({"feature": f, "label": LABELS[f], "level": lvl, "text": text})
        risk = max((LEVEL[x["level"]] for x in factors), default=0)
        # dust and polluting gases together are worse than either alone
        warned = {x["feature"] for x in factors}
        if {"dust_ugm3", "aqi_proxy"} <= warned:
            risk = 2
        factors.sort(key=lambda x: -LEVEL[x["level"]])
        return result(CLASSES[risk], factors, issues)

    @staticmethod
    def _check(f, v):
        name = LABELS[f]
        warn, danger = HIGH[f]
        if danger is not None and v >= danger:
            return "DANGER", "{} is very high at {} (limit {}).".format(name, fmt(f, v), fmt(f, danger))
        if v >= warn:
            return "WARNING", "{} is elevated at {} (above {}).".format(name, fmt(f, v), fmt(f, warn))
        if f in LOW and v <= LOW[f][0]:
            what = "Cold air" if f == "temperature_c" else "Very dry air"
            return "WARNING", "{} ({}) can irritate the airways.".format(what, fmt(f, v))
        return None, None


# --------------------------------------------------------------------------- model engine
class ModelEngine:
    """The trained classifier. Warnings name the readings that push its prediction up most.

    For each reading the model is asked again with that one value replaced by a clean-air
    value; the bigger the drop in predicted risk, the more that reading is to blame.
    """

    def __init__(self, bundle):
        import numpy as np
        self.np = np
        self.pipeline = bundle["pipeline"]
        self.features = bundle["features"]
        self.classes = bundle["classes"]
        self.meta = {k: bundle.get(k) for k in ("trained_at", "sklearn_version", "metrics")}

    def _scores(self, rows):
        X = self.np.array([[self.np.nan if r[f] is None else r[f] for f in self.features] for r in rows], dtype=float)
        proba = self.pipeline.predict_proba(X)
        order = [self.classes[int(c)] for c in self.pipeline.classes_]
        out = []
        for p in proba:
            probs = dict(zip(order, (float(x) for x in p)))
            out.append((probs, probs.get("WARNING", 0.0) * 0.5 + probs.get("DANGER", 0.0)))
        return out

    def analyze(self, reading):
        feats = usable_features(reading)
        issues = sensor_issues(reading)
        if sum(v is not None for v in feats.values()) < MIN_FEATURES:
            return no_data_result(issues)
        present = [f for f in self.features if feats[f] is not None]
        variants = [feats] + [dict(feats, **{f: CLEAN[f]}) for f in present]
        scored = self._scores(variants)
        probs, base = scored[0]
        risk = max(probs, key=probs.get)
        factors = []
        if risk != "SAFE":
            for f, (_, s) in zip(present, scored[1:]):
                drop = base - s
                if drop < 0.08:
                    continue
                lvl = "DANGER" if drop >= 0.3 else "WARNING"
                role = "a major driver of the risk" if lvl == "DANGER" else "adding to the risk"
                text = "{} at {} is {}.".format(LABELS[f], fmt(f, feats[f]), role)
                factors.append({"feature": f, "label": LABELS[f], "level": lvl, "text": text, "_drop": drop})
            factors.sort(key=lambda x: -x["_drop"])
            for x in factors:
                del x["_drop"]
            if not factors:
                factors.append({"feature": None, "label": "Combined", "level": risk,
                                "text": "No single reading stands out; the combination of conditions raises the risk."})
        return result(risk, factors, issues)


# --------------------------------------------------------------------------- choose the engine
def load_model(path):
    import joblib
    import sklearn
    bundle = joblib.load(path)
    if bundle.get("sklearn_version") != sklearn.__version__:
        raise ValueError("model was saved with scikit-learn {}, this is {}".format(
            bundle.get("sklearn_version"), sklearn.__version__))
    if list(bundle.get("features", [])) != FEATURES:
        raise ValueError("model was trained on different inputs: {}".format(bundle.get("features")))
    return ModelEngine(bundle)


def make_analyzer(use_rules, model_path):
    """Returns the engine to use. Never raises: if the model cannot be used, the rules take over."""
    if use_rules:
        log.info("analysis: rule-based mode (USE_RULE_BASED_FALLBACK = True)")
        return RuleEngine()
    model_path = Path(model_path)
    for attempt in ("load", "retrain"):
        try:
            if attempt == "retrain" or not model_path.exists():
                log.info("analysis: training the model (takes a few seconds) ...")
                import train_model
                train_model.main(model_path.parent)
            engine = load_model(model_path)
            log.info("analysis: model mode (%s, trained %s)", engine.meta["metrics"].get("model"), engine.meta["trained_at"])
            return engine
        except Exception as e:                       # noqa: BLE001 - any failure means "use the rules"
            log.warning("analysis: model %s failed: %s", attempt, e)
    log.warning("analysis: model unavailable, using the rule-based engine instead")
    return RuleEngine()


def clean_number(v, lo, hi):
    """A finite number inside [lo, hi], else None."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return float(v) if lo <= v <= hi else None
