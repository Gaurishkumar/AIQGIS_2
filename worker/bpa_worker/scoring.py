"""S4 - Candidate scoring and identity decision.

Initial score (before calibration):
    score = 0.40 s_dist + 0.20 s_name + 0.10 s_addr + 0.15 s_road + 0.10 s_plaus + 0.05 s_source
IDENTITY_CLEAR: top >= 0.70 and beats the second by >= 0.15, or a historic
polygon contains the point / lies within 5 m. Otherwise IDENTITY_AMBIGUOUS;
no candidates -> NO_CANDIDATE.

When a fitted identity model exists (calibration.py) its logistic
probabilities replace the hand-set weights, normalised within the record.
"""

import math

from bpa_common import constants as C
from bpa_common.config import get

from .candidates import FEATURES


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class Scorer(object):
    def __init__(self, cfg, calibration=None):
        self.cfg = cfg
        self.weights = {k: float(v) for k, v in (get(cfg, "scoring.weights", {}) or {}).items()}
        dec = get(cfg, "scoring.decision", {}) or {}
        self.min_top = float(dec.get("min_top_score", 0.70))
        self.min_margin = float(dec.get("min_margin", 0.15))
        self.hist_dist = float(dec.get("historic_clear_distance_m", 5.0))
        self.on_road_min_name = dec.get("on_road_min_name")
        self.far_override_below = float(dec.get("far_name_override_below", 0.45))
        self.model = (calibration or {}).get("identity")

    def score(self, cands):
        for c in cands:
            c.score = sum(self.weights.get(k, 0.0) * float(c.features.get(k, 0.0)) for k in FEATURES)
        cands.sort(key=lambda c: (-c.score, c.evidence.get("distance_m", 1e9)))
        if self.model:
            self._apply_model(cands)
        # Buildings found by name far from the point are offered, but only win when
        # nothing near the point is a reasonable match.
        local = [c for c in cands if not c.evidence.get("found_by_name")]
        far = [c for c in cands if c.evidence.get("found_by_name")]
        if local and far and local[0].score >= self.far_override_below:
            cands[:] = local + far
        for i, c in enumerate(cands, start=1):
            c.rank = i
        return cands

    def _apply_model(self, cands):
        feats = self.model["features"]
        coef = self.model["coef"]
        b = float(self.model["intercept"])
        raw = []
        for i, c in enumerate(cands):
            nxt = cands[i + 1].score if i + 1 < len(cands) else 0.0
            vals = dict(c.features)
            vals["margin"] = c.score - nxt if i == 0 else c.score - cands[0].score
            raw.append(_sigmoid(b + sum(w * float(vals.get(f, 0.0)) for f, w in zip(feats, coef))))
        total = sum(raw) or 1.0
        for c, p in zip(cands, raw):
            c.probability = p / total if self.model.get("normalise", True) else p
        cands.sort(key=lambda c: (-(c.probability or 0.0), -c.score))

    def decide(self, cands, on_road=None):
        """Return (decision, chosen_candidate, identity_probability, reason).

        ``on_road``: name of the road the survey point stands on. From a point on
        a carriageway geometry cannot tell which side's building is meant, so the
        decision is only clear with name evidence (or a historic polygon).
        """
        decision, chosen, p, reason = self._decide(cands)
        if (decision == C.IDENTITY_CLEAR and on_road and self.on_road_min_name is not None
                and chosen.source != "historic"
                and float(chosen.features.get("s_name", 0.0)) < float(self.on_road_min_name)):
            return (C.IDENTITY_AMBIGUOUS, chosen, p,
                    "Survey point is on %s: the building side cannot be told from position alone" % on_road)
        return decision, chosen, p, reason

    def _decide(self, cands):
        if not cands:
            return C.NO_CANDIDATE, None, 0.0, "No footprint within the search radius"
        hist = [c for c in cands if c.source == "historic" and c.evidence.get("distance_m", 1e9) <= self.hist_dist]
        if hist:
            best = min(hist, key=lambda c: (c.evidence.get("distance_m", 0.0), -c.score))
            p = float(get(self.cfg, "confidence.historic_identity_prob", 0.95))
            how = "contains the survey point" if best.evidence.get("inside") else "lies within %.0f m" % self.hist_dist
            return C.IDENTITY_CLEAR, best, p, "Historic polygon %s" % how

        top = cands[0]
        second = cands[1] if len(cands) > 1 else None
        if self.model:
            p_top = float(top.probability or 0.0)
            p_2 = float(second.probability or 0.0) if second else 0.0
            thr = float(self.model.get("threshold", self.min_top))
            mar = float(self.model.get("margin", self.min_margin))
            if p_top >= thr and p_top - p_2 >= mar:
                return C.IDENTITY_CLEAR, top, p_top, "Calibrated probability %.2f (next %.2f)" % (p_top, p_2)
            return C.IDENTITY_AMBIGUOUS, top, p_top, "Calibrated probability %.2f vs next %.2f" % (p_top, p_2)

        s2 = second.score if second else 0.0
        margin = top.score - s2
        p = self.identity_probability(top.score, margin)
        if top.score >= self.min_top and margin >= self.min_margin:
            return C.IDENTITY_CLEAR, top, p, "Top score %.2f beats next by %.2f" % (top.score, margin)
        if top.score < self.min_top:
            reason = "Best score %.2f is below %.2f" % (top.score, self.min_top)
        else:
            reason = "Top two candidates are close (%.2f vs %.2f)" % (top.score, s2)
        return C.IDENTITY_AMBIGUOUS, top, p, reason

    def identity_probability(self, top_score, margin):
        """Uncalibrated identity probability: the top score, discounted when the margin is thin."""
        m = max(0.0, min(1.0, margin / self.min_margin)) if self.min_margin > 0 else 1.0
        return max(0.0, min(1.0, top_score * (0.5 + 0.5 * m)))
