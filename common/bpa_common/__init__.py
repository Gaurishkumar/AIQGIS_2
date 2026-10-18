"""Shared, dependency-light code used by both the QGIS plugin and the worker.

Only the standard library, openpyxl and PyYAML are imported here, because this
package also runs inside QGIS's own Python (no GeoPandas, no PyTorch).
"""

__version__ = "1.0.0"
