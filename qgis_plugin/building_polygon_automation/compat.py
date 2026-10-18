"""Small helpers that keep the plugin working on QGIS 3.44 (Qt5) and QGIS 4.x (Qt6).

Rules followed throughout the plugin: import Qt only through qgis.PyQt,
use fully scoped enums, call exec() (never exec_()).
"""

try:  # Qt6: QAction/QShortcut live in QtGui
    from qgis.PyQt.QtGui import QAction, QShortcut  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction, QShortcut  # noqa: F401


def val(v):
    """Attribute value -> plain Python (QGIS 3 returns QVariant NULL for empty fields)."""
    if v is None:
        return None
    is_null = getattr(v, "isNull", None)
    if callable(is_null):
        try:
            if is_null():
                return None
        except TypeError:
            pass
    return v


def geom_equal(a, b):
    """Exact geometry equality: isExactlyEqual on QGIS >= 4.2, equals() before (deprecated in 4.2)."""
    if a is None or b is None:
        return a is b
    fn = getattr(a, "isExactlyEqual", None)
    if fn is not None:
        return bool(fn(b))
    return bool(a.equals(b))


def num(v, default=None):
    v = val(v)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
