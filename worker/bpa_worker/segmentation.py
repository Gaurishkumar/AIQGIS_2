"""S7 - Segmentation with SAM 2.1.

* ``set_image`` runs once per crop; the embedding is cached so click-to-fix
  only runs the fast prompt decoder.
* Three prompt variants (A: points + negatives + box, B: box, C: points +
  negatives). Stability = mean pairwise IoU of the three masks.
* No candidate: the survey point is never a positive prompt. The automatic
  mask generator runs and building-shaped masks are kept.
"""

import collections
import hashlib
import logging
import os
import threading
import time

import numpy as np
from shapely.geometry import Point, box

from bpa_common.config import get, resolve_path

log = logging.getLogger(__name__)

MODEL_CONFIGS = {
    "sam2.1_hiera_tiny": "configs/sam2.1/sam2.1_hiera_t.yaml",
    "sam2.1_hiera_small": "configs/sam2.1/sam2.1_hiera_s.yaml",
    "sam2.1_hiera_base_plus": "configs/sam2.1/sam2.1_hiera_b+.yaml",
    "sam2.1_hiera_large": "configs/sam2.1/sam2.1_hiera_l.yaml",
}


def mask_iou(a, b):
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union else 0.0


def stability(masks):
    """Mean pairwise IoU of the variant masks (1.0 for a single mask)."""
    ms = [m for m in masks if m is not None]
    if len(ms) < 2:
        return 1.0 if ms else 0.0
    vals = [mask_iou(ms[i], ms[j]) for i in range(len(ms)) for j in range(i + 1, len(ms))]
    return float(np.mean(vals))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_checkpoint(ckpt_dir, filename):
    """(ok, sha256). ok is None when data/models/manifest.json does not list the file."""
    import json
    digest = _sha256(os.path.join(ckpt_dir, filename))
    manifest = os.path.join(ckpt_dir, "manifest.json")
    if not os.path.isfile(manifest):
        return None, digest
    with open(manifest, "r", encoding="utf-8") as fh:
        expected = (json.load(fh).get("files") or {}).get(filename, {}).get("sha256")
    if not expected:
        return None, digest
    return digest == expected, digest


