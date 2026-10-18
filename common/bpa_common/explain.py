"""Plain-language explanation of one record for the operator: lane, why, doubts, what to do.

Pure Python (no QGIS, no worker imports) so the plugin, reports and tests share it.

Lanes
  sure     the system is sure of the building and found nothing to check: glance and approve
  check    probably right, but something needs a look (close call, odd size, far away, ...)
  draw     no usable outline: the operator draws the building at the pin
  done / rejected / waiting / invalid / failed   record is finished or cannot be reviewed yet
"""

from . import constants as C

LANE_SURE, LANE_CHECK, LANE_DRAW = "sure", "check", "draw"
LANE_DONE, LANE_REJECTED, LANE_WAITING, LANE_INVALID, LANE_FAILED = "done", "rejected", "waiting", "invalid", "failed"
OPEN_LANES = (LANE_SURE, LANE_CHECK, LANE_DRAW)

LANE_LABEL = {LANE_SURE: "Sure", LANE_CHECK: "Check", LANE_DRAW: "Draw", LANE_DONE: "Done",
              LANE_REJECTED: "Rejected", LANE_WAITING: "Waiting", LANE_INVALID: "Fix Excel", LANE_FAILED: "Failed"}
LANE_ORDER = {LANE_SURE: 0, LANE_CHECK: 1, LANE_DRAW: 2, LANE_WAITING: 3, LANE_FAILED: 4, LANE_INVALID: 5,
              LANE_DONE: 6, LANE_REJECTED: 7}

# Flags in operator language. Size and far-away flags get their numbers filled in below.
FLAG_PLAIN = {
    C.FLAG_FAR_FROM_SURVEY: "The outline is far from the survey point.",
    C.FLAG_LOW_FOOTPRINT_AGREEMENT: "The AI outline and the map outline disagree.",
    C.FLAG_OVERLAPS_APPROVED: "It overlaps a building that is already approved.",
    C.FLAG_HEIGHT_MISMATCH: "The map height does not match the height in the Excel.",
    C.FLAG_SLIVER: "The outline has a thin spike or sliver.",
    C.FLAG_NO_CANDIDATE_PATH: "There is no map outline here; the AI drew one from the picture.",
    C.FLAG_SAM_REFINEMENT_FAILED: "The AI could not trace the picture; the map outline is kept.",
    C.FLAG_LOW_STABILITY: "The AI outline is unstable - check its edges.",
    C.FLAG_CANDIDATE_ACROSS_ROAD: "The building is across a road from the survey point.",
    C.FLAG_IMAGERY_TOO_COARSE: "The picture is too coarse for a building this size, so the map "
                               "outline is kept (the AI was not used).",
}
OUTLINE_FLAGS = (C.FLAG_LOW_FOOTPRINT_AGREEMENT, C.FLAG_SLIVER, C.FLAG_LOW_STABILITY, C.FLAG_SAM_REFINEMENT_FAILED,
                 C.FLAG_NO_CANDIDATE_PATH, C.FLAG_IMPLAUSIBLE_AREA)
PEOPLE_DRAWN = ("OpenStreetMap", "Esri Community Maps")
SURE_TODO = "Glance at it and press A - or press Q to approve many sure ones at once."
DRAW_TODO = "Find the building at the pin and draw it: B (3-click box) or M (any shape)."


def _chosen(d):
    for c in d.get("candidates") or []:
        if c.get("chosen"):
            return c
    return None


def _runner_up(d, chosen):
    for c in sorted(d.get("candidates") or [], key=lambda x: x.get("rank") or 99):
        if chosen is None or c.get("candidate_id") != chosen.get("candidate_id"):
            return c
    return None


def lane(record, detail=None, building_status=None):
    """Lane of a record from its state, the worker detail and (optionally) its review status."""
    state = (record or {}).get("state")
    if building_status in (C.STATUS_APPROVED, C.STATUS_APPROVED_MANUAL) or state in (C.APPROVED, C.APPROVED_MANUAL,
                                                                                   C.EXPORTED):
        return LANE_DONE
    if building_status == C.STATUS_REJECTED or state == C.REJECTED:
        return LANE_REJECTED
    if state == C.INVALID:
        return LANE_INVALID
    if state == C.FAILED:
        return LANE_FAILED
    if state not in (C.REVIEW, C.MANUAL, C.AUTO_ACCEPTED):
        return LANE_WAITING
    d = detail or {}
    p = d.get("proposal") or {}
    if state == C.MANUAL or not p or p.get("route") == C.ROUTE_MANUAL or p.get("hard_failures"):
        return LANE_DRAW
    chosen = _chosen(d) or {}
    if (d.get("decision") == C.IDENTITY_CLEAR and not p.get("flags")
            and not (chosen.get("evidence") or {}).get("found_by_name")):
        return LANE_SURE
    return LANE_CHECK


