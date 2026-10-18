"""Per-record pipeline (S2-S10) and the record state machine.

Paths (Documentation 7.1):
  historic match / footprint  -> footprint proposal (refined with SAM when licensed imagery exists)
  ambiguous + imagery         -> imagery + SAM from the top candidate
  ambiguous, no imagery       -> top candidate shown for review with all candidates
  no candidate + imagery      -> automatic masks, nearest building-shaped mask, always reviewed
  no candidate, no imagery    -> MANUAL
"""

import json
import logging
import math
import os
import threading
import traceback

import cv2
import numpy as np
from shapely.geometry import box, mapping

from bpa_common import constants as C
from bpa_common.config import config_hash, get

from . import confidence as conf_mod
from .candidates import CandidateGenerator, search_radius
from .coords import corrected_coordinate
from .geo import aligned_iou, compactness, iou, point_utm, to_wgs84, utm_epsg_for
from .imagery import (ImageryUnavailable, LicenceGateClosed, aoi_bounds, enlarge_bounds, read_crop, Crop)
from .polygonize import mask_to_polygon, touches_edge
from .scoring import Scorer
from .segmentation import build_prompts, run_variants
from .store import now_iso
from .textmatch import TextMatcher
from .validation import validate

log = logging.getLogger(__name__)


class StateError(RuntimeError):
    pass


class RecordState(object):
    """Mutable processing state of one survey record."""

    def __init__(self, data):
        self.survey_id = data["survey_id"]
        self.row = data.get("row")
        self.latitude = data.get("latitude")
        self.longitude = data.get("longitude")
        self.fields = dict(data.get("fields") or {})
        self.source = dict(data.get("source") or {})
        self.warnings = list(data.get("warnings") or [])
        self.reasons = list(data.get("reasons") or [])
        self.state = data.get("state") or C.IMPORTED
        if self.state == C.INVALID or self.reasons:
            self.state = C.INVALID
        self.error_code = None
        self.error_message = None
        self.utm_epsg = None
        self.radius_m = None
        self.decision = None
        self.decision_reason = None
        self.identity_prob = None
        self.candidates = []
        self.chosen_candidate_id = None
        self.proposal = None
        self.alternatives = []
        self.segmentation = None
        self.imagery = None
        self.crop_path = None
        self.history = [(self.state, now_iso())]
        self.revision = 0
        self.name_found_far = []
        self.lock = threading.RLock()

    def transition(self, new_state):
        if new_state == self.state:
            return
        allowed = C.TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise StateError("%s: illegal transition %s -> %s" % (self.survey_id, self.state, new_state))
        self.state = new_state
        self.history.append((new_state, now_iso()))

    def as_input(self):
        return {"survey_id": self.survey_id, "latitude": self.latitude, "longitude": self.longitude,
                "fields": self.fields, "warnings": self.warnings}

    def candidate(self, candidate_id):
        for c in self.candidates:
            if c.candidate_id == candidate_id:
                return c
        return None

    # -- serialisation -----------------------------------------------------
    def summary(self):
        p = self.proposal or {}
        return {"survey_id": self.survey_id, "row": self.row, "state": self.state,
                "route": p.get("route"), "would_route": p.get("would_route"),
                "confidence": p.get("confidence"), "method": p.get("method"),
                "decision": self.decision, "error_code": self.error_code,
                "n_candidates": len(self.candidates), "flags": p.get("flags", []),
                "revision": self.revision}

    def detail(self):
        p = self.proposal
        prop = None
        if p:
            prop = {k: v for k, v in p.items() if k not in ("geometry", "geometry_utm")}
            prop["geometry"] = mapping(p["geometry"]) if p.get("geometry") is not None else None
            prop["flag_text"] = [C.FLAG_TEXT.get(f, f) for f in p.get("flags", []) + p.get("hard_failures", [])]
        cands = []
        for c in self.candidates:
            d = c.to_dict()
            d["geometry"] = mapping(c.geom)
            d["chosen"] = c.candidate_id == self.chosen_candidate_id
            cands.append(d)
        return {
            "survey_id": self.survey_id, "row": self.row, "state": self.state,
            "latitude": self.latitude, "longitude": self.longitude, "utm_epsg": self.utm_epsg,
            "radius_m": self.radius_m, "fields": self.fields, "warnings": self.warnings,
            "reasons": self.reasons, "error_code": self.error_code, "error_message": self.error_message,
            "decision": self.decision, "decision_reason": self.decision_reason,
            "identity_prob": self.identity_prob, "chosen_candidate_id": self.chosen_candidate_id,
            "candidates": cands, "proposal": prop, "alternatives": self.alternatives,
            "segmentation": self.segmentation, "imagery": self.imagery, "crop_path": self.crop_path,
            "name_found_far": self.name_found_far,
            "history": self.history[-30:], "revision": self.revision,
        }