class SamSegmenter(object):
    """Lazy SAM 2.1 wrapper with a per-crop embedding cache."""

    def __init__(self, cfg):
        self.cfg = cfg
        sc = get(cfg, "segmentation", {}) or {}
        self.backend_name = sc.get("backend", "auto")
        self.model_name = sc.get("model", "sam2.1_hiera_base_plus")
        if sc.get("use_cpu_fallback"):
            self.model_name = sc.get("cpu_fallback_model", "sam2.1_hiera_small")
        self.ckpt_dir = resolve_path(cfg, sc.get("checkpoints_dir", "data/models"))
        self.device_pref = sc.get("device", "auto")
        self.threads = int(sc.get("torch_threads", 0) or 0)
        self.cache_size = int(sc.get("embedding_cache_size", 16))
        self.auto_cfg = sc.get("auto_mask", {}) or {}
        self.predictor = None
        self.model = None
        self.device = None
        self.model_version = None
        self.error = None
        self._cache = collections.OrderedDict()
        self._current = None
        self._lock = threading.RLock()

    # -- loading -------------------------------------------------------------
    @property
    def loaded(self):
        return self.predictor is not None

    def load(self):
        if self.loaded or self.backend_name == "none":
            return self.loaded
        with self._lock:
            if self.loaded:
                return True
            try:
                import torch
                from sam2.build_sam import build_sam2
                from sam2.sam2_image_predictor import SAM2ImagePredictor
            except Exception as exc:
                self.error = "SAM 2 is not installed in the worker environment: %s" % exc
                log.error(self.error)
                return False
            ckpt = os.path.join(self.ckpt_dir, self.model_name + ".pt")
            if not os.path.isfile(ckpt):
                self.error = "Checkpoint not found: %s" % ckpt
                log.error(self.error)
                return False
            ok, digest = verify_checkpoint(self.ckpt_dir, self.model_name + ".pt")
            if ok is False:
                self.error = "Checkpoint SHA-256 does not match data/models/manifest.json: %s" % ckpt
                log.error(self.error)
                return False
            if self.device_pref == "auto":
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            else:
                self.device = self.device_pref
            if self.threads:
                torch.set_num_threads(self.threads)
            t0 = time.time()
            self.model = build_sam2(MODEL_CONFIGS[self.model_name], ckpt, device=self.device)
            self.predictor = SAM2ImagePredictor(self.model)
            self.model_version = "%s@%s" % (self.model_name, digest[:12])
            log.info("Loaded %s on %s in %.1fs", self.model_version, self.device, time.time() - t0)
            return True

    def status(self):
        return {"backend": "sam2" if self.loaded else self.backend_name, "model": self.model_name,
                "model_version": self.model_version, "loaded": self.loaded, "device": self.device,
                "error": self.error, "cached_embeddings": len(self._cache)}

    # -- embeddings ------------------------------------------------------------
    def _activate(self, key, image):
        """Make ``key`` the predictor's current image, reusing a cached embedding."""
        if self._current == key and self.predictor._is_image_set:
            return
        state = self._cache.get(key)
        if state is not None:
            p = self.predictor
            p._features, p._orig_hw, p._is_batch = state
            p._is_image_set = True
            self._cache.move_to_end(key)
            self._current = key
            return
        self.predictor.set_image(image)
        p = self.predictor
        self._cache[key] = (p._features, p._orig_hw, p._is_batch)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        self._current = key

    def forget(self, key):
        with self._lock:
            self._cache.pop(key, None)
            if self._current == key:
                self._current = None

    def predict(self, key, image, pos=None, neg=None, box_px=None):
        """Return (mask bool HxW, predicted IoU). Points are (col, row) pixels."""
        return self.predict_all(key, image, pos, neg, box_px, multimask=False)[0]

    def predict_all(self, key, image, pos=None, neg=None, box_px=None, multimask=False):
        """All masks for a prompt as [(mask, predicted IoU), ...].

        ``multimask`` returns SAM's three nested masks (object part ... whole
        object), which SAM recommends for ambiguous single-point prompts.
        """
        if not self.load():
            raise RuntimeError(self.error or "SAM model not loaded")
        pts, lbl = [], []
        for p in pos or []:
            pts.append(p)
            lbl.append(1)
        for p in neg or []:
            pts.append(p)
            lbl.append(0)
        with self._lock:
            self._activate(key, image)
            masks, scores, _ = self.predictor.predict(
                point_coords=np.asarray(pts, dtype=np.float32) if pts else None,
                point_labels=np.asarray(lbl, dtype=np.int32) if pts else None,
                box=np.asarray(box_px, dtype=np.float32) if box_px is not None else None,
                multimask_output=multimask)
        return [(m > 0, float(s)) for m, s in zip(masks, scores)]

    def auto_masks(self, image):
        """SAM automatic mask generator (no-candidate records)."""
        if not self.load():
            raise RuntimeError(self.error or "SAM model not loaded")
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
        with self._lock:
            gen = SAM2AutomaticMaskGenerator(
                self.model, points_per_side=int(self.auto_cfg.get("points_per_side", 16)),
                pred_iou_thresh=0.7, stability_score_thresh=0.85, min_mask_region_area=20)
            out = gen.generate(image)
            self._current = None   # the generator replaced the predictor's image
        return [{"mask": m["segmentation"].astype(bool), "score": float(m.get("predicted_iou", 0.0))}
                for m in out]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def interior_points(poly_utm, n=3, erosion_m=2.0):
    """1..n well-separated points inside an eroded copy of the polygon."""
    inner = poly_utm
    for e in (erosion_m, erosion_m / 2.0, 0.5, 0.0):
        cand = poly_utm.buffer(-e) if e > 0 else poly_utm
        if not cand.is_empty and cand.area > 0:
            inner = cand
            break
    if inner.geom_type == "MultiPolygon":
        inner = max(inner.geoms, key=lambda g: g.area)
    pts = [inner.representative_point()]
    if n > 1:
        rect = inner.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)[:4]
        edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
        (a, b) = max(edges, key=lambda e: Point(e[0]).distance(Point(e[1])))
        c = inner.centroid
        dx, dy = (b[0] - a[0]), (b[1] - a[1])
        for t in (-0.3, 0.3):
            p = Point(c.x + dx * t, c.y + dy * t)
            if inner.contains(p) and all(p.distance(q) > 1.0 for q in pts):
                pts.append(p)
            if len(pts) >= n:
                break
    return pts[:n]


