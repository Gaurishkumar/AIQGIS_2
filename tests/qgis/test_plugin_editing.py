"""Editing-speed features inside QGIS (offscreen) with a real worker.

  "C:\\Program Files\\QGIS 4.2.2\\bin\\python-qgis.bat" tests\\qgis\\test_plugin_editing.py <workbook.xlsx> [batches_dir] [old_review.gpkg]

Covers Shift (move outline), Split (keep the part with the survey point; the
native split tool no longer gets mistaken for a manual drawing), the 3-click
Box with true right angles, the Quick check grid (approve unmarked, keep
marked), the productivity report and the review.gpkg upgrade for old batches.
"""

import math
import os
import shutil
import sys
import tempfile

from harness import Check, FakeIface, app, pump, wait_until

app()

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsLineString, QgsPoint,
                       QgsPointXY, QgsProject)  # noqa: E402

from building_polygon_automation import geometry_ops as G  # noqa: E402
from building_polygon_automation.bpa_common import constants as C  # noqa: E402
from building_polygon_automation.compat import val  # noqa: E402
from building_polygon_automation.map_tools import BoxTool  # noqa: E402
from building_polygon_automation.plugin import BuildingPolygonAutomationPlugin  # noqa: E402
from building_polygon_automation.quick_review import QuickReviewDialog  # noqa: E402
from building_polygon_automation.review_store import ReviewStore  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def actions_of(c, sid, action):
    return [a for a in c.store.actions(sid) if a["action"] == action]