def explain(record, detail=None, building_status=None, reviewer=None, close_margin=0.15):
    """dict(lane, label, headline, why[], doubts[], todo, short) for one record.

    ``close_margin``: score gap below which the runner-up counts as "almost as likely"
    (scoring.decision.min_margin).
    """
    r = record or {}
    d = detail or {}
    p = d.get("proposal") or {}
    f = r.get("fields") or d.get("fields") or {}
    ln = lane(r, d, building_status)
    why, doubts = [], []
    chosen = _chosen(d)
    ev = (chosen or {}).get("evidence") or {}
    feats = (chosen or {}).get("features") or {}

    if ln == LANE_WAITING:
        return _out(ln, "Not processed yet.", why, doubts, "Click Process.")
    if ln == LANE_INVALID:
        doubts = list(r.get("reasons") or d.get("reasons") or [])
        return _out(ln, "This Excel row cannot be used.", why, doubts,
                    "Correct the row in the Excel and import it again, or reject it (R).")
    if ln == LANE_FAILED:
        code = d.get("error_code") or ""
        if code == "REFERENCE_NOT_COVERED":
            return _out(ln, "The map data for this area is not downloaded yet.", why, doubts,
                        "Click Process and accept the download (needs internet once).")
        doubts.append(d.get("error_message") or "Processing failed.")
        return _out(ln, "Processing failed for this record.", why, doubts, "Click Process to retry, or draw it (M).")
    if ln == LANE_DONE:
        head = "Done - approved%s." % (" by %s" % reviewer if reviewer else "")
        return _out(ln, head, why, doubts, "Nothing to do. Export when the batch is finished.")
    if ln == LANE_REJECTED:
        return _out(ln, "Rejected%s." % (" by %s" % reviewer if reviewer else ""), why, doubts, "Nothing to do.")

    # -- why this building ----------------------------------------------------------------
    if chosen is not None:
        dist = ev.get("distance_m")
        if chosen.get("source") == "historic":
            why.append("This building was approved before for an earlier survey.")
        if ev.get("found_by_name"):
            why.append("Found by its name '%s', %s from the survey point." % (ev["found_by_name"], _m(dist)))
        elif ev.get("inside"):
            why.append("The survey point is inside this building.")
        elif dist is not None:
            why.append("Closest suitable building, %s from the survey point." % _m(dist))
        if ev.get("matched_name") and feats.get("s_name", 0) > 0:
            why.append("The map name '%s' matches the building name." % ev["matched_name"])
        if ev.get("customer_place"):
            why.append("The customer's outlet '%s' is in this building." % ev["customer_place"])
        if ev.get("on_named_street"):
            why.append("It stands on %s, the street in the address." % ev["on_named_street"])
    img = d.get("imagery") or {}
    traced = "SAM" in (p.get("method") or "") or p.get("method") == C.METHOD_CLICK_TO_FIX
    if traced:
        why.append("Outline traced by AI on the imagery '%s' (%s per pixel)." % (
            img.get("provider") or "licensed", _cm(img.get("gsd_m"))))
    elif chosen is not None:
        ds = chosen.get("dataset") or ""
        if ds == "historic":
            why.append("Outline: your team's earlier approved outline.")
        elif ds in PEOPLE_DRAWN:
            why.append("Outline: drawn by people on %s." % ds)
        elif ds:
            why.append("Outline: detected by computer (%s) - edges can be rough." % ds)

    # -- doubts ------------------------------------------------------------------------------
    if d.get("decision") == C.IDENTITY_AMBIGUOUS:
        other = _runner_up(d, chosen)
        reason = d.get("decision_reason") or ""
        unnamed = (chosen is not None and not ev.get("matched_name") and not ev.get("customer_place")
                   and chosen.get("source") != "historic")
        if "on " in reason and "side" in reason:
            doubts.append("The survey point is on the road, so the building could be on either side.")
        elif (other is not None and chosen is not None
              and float(chosen.get("score") or 0) - float(other.get("score") or 0) < close_margin):
            doubts.append("Another building %s from the survey point is almost as likely (#%s)." % (
                _m((other.get("evidence") or {}).get("distance_m")), other.get("rank")))
        elif unnamed:
            doubts.append("Only its position supports this building - the map does not know its name and no "
                          "outlet or street confirms it.")
            unnamed = False
        else:
            doubts.append("The evidence is not strong enough to be sure.")
        if unnamed and (f.get("building_name") or f.get("customer_name")):
            doubts.append("The map does not know this building's name, so the choice rests on position.")
    for flag in p.get("flags") or []:
        if flag == C.FLAG_IMPLAUSIBLE_AREA:
            doubts.append(_size_doubt(p.get("area_m2"), ev.get("area_band_m2"), f))
        elif flag == C.FLAG_HEIGHT_MISMATCH and ev.get("reference_height_m") and f.get("building_height_m"):
            doubts.append("The map says about %s m tall, the Excel says %s m - it may be a different building." % (
                _num(ev["reference_height_m"]), f.get("building_height_m")))
        elif flag in FLAG_PLAIN:
            doubts.append(FLAG_PLAIN[flag])
    for note in p.get("notes") or []:
        if note in FLAG_PLAIN:
            doubts.append(FLAG_PLAIN[note])
        elif note == C.FLAG_FOUND_BY_NAME:
            doubts.append("It was found by name away from the survey point - check that the GPS, not the name, "
                          "is wrong.")
        elif note == C.FLAG_NAME_FOUND_FAR:
            far = [x for x in d.get("name_found_far") or []]
            doubts.append("A building with this name also exists %s - see 'found by name' in the list below." % (
                ("%s away" % _m(far[0].get("distance_m"))) if far and isinstance(far[0], dict) else "further away"))
    for h in p.get("hard_failures") or []:
        doubts.append(C.FLAG_TEXT.get(h, h))
    for w in r.get("warnings") or []:
        doubts.append("Excel: %s." % str(w).rstrip("."))

    # -- what to do ----------------------------------------------------------------------
    if ln == LANE_SURE:
        return _out(ln, "Sure - this is very likely the right building.", why, doubts, SURE_TODO)
    if ln == LANE_DRAW:
        head = "Draw - no usable outline was found here."
        todo = DRAW_TODO
        if img.get("status") == "OK":
            todo += " Or press F and click inside the roof to let the AI trace it."
        return _out(ln, head, why, doubts, todo)
    flags = set(p.get("flags") or []) | set(p.get("notes") or [])
    steps = []
    if d.get("decision") == C.IDENTITY_AMBIGUOUS or flags & {C.FLAG_CANDIDATE_ACROSS_ROAD, C.FLAG_FAR_FROM_SURVEY,
                                                             C.FLAG_FOUND_BY_NAME, C.FLAG_NAME_FOUND_FAR}:
        steps.append("compare with the dashed outlines - other building: C (or 'use' below)")
    if flags & set(OUTLINE_FLAGS):
        steps.append("shape off: S shift, E corners, X split")
    steps.append("right: A")
    return _out(ln, "Check - probably right, but look at the point below.", why, doubts,
                "; ".join(s[0].upper() + s[1:] if i == 0 else s for i, s in enumerate(steps)) + ".")


