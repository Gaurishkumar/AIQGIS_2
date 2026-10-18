"""Building Polygon Automation - QGIS plugin entry point."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import BuildingPolygonAutomationPlugin
    return BuildingPolygonAutomationPlugin(iface)
