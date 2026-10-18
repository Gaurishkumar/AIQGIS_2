"""review.gpkg - the plugin-owned GeoPackage (single writer: this plugin).

Layers: survey_points, buildings (editable), corrected_points, candidates,
review_action (audit, no geometry) and batch_meta.
All writes go through QgsVectorLayer data providers so QGIS keeps a single
connection to the file.
"""

import datetime as dt
import os
import uuid

from osgeo import ogr, osr
from qgis.core import (QgsExpression, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsPointXY,
                       QgsVectorLayer)

from .compat import val

STR, INT, REAL = ogr.OFTString, ogr.OFTInteger, ogr.OFTReal

SCHEMA = {
    "survey_points": (ogr.wkbPoint, [
        ("survey_id", STR), ("row", INT), ("building_name", STR), ("address", STR), ("state", STR),
        ("route", STR), ("would_route", STR), ("confidence", REAL), ("utm_epsg", INT), ("reasons", STR),
        ("warnings", STR), ("updated_at", STR)]),
    "buildings": (ogr.wkbPolygon, [
        ("polygon_id", STR), ("survey_id", STR), ("status", STR), ("method", STR), ("confidence", REAL),
        ("route", STR), ("would_route", STR), ("area_m2", REAL), ("source_proposal_ref", STR),
        ("footprint_source", STR), ("imagery_source", STR), ("flags", STR), ("config_version", STR),
        ("model_version", STR), ("manifest_hash", STR), ("reviewer", STR), ("reviewed_at", STR),
        ("reason_code", STR), ("updated_at", STR)]),
    "corrected_points": (ogr.wkbPoint, [
        ("survey_id", STR), ("strategy", STR), ("corrected_lat", REAL), ("corrected_lon", REAL),
        ("shift_m", REAL), ("dist_to_polygon_m", REAL), ("created_at", STR)]),
    "candidates": (ogr.wkbPolygon, [
        ("survey_id", STR), ("candidate_id", STR), ("rank", INT), ("score", REAL), ("dataset", STR),
        ("chosen", INT), ("label", STR)]),
    "review_action": (ogr.wkbNone, [
        ("action_id", STR), ("survey_id", STR), ("operator_id", STR), ("action", STR), ("reason_code", STR),
        ("note", STR), ("geometry_before", STR), ("geometry_after", STR), ("timestamp", STR),
        ("duration_s", REAL)]),
    "batch_meta": (ogr.wkbNone, [("key", STR), ("value", STR)]),
}


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


def create_review_gpkg(path):
    """Create an empty review.gpkg with all layers (EPSG:4326)."""
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    for name, (gtype, fields) in SCHEMA.items():
        if gtype == ogr.wkbNone:
            lyr = ds.CreateLayer(name, None, ogr.wkbNone, options=["FID=fid"])
        else:
            lyr = ds.CreateLayer(name, srs, gtype, options=["FID=fid", "GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES"])
        for fname, ftype in fields:
            lyr.CreateField(ogr.FieldDefn(fname, ftype))
    ds = None
    return path


