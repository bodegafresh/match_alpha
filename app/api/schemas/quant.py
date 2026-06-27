from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EVOpportunity(BaseModel):
    betting_decision_id: str
    competition_season_id: str
    match_id: str
    decision_status: str
    risk_level: str | None = None
    block_reason: str | None = None
    block_reasons: list[str] = []
    edge: float | None = None
    ev: float | None = None
    kelly_fraction: float | None = None
    stake_fraction: float | None = None
    model_probability: float | None = None
    market_probability: float | None = None
    decided_at: datetime | None = None
    market_code: str
    selection_code: str
    raw_probability: float | None = None
    calibrated_probability: float | None = None
    prediction_status: str | None = None
    confidence_score: float | None = None
    explanation: dict[str, Any] | None = None
    decimal_odds: float | None = None
    market_implied_probability: float | None = None
    odds_captured_at: datetime | None = None
    odds_age_minutes: float | None = None
    kickoff_at: datetime | None = None
    match_status: str | None = None
    home_team_name: str | None = None
    home_country_code: str | None = None
    home_flag_emoji: str | None = None
    away_team_name: str | None = None
    away_country_code: str | None = None
    away_flag_emoji: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    model_family: str | None = None


class BlockedDecision(BaseModel):
    betting_decision_id: str
    competition_season_id: str
    match_id: str
    decision_status: str
    risk_level: str | None = None
    block_reason: str | None = None
    block_reasons: list[str] = []
    edge: float | None = None
    ev: float | None = None
    decided_at: datetime | None = None
    market_code: str
    selection_code: str
    raw_probability: float | None = None
    calibrated_probability: float | None = None
    prediction_status: str | None = None
    confidence_score: float | None = None
    kickoff_at: datetime | None = None
    home_team_name: str | None = None
    home_flag_emoji: str | None = None
    away_team_name: str | None = None
    away_flag_emoji: str | None = None


class CalibrationSummary(BaseModel):
    calibration_run_id: str
    model_name: str | None = None
    model_version: str | None = None
    competition_season_id: str
    market_code: str
    stage_type: str | None = None
    method: str | None = None
    sample_size: int | None = None
    brier_score: float | None = None
    log_loss: float | None = None
    ece: float | None = None
    sharpness: float | None = None
    created_at: datetime | None = None


class ModelDiagnostic(BaseModel):
    model_id: str
    model_name: str
    model_version: str
    model_family: str | None = None
    champion_status: str | None = None
    run_count: int = 0
    prediction_count: int = 0
    last_finished_at: datetime | None = None
    last_metric_at: datetime | None = None
    last_drift_detected_at: datetime | None = None
    severe_drift_reports: int = 0


class BankrollPoint(BaseModel):
    betting_decision_id: str
    decided_at: datetime | None = None
    decision_status: str
    ev: float | None = None
    edge: float | None = None
    kelly_fraction: float | None = None
    stake_fraction: float | None = None
    settlement_status: str | None = None
    settlement_profit_units: float | None = None
    market_code: str | None = None
    selection_code: str | None = None


class ROIByEVBucket(BaseModel):
    ev_bucket: str          # e.g. "0-1%", "1-3%", "3-5%", "5%+"
    ev_min: float
    ev_max: float | None    # None = open-ended (5%+)
    count: int
    settled_count: int
    roi_pct: float | None   # None if no settled picks in bucket
    avg_ev: float | None
    avg_edge: float | None
