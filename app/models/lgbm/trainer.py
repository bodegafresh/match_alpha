"""
LightGBM trainer — Phase 3.

Trains a binary classifier (HOME win probability) on labeled feature_snapshots.
Requires 200+ settled matches. Training runs OFFLINE (not in the API process).

Guard: n < 200 → abort with WARN.
Feature leakage guard: dataset_builder enforces as_of < kickoff_at.
"""
from __future__ import annotations

import json
import pickle
import tempfile
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
from sklearn.model_selection import train_test_split

MIN_SAMPLES = 200

FEATURE_COLS = [
    "home_elo_global",
    "home_elo_diff",
    "home_attack_strength",
    "home_defense_strength",
    "home_form_points",
    "home_form_gd",
    "home_rest_days",
    "away_elo_global",
    "away_attack_strength",
    "away_defense_strength",
    "away_form_points",
    "away_form_gd",
    "away_rest_days",
    "stage_pressure",
    "is_neutral",
    "feature_completeness",
]

LGB_PARAMS = {
    "objective": "binary",
    "metric": ["binary_logloss", "auc"],
    "num_leaves": 31,
    "learning_rate": 0.05,
    "n_estimators": 300,
    "min_child_samples": 20,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_alpha": 0.1,
    "reg_lambda": 0.1,
    "verbose": -1,
    "random_state": 42,
}


def train_lgbm(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Train LightGBM on labeled rows from dataset_builder.build_dataset().
    Returns result dict with model bytes (pickled), metrics, and feature importances.
    """
    n = len(rows)
    if n < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient_samples", "n": n}

    X = np.array([[float(r.get(f) or 0) for f in FEATURE_COLS] for r in rows])
    y = np.array([int(r["outcome"]) for r in rows])

    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    model = lgb.LGBMClassifier(**LGB_PARAMS)
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(period=-1)],
    )

    val_probs = model.predict_proba(X_val)[:, 1]
    brier = float(np.mean((val_probs - y_val) ** 2))
    logloss = float(-np.mean(
        y_val * np.log(np.clip(val_probs, 1e-12, 1))
        + (1 - y_val) * np.log(np.clip(1 - val_probs, 1e-12, 1))
    ))

    importances = dict(zip(FEATURE_COLS, model.feature_importances_.tolist(), strict=True))

    model_bytes = pickle.dumps(model)

    return {
        "ok": True,
        "n_train": len(X_train),
        "n_val": len(X_val),
        "brier_score": round(brier, 6),
        "log_loss": round(logloss, 6),
        "best_iteration": int(model.best_iteration_),
        "feature_importances": importances,
        "model_bytes": model_bytes,
    }


def predict_lgbm(model_bytes: bytes, feature_row: dict[str, Any]) -> float:
    """Run inference on a single feature row. Returns HOME win probability."""
    model = pickle.loads(model_bytes)
    X = np.array([[float(feature_row.get(f) or 0) for f in FEATURE_COLS]])
    prob = model.predict_proba(X)[0][1]
    return round(float(prob), 6)
