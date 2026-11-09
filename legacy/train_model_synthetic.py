#!/usr/bin/env python3
"""train_model.py - PRJ-004 asthma-risk model trainer.

Generates a synthetic environmental dataset (temperature, humidity, PM2.5-equivalent dust
density, air-quality index) for metro-station scenarios, labels each row Safe / Warning /
Danger, and trains a Random Forest classifier.

IMPORTANT - what this model is and is not
  * The data is SYNTHETIC. The labels come from the engineering rule set in `risk_score()`
    below (US EPA-style PM2.5 breakpoints plus comfort and humidity terms, with noise).
    The model therefore learns that rule set, and its test scores say how well it learned
    it, not how well it predicts real asthma events.
  * It is not clinically validated. Replace the generator with logged station data
    (station_log.db) and clinician-reviewed labels before any real-world use.

Usage:
    python train_model.py                      # 20,000 rows, writes model.joblib
    python train_model.py --samples 50000 --seed 7 --out model.joblib
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.pipeline import Pipeline

FEATURES = ["temperature_c", "humidity_pct", "dust_ugm3", "aqi_proxy"]
CLASSES = ["SAFE", "WARNING", "DANGER"]

# name, share of rows, then (mean, sd) of temperature / humidity / aqi and the dust
# distribution (lognormal: median, sigma). Rough metro-platform situations.
SCENARIOS = [
    # name                 share  temp        hum         dust(med,sig)  aqi
    ("normal_clean",       0.42, (26, 3.5),  (50, 12),   (10, 0.55),    (55, 25)),
    ("rush_hour",          0.20, (29, 3.0),  (58, 10),   (24, 0.50),    (110, 40)),
    ("dust_storm_ingress", 0.16, (34, 4.0),  (22, 8),    (85, 0.55),    (150, 60)),
    ("ventilation_fault",  0.10, (33, 3.0),  (70, 10),   (38, 0.50),    (260, 70)),
    ("hot_humid",          0.06, (38, 3.0),  (80, 8),    (18, 0.50),    (100, 40)),
    ("cold_dry",           0.06, (12, 4.0),  (25, 8),    (18, 0.50),    (60, 25)),
]


def generate_features(n, rng):
    shares = np.array([s[1] for s in SCENARIOS])
    shares = shares / shares.sum()
    which = rng.choice(len(SCENARIOS), size=n, p=shares)
    temp = np.empty(n)
    hum = np.empty(n)
    dust = np.empty(n)
    aqi = np.empty(n)
    for i, (_, _, t, h, d, a) in enumerate(SCENARIOS):
        m = which == i
        k = int(m.sum())
        temp[m] = rng.normal(t[0], t[1], k)
        hum[m] = rng.normal(h[0], h[1], k)
        dust[m] = rng.lognormal(np.log(d[0]), d[1], k)
        aqi[m] = rng.normal(a[0], a[1], k)
    # physical coupling: more dust usually means a worse gas index, and very dry air
    # resuspends more dust
    aqi += 0.35 * (dust - np.median(dust))
    dust *= 1.0 + 0.4 * np.clip((30.0 - hum) / 30.0, 0, 1)
    df = pd.DataFrame({
        "temperature_c": np.clip(temp, -5, 55).round(1),
        "humidity_pct": np.clip(hum, 5, 100).round(1),
        "dust_ugm3": np.clip(dust, 0, 1000).round(1),
        "aqi_proxy": np.clip(aqi, 0, 500).round(0),
    })
    df["scenario"] = [SCENARIOS[i][0] for i in which]
    return df


def risk_score(df):
    """Domain rule set that defines the synthetic labels (documented assumptions)."""
    dust = np.interp(df["dust_ugm3"], [0, 12, 35.5, 55.5, 150, 300], [0, 0, 1.0, 2.0, 3.5, 5.0])
    gas = np.interp(df["aqi_proxy"], [0, 100, 150, 300, 500], [0, 0, 0.8, 2.2, 4.0])
    hum = np.interp(df["humidity_pct"], [5, 20, 30, 80, 90, 100], [1.2, 0.8, 0, 0, 0.7, 1.2])
    temp = np.interp(df["temperature_c"], [-5, 5, 15, 32, 38, 45], [1.0, 0.6, 0, 0, 0.8, 1.5])
    score = dust + 0.8 * gas + hum + temp
    score += 0.4 * ((dust > 0.5) & (df["humidity_pct"] < 30))        # dry dust resuspension
    score += 0.3 * ((dust > 0.5) & (df["aqi_proxy"] > 150))          # dust + gases together
    return score.to_numpy()


def label(score, rng):
    noisy = score + rng.normal(0, 0.25, len(score))                  # person / sensor variability
    return np.where(noisy >= 2.5, 2, np.where(noisy >= 1.0, 1, 0))


def mask_missing(df, rng, p=0.04):
    """Randomly blank values so the imputer learns the medians the server will use."""
    out = df.copy()
    for col in FEATURES:
        out.loc[rng.random(len(out)) < p, col] = np.nan
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="model.joblib")
    ap.add_argument("--dataset", default="synthetic_dataset.csv", help="CSV of the data (use '' to skip)")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    clean = generate_features(args.samples, rng)
    y = label(risk_score(clean), rng)
    data = mask_missing(clean, rng)
    data["risk"] = y
    if args.dataset:
        data.assign(risk_name=[CLASSES[i] for i in y]).to_csv(args.dataset, index=False)

    share = np.bincount(y, minlength=3) / len(y)
    print("Rows: {}   SAFE {:.1%}  WARNING {:.1%}  DANGER {:.1%}".format(len(y), *share))

    X = data[FEATURES]
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, stratify=y, random_state=args.seed)
    model = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("rf", RandomForestClassifier(n_estimators=250, min_samples_leaf=3, class_weight="balanced",
                                      n_jobs=-1, random_state=args.seed)),
    ])
    cv = cross_val_score(model, X_tr, y_tr, cv=5, scoring="f1_macro")
    model.fit(X_tr, y_tr)
    pred = model.predict(X_te)
    report = classification_report(y_te, pred, target_names=CLASSES, output_dict=True, zero_division=0)
    print("\n5-fold CV macro-F1 (train): {:.3f} +/- {:.3f}".format(cv.mean(), cv.std()))
    print("\nHold-out test report:")
    print(classification_report(y_te, pred, target_names=CLASSES, digits=3, zero_division=0))
    cm = confusion_matrix(y_te, pred)
    print("Confusion matrix (rows = true, columns = predicted: SAFE, WARNING, DANGER):")
    print(cm)
    importances = dict(zip(FEATURES, model.named_steps["rf"].feature_importances_.round(4).tolist()))
    print("\nFeature importances:", importances)
    # a Danger called Safe is the costly mistake: report it separately
    danger = y_te == 2
    missed = float(((pred == 0) & danger).sum()) / max(1, int(danger.sum()))
    print("DANGER rows predicted SAFE: {:.2%}".format(missed))

    print("\nSanity cases:")
    cases = pd.DataFrame([
        [26, 50, 8, 50], [30, 55, 40, 140], [34, 20, 120, 220], [33, 70, 45, 280], [24, 45, np.nan, 60],
    ], columns=FEATURES)
    for row, p in zip(cases.itertuples(index=False), model.predict(cases)):
        print("  T={:>4} RH={:>4} dust={:>5} aqi={:>4}  ->  {}".format(*row, CLASSES[p]))

    bundle = {
        "pipeline": model,
        "features": FEATURES,
        "classes": CLASSES,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sklearn_version": sklearn.__version__,
        "n_samples": int(args.samples),
        "synthetic": True,
        "metrics": {"cv_macro_f1": round(float(cv.mean()), 4),
                    "test_macro_f1": round(float(report["macro avg"]["f1-score"]), 4),
                    "danger_missed_as_safe": round(missed, 4),
                    "feature_importances": importances},
    }
    joblib.dump(bundle, args.out)
    Path(args.out).with_suffix(".json").write_text(json.dumps(bundle["metrics"], indent=2))
    print("\nSaved model to {} (scikit-learn {})".format(args.out, sklearn.__version__))
    print("Note: labels are synthetic, so these scores describe the rule set, not real-world accuracy.")


if __name__ == "__main__":
    sys.exit(main())