def build_prompts(crop, candidate_utm, neighbours_utm, cfg):
    """Prompt variants A, B, C in pixel coordinates for one candidate."""
    sc = get(cfg, "segmentation", {}) or {}
    n_pos = int(sc.get("positive_points", 3))
    erosion = float(sc.get("interior_erosion_m", 2.0))
    pad = float(sc.get("box_pad_m", 2.0))
    h, w = crop.shape
    crop_box = box(*crop.bounds)

    pos = [crop.to_pixel(p.x, p.y) for p in interior_points(candidate_utm, n_pos, erosion)]
    neg = []
    cand_buf = candidate_utm.buffer(1.0)
    near = sorted(neighbours_utm, key=lambda g: g.distance(candidate_utm))
    for g in near:
        rp = g.representative_point()
        if not crop_box.contains(rp) or cand_buf.contains(rp):
            continue
        if g.intersection(candidate_utm).area > 0.3 * min(g.area, candidate_utm.area):
            continue  # overlapping duplicate of the candidate, not a neighbour
        neg.append(crop.to_pixel(rp.x, rp.y))
        if len(neg) >= 12:
            break
    minx, miny, maxx, maxy = candidate_utm.buffer(pad, join_style=2).bounds
    c0, r0 = crop.to_pixel(minx, maxy)
    c1, r1 = crop.to_pixel(maxx, miny)
    bx = [max(0.0, min(c0, c1)), max(0.0, min(r0, r1)), min(w - 1.0, max(c0, c1)), min(h - 1.0, max(r0, r1))]
    return {
        "A": {"pos": pos, "neg": neg, "box": bx},
        "B": {"pos": [], "neg": [], "box": bx},
        "C": {"pos": pos, "neg": neg, "box": None},
    }


def run_variants(segmenter, key, crop, prompts):
    """Run all prompt variants; returns dict variant -> {mask, sam_iou}, stability."""
    out = {}
    for name in ("A", "B", "C"):
        pr = prompts[name]
        mask, score = segmenter.predict(key, crop.image, pr["pos"], pr["neg"], pr["box"])
        out[name] = {"mask": mask, "sam_iou": score, "pixels": int(mask.sum())}
    stab = stability([v["mask"] for v in out.values()])
    return out, stab


# ---------------------------------------------------------------------------
# Week-1 benchmark
# ---------------------------------------------------------------------------

def benchmark_models(cfg, crops_dir=None, repeats=3, models=("sam2.1_hiera_small", "sam2.1_hiera_base_plus")):
    """Time set_image + prompt decoding per model on real cached crops (or a synthetic image)."""
    import copy
    from .imagery import read_crop
    images = []
    if crops_dir and os.path.isdir(crops_dir):
        for root, _, files in os.walk(crops_dir):
            for f in files:
                if f == "crop.tif":
                    images.append(read_crop(os.path.join(root, f))[0])
    if not images:
        rng = np.random.default_rng(0)
        img = np.full((256, 256, 3), 90, np.uint8)
        img[60:180, 70:200] = (190, 70, 60)
        images = [np.clip(img + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)]
    report = {"images": len(images), "models": {}}
    for m in models:
        c = copy.deepcopy(cfg)
        c.setdefault("segmentation", {})["model"] = m
        c["segmentation"]["use_cpu_fallback"] = False
        s = SamSegmenter(c)
        t0 = time.time()
        if not s.load():
            report["models"][m] = {"error": s.error}
            continue
        load_s = time.time() - t0
        emb, dec = [], []
        for _ in range(repeats):
            for i, img in enumerate(images):
                s.forget(i)
                t = time.time()
                h, w = img.shape[:2]
                s.predict(i, img, [(w / 2, h / 2)], [], None)
                emb.append(time.time() - t)
                t = time.time()
                s.predict(i, img, [(w / 2, h / 2)], [(5, 5)], [w * 0.2, h * 0.2, w * 0.8, h * 0.8])
                dec.append(time.time() - t)
        report["models"][m] = {"device": s.device, "load_s": round(load_s, 2),
                               "embed_plus_decode_s_median": round(float(np.median(emb)), 3),
                               "decode_only_s_median": round(float(np.median(dec)), 3)}
    return report
