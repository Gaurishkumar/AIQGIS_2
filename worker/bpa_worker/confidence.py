"""Record confidence and routing (Final MVP Specification, Section 6).

Before calibration the confidence is a transparent product of the identity
probability, a geometry-quality term and a penalty per soft flag. Once a
record-confidence model is fitted (calibration.py) its logistic output is used.

Routing: AUTO_ACCEPT needs confidence >= T_auto, no soft flags, a clear
identity and not the no-candidate path. In shadow mode (pilot) such records
are logged as "would auto-accept" and sent to REVIEW. A failed hard gate or a
missing proposal routes to MANUAL.
"""

import hashlib
import math

from bpa_common import constants as C
from bpa_common.config import get

SIGNALS = ("identity_prob", "sam_iou", "stability", "footprint_agreement", "has_sam", "n_flags")


def signal_vector(signals):
    return {
        "identity_prob": float(signals.get("identity_prob") or 0.0),
        "sam_iou": float(signals.get("sam_iou") or 0.0),
        "stability": float(signals.get("stability") or 0.0),
        "footprint_agreement": float(signals.get("footprint_agreement") or 0.0),
        "has_sam": 1.0 if signals.get("sam_iou") is not None else 0.0,
        "n_flags": float(signals.get("n_flags") or 0.0),
    }


def record_confidence(signals, cfg, calibration=None):
    """signals: identity_prob, sam_iou, stability, footprint_agreement, n_flags, dataset."""
    model = (calibration or {}).get("confidence")
    if model:
        vec = signal_vector(signals)
        z = float(model["intercept"]) + sum(float(w) * vec.get(f, 0.0) for f, w in zip(model["features"], model["coef"]))
        return 1.0 / (1.0 + math.exp(-z))
    cc = get(cfg, "confidence", {}) or {}
    p_id = float(signals.get("identity_prob") or 0.0)
    seg = [signals.get(k) for k in ("sam_iou", "stability", "footprint_agreement") if signals.get(k) is not None]
    if signals.get("sam_iou") is not None and seg:
        q = sum(float(s) for s in seg) / len(seg)
    else:
        priors = cc.get("geometry_prior", {}) or {}
        q = float(priors.get(signals.get("dataset") or "", priors.get("default", 0.75)))
    penalty = float(cc.get("flag_penalty", 0.85)) ** int(signals.get("n_flags") or 0)
    return max(0.0, min(1.0, p_id * q * penalty))


def _audit_pick(survey_id, rate):
    if rate <= 0:
        return False
    h = int(hashlib.sha256(survey_id.encode("utf-8")).hexdigest()[:8], 16) / float(0xFFFFFFFF)
    return h < rate


def route(survey_id, confidence, hard_failures, soft_flags, decision, path, has_proposal, cfg):
    """Return (route, would_route, reason)."""
    rc = get(cfg, "routing", {}) or {}
    if not has_proposal:
        return C.ROUTE_MANUAL, C.ROUTE_MANUAL, "No proposal could be produced"
    if hard_failures:
        return C.ROUTE_MANUAL, C.ROUTE_MANUAL, "Hard gate failed: %s" % ", ".join(hard_failures)
    t_auto = float(rc.get("t_auto", 0.9))
    would_auto = (confidence >= t_auto and not soft_flags and decision == C.IDENTITY_CLEAR
                  and path != "no_candidate")
    would = C.ROUTE_AUTO_ACCEPT if would_auto else C.ROUTE_REVIEW
    if would == C.ROUTE_REVIEW:
        return C.ROUTE_REVIEW, would, "Operator review"
    if rc.get("shadow_mode", True) or not rc.get("auto_accept_enabled", False):
        return C.ROUTE_REVIEW, would, "Shadow mode: would auto-accept, sent to review"
    if _audit_pick(survey_id, float(rc.get("audit_sample_rate", 0.05))):
        return C.ROUTE_REVIEW, would, "Random audit sample of auto-accepted records"
    return C.ROUTE_AUTO_ACCEPT, would, "Auto-accepted"
