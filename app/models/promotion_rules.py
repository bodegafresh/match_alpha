from dataclasses import dataclass


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool
    reasons: list[str]


def should_promote_challenger(
    *,
    champion_brier: float | None,
    challenger_brier: float | None,
    champion_log_loss: float | None,
    challenger_log_loss: float | None,
    challenger_ece: float | None,
    max_ece: float,
    sample_size: int,
    min_sample_size: int,
    severe_drift_open: bool,
    paper_roi_delta: float | None = None,
    clv_delta: float | None = None,
) -> PromotionDecision:
    reasons: list[str] = []
    if sample_size < min_sample_size:
        reasons.append("sample_size_below_minimum")
    if severe_drift_open:
        reasons.append("severe_drift_open")
    if challenger_ece is None or challenger_ece > max_ece:
        reasons.append("ece_above_threshold")
    brier_better = champion_brier is not None and challenger_brier is not None and challenger_brier < champion_brier
    log_loss_better = champion_log_loss is not None and challenger_log_loss is not None and challenger_log_loss < champion_log_loss
    if not (brier_better or log_loss_better):
        reasons.append("challenger_not_better_than_champion")
    if paper_roi_delta is not None and paper_roi_delta < 0:
        reasons.append("paper_roi_worse_than_baseline")
    if clv_delta is not None and clv_delta < 0:
        reasons.append("clv_worse_than_baseline")
    return PromotionDecision(promote=not reasons, reasons=reasons)