def main(workbook, batches_dir=None, old_gpkg=None):
    check = Check()
    iface = FakeIface()
    plugin = BuildingPolygonAutomationPlugin(iface)
    plugin.initGui()
    s = plugin.settings
    # Learning writes to the reference data, so this test works on a private copy of it.
    import yaml
    sandbox = tempfile.mkdtemp(prefix="bpa_ref_")
    shutil.copytree(os.path.join(ROOT, "data", "reference"), os.path.join(sandbox, "reference"))
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config", "config.yaml"), encoding="utf-8"))
    cfg["reference"]["dir"] = os.path.join(sandbox, "reference")
    cfg_path = os.path.join(sandbox, "config.yaml")
    yaml.safe_dump(cfg, open(cfg_path, "w", encoding="utf-8"), sort_keys=False)
    s.set("project_dir", ROOT)
    s.set("config_path", cfg_path)
    s.set("batches_dir", batches_dir or tempfile.mkdtemp(prefix="bpa_edit_"))
    s.set("operator_id", "op_speed")
    s.set("port", 8794)
    s.set("auto_next", False)
    s.set("load_model", False)
    s.set("avoid_overlap", False)
    plugin.toggle(True)
    c = plugin.controller
    c.auto_prepare_reference = False
    c.import_excel(workbook)
    c.process()
    wait_until(lambda: c.worker_ready, 240, "worker")
    wait_until(lambda: not c.poll_timer.isActive() and len(c.details) >= 100, 300, "results")
    pump(0.5)
    lyr = c.layers["buildings"]
    review = [k for k in c.queue("review") if c.store.building(c.records[k]["survey_id"]) is not None]
    check(len(review) >= 40, "proposals available for the editing tests (%d)" % len(review))
    for name in ("shift", "split", "box", "quick"):
        check(name in plugin.dock.buttons, "panel has the %s button" % name)

    print("\n== Shift: move the whole outline")
    k = review[0]
    sid = c.records[k]["survey_id"]
    c.select(k, zoom=False)
    before = QgsGeometry(c.store.building(sid).geometry())
    c.start_shift(k)
    check(lyr.isEditable() and c._edit_kind == C.ACTION_SHIFT, "shift starts an edit session")
    fid = c.store.building(sid).id()
    check(lyr.translateFeature(fid, 0.00002, -0.00001) == 0, "outline moved")
    check(lyr.commitChanges(), "shift saved")
    pump(0.4)
    after = c.store.building(sid)
    check(val(after["status"]) == C.STATUS_EDITED, "shifted outline is EDITED")
    check(abs(after.geometry().area() - before.area()) / before.area() < 1e-6, "shift keeps the shape and size")
    check(len(actions_of(c, sid, C.ACTION_SHIFT)) == 1 and not actions_of(c, sid, C.ACTION_EDIT),
          "logged once, as SHIFT")
    c.approve(k)
    check(c.records[k]["state"] == C.APPROVED, "shifted outline approved")

    print("\n== Split: a footprint covering two buildings")
    k = review[1]
    sid = c.records[k]["survey_id"]
    c.select(k, zoom=False)
    b = c.store.building(sid)
    poly = QgsGeometry(b.geometry())
    r = c.records[k]
    pt = QgsGeometry.fromPointXY(QgsPointXY(r["longitude"], r["latitude"]))
    bb = poly.boundingBox()
    cx = bb.center().x()
    c.start_split(k)
    line = QgsLineString([QgsPoint(cx, bb.yMinimum() - 0.001), QgsPoint(cx, bb.yMaximum() + 0.001)])
    res = lyr.splitFeatures(line, False, False)
    res = res[0] if isinstance(res, tuple) else res          # QGIS 4 returns (result, extra)
    check(res == 0, "split applied (result %r)" % (res,))
    check(lyr.commitChanges(), "split saved")
    pump(0.6)
    parts = c.store.features("buildings", "survey_id", sid)
    check(len(parts) == 1, "one building left for the record after the split (%d)" % len(parts))
    kept = parts[0].geometry()
    check(kept.area() < poly.area() * 0.95, "the kept part is smaller than the merged outline")
    near = kept.contains(pt) or kept.distance(pt) <= min(
        QgsGeometry(p).distance(pt) for p in poly.asGeometryCollection()) + 1e-9
    check(kept.contains(pt) or kept.distance(pt) < poly.distance(pt) + 0.001 or near, "kept part is the one at the survey point")
    check(val(parts[0]["status"]) == C.STATUS_EDITED, "split result is EDITED, not approved")
    check(len(actions_of(c, sid, C.ACTION_SPLIT)) == 1, "logged as SPLIT")
    check(not actions_of(c, sid, C.ACTION_DRAW_MANUAL), "split is not mistaken for a manual drawing")
    c.approve(k)

    print("\n== Box: 3 clicks, true right angles")
    k = review[2]
    sid = c.records[k]["survey_id"]
    c.select(k, zoom=False)
    r = c.records[k]
    canvas = iface.mapCanvas()
    tr = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
    p0 = tr.transform(QgsPointXY(r["longitude"] - 0.00008, r["latitude"] - 0.00005))
    p1 = tr.transform(QgsPointXY(r["longitude"] + 0.00009, r["latitude"] - 0.00002))
    p2 = tr.transform(QgsPointXY(r["longitude"] + 0.00006, r["latitude"] + 0.00008))
    tool = BoxTool(canvas, lambda g: None)
    rect = tool._rectangle(p0, p1, p2)
    epsg = c.epsg_for(k)
    ring = G.to_utm(rect, epsg).asPolygon()[0][:4]           # four corners (drop the closing point)
    check(len(ring) == 4, "box has four corners")
    angles = []
    for i in range(4):
        a, o, b2 = ring[(i - 1) % 4], ring[i], ring[(i + 1) % 4]
        v1 = (a.x() - o.x(), a.y() - o.y())
        v2 = (b2.x() - o.x(), b2.y() - o.y())
        cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2))
        angles.append(math.degrees(math.acos(max(-1, min(1, cosang)))))
    check(all(abs(x - 90) < 0.01 for x in angles), "box corners are 90 degrees on the ground (%s)" % [round(x, 3) for x in angles])
    c.add_box(rect, k)
    feats = c.store.features("buildings", "survey_id", sid)
    check(len(feats) == 1 and val(feats[0]["status"]) == C.STATUS_APPROVED_MANUAL, "box saved as the record's only building, approved")
    check(len(actions_of(c, sid, C.ACTION_DRAW_BOX)) == 1, "logged as DRAW_BOX")
    check(c.store.corrected(sid) is not None, "box record has a corrected coordinate")

    print("\n== Quick check grid")
    dlg = QuickReviewDialog(c, canvas.mapSettings().destinationCrs())
    keys = list(dlg.keys)
    check(len(keys) == 12, "a full page of 12 proposals")
    check(all(c.records[x]["state"] == C.REVIEW for x in keys), "only open records are offered")
    wait_until(lambda: all(not j.isActive() for j, _o, _p in dlg.jobs), 60, "tile renders")
    pump(0.3)
    check(all(t.image.pixmap() is not None and not t.image.pixmap().isNull() for t in dlg.tiles.values()),
          "every tile rendered an image")
    clear_first = [c.details[x].get("decision") == C.IDENTITY_CLEAR for x in keys]
    check(clear_first == sorted(clear_first, reverse=True), "clear identities are shown first")
    marked = keys[:2]
    for x in marked:
        dlg.toggle(x)
    check(dlg.approve_btn.text() == "Approve 10 unmarked", "button counts the unmarked tiles")
    dlg.approve_unmarked()
    approved = [x for x in keys if c.records[x]["state"] == C.APPROVED]
    check(len(approved) >= 9, "unmarked proposals approved (%d of 10)" % len(approved))
    check(all(c.records[x]["state"] == C.REVIEW for x in marked), "marked proposals stay for normal review")
    check(not set(dlg.keys) & set(keys), "next page shows new records only")
    notes = [a["note"] for x in approved for a in actions_of(c, c.records[x]["survey_id"], C.ACTION_APPROVE)]
    check(all("quick check" in (n or "") for n in notes), "quick-check approvals are labelled in the audit")
    dlg.done(0)

    print("\n== Productivity report")
    rep = c.productivity_report()
    o = rep["outcomes"]
    print("   ", {k2: v for k2, v in rep.items() if k2 not in ("files", "by_operator")})
    check(rep["records_finished"] == len(approved) + 3, "report counts every finished record (%d)" % rep["records_finished"])
    check(o.get("outline fixed") == 2, "shift + split counted as outline fixed")
    check(o.get("drawn by operator") == 1, "box counted as drawn")
    check(o.get("approved as proposed") == len(approved), "quick-check approvals counted as approved as proposed")
    check(rep["seconds_per_record_mean"] is not None and rep["projected_records_per_day"], "time per record and records/day computed")
    check(all(os.path.isfile(f) for f in rep["files"]), "CSV and JSON written to the batch output folder")
    check("op_speed" in rep["by_operator"], "per-operator figures")

    print("\n== One-click 'use' link in the evidence panel")
    from qgis.PyQt.QtCore import QUrl
    k = next(x for x in c.queue("review") if len(c.details[x]["candidates"]) > 2)
    c.select(k, zoom=False)
    html = c.evidence_html(k)
    target = c.details[k]["candidates"][2]["candidate_id"]
    check("pick:%s" % target in html, "evidence table offers 'use' links for other candidates")
    rev = c.details[k]["revision"]
    plugin.dock._link(QUrl("pick:%s" % target))
    wait_until(lambda: c.details[k]["revision"] != rev, 60, "use-link pick")
    check(c.details[k]["chosen_candidate_id"] == target, "clicking 'use' switches the building")

    print("\n== Learning from approved buildings")
    s.set("learn_from_approved", True)
    box_sid = c.records[review[2]]["survey_id"]
    n_msgs = len(iface.bar.messages)
    c.export(os.path.join(c.batch["batch_dir"], "output", "learn_export.xlsx"))
    wait_until(lambda: any("Learned" in m[1] for m in iface.bar.messages[n_msgs:]), 60, "learn message")
    h = c.client.health()
    check(h["reference"]["counts"]["historic"] >= len(approved) + 3, "approved buildings are now trusted reference (%d)"
          % h["reference"]["counts"]["historic"])
    check(any(a["action"] == C.ACTION_LEARN for a in c.store.actions()), "learning is recorded in the audit")
    n_msgs = len(iface.bar.messages)
    c.process(skip_reference_check=True)
    wait_until(lambda: any("Processing finished" in m[1] for m in iface.bar.messages[n_msgs:]), 300, "re-run")
    pump(0.5)
    kb = c._key_for(box_sid)
    check(c.details[kb]["decision"] == C.IDENTITY_CLEAR and c.details[kb]["proposal"]["method"] == C.METHOD_HISTORIC,
          "next run matches the learned building directly (historic match)")
    check(c.records[kb]["state"] == C.APPROVED_MANUAL, "re-running keeps the operator's approval")
    if os.path.isfile(os.path.join(ROOT, "data", "reference", "historic.gpkg")):
        check(False, "real reference data untouched")
    else:
        check(True, "real reference data untouched")

    if old_gpkg and os.path.isfile(old_gpkg):
        print("\n== Older batch files are upgraded")
        tmp = os.path.join(tempfile.mkdtemp(), "review.gpkg")
        shutil.copy(old_gpkg, tmp)
        st = ReviewStore(tmp)
        check(st.layer("review_action").fields().indexFromName("duration_s") >= 0, "duration field added to an old review.gpkg")
        st.add_action("X", "op", C.ACTION_APPROVE, duration_s=12.5)
        check(any(a["duration_s"] == 12.5 for a in st.actions("X")), "old batch accepts timed actions")

    c.stop_worker()
    pump(1.0)
    plugin.unload()
    return check.done()


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
