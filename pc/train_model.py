#!/usr/bin/env python3
"""train_model.py - trains the asthma-trigger risk model from the Kaggle dataset.

    python train_model.py            # writes model/model.joblib and model/model_report.md

Dataset: "Air Quality and Pollution Assessment" by Mujtaba Mateen (Kaggle,
mujtabamatin/air-quality-and-pollution-assessment, Apache 2.0), file
data/updated_pollution_dataset.csv: 5,000 rows, labels Good / Moderate / Poor / Hazardous.

The model only uses inputs the Pico can measure:
    temperature_c   DHT-22                 <- Temperature
    humidity_pct    DHT-22                 <- Humidity (clipped to 0-100 %)
    dust_ugm3       Sharp GP2Y1010AU0F     <- PM2.5
    aqi_proxy       MQ-135 (0-500 index)   <- gas index built from CO and NO2 (see gas_index)
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, classification_report, confusion_matrix,
                             f1_score)
from sklearn.model_selection import StratifiedKFold, cross_validate, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

from analysis import CLASSES, FEATURES

BASE = Path(__file__).resolve().parent
DATA = BASE / "data" / "updated_pollution_dataset.csv"
OUT_DIR = BASE / "model"
SEED = 42
LABEL_MAP = {"Good": 0, "Moderate": 1, "Poor": 2, "Hazardous": 2}   # -> SAFE, WARNING, DANGER


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("dataset is empty: {}".format(path))
    return rows


def gas_index(co_ppm, no2_ppb, anchors=None):
    """CO and NO2 -> one 0-500 gas index on the same scale as the Pico's MQ-135 aqi_proxy.

    1. Each gas is turned into its US EPA AQI sub-index (0-50 band: CO 0-4.4 ppm, NO2 0-53 ppb).
    2. The two sub-indices are added (the MQ-135 responds to both gases at once).
    3. The sum is mapped linearly so the 1st percentile of the training data is 0 and the
       99th percentile is 300, the start of the EPA "Very unhealthy" band. The anchors are
       fitted on the training split only and stored in the model file.
    """
    s = np.clip(co_ppm, 0, 4.4) / 4.4 * 50 + np.clip(no2_ppb, 0, 53) / 53 * 50
    if anchors is None:
        anchors = (float(np.percentile(s, 1)), float(np.percentile(s, 99)))
    lo, hi = anchors
    return np.clip((s - lo) / (hi - lo) * 300.0, 0, 500), anchors


def preprocess(rows):
    """Raw CSV rows -> feature matrix pieces, labels and a log of what was cleaned."""
    num = lambda k: np.array([float(r[k]) for r in rows])
    temp, hum, pm25 = num("Temperature"), num("Humidity"), num("PM2.5")
    co, no2 = num("CO"), num("NO2")
    y = np.array([LABEL_MAP[r["Air Quality"]] for r in rows])
    hum = np.clip(hum, 0, 100)
    pm25 = np.clip(pm25, 0, None)
    # Health floor: an asthma warning must not depend on the other readings when fine dust alone
    # is unhealthy for sensitive groups (US EPA PM2.5 breakpoints 35.5 and 55.5 ug/m3). In the raw
    # data high dust always comes with other high values, so without this the model would not
    # learn that dust on its own is enough.
    floor = np.where(pm25 >= 55.5, 2, np.where(pm25 >= 35.5, 1, 0))
    raised = int((floor > y).sum())
    y = np.maximum(y, floor)
    notes = {
        "rows": len(rows),
        "humidity_above_100_clipped": int((num("Humidity") > 100).sum()),
        "negative_pm25_clipped": int((num("PM2.5") < 0).sum()),
        "labels_raised_by_pm25_floor": raised,
    }
    return temp, hum, pm25, co, no2, y, notes


def augment_dust(X, y, rng, frac=0.15):
    """Copies of random training rows with only the dust value raised into the unhealthy range.

    Teaches the model the health floor above for conditions that are clean apart from the dust
    (a dusty metro platform on a mild day), which the raw data does not contain.
    """
    pick = rng.choice(len(y), int(len(y) * frac), replace=False)
    Xa = X[pick].copy()
    Xa[:, FEATURES.index("dust_ugm3")] = np.exp(rng.uniform(np.log(36), np.log(300), len(pick)))
    dust = Xa[:, FEATURES.index("dust_ugm3")]
    ya = np.maximum(y[pick], np.where(dust >= 55.5, 2, 1))
    return np.vstack([X, Xa]), np.concatenate([y, ya]), len(pick)


def mask_missing(X, rng, p=0.03):
    """Blank a few values so the imputer is exercised (a sensor can fail at any time)."""
    X = X.copy()
    X[rng.random(X.shape) < p] = np.nan
    return X


def candidates():
    imp = ("impute", SimpleImputer(strategy="median"))
    return {
        "Logistic regression": Pipeline([imp, ("scale", StandardScaler()),
                                         ("clf", LogisticRegression(max_iter=2000, class_weight="balanced"))]),
        "Decision tree (depth 6)": Pipeline([imp, ("clf", DecisionTreeClassifier(max_depth=6, class_weight="balanced",
                                                                                  random_state=SEED))]),
        "Random forest (150 trees)": Pipeline([imp, ("clf", RandomForestClassifier(
            n_estimators=150, max_depth=12, min_samples_leaf=3, class_weight="balanced", n_jobs=-1,
            random_state=SEED))]),
    }


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main(out_dir=OUT_DIR):
    out_dir = Path(out_dir)
    if not DATA.exists():
        raise SystemExit("Dataset not found: {}\nSee README.md, section 'Training the model'.".format(DATA))
    rng = np.random.default_rng(SEED)
    temp, hum, pm25, co, no2, y, notes = preprocess(load_rows(DATA))

    idx = np.arange(len(y))
    tr, te = train_test_split(idx, test_size=0.2, stratify=y, random_state=SEED)
    gas_tr, anchors = gas_index(co[tr], no2[tr])
    gas_te, _ = gas_index(co[te], no2[te], anchors)
    X_tr = np.column_stack([temp[tr], hum[tr], pm25[tr], gas_tr])
    X_te = np.column_stack([temp[te], hum[te], pm25[te], gas_te])
    y_tr, y_te = y[tr], y[te]
    X_tr, y_tr, notes["augmented_rows"] = augment_dust(X_tr, y_tr, rng)
    X_tr_m = mask_missing(X_tr, rng)

    print("Rows: {}  train {}  test {}".format(len(y), len(tr), len(te)))
    print("Class share: " + "  ".join("{} {:.1%}".format(c, (y == i).mean()) for i, c in enumerate(CLASSES)))

    # model selection: 5-fold CV macro-F1 on the training split only. Every candidate is also
    # fitted once and scored on the test split, for the comparison table (not used to choose).
    cv = StratifiedKFold(5, shuffle=True, random_state=SEED)
    scores = {}
    fitted = {}
    for name, pipe in candidates().items():
        s = cross_validate(pipe, X_tr_m, y_tr, cv=cv, scoring=("f1_macro", "accuracy"))
        fitted[name] = pipe.fit(X_tr_m, y_tr)
        p = pipe.predict(X_te)
        scores[name] = {"cv_f1": (float(s["test_f1_macro"].mean()), float(s["test_f1_macro"].std())),
                        "cv_acc": (float(s["test_accuracy"].mean()), float(s["test_accuracy"].std())),
                        "test_acc": float(accuracy_score(y_te, p)),
                        "test_f1": float(f1_score(y_te, p, average="macro"))}
        print("  {:<28} CV macro-F1 {:.3f}  CV accuracy {:.3f}  test accuracy {:.3f}".format(
            name, scores[name]["cv_f1"][0], scores[name]["cv_acc"][0], scores[name]["test_acc"]))
    best_name = max(scores, key=lambda k: scores[k]["cv_f1"][0])
    model = fitted[best_name]

    # hold-out evaluation
    pred = model.predict(X_te)
    rep = classification_report(y_te, pred, target_names=CLASSES, output_dict=True, zero_division=0)
    cm = confusion_matrix(y_te, pred)
    danger = y_te == 2
    missed = float(((pred == 0) & danger).sum()) / max(1, int(danger.sum()))
    # robustness: the same test set with one sensor removed at a time
    robust = {}
    for j, f in enumerate(FEATURES):
        Xd = X_te.copy()
        Xd[:, j] = np.nan
        robust[f] = float(f1_score(y_te, model.predict(Xd), average="macro"))
    metrics = {
        "model": best_name,
        "test_accuracy": round(float(accuracy_score(y_te, pred)), 4),
        "test_balanced_accuracy": round(float(balanced_accuracy_score(y_te, pred)), 4),
        "test_macro_f1": round(float(rep["macro avg"]["f1-score"]), 4),
        "danger_recall": round(float(rep["DANGER"]["recall"]), 4),
        "danger_missed_as_safe": round(missed, 4),
        "model_comparison": {k: {"cv_macro_f1": round(v["cv_f1"][0], 4), "cv_accuracy": round(v["cv_acc"][0], 4),
                                 "test_accuracy": round(v["test_acc"], 4), "test_macro_f1": round(v["test_f1"], 4)}
                             for k, v in scores.items()},
        "macro_f1_without_sensor": {k: round(v, 4) for k, v in robust.items()},
    }
    clf = model.named_steps["clf"]
    if hasattr(clf, "feature_importances_"):
        metrics["feature_importances"] = dict(zip(FEATURES, clf.feature_importances_.round(4).tolist()))

    print("\nSelected: {}".format(best_name))
    print(classification_report(y_te, pred, target_names=CLASSES, digits=3, zero_division=0))
    print("Confusion matrix (rows = true, cols = predicted):\n{}".format(cm))
    print("DANGER predicted as SAFE: {:.2%}".format(missed))

    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = {"pipeline": model, "features": FEATURES, "classes": CLASSES, "gas_anchors": anchors,
              "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "sklearn_version": sklearn.__version__, "metrics": metrics}
    joblib.dump(bundle, out_dir / "model.joblib")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    write_report(out_dir, notes, anchors, scores, best_name, rep, cm, metrics, len(tr), len(te))
    print("\nSaved model.joblib, metrics.json and model_report.md in {}".format(out_dir))


def write_report(out_dir, notes, anchors, scores, best_name, rep, cm, metrics, n_tr, n_te):
    per_class = [[c, "{:.3f}".format(rep[c]["precision"]), "{:.3f}".format(rep[c]["recall"]),
                  "{:.3f}".format(rep[c]["f1-score"]), int(rep[c]["support"])] for c in CLASSES]
    lines = [
        "# Model report",
        "",
        "Generated by `train_model.py` on {} (scikit-learn {}).".format(
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), sklearn.__version__),
        "",
        "## Dataset",
        "",
        "- **Source:** Kaggle, [Air Quality and Pollution Assessment]"
        "(https://www.kaggle.com/datasets/mujtabamatin/air-quality-and-pollution-assessment) by Mujtaba Mateen, "
        "Apache 2.0 licence. Found and downloaded with the Kaggle MCP server.",
        "- **File:** `data/updated_pollution_dataset.csv`, {} rows, no missing values.".format(notes["rows"]),
        "- **Columns:** temperature, humidity, PM2.5, PM10, NO2, SO2, CO, distance to industry, population "
        "density, and the label `Air Quality` (Good 2000, Moderate 1500, Poor 1000, Hazardous 500).",
        "- **Why this one:** small, clean, labelled, and its columns overlap with the four Pico sensors.",
        "",
        "## Preprocessing",
        "",
        "1. Kept only what the Pico can measure: temperature, humidity, PM2.5, and CO + NO2 for the gas sensor. "
        "PM10, SO2, distance to industry and population density were dropped.",
        "2. Clipped humidity to 0-100 % ({} rows were above 100 %) and PM2.5 to >= 0 ({} rows).".format(
            notes["humidity_above_100_clipped"], notes["negative_pm25_clipped"]),
        "3. Built the gas index that matches the MQ-135 `aqi_proxy` (0-500): CO and NO2 converted to their US EPA "
        "AQI sub-indices, added, then mapped linearly so the training data's 1st percentile is 0 and its 99th "
        "percentile is 300 (anchors {:.2f} and {:.2f}, stored in the model file).".format(*anchors),
        "4. Labels merged into the three dashboard levels: Good -> SAFE, Moderate -> WARNING, "
        "Poor and Hazardous -> DANGER.",
        "5. Health floor from the US EPA PM2.5 breakpoints for sensitive groups: PM2.5 >= 35.5 ug/m3 is at "
        "least WARNING and >= 55.5 ug/m3 is DANGER ({} labels raised). The test set uses the same labels.".format(
            notes["labels_raised_by_pm25_floor"]),
        "6. Stratified 80 / 20 train / test split ({} / {} rows), seed {}.".format(n_tr, n_te, SEED),
        "7. Training split only: {} extra rows copied from random training rows with just the dust value "
        "raised to 36-300 ug/m3 (labelled by the floor above). The raw data never has high dust with "
        "otherwise clean air, so without this the model would ignore dust on its own.".format(
            notes["augmented_rows"]),
        "8. 3 % of training values blanked at random, so the median imputer learns to cope with a failed sensor.",
        "",
        "## Model and training",
        "",
        "Three lightweight models were compared with 5-fold stratified cross-validation on the training "
        "split. The one with the best CV macro-F1 was selected. The test columns are shown for comparison "
        "only; the test set was not used to choose.",
        "",
        md_table(["Model", "CV macro-F1", "CV accuracy", "Test accuracy", "Test macro-F1"],
                 [[k, "{:.3f} +/- {:.3f}".format(*v["cv_f1"]), "{:.3f} +/- {:.3f}".format(*v["cv_acc"]),
                   "{:.3f}".format(v["test_acc"]), "{:.3f}".format(v["test_f1"])] for k, v in scores.items()]),
        "",
        "**Selected:** {}. Pipeline: median imputer -> classifier, `class_weight=\"balanced\"`. "
        "Inputs: `{}`.".format(best_name, ", ".join(FEATURES)),
        "",
        "## Evaluation (held-out test set)",
        "",
        "- Accuracy: **{:.3f}**".format(metrics["test_accuracy"]),
        "- Balanced accuracy: **{:.3f}**".format(metrics["test_balanced_accuracy"]),
        "- Macro-F1: **{:.3f}**".format(metrics["test_macro_f1"]),
        "- DANGER recall: **{:.3f}**; DANGER rows predicted SAFE: **{:.2%}**".format(
            metrics["danger_recall"], metrics["danger_missed_as_safe"]),
        "",
        md_table(["Class", "Precision", "Recall", "F1", "Support"], per_class),
        "",
        "Confusion matrix (rows = true, columns = predicted):",
        "",
        md_table(["", "SAFE", "WARNING", "DANGER"], [[c] + list(cm[i]) for i, c in enumerate(CLASSES)]),
        "",
        "Macro-F1 when one sensor is missing (its value is imputed):",
        "",
        md_table(["Missing sensor", "Macro-F1"],
                 [[k, "{:.3f}".format(v)] for k, v in metrics["macro_f1_without_sensor"].items()]),
        "",
    ]
    if "feature_importances" in metrics:
        lines += ["Feature importances: " + ", ".join(
            "{} {:.2f}".format(k, v) for k, v in metrics["feature_importances"].items()), ""]
    lines += [
        "## Limitations",
        "",
        "- The labels describe general air quality, not asthma attacks. The model is a screening aid, "
        "not a medical device.",
        "- The dataset is regional outdoor data; the Pico measures one indoor spot with low-cost sensors. "
        "The MQ-135 index and the Sharp dust reading are only as good as their calibration in `pico/config.py`.",
        "- Retrain on your own logged readings (`data/readings_log.csv`) once you have reviewed labels.",
        "",
    ]
    (out_dir / "model_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
