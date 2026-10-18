"""Layer styles: buildings by status (proposals amber, approved green, manual red outline)."""

from qgis.core import (QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsMarkerSymbol, QgsPalLayerSettings,
                       QgsRendererCategory, QgsRuleBasedRenderer, QgsTextFormat, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor


def style_buildings(layer):
    root = QgsRuleBasedRenderer.Rule(None)
    rules = [
        ("Proposed", "\"status\" = 'PROPOSED'", {"color": "255,191,0,70", "outline_color": "255,176,0,255", "outline_width": "0.7"}),
        ("Edited", "\"status\" = 'EDITED'", {"color": "255,140,0,70", "outline_color": "255,120,0,255", "outline_width": "0.7", "outline_style": "dash"}),
        ("Approved", "\"status\" = 'APPROVED'", {"color": "40,180,70,80", "outline_color": "20,140,50,255", "outline_width": "0.7"}),
        ("Approved (manual)", "\"status\" = 'APPROVED_MANUAL'", {"color": "40,180,70,80", "outline_color": "220,30,30,255", "outline_width": "0.9"}),
        ("Auto-accepted", "\"status\" = 'AUTO_ACCEPTED'", {"color": "40,120,220,70", "outline_color": "30,90,200,255", "outline_width": "0.7"}),
        ("Rejected", "\"status\" = 'REJECTED'", {"color": "0,0,0,0", "outline_color": "130,130,130,255", "outline_width": "0.5", "outline_style": "dot"}),
    ]
    for label, expr, props in rules:
        r = QgsRuleBasedRenderer.Rule(QgsFillSymbol.createSimple(props), 0, 0, expr, label)
        root.appendChild(r)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    layer.triggerRepaint()


def style_survey_points(layer):
    cats = [
        ("INVALID", "220,20,60", "cross2", 3.2), ("FAILED", "200,0,200", "square", 2.6),
        ("MANUAL", "230,40,40", "circle", 2.8), ("REVIEW", "255,176,0", "circle", 2.6),
        ("APPROVED", "20,160,60", "circle", 2.4), ("APPROVED_MANUAL", "20,160,60", "circle", 2.4),
        ("REJECTED", "130,130,130", "circle", 2.2), ("AUTO_ACCEPTED", "30,90,200", "circle", 2.4),
    ]
    categories = []
    for value, color, shape, size in cats:
        sym = QgsMarkerSymbol.createSimple({"name": shape, "color": color, "outline_color": "255,255,255",
                                            "outline_width": "0.4", "size": str(size)})
        categories.append(QgsRendererCategory(value, sym, value.replace("_", " ").title()))
    other = QgsMarkerSymbol.createSimple({"name": "circle", "color": "70,130,230", "outline_color": "255,255,255",
                                          "outline_width": "0.4", "size": "2.4"})
    categories.append(QgsRendererCategory(None, other, "Processing / other"))
    layer.setRenderer(QgsCategorizedSymbolRenderer("state", categories))
    layer.triggerRepaint()


def style_candidates(layer):
    root = QgsRuleBasedRenderer.Rule(None)
    root.appendChild(QgsRuleBasedRenderer.Rule(
        QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_color": "255,230,0,255", "outline_width": "0.8"}),
        0, 0, "\"chosen\" = 1", "Chosen candidate"))
    root.appendChild(QgsRuleBasedRenderer.Rule(
        QgsFillSymbol.createSimple({"color": "0,229,255,20", "outline_color": "0,200,230,255", "outline_width": "0.4",
                                    "outline_style": "dash"}), 0, 0, "ELSE", "Other candidates"))
    layer.setRenderer(QgsRuleBasedRenderer(root))
    s = QgsPalLayerSettings()
    s.fieldName = "label"
    fmt = QgsTextFormat()
    fmt.setSize(8)
    fmt.setColor(QColor(0, 90, 110))
    buf = fmt.buffer()
    buf.setEnabled(True)
    buf.setSize(0.8)
    fmt.setBuffer(buf)
    s.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def style_corrected(layer):
    sym = QgsMarkerSymbol.createSimple({"name": "diamond", "color": "118,255,3", "outline_color": "0,0,0",
                                        "outline_width": "0.3", "size": "2.2"})
    from qgis.core import QgsSingleSymbolRenderer
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()