class Pipeline(object):
    def __init__(self, cfg, reference, imagery, segmenter, calibration=None):
        self.cfg = cfg
        self.ref = reference
        self.imagery = imagery
        self.seg = segmenter
        self.calibration = calibration or {}
        self.text = TextMatcher(cfg)
        self.gen = CandidateGenerator(cfg, reference, self.text)
        self.scorer = Scorer(cfg, self.calibration)
        self.cfg_version = str(get(cfg, "config_version", ""))
        self.cfg_hash = config_hash(cfg)

    # ------------------------------------------------------------------
    def versions(self):
        return {"config_version": self.cfg_version, "config_hash": self.cfg_hash,
                "model_version": self.seg.model_version if self.seg else None,
                "manifest_hash": self.ref.manifest_hash if self.ref else None,
                "overture_release": self.ref.overture_release if self.ref else None}

    def _fail(self, rec, ctx, code, exc):
        rec.error_code = code
        rec.error_message = "%s: %s" % (type(exc).__name__, exc)
        log.error("%s failed (%s): %s\n%s", rec.survey_id, code, exc, traceback.format_exc())
        try:
            rec.transition(C.FAILED)
        except StateError:
            rec.state = C.FAILED
            rec.history.append((C.FAILED, now_iso()))
        rec.proposal = None
        self._persist(rec, ctx)

    def _persist(self, rec, ctx):
        store = ctx.get("store")
        if store is None:
            return
        store.write_record(rec, self.cfg_version, self.cfg_hash)
        store.write_proposal(rec.survey_id, rec.proposal, self.versions())

    # ------------------------------------------------------------------
    def process(self, rec, ctx, forced_candidate_id=None):
        """Run one record end to end. ``ctx``: store, batch_id, cache_dir."""
        with rec.lock:
            if rec.state == C.INVALID:
                self._persist(rec, ctx)
                return rec
            if rec.state in (C.IMPORTED, C.FAILED):
                rec.transition(C.QUEUED)
            rec.transition(C.MATCHING)
            rec.error_code = rec.error_message = None
            rec.revision += 1
            store = ctx.get("store")
            started = now_iso()
            if self.ref is not None and self.ref.covers(rec.longitude, rec.latitude) is False:
                # Never mistake "no data downloaded here" for "no building here".
                if store is not None:
                    store.job(ctx.get("batch_id"), rec.survey_id, "MATCHING", "FAILED", started,
                              "REFERENCE_NOT_COVERED")
                self._fail(rec, ctx, "REFERENCE_NOT_COVERED", LookupError(
                    "Survey point is outside the downloaded reference data; run prepare-reference "
                    "for this workbook, then retry"))
                return rec
            try:
                epsg = utm_epsg_for(rec.longitude, rec.latitude)
                rec.utm_epsg = epsg
                radius = search_radius(self.cfg, self.calibration)
                rec.radius_m = radius
                cands, gctx = self.gen.generate(rec.as_input(), epsg, radius)
                self.scorer.score(cands)
                decision, chosen, p_id, reason = self.scorer.decide(cands, gctx.get("on_road"))
                if forced_candidate_id:
                    forced = next((c for c in cands if c.candidate_id == forced_candidate_id), None)
                    if forced is None:
                        raise ValueError("Candidate %s is not near this record" % forced_candidate_id)
                    decision, chosen, p_id = C.IDENTITY_CLEAR, forced, 1.0
                    reason = "Selected by the operator"
                rec.candidates = cands
                rec.decision, rec.decision_reason, rec.identity_prob = decision, reason, round(p_id, 4)
                rec.chosen_candidate_id = chosen.candidate_id if chosen else None
                rec.alternatives, rec.segmentation, rec.imagery = [], None, None
                if store is not None:
                    store.write_candidates(rec.survey_id, cands)
                    store.job(ctx.get("batch_id"), rec.survey_id, "MATCHING", "DONE", started)
            except Exception as exc:
                if store is not None:
                    store.job(ctx.get("batch_id"), rec.survey_id, "MATCHING", "FAILED", started, "MATCHING_ERROR")
                self._fail(rec, ctx, "MATCHING_ERROR", exc)
                return rec

            pt = gctx["point_utm"]
            neighbours = [g for (_, g, _) in gctx.get("neighbours", [])]
            neighbours += [c.geom_utm for c in cands if c.source == "historic"]
            approved = self._approved_near(rec, chosen, gctx)
            base_ctx = {"point_utm": pt, "radius_m": radius, "fields": rec.fields, "approved_utm": approved}
            if chosen is not None and chosen.evidence.get("found_by_name"):
                # Found by name further away on purpose: the far-from-point gate must not reject it.
                base_ctx["crs_limit_m"] = float(chosen.evidence.get("distance_m") or 0.0) + 2.0 * radius
            rec.name_found_far = gctx.get("name_found_far") or []
            try:
                if decision == C.IDENTITY_CLEAR:
                    rec.transition(C.FOOTPRINT_READY)
                    options = [self._footprint_option(chosen)]
                    crop = self._get_crop(rec, ctx, chosen.geom_utm, epsg)
                    if crop is not None and self._too_coarse(rec, chosen, crop):
                        options[0].setdefault("notes", []).append(C.FLAG_IMAGERY_TOO_COARSE)
                        crop = None
                    if crop is not None:
                        rec.transition(C.SEGMENTING)
                        try:
                            options.append(self._segment_candidate(rec, ctx, crop, chosen, neighbours,
                                                                   C.METHOD_FOOTPRINT_SAM))
                        except Exception as exc:
                            log.warning("%s: SAM refinement failed: %s", rec.survey_id, exc)
                            options[0].setdefault("notes", []).append(C.FLAG_SAM_REFINEMENT_FAILED)
                            rec.transition(C.POLYGONIZING)
                    rec.transition(C.VALIDATING)
                    self._finalise(rec, ctx, options, chosen, base_ctx, "footprint")
                else:
                    rec.transition(C.NEEDS_IMAGERY)
                    aoi_geom = chosen.geom_utm if chosen else pt.buffer(radius)
                    crop = self._get_crop(rec, ctx, aoi_geom, epsg, circle=chosen is None)
                    coarse = crop is not None and chosen is not None and self._too_coarse(rec, chosen, crop)
                    if coarse:
                        crop = None
                    if crop is not None:
                        rec.transition(C.SEGMENTING)
                        if chosen is not None:
                            path = "imagery"
                            fallback = self._footprint_option(chosen)
                            try:
                                options = [self._segment_candidate(rec, ctx, crop, chosen, neighbours,
                                                                   C.METHOD_IMAGERY_SAM), fallback]
                            except Exception as exc:
                                log.warning("%s: segmentation failed, keeping the footprint: %s", rec.survey_id, exc)
                                fallback.setdefault("notes", []).append(C.FLAG_SAM_REFINEMENT_FAILED)
                                options = [fallback]
                        else:
                            path = "no_candidate"
                            try:
                                options = [o for o in [self._segment_auto(rec, ctx, crop, pt)] if o]
                            except Exception as exc:
                                self._fail(rec, ctx, "SEGMENTATION_ERROR", exc)
                                return rec
                        if rec.state == C.SEGMENTING:
                            rec.transition(C.POLYGONIZING)
                        rec.transition(C.VALIDATING)
                        self._finalise(rec, ctx, options, chosen, base_ctx, path)
                    else:
                        rec.transition(C.NO_IMAGERY_AVAILABLE)
                        options = [self._footprint_option(chosen)] if chosen is not None else []
                        if coarse:
                            options[0].setdefault("notes", []).append(C.FLAG_IMAGERY_TOO_COARSE)
                        self._finalise(rec, ctx, options, chosen, base_ctx,
                                       "no_imagery" if chosen is not None else "no_candidate")
            except Exception as exc:
                self._fail(rec, ctx, "PIPELINE_ERROR", exc)
                return rec
            self._persist(rec, ctx)
            if store is not None:
                store.job(ctx.get("batch_id"), rec.survey_id, "PIPELINE", rec.state, started)
            return rec

    # ------------------------------------------------------------------
    def _approved_near(self, rec, chosen, gctx):
        """Approved (historic) polygons near the record, excluding the chosen source polygon."""
        out = []
        for c in rec.candidates:
            if c.source == "historic" and (chosen is None or c.candidate_id != chosen.candidate_id):
                out.append(c.geom_utm)
        return out

    @staticmethod
    def _footprint_option(cand):
        method = C.METHOD_HISTORIC if cand.source == "historic" else C.METHOD_FOOTPRINT
        return {"geometry_utm": cand.geom_utm, "method": method, "is_segmented": False,
                "footprint_source": "%s:%s" % (cand.dataset, cand.source_id), "dataset": cand.dataset}

    def _too_coarse(self, rec, cand, crop):
        """True when the building is too few pixels wide in the crop for a trustworthy AI outline.

        A fixed resolution limit is not enough: a 200 m warehouse is fine at 0.6 m per pixel,
        a 6 m house is not. The building's short side must span imagery.min_building_px pixels.
        """
        min_px = float(get(self.cfg, "imagery.min_building_px", 20) or 0)
        if not min_px or not crop.gsd_m:
            return False
        rect = cand.geom_utm.minimum_rotated_rectangle
        xs, ys = rect.exterior.coords.xy
        sides = [math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(len(xs) - 1)]
        short = min(s for s in sides if s > 0) if any(s > 0 for s in sides) else 0.0
        px = short / float(crop.gsd_m)
        if px < min_px:
            rec.imagery = dict(rec.imagery or {}, status="TOO_COARSE",
                               note="building is %.0f px wide at %.2f m per pixel" % (px, crop.gsd_m))
            return True
        return False

    def _get_crop(self, rec, ctx, geom_utm, epsg, circle=False):
        """Return a Crop from the first licence-open provider covering the AOI, else None."""
        if self.imagery is None or not self.imagery.providers:
            rec.imagery = {"status": "NO_PROVIDERS_CONFIGURED"}
            return None
        if circle:
            bounds = aoi_bounds(geom_utm, 0.0, self.imagery.min_size_m)
        else:
            bounds = aoi_bounds(geom_utm, self.imagery.buffer_m, self.imagery.min_size_m)
        return self._crop_bounds(rec, ctx, bounds, epsg)

    def _crop_bounds(self, rec, ctx, bounds, epsg, suffix=""):
        aoi_wgs = to_wgs84(box(*bounds), epsg)
        provider = self.imagery.select(bounds, epsg)
        store = ctx.get("store")
        if provider is None:
            refused = [p for p in self.imagery.providers if not p.gate_open]
            if refused:
                rec.imagery = {"status": "REFUSED_OR_UNAVAILABLE",
                               "refused": [{"id": p.id, "reason": p.gate_reason()} for p in refused]}
            else:
                rec.imagery = {"status": "NO_COVERAGE"}
            return None
        if self.seg is None:
            # Worker started without the AI model: imagery cannot be used, footprints still work.
            rec.imagery = {"status": "MODEL_NOT_LOADED", "provider": provider.id}
            return None
        cache = os.path.join(ctx.get("cache_dir") or ".", _safe(rec.survey_id))
        path = os.path.join(cache, "crop%s.tif" % suffix)
        try:
            crop = provider.crop(bounds, epsg, path)
            status, err = "OK", ""
        except LicenceGateClosed as exc:
            crop, status, err = None, "REFUSED", str(exc)
        except (ImageryUnavailable, OSError, ValueError) as exc:
            crop, status, err = None, "UNAVAILABLE", str(exc)
        req = self.imagery.request_record(rec.survey_id, provider, crop, aoi_wgs, status, err)
        if store is not None:
            store.write_imagery_request(req, aoi_wgs)
        rec.imagery = {k: v for k, v in req.items() if k != "aoi"}
        if crop is not None:
            rec.crop_path = crop.path
        return crop

    def _save_mask(self, rec, ctx, crop, mask, name):
        cache = os.path.dirname(crop.path)
        path = os.path.join(cache, "mask_%s.png" % name)
        cv2.imwrite(path, (mask.astype(np.uint8) * 255))
        return path

    def _segment_candidate(self, rec, ctx, crop, cand, neighbours, method):
        """Prompt variants A/B/C from the candidate footprint -> regularised polygon."""
        others = [g for g in neighbours if not g.equals(cand.geom_utm)]
        retries = self.imagery.max_edge_retries
        attempt = 0
        while True:
            prompts = build_prompts(crop, cand.geom_utm, others, self.cfg)
            key = crop.path
            self.seg.forget(key)
            variants, stab = run_variants(self.seg, key, crop, prompts)
            polys = {}
            for n in ("A", "C", "B"):
                if variants[n]["pixels"] == 0:
                    continue
                poly_n, info_n = mask_to_polygon(variants[n]["mask"], crop, self.cfg,
                                                 positive_px=prompts[n]["pos"] or prompts["A"]["pos"],
                                                 candidate_utm=cand.geom_utm)
                if poly_n is not None:
                    shape_fit = aligned_iou(poly_n, cand.geom_utm)
                    polys[n] = (poly_n, info_n, shape_fit)
                    variants[n]["shape_fit"] = shape_fit
            if not polys:
                raise RuntimeError("No prompt variant produced a usable mask")
            # The final mask is the variant whose outline best matches the footprint's
            # shape once the footprint's own offset is allowed for. A box prompt taken
            # from a displaced footprint tends to pull in shadow or neighbouring ground,
            # so on a near tie the box-free variant C wins.
            best_fit = max(v[2] for v in polys.values())
            tie = float(get(self.cfg, "segmentation.variant_tie_tolerance", 0.02))
            chosen = next(n for n in ("C", "A", "B") if n in polys and polys[n][2] >= best_fit - tie)
            poly, info, _ = polys[chosen]
            if info.get("touches_edge") and attempt < retries:
                attempt += 1
                crop = self._crop_bounds(rec, ctx, enlarge_bounds(crop.bounds, self.imagery.enlarge_factor),
                                         crop.epsg, suffix="_r%d" % attempt) or crop
                continue
            break
        rec.transition(C.POLYGONIZING)
        mask_paths = {n: self._save_mask(rec, ctx, crop, v["mask"], n) for n, v in variants.items()}
        with open(os.path.join(os.path.dirname(crop.path), "prompts.json"), "w", encoding="utf-8") as fh:
            json.dump({"candidate_id": cand.candidate_id, "variants": prompts, "chosen": chosen,
                       "crop": os.path.basename(crop.path)}, fh, indent=1)
        store = ctx.get("store")
        if store is not None:
            store.write_segmentation(rec.survey_id, self.seg.model_version,
                                     {n: {"sam_iou": v["sam_iou"], "pixels": v["pixels"]} for n, v in variants.items()},
                                     stab, chosen, mask_paths)
        rec.segmentation = {"variants": {n: {"sam_iou": round(v["sam_iou"], 4), "pixels": v["pixels"],
                                             "shape_fit": round(v.get("shape_fit", 0.0), 4)}
                                         for n, v in variants.items()},
                            "stability": round(stab, 4), "chosen_variant": chosen,
                            "model_version": self.seg.model_version, "polygonize": info,
                            "crop_path": crop.path, "gsd_m": crop.gsd_m}
        if poly is None:
            raise RuntimeError("Mask could not be converted to a polygon: %s" % info.get("error"))
        return {"geometry_utm": poly, "method": method, "is_segmented": True,
                "touches_edge": info.get("touches_edge", False), "sam_iou": variants[chosen]["sam_iou"],
                "stability": stab, "footprint_utm": cand.geom_utm,
                "footprint_source": "%s:%s" % (cand.dataset, cand.source_id), "dataset": cand.dataset,
                "imagery_source": "%s (%s)" % (crop.provider_id, crop.licence_ref)}

    def _segment_auto(self, rec, ctx, crop, pt):
        """No candidate: automatic masks, keep building-shaped ones, offer the nearest."""
        ac = get(self.cfg, "segmentation.auto_mask", {}) or {}
        amin, amax = float(ac.get("min_area_m2", 20)), float(ac.get("max_area_m2", 20000))
        cmin = float(ac.get("min_compactness", 0.35))
        masks = self.seg.auto_masks(crop.image)
        rec.transition(C.POLYGONIZING)
        px_area = crop.gsd_m ** 2
        best = None
        kept = 0
        for m in masks:
            a = m["mask"].sum() * px_area
            if not amin <= a <= amax * 1.3:
                continue
            poly, info = mask_to_polygon(m["mask"], crop, self.cfg)
            if poly is None or not amin <= poly.area <= amax or compactness(poly) < cmin:
                continue
            kept += 1
            d = poly.distance(pt)
            if best is None or d < best[0]:
                best = (d, poly, m["score"], info)
        rec.segmentation = {"auto_masks": len(masks), "building_shaped": kept,
                            "model_version": self.seg.model_version, "crop_path": crop.path,
                            "gsd_m": crop.gsd_m}
        if best is None:
            return None
        d, poly, score, info = best
        return {"geometry_utm": poly, "method": C.METHOD_IMAGERY_SAM, "is_segmented": True,
                "touches_edge": info.get("touches_edge", False), "sam_iou": score, "stability": None,
                "footprint_utm": None, "footprint_source": "", "dataset": "",
                "imagery_source": "%s (%s)" % (crop.provider_id, crop.licence_ref),
                "notes": [C.FLAG_NO_CANDIDATE_PATH]}

    # ------------------------------------------------------------------
    def _evaluate(self, rec, opt, chosen, base_ctx):
        vctx = dict(base_ctx)
        vctx.update({"footprint_utm": opt.get("footprint_utm"), "is_segmented": opt.get("is_segmented"),
                     "touches_edge": opt.get("touches_edge", False),
                     "candidate_evidence": chosen.evidence if chosen else {}})
        poly, hard, soft, details = validate(opt["geometry_utm"], vctx, self.cfg)
        signals = {"identity_prob": rec.identity_prob, "sam_iou": opt.get("sam_iou"),
                   "stability": opt.get("stability"), "footprint_agreement": details.get("footprint_agreement"),
                   "n_flags": len(soft), "dataset": opt.get("dataset")}
        conf = conf_mod.record_confidence(signals, self.cfg, self.calibration)
        return {"opt": opt, "poly": poly, "hard": hard, "soft": soft, "details": details,
                "signals": signals, "confidence": conf}

    def _finalise(self, rec, ctx, options, chosen, base_ctx, path):
        pt = base_ctx["point_utm"]
        evals = [self._evaluate(rec, o, chosen, base_ctx) for o in options if o and o.get("geometry_utm") is not None]
        rec.alternatives = [{"method": e["opt"]["method"], "confidence": round(e["confidence"], 4),
                             "hard_failures": e["hard"], "flags": e["soft"],
                             "area_m2": e["details"].get("area_m2"),
                             "footprint_agreement": e["details"].get("footprint_agreement")} for e in evals]
        if len(evals) > 1 and evals[0]["poly"] is not None and evals[1]["poly"] is not None:
            rec.alternatives[0]["iou_with_next"] = round(iou(evals[0]["poly"], evals[1]["poly"]), 4)
        usable = [e for e in evals if e["poly"] is not None]
        passing = [e for e in usable if not e["hard"]]
        pick = max(passing or usable, key=lambda e: e["confidence"]) if usable else None

        notes = []
        if rec.decision == C.IDENTITY_AMBIGUOUS:
            notes.append(C.FLAG_IDENTITY_AMBIGUOUS)
        if chosen is not None and chosen.evidence.get("found_by_name"):
            notes.append(C.FLAG_FOUND_BY_NAME)
        elif getattr(rec, "name_found_far", None):
            notes.append(C.FLAG_NAME_FOUND_FAR)
        if chosen is not None and chosen.evidence.get("crosses_road"):
            notes.append(C.FLAG_CANDIDATE_ACROSS_ROAD)
        if pick is None:
            route, would, why = conf_mod.route(rec.survey_id, 0.0, [], [], rec.decision, path, False, self.cfg)
            rec.proposal = None if not evals else self._proposal_dict(rec, evals[0], chosen, route, would, why,
                                                                      notes, path, pt)
            if rec.proposal is not None:
                rec.proposal["geometry"] = None
            self._to_final_state(rec, route)
            return
        notes += pick["opt"].get("notes", [])
        route, would, why = conf_mod.route(rec.survey_id, pick["confidence"], pick["hard"], pick["soft"],
                                           rec.decision, path, True, self.cfg)
        if path in ("no_imagery", "no_candidate") and route == C.ROUTE_AUTO_ACCEPT:
            route = C.ROUTE_REVIEW
        rec.proposal = self._proposal_dict(rec, pick, chosen, route, would, why, notes, path, pt)
        self._to_final_state(rec, route)

    def _proposal_dict(self, rec, e, chosen, route, would, why, notes, path, pt):
        poly = e["poly"]
        cc = corrected_coordinate(poly, pt, rec.utm_epsg, self.cfg) if poly is not None else {}
        s = e["signals"]
        return {
            "geometry_utm": poly,
            "geometry": to_wgs84(poly, rec.utm_epsg) if poly is not None else None,
            "method": e["opt"]["method"], "path": path,
            "confidence": round(float(e["confidence"]), 4),
            "route": route, "would_route": would, "route_reason": why,
            "decision": rec.decision, "identity_prob": rec.identity_prob,
            "sam_iou": None if s.get("sam_iou") is None else round(float(s["sam_iou"]), 4),
            "stability": None if s.get("stability") is None else round(float(s["stability"]), 4),
            "footprint_agreement": e["details"].get("footprint_agreement"),
            "flags": list(e["soft"]), "hard_failures": list(e["hard"]), "notes": sorted(set(notes)),
            "validation": e["details"],
            "area_m2": e["details"].get("area_m2"),
            "chosen_candidate_id": chosen.candidate_id if chosen else None,
            "footprint_source": e["opt"].get("footprint_source", ""),
            "imagery_source": e["opt"].get("imagery_source", ""),
            "revision": rec.revision,
            **cc,
        }

    def _to_final_state(self, rec, route):
        target = {C.ROUTE_MANUAL: C.MANUAL, C.ROUTE_REVIEW: C.REVIEW, C.ROUTE_AUTO_ACCEPT: C.AUTO_ACCEPTED}[route]
        rec.transition(target)

    # ------------------------------------------------------------------
    def resegment(self, rec, ctx, positive, negative, box_lonlat=None):
        """Click-to-fix: new mask from operator prompts on the cached crop."""
        with rec.lock:
            if not rec.crop_path or not os.path.exists(rec.crop_path):
                raise LookupError("No licensed imagery crop is cached for this record")
            if not self.seg or not self.seg.load():
                raise RuntimeError("Segmentation model is not loaded")
            img, transform, epsg, gsd = read_crop(rec.crop_path)
            crop = Crop(image=img, transform=transform, epsg=epsg, gsd_m=gsd, path=rec.crop_path,
                        provider_id=(rec.imagery or {}).get("provider", ""),
                        licence_ref=(rec.imagery or {}).get("licence_ref", ""),
                        capture_date=(rec.imagery or {}).get("capture_date", ""),
                        bounds=(transform.c, transform.f + transform.e * img.shape[0],
                                transform.c + transform.a * img.shape[1], transform.f))

            def px(lonlat):
                p = point_utm(float(lonlat[0]), float(lonlat[1]), epsg)
                return crop.to_pixel(p.x, p.y)

            pos = [px(p) for p in positive or []]
            neg = [px(p) for p in negative or []]
            bx = None
            if box_lonlat:
                c0, r0 = px((box_lonlat[0], box_lonlat[3]))
                c1, r1 = px((box_lonlat[2], box_lonlat[1]))
                bx = [min(c0, c1), min(r0, r1), max(c0, c1), max(r0, r1)]
            if not pos and bx is None:
                raise ValueError("Click-to-fix needs at least one positive point or a box")
            rec.transition(C.SEGMENTING)
            if len(pos) == 1 and bx is None:
                # A single click is ambiguous (roof object vs. whole roof): take SAM's three
                # nested masks and keep the best-scoring one with a plausible building area.
                amin, amax = get(self.cfg, "validation.area_m2", [15.0, 20000.0])
                px_area = gsd * gsd
                masks = self.seg.predict_all(crop.path, img, pos, neg, None, multimask=True)
                plausible = [m for m in masks if float(amin) <= m[0].sum() * px_area <= float(amax)
                             and not touches_edge(m[0], int(get(self.cfg, "validation.edge_touch_px", 2)))]
                mask, score = max(plausible or masks, key=lambda m: m[1])
            else:
                mask, score = self.seg.predict(crop.path, img, pos, neg, bx)
            rec.transition(C.POLYGONIZING)
            chosen = rec.candidate(rec.chosen_candidate_id) if rec.chosen_candidate_id else None
            poly, info = mask_to_polygon(mask, crop, self.cfg, positive_px=pos,
                                         candidate_utm=chosen.geom_utm if chosen else None)
            rec.transition(C.VALIDATING)
            rec.revision += 1
            pt = point_utm(rec.longitude, rec.latitude, epsg)
            base_ctx = {"point_utm": pt, "radius_m": rec.radius_m or search_radius(self.cfg, self.calibration),
                        "fields": rec.fields, "approved_utm": self._approved_near(rec, chosen, {})}
            rec.segmentation = {"click_to_fix": {"positive": len(pos), "negative": len(neg), "box": bx is not None,
                                                 "sam_iou": round(score, 4)},
                                "model_version": self.seg.model_version, "polygonize": info,
                                "crop_path": crop.path, "gsd_m": gsd}
            options = []
            if poly is not None:
                options.append({"geometry_utm": poly, "method": C.METHOD_CLICK_TO_FIX, "is_segmented": True,
                                "touches_edge": info.get("touches_edge", False), "sam_iou": score,
                                "stability": None, "footprint_utm": chosen.geom_utm if chosen else None,
                                "footprint_source": ("%s:%s" % (chosen.dataset, chosen.source_id)) if chosen else "",
                                "dataset": chosen.dataset if chosen else "",
                                "imagery_source": "%s (%s)" % (crop.provider_id, crop.licence_ref)})
            self._finalise(rec, ctx, options, chosen, base_ctx, "click_to_fix")
            if rec.proposal and rec.proposal.get("route") == C.ROUTE_AUTO_ACCEPT:
                rec.proposal["route"] = C.ROUTE_REVIEW
            store = ctx.get("store")
            if store is not None:
                store.write_segmentation(rec.survey_id, self.seg.model_version,
                                         {"CLICK": {"sam_iou": score, "pixels": int(mask.sum())}}, None, "CLICK",
                                         {"CLICK": self._save_mask(rec, ctx, crop, mask, "click")})
            self._persist(rec, ctx)
            return rec

    def select_candidate(self, rec, ctx, candidate_id):
        """Operator picked another candidate: re-run the footprint path from it."""
        if not candidate_id:
            raise LookupError("A candidate_id is required")
        with rec.lock:
            if rec.state == C.INVALID:
                raise StateError("%s is INVALID and cannot be reprocessed" % rec.survey_id)
            if rec.candidate(candidate_id) is None:
                raise LookupError("Candidate %s is not a candidate of %s" % (candidate_id, rec.survey_id))
            return self.process(rec, ctx, forced_candidate_id=candidate_id)


def _safe(name):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name))
