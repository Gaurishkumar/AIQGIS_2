"""Single-building entry: read a location the way operators paste it.

Accepted: "19.07604, 72.87773" / "19.07604 72.87773" / "19.0760° N, 72.8777° E" /
"19°04'33.7\"N 72°52'39.8\"E" (Google Maps info card) / Google Maps links (place pin
"!3d..!4d..", "?q=lat,lon", "ll=", or the map centre "@lat,lon,zoom").
"""

import re
from urllib.parse import unquote

_NUM = r"[-+]?\d{1,3}(?:\.\d+)?"
_PIN = re.compile(r"!3d(%s)!4d(%s)" % (_NUM, _NUM))
_QUERY = re.compile(r"[?&](?:q|query|ll|destination|center|daddr)=(?:loc:)?\s*(%s)\s*,\s*(%s)" % (_NUM, _NUM))
_AT = re.compile(r"@(%s),(%s)" % (_NUM, _NUM))
_DMS = re.compile(r"(\d{1,3})\s*[°º:\s]\s*(\d{1,2})\s*['′:\s]\s*(\d{1,2}(?:\.\d+)?)\s*(?:\"|″|'')?\s*([NSEW])", re.I)
_DM = re.compile(r"(\d{1,3})\s*[°º]\s*(\d{1,2}(?:\.\d+)?)\s*['′]\s*([NSEW])", re.I)
_DEC_HEMI = re.compile(r"(%s)\s*°?\s*([NSEW])" % _NUM.replace("[-+]?", ""), re.I)
_PAIR = re.compile(r"^\s*(%s)\s*°?\s*[,;\s]\s*(%s)\s*°?\s*$" % (_NUM, _NUM))


class LocationError(ValueError):
    """The text is not a location the parser understands."""


def _hemi(value, letter):
    return -value if letter.upper() in "SW" else value


def _lat_lon_from_hemis(parts):
    lat = lon = None
    for v, h in parts:
        if h.upper() in "NS":
            lat = _hemi(v, h)
        else:
            lon = _hemi(v, h)
    if lat is None or lon is None:
        raise LocationError("Need one N/S and one E/W value")
    return lat, lon


def _raw(text):
    t = unquote(str(text or "")).strip().replace(" ", " ")
    if not t:
        raise LocationError("Enter or paste a location")
    if "://" in t or t.lower().startswith(("www.", "maps.", "goo.gl")):
        for rx, note in ((_PIN, "pin from link"), (_QUERY, "from link"),
                         (_AT, "map centre from link - check the point")):   # "@" is not a pin
            m = rx.search(t)
            if m:
                return float(m.group(1)), float(m.group(2)), note
        if "goo.gl" in t or "maps.app" in t:
            raise LocationError("Short links hide the location - open the link and copy the coordinates instead")
        raise LocationError("No coordinates found in this link")
    dms = _DMS.findall(t)
    if len(dms) == 2:
        parts = [(int(d) + int(m) / 60.0 + float(s) / 3600.0, h) for d, m, s, h in dms]
        return _lat_lon_from_hemis(parts) + ("degrees-minutes-seconds",)
    dm = _DM.findall(t)
    if len(dm) == 2:
        parts = [(int(d) + float(m) / 60.0, h) for d, m, h in dm]
        return _lat_lon_from_hemis(parts) + ("degrees-minutes",)
    hem = _DEC_HEMI.findall(t)
    if len(hem) == 2:
        return _lat_lon_from_hemis([(float(v), h) for v, h in hem]) + ("",)
    m = _PAIR.match(t)
    if m:
        return float(m.group(1)), float(m.group(2)), ""
    raise LocationError("Use 'latitude, longitude' (e.g. 19.07604, 72.87773) or paste a map link")


def parse_location(text, bounds=None):
    """Return dict(lat, lon, note). ``bounds``: project.country_bounds (swap detection, range check)."""
    lat, lon, note = _raw(text)
    notes = [note] if note else []

    def inside(a, o):
        if not bounds:
            return -90 <= a <= 90 and -180 <= o <= 180
        return (float(bounds.get("min_lat", -90)) <= a <= float(bounds.get("max_lat", 90))
                and float(bounds.get("min_lon", -180)) <= o <= float(bounds.get("max_lon", 180)))

    if not inside(lat, lon):
        if inside(lon, lat):
            lat, lon = lon, lat
            notes.append("latitude and longitude were swapped")
        else:
            raise LocationError("%.6f, %.6f is outside the project area" % (lat, lon))
    return {"lat": round(lat, 7), "lon": round(lon, 7), "note": "; ".join(notes)}