def _out(ln, headline, why, doubts, todo):
    short = doubts[0] if doubts and ln in (LANE_CHECK, LANE_DRAW, LANE_INVALID, LANE_FAILED) else (
        why[0] if why else headline)
    return {"lane": ln, "label": LANE_LABEL[ln], "headline": headline, "why": why, "doubts": doubts,
            "todo": todo, "short": short}


def _m(v):
    if v is None:
        return "an unknown distance"
    v = float(v)
    return "%.0f m" % v if v >= 1.0 else "under 1 m"


def _cm(v):
    if v is None:
        return "?"
    v = float(v)
    return "%.0f cm" % (v * 100) if v < 1.0 else "%.1f m" % v


def _area(v):
    return "%.0f m2" % float(v) if v is not None else "unknown area"


def _num(v):
    v = float(v)
    return "%.0f" % v if v == int(v) else "%.1f" % v


def _size_doubt(area, band, f):
    cat = (f.get("building_category") or "").strip() or "building"
    what = "%s %s" % ("an" if cat[:1].lower() in "aeiou" else "a", cat)
    if f.get("floors"):
        what += " with %s floors" % f["floors"]
    if band and area is not None and float(area) < float(band[0]):
        return ("Its size (%s) is small for %s (expected at least %s) - the outline may be only part of the "
                "building, or the wrong one." % (_area(area), what, _area(band[0])))
    if band and area is not None and float(area) > float(band[1]):
        return ("Its size (%s) is large for %s (expected at most %s) - it may cover several buildings (X splits)."
                % (_area(area), what, _area(band[1])))
    return "Its size (%s) looks unusual for %s." % (_area(area), what)
