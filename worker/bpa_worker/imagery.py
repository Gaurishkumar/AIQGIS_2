"""S6 - Imagery providers, licence gate and georeferenced crops.

Every provider declares ``allow_automated_analysis`` and a ``licence_ref``.
The worker refuses to crop from any provider whose gate is closed, and every
crop records provider, capture date, ground resolution and licence reference.
Crops are written in the record's local UTM CRS so pixels line up with the
metric geometry used everywhere else.
"""

import datetime as dt
import logging
import math
import os
import threading
import urllib.request
from dataclasses import dataclass, field

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import from_bounds
from shapely.geometry import box

from bpa_common.config import get, resolve_path

from .geo import reproject as reproject_geom

log = logging.getLogger(__name__)


class LicenceGateClosed(PermissionError):
    """Raised when a crop is requested from a source without automated-analysis rights."""


class ImageryUnavailable(RuntimeError):
    pass


@dataclass
class Crop:
    image: np.ndarray            # H x W x 3 uint8
    transform: object            # affine, record UTM
    epsg: int
    gsd_m: float
    path: str
    provider_id: str
    licence_ref: str
    capture_date: str
    bounds: tuple                # UTM (minx, miny, maxx, maxy)
    meta: dict = field(default_factory=dict)

    @property
    def shape(self):
        return self.image.shape[:2]

    def to_pixel(self, x, y):
        col, row = ~self.transform @ (x, y)
        return float(col), float(row)

    def to_map(self, col, row):
        return self.transform @ (col, row)