class ReviewStore(object):
    def __init__(self, path):
        self.path = path
        create_review_gpkg(path)
        self._layers = {}
        self._migrate()

    def _migrate(self):
        """Add fields introduced after a review.gpkg was created (older batches keep working)."""
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QMetaType
        types = {REAL: QMetaType.Type.Double, INT: QMetaType.Type.Int, STR: QMetaType.Type.QString}
        for name, (_, fields) in SCHEMA.items():
            lyr = self.layer(name)
            missing = [QgsField(f, types[t]) for f, t in fields if lyr.fields().indexFromName(f) < 0]
            if missing:
                lyr.dataProvider().addAttributes(missing)
                lyr.updateFields()

    def uri(self, name):
        return "%s|layername=%s" % (self.path, name)

    def layer(self, name):
        lyr = self._layers.get(name)
        if lyr is None or not _alive(lyr):
            lyr = QgsVectorLayer(self.uri(name), name, "ogr")
            if not lyr.isValid():
                raise RuntimeError("Cannot open %s in %s" % (name, self.path))
            self._layers[name] = lyr
        return lyr

    def adopt(self, name, layer):
        """Use a layer instance already added to the project for writes."""
        self._layers[name] = layer

    # -- generic helpers ----------------------------------------------------------
    @staticmethod
    def _where(field, value):
        return "%s = %s" % (QgsExpression.quotedColumnRef(field), QgsExpression.quotedValue(value))

    def features(self, name, field=None, value=None):
        lyr = self.layer(name)
        req = QgsFeatureRequest()
        if field is not None:
            req.setFilterExpression(self._where(field, value))
        return list(lyr.getFeatures(req))

    def first(self, name, field, value):
        feats = self.features(name, field, value)
        return feats[0] if feats else None

    def _add(self, name, attrs, geom=None):
        lyr = self.layer(name)
        f = QgsFeature(lyr.fields())
        for k, v in attrs.items():
            idx = lyr.fields().indexFromName(k)
            if idx >= 0:
                f.setAttribute(idx, v)
        if geom is not None:
            f.setGeometry(geom)
        ok, out = lyr.dataProvider().addFeatures([f])
        if not ok:
            raise RuntimeError("Could not add a feature to %s: %s" % (name, lyr.dataProvider().lastError()))
        lyr.triggerRepaint()
        return out[0].id() if out else None

    def _update(self, name, fid, attrs=None, geom=None):
        lyr = self.layer(name)
        pr = lyr.dataProvider()
        if attrs:
            changes = {}
            for k, v in attrs.items():
                idx = lyr.fields().indexFromName(k)
                if idx >= 0:
                    changes[idx] = v
            if changes and not pr.changeAttributeValues({fid: changes}):
                raise RuntimeError("Could not update %s fid %s" % (name, fid))
        if geom is not None and not pr.changeGeometryValues({fid: geom}):
            raise RuntimeError("Could not update geometry of %s fid %s" % (name, fid))
        lyr.triggerRepaint()

    def _delete(self, name, fids):
        if fids:
            self.layer(name).dataProvider().deleteFeatures(list(fids))
            self.layer(name).triggerRepaint()

    # -- survey points --------------------------------------------------------------
    def load_survey_points(self, records):
        lyr = self.layer("survey_points")
        self._delete("survey_points", [f.id() for f in lyr.getFeatures()])
        feats = []
        for r in records:
            f = QgsFeature(lyr.fields())
            fl = r.get("fields", {})
            addr = ", ".join(x for x in [fl.get("street"), fl.get("area"), fl.get("city"), fl.get("pincode")] if x)
            f.setAttributes([None] * lyr.fields().count())
            for k, v in (("survey_id", r["survey_id"]), ("row", r.get("row")),
                         ("building_name", fl.get("building_name", "")), ("address", addr),
                         ("state", r.get("state")), ("reasons", "; ".join(r.get("reasons") or [])),
                         ("warnings", "; ".join(r.get("warnings") or [])), ("updated_at", now_iso())):
                f.setAttribute(lyr.fields().indexFromName(k), v)
            if r.get("latitude") is not None and r.get("longitude") is not None:
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r["longitude"], r["latitude"])))
            feats.append(f)
        lyr.dataProvider().addFeatures(feats)
        lyr.updateExtents()
        lyr.triggerRepaint()

    def set_survey(self, survey_id, **attrs):
        f = self.first("survey_points", "survey_id", survey_id)
        if f is not None:
            attrs["updated_at"] = now_iso()
            self._update("survey_points", f.id(), attrs)

    def survey_rows(self):
        out = []
        for f in self.layer("survey_points").getFeatures():
            g = f.geometry()
            pt = g.asPoint() if g is not None and not g.isEmpty() else None
            out.append({"survey_id": val(f["survey_id"]), "row": val(f["row"]),
                        "building_name": val(f["building_name"]), "address": val(f["address"]),
                        "state": val(f["state"]), "route": val(f["route"]), "would_route": val(f["would_route"]),
                        "confidence": val(f["confidence"]), "utm_epsg": val(f["utm_epsg"]),
                        "reasons": val(f["reasons"]), "warnings": val(f["warnings"]),
                        "longitude": pt.x() if pt else None, "latitude": pt.y() if pt else None})
        return out

    # -- buildings ---------------------------------------------------------------------
    def building(self, survey_id):
        return self.first("buildings", "survey_id", survey_id)

    def all_buildings(self, statuses=None):
        feats = list(self.layer("buildings").getFeatures())
        if statuses:
            feats = [f for f in feats if val(f["status"]) in statuses]
        return feats

    def upsert_building(self, survey_id, geom, attrs):
        attrs = dict(attrs)
        attrs["survey_id"] = survey_id
        attrs["updated_at"] = now_iso()
        f = self.building(survey_id)
        if f is None:
            return self._add("buildings", attrs, geom)
        self._update("buildings", f.id(), attrs, geom)
        return f.id()

    def update_building(self, fid, attrs=None, geom=None):
        attrs = dict(attrs or {})
        attrs["updated_at"] = now_iso()
        self._update("buildings", fid, attrs, geom)

    def delete_buildings(self, fids):
        self._delete("buildings", fids)

    # -- candidates ----------------------------------------------------------------------
    def replace_candidates(self, survey_id, candidates):
        """candidates: list of dicts with candidate_id, rank, score, dataset, chosen, geometry (QgsGeometry)."""
        lyr = self.layer("candidates")
        self._delete("candidates", [f.id() for f in self.features("candidates", "survey_id", survey_id)])
        feats = []
        for c in candidates:
            f = QgsFeature(lyr.fields())
            f.setAttributes([None] * lyr.fields().count())
            for k in ("candidate_id", "rank", "score", "dataset", "chosen", "label"):
                f.setAttribute(lyr.fields().indexFromName(k), c.get(k))
            f.setAttribute(lyr.fields().indexFromName("survey_id"), survey_id)
            if c.get("geometry") is not None:
                f.setGeometry(c["geometry"])
            feats.append(f)
        if feats:
            lyr.dataProvider().addFeatures(feats)
        lyr.updateExtents()
        lyr.triggerRepaint()

    # -- corrected points -------------------------------------------------------------------
    def upsert_corrected(self, survey_id, cc):
        geom = QgsGeometry.fromPointXY(QgsPointXY(cc["corrected_lon"], cc["corrected_lat"]))
        attrs = {"survey_id": survey_id, "strategy": cc["strategy"], "corrected_lat": cc["corrected_lat"],
                 "corrected_lon": cc["corrected_lon"], "shift_m": cc["shift_m"],
                 "dist_to_polygon_m": cc["dist_to_polygon_m"], "created_at": now_iso()}
        f = self.first("corrected_points", "survey_id", survey_id)
        if f is None:
            return self._add("corrected_points", attrs, geom)
        self._update("corrected_points", f.id(), attrs, geom)
        return f.id()

    def delete_corrected(self, survey_id):
        self._delete("corrected_points", [f.id() for f in self.features("corrected_points", "survey_id", survey_id)])

    def corrected(self, survey_id):
        return self.first("corrected_points", "survey_id", survey_id)

    # -- audit ---------------------------------------------------------------------------------
    def add_action(self, survey_id, operator_id, action, reason_code=None, note=None,
                   before=None, after=None, duration_s=None):
        """Audit row. ``duration_s``: operator seconds spent on the record, set on final actions."""
        return self._add("review_action", {
            "action_id": uuid.uuid4().hex, "survey_id": survey_id, "operator_id": operator_id or "",
            "action": action, "reason_code": reason_code or "", "note": note or "",
            "geometry_before": before.asWkt(8) if isinstance(before, QgsGeometry) and not before.isEmpty() else (before or ""),
            "geometry_after": after.asWkt(8) if isinstance(after, QgsGeometry) and not after.isEmpty() else (after or ""),
            "timestamp": now_iso(), "duration_s": None if duration_s is None else round(float(duration_s), 1)})

    def actions(self, survey_id=None):
        feats = self.features("review_action", "survey_id", survey_id) if survey_id else list(
            self.layer("review_action").getFeatures())
        keys = ("action_id", "survey_id", "operator_id", "action", "reason_code", "note",
                "geometry_before", "geometry_after", "timestamp", "duration_s")
        return [{k: (val(f[k]) if f.fields().indexFromName(k) >= 0 else None) for k in keys} for f in feats]

    # -- metadata --------------------------------------------------------------------------------
    def meta(self):
        return {val(f["key"]): val(f["value"]) for f in self.layer("batch_meta").getFeatures()}

    def set_meta(self, **kv):
        for k, v in kv.items():
            f = self.first("batch_meta", "key", k)
            if f is None:
                self._add("batch_meta", {"key": k, "value": "" if v is None else str(v)})
            else:
                self._update("batch_meta", f.id(), {"value": "" if v is None else str(v)})


def _alive(layer):
    try:
        layer.id()
        return True
    except RuntimeError:  # underlying C++ object deleted (layer removed from project)
        return False