def _to_uint8(arr):
    """Scale any band stack to uint8 (2-98 percentile stretch for non-8-bit data)."""
    if arr.dtype == np.uint8:
        return arr
    out = np.zeros(arr.shape, dtype=np.uint8)
    for i in range(arr.shape[0]):
        band = arr[i].astype(np.float64)
        valid = band[np.isfinite(band) & (band != 0)]
        if valid.size == 0:
            continue
        lo, hi = np.percentile(valid, 2), np.percentile(valid, 98)
        if hi <= lo:
            hi = lo + 1
        out[i] = np.clip((band - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    return out


def _rgb(arr, bands=None):
    """(bands, H, W) -> (H, W, 3) uint8."""
    if bands:
        arr = arr[[b - 1 for b in bands]]
    if arr.shape[0] == 1:
        arr = np.repeat(arr, 3, axis=0)
    elif arr.shape[0] > 3:
        arr = arr[:3]
    arr = _to_uint8(arr)
    return np.transpose(arr, (1, 2, 0)).copy()


def aoi_bounds(geom_utm, buffer_m, min_size_m):
    """Candidate bbox buffered by ``buffer_m``, expanded to at least min_size x min_size."""
    minx, miny, maxx, maxy = geom_utm.buffer(buffer_m).bounds
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    w = max(maxx - minx, min_size_m)
    h = max(maxy - miny, min_size_m)
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def enlarge_bounds(bounds, factor):
    minx, miny, maxx, maxy = bounds
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    w, h = (maxx - minx) * factor, (maxy - miny) * factor
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def _date_key(text):
    """Sortable day number from '2025:01:15 10:00:00' / '2025-01-15' / '' (unknown = oldest)."""
    t = str(text or "").strip().replace(":", "-", 2)[:10]
    try:
        return dt.date.fromisoformat(t).toordinal()
    except ValueError:
        return 0


def write_crop(path, image, transform, epsg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h, w = image.shape[:2]
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=3, dtype="uint8",
                       crs=CRS.from_epsg(epsg), transform=transform, compress="deflate",
                       photometric="RGB") as dst:
        dst.write(np.transpose(image, (2, 0, 1)))


def read_crop(path):
    with rasterio.open(path) as src:
        img = _rgb(src.read())
        return img, src.transform, src.crs.to_epsg(), abs(src.transform.a)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class Provider(object):
    kind = "base"

    def __init__(self, cfg, spec):
        self.cfg = cfg
        self.spec = spec
        self.id = spec["id"]
        self.allow = bool(spec.get("allow_automated_analysis", False))
        self.licence_ref = str(spec.get("licence_ref") or "").strip()
        self.capture_date = str(spec.get("capture_date") or "")
        self.priority = int(spec.get("priority", 100))
        self.max_gsd = float(get(cfg, "imagery.max_gsd_m", 0.6))
        # Very sharp imagery (2-5 cm drone) of a large building would give a huge crop that SAM
        # shrinks to 1024 px anyway: crops are resampled so the long side stays within max_pixels.
        self.max_px = int(get(cfg, "imagery.crop.max_pixels", 2048) or 0)
        self.max_blank = float(get(cfg, "imagery.max_blank_fraction", 0.25))

    def work_gsd(self, native_gsd, bounds_utm):
        """Ground resolution of the crop: the native one unless the crop would exceed max_pixels."""
        minx, miny, maxx, maxy = bounds_utm
        if not self.max_px:
            return native_gsd
        return max(native_gsd, (maxx - minx) / self.max_px, (maxy - miny) / self.max_px)

    def check_blank(self, image):
        """Refuse crops that are mostly no-data (irregular drone mosaics, image edges)."""
        blank = float(np.mean(np.all(image == 0, axis=2))) if image.size else 1.0
        if blank > self.max_blank:
            raise ImageryUnavailable("%s has no picture for %.0f%% of this area" % (self.id, blank * 100))

    @property
    def gate_open(self):
        return self.allow and bool(self.licence_ref)

    def gate_reason(self):
        if not self.allow:
            return "allow_automated_analysis is false"
        if not self.licence_ref:
            return "no licence_ref recorded"
        return ""

    def check_gate(self):
        if not self.gate_open:
            raise LicenceGateClosed("Imagery source %r refused: %s" % (self.id, self.gate_reason()))

    def status(self):
        return {"id": self.id, "type": self.kind, "allow_automated_analysis": self.allow,
                "licence_ref": self.licence_ref, "enabled": self.gate_open,
                "reason": self.gate_reason(), "capture_date": self.capture_date,
                "gsd_m": self.gsd_hint()}

    def gsd_hint(self):
        return self.spec.get("gsd_m")

    def covers(self, bounds_utm, epsg):
        raise NotImplementedError

    def crop(self, bounds_utm, epsg, out_path):
        raise NotImplementedError


class LocalRasterProvider(Provider):
    """GeoTIFF / COG / VRT files on disk (client-licensed or purchased imagery)."""
    kind = "local_raster"

    def __init__(self, cfg, spec):
        super().__init__(cfg, spec)
        paths = spec.get("paths") or ([spec["path"]] if spec.get("path") else [])
        self.paths = [resolve_path(cfg, p) for p in paths]
        self.bands = spec.get("bands")
        self._info = []
        for p in self.paths:
            if not os.path.exists(p):
                log.warning("Imagery file for %s not found: %s", self.id, p)
                continue
            with rasterio.open(p) as src:
                b4326 = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
                gsd = self._gsd(src)
                self._info.append({"path": p, "bounds4326": b4326, "gsd": gsd,
                                   "tags": src.tags()})

    @staticmethod
    def _gsd(src):
        if src.crs and src.crs.is_projected:
            try:
                factor = float(src.crs.linear_units_factor[1])
            except Exception:
                factor = 1.0
            return abs(src.res[0]) * factor
        # geographic: metres per degree of longitude at the image centre
        lat = (src.bounds.bottom + src.bounds.top) / 2.0
        return abs(src.res[0]) * 111320.0 * math.cos(math.radians(lat))

    def gsd_hint(self):
        return round(min(i["gsd"] for i in self._info), 3) if self._info else self.spec.get("gsd_m")

    def _find(self, bounds_utm, epsg):
        """Best file covering the area: usable resolution first, then the sharpest, then the newest."""
        minx, miny, maxx, maxy = bounds_utm
        aoi = reproject_geom(box(minx, miny, maxx, maxy), epsg, 4326)
        covering = [i for i in self._info if box(*i["bounds4326"]).buffer(1e-7).contains(aoi)]
        if not covering:
            return None
        return min(covering, key=lambda i: (i["gsd"] > self.max_gsd, round(i["gsd"], 2), -_date_key(
            i["tags"].get("TIFFTAG_DATETIME") or self.capture_date)))

    def covers(self, bounds_utm, epsg):
        return self._find(bounds_utm, epsg) is not None

    def crop(self, bounds_utm, epsg, out_path):
        self.check_gate()
        info = self._find(bounds_utm, epsg)
        if info is None:
            raise ImageryUnavailable("%s does not cover the area of interest" % self.id)
        if info["gsd"] > self.max_gsd:
            raise ImageryUnavailable("%s resolution %.2f m is coarser than %.2f m" % (self.id, info["gsd"], self.max_gsd))
        minx, miny, maxx, maxy = bounds_utm
        dst_crs = CRS.from_epsg(epsg)
        with rasterio.open(info["path"]) as src:
            gsd = self.work_gsd(info["gsd"], bounds_utm)
            north_up = src.transform.b == 0 and src.transform.d == 0
            if src.crs == dst_crs and north_up and gsd <= info["gsd"] * 1.001:
                win = from_bounds(minx, miny, maxx, maxy, transform=src.transform)
                data = src.read(window=win, boundless=True, fill_value=0)
                transform = src.window_transform(win)
                gsd = abs(src.transform.a)
            else:
                # Other CRS, rotated image or resampled crop: warp onto exactly the crop grid.
                width = max(1, int(round((maxx - minx) / gsd)))
                height = max(1, int(round((maxy - miny) / gsd)))
                transform = from_origin(minx, maxy, (maxx - minx) / width, (maxy - miny) / height)
                # The VRT is defined on exactly the crop grid, so reading it whole is the
                # windowed read (GDAL only fetches the source blocks it needs).
                with WarpedVRT(src, crs=dst_crs, transform=transform, width=width, height=height,
                               resampling=Resampling.bilinear, src_nodata=src.nodata, nodata=0) as vrt:
                    data = vrt.read()
        image = _rgb(data, self.bands)
        self.check_blank(image)
        write_crop(out_path, image, transform, epsg)
        capture = self.capture_date or info["tags"].get("TIFFTAG_DATETIME", "")
        return Crop(image=image, transform=transform, epsg=epsg, gsd_m=float(gsd), path=out_path,
                    provider_id=self.id, licence_ref=self.licence_ref, capture_date=capture,
                    bounds=bounds_utm, meta={"source_file": os.path.basename(info["path"])})


class XYZTileProvider(Provider):
    """Licensed XYZ/TMS tile service (or a local {z}/{x}/{y} tile folder)."""
    kind = "xyz_tiles"
    TILE = 256
    ORIGIN = 20037508.342789244

    def __init__(self, cfg, spec):
        super().__init__(cfg, spec)
        self.url = spec["url"]
        self.zoom = int(spec.get("zoom", 19))
        self.headers = dict(spec.get("headers") or {})
        self.headers.setdefault("User-Agent", "bpa-worker/1.0 (licensed imagery client)")
        self.timeout = float(spec.get("timeout_s", 20))
        self.coverage = spec.get("bounds")  # optional [W, S, E, N] in EPSG:4326
        self._lock = threading.Lock()

    def gsd_hint(self):
        return round(2 * self.ORIGIN / (self.TILE * 2 ** self.zoom), 3)

    def covers(self, bounds_utm, epsg):
        if not self.coverage:
            return True
        aoi = reproject_geom(box(*bounds_utm), epsg, 4326)
        return box(*self.coverage).contains(aoi)

    def _tile(self, z, x, y):
        url = self.url.replace("{-y}", str(2 ** z - 1 - y)).format(z=z, x=x, y=y)
        if url.startswith("file://") or os.path.exists(url):
            path = url[7:] if url.startswith("file://") else url
            with open(path, "rb") as fh:
                return fh.read()
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def crop(self, bounds_utm, epsg, out_path):
        self.check_gate()
        import cv2
        minx, miny, maxx, maxy = bounds_utm
        w3857, s3857, e3857, n3857 = transform_bounds(CRS.from_epsg(epsg), CRS.from_epsg(3857),
                                                      minx, miny, maxx, maxy, densify_pts=21)
        z = self.zoom
        res = 2 * self.ORIGIN / (self.TILE * 2 ** z)
        x0 = int(math.floor((w3857 + self.ORIGIN) / (res * self.TILE)))
        x1 = int(math.floor((e3857 + self.ORIGIN) / (res * self.TILE)))
        y0 = int(math.floor((self.ORIGIN - n3857) / (res * self.TILE)))
        y1 = int(math.floor((self.ORIGIN - s3857) / (res * self.TILE)))
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 400:
            raise ImageryUnavailable("Area of interest needs too many tiles")
        mosaic = np.zeros(((y1 - y0 + 1) * self.TILE, (x1 - x0 + 1) * self.TILE, 3), np.uint8)
        with self._lock:
            for ty in range(y0, y1 + 1):
                for tx in range(x0, x1 + 1):
                    data = self._tile(z, tx, ty)
                    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    if img is None:
                        raise ImageryUnavailable("Tile %d/%d/%d could not be decoded" % (z, tx, ty))
                    img = cv2.resize(img, (self.TILE, self.TILE)) if img.shape[:2] != (self.TILE, self.TILE) else img
                    oy, ox = (ty - y0) * self.TILE, (tx - x0) * self.TILE
                    mosaic[oy:oy + self.TILE, ox:ox + self.TILE] = img[:, :, ::-1]
        src_transform = from_origin(-self.ORIGIN + x0 * self.TILE * res, self.ORIGIN - y0 * self.TILE * res, res, res)
        lat = (reproject_geom(box(*bounds_utm), epsg, 4326).centroid.y)
        gsd = res * math.cos(math.radians(lat))
        if gsd > self.max_gsd:
            raise ImageryUnavailable("%s zoom %d gives %.2f m, coarser than %.2f m" % (self.id, z, gsd, self.max_gsd))
        gsd = self.work_gsd(gsd, bounds_utm)
        width = max(1, int(round((maxx - minx) / gsd)))
        height = max(1, int(round((maxy - miny) / gsd)))
        dst_transform = from_origin(minx, maxy, (maxx - minx) / width, (maxy - miny) / height)
        out = np.zeros((3, height, width), np.uint8)
        reproject(np.transpose(mosaic, (2, 0, 1)), out, src_transform=src_transform, src_crs=CRS.from_epsg(3857),
                  dst_transform=dst_transform, dst_crs=CRS.from_epsg(epsg), resampling=Resampling.bilinear)
        image = np.transpose(out, (1, 2, 0)).copy()
        self.check_blank(image)
        write_crop(out_path, image, dst_transform, epsg)
        return Crop(image=image, transform=dst_transform, epsg=epsg, gsd_m=float(gsd), path=out_path,
                    provider_id=self.id, licence_ref=self.licence_ref, capture_date=self.capture_date,
                    bounds=bounds_utm, meta={"zoom": z})


PROVIDER_TYPES = {"local_raster": LocalRasterProvider, "xyz_tiles": XYZTileProvider}


class ImageryManager(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.providers = []
        for spec in get(cfg, "imagery.providers", []) or []:
            cls = PROVIDER_TYPES.get(spec.get("type"))
            if cls is None:
                log.error("Unknown imagery provider type %r (%s)", spec.get("type"), spec.get("id"))
                continue
            try:
                self.providers.append(cls(cfg, spec))
            except Exception as exc:
                log.error("Imagery provider %s could not be initialised: %s", spec.get("id"), exc)
        self.providers.sort(key=lambda p: p.priority)
        crop = get(cfg, "imagery.crop", {}) or {}
        self.buffer_m = float(crop.get("buffer_m", 15.0))
        self.min_size_m = float(crop.get("min_size_m", 60.0))
        self.enlarge_factor = float(crop.get("enlarge_factor", 1.6))
        self.max_edge_retries = int(crop.get("max_edge_retries", 1))

    def status(self):
        return [p.status() for p in self.providers]

    def any_enabled(self):
        return any(p.gate_open for p in self.providers)

    def select(self, bounds_utm, epsg):
        """First licence-open provider covering the AOI (None if there is none)."""
        for p in self.providers:
            if not p.gate_open:
                continue
            try:
                if p.covers(bounds_utm, epsg):
                    return p
            except Exception as exc:
                log.warning("Coverage check failed for %s: %s", p.id, exc)
        return None

    def get_provider(self, provider_id):
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None

    def request_record(self, survey_id, provider, crop, aoi_wgs84, status, error=""):
        return {
            "request_id": "%s:%s:%s" % (survey_id, provider.id if provider else "none",
                                        dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")),
            "survey_id": survey_id,
            "provider": provider.id if provider else "",
            "licence_ref": provider.licence_ref if provider else "",
            "capture_date": (crop.capture_date if crop else (provider.capture_date if provider else "")),
            "gsd_m": crop.gsd_m if crop else None,
            "crop_path": crop.path if crop else "",
            "status": status,
            "error": error,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "aoi": aoi_wgs84,
        }
