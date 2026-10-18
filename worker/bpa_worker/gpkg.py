"""Minimal GeoPackage 1.3 writer on top of sqlite3.

The worker is the only writer of work.gpkg. Writing through sqlite3 directly
gives transactional upserts without an OGR dependency; the files are standard
GeoPackages that QGIS, GDAL and pyogrio read natively. Tables are created
without the R-tree extension so no GDAL SQL functions are needed on write.
"""

import sqlite3
import struct
import threading

import shapely

GPKG_APPLICATION_ID = 0x47504B47
GPKG_USER_VERSION = 10300

_WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
    'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
    'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AXIS["Latitude",NORTH],AXIS["Longitude",EAST],AUTHORITY["EPSG","4326"]]'
)


def encode_geometry(geom, srs_id=4326):
    """Shapely geometry -> GeoPackage binary (little endian, XY envelope)."""
    if geom is None:
        return None
    if geom.is_empty:
        header = b"GP" + bytes([0, 0x01 | 0x10]) + struct.pack("<i", srs_id)
        return header + shapely.to_wkb(geom, byte_order=1, output_dimension=2)
    minx, miny, maxx, maxy = geom.bounds
    flags = 0x01 | (1 << 1)  # little endian + [minx, maxx, miny, maxy]
    header = b"GP" + bytes([0, flags]) + struct.pack("<i", srs_id)
    header += struct.pack("<4d", minx, maxx, miny, maxy)
    return header + shapely.to_wkb(geom, byte_order=1, output_dimension=2)


def decode_geometry(blob):
    if blob is None:
        return None
    b = bytes(blob)
    if b[:2] != b"GP":
        raise ValueError("Not a GeoPackage geometry blob")
    flags = b[3]
    env = (flags >> 1) & 0x07
    env_len = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[env]
    return shapely.from_wkb(b[8 + env_len:])


class GeoPackage(object):
    """Thread-safe single-writer GeoPackage."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self._init_core()
        self._tables = {}

    # -- core tables ---------------------------------------------------------
    def _init_core(self):
        with self._lock:
            c = self.conn
            c.execute("PRAGMA application_id=%d" % GPKG_APPLICATION_ID)
            c.execute("PRAGMA user_version=%d" % GPKG_USER_VERSION)
            c.execute("""CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL, description TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
                identifier TEXT UNIQUE, description TEXT DEFAULT '',
                last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
                srs_id INTEGER, CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id)
                REFERENCES gpkg_spatial_ref_sys(srs_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (
                table_name TEXT NOT NULL, column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
                z TINYINT NOT NULL, m TINYINT NOT NULL,
                CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
                CONSTRAINT uk_gc_table_name UNIQUE (table_name),
                CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
                CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id))""")
            rows = [
                ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
                ("WGS 84 geodetic", 4326, "EPSG", 4326, _WGS84_WKT, "longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid"),
            ]
            c.executemany("INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", rows)

    # -- schema ----------------------------------------------------------------
    def create_table(self, name, columns, geometry_type=None, key=None, srs_id=4326, description=""):
        """Create a feature or attribute table.

        columns: list of (name, sqlite_type). key: unique column for upserts.
        """
        with self._lock:
            cols = ["fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL"]
            if geometry_type:
                cols.append("geom %s" % geometry_type.upper())
            for cname, ctype in columns:
                extra = " UNIQUE" if cname == key else ""
                cols.append('"%s" %s%s' % (cname, ctype, extra))
            self.conn.execute('CREATE TABLE IF NOT EXISTS "%s" (%s)' % (name, ", ".join(cols)))
            exists = self.conn.execute("SELECT 1 FROM gpkg_contents WHERE table_name=?", (name,)).fetchone()
            if not exists:
                self.conn.execute(
                    "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, srs_id) VALUES (?,?,?,?,?)",
                    (name, "features" if geometry_type else "attributes", name, description,
                     srs_id if geometry_type else None))
                if geometry_type:
                    self.conn.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,0,0)",
                                      (name, "geom", geometry_type.upper(), srs_id))
            self._tables[name] = {"columns": [c for c, _ in columns], "geom": bool(geometry_type),
                                  "key": key, "srs_id": srs_id}

    # -- writes -----------------------------------------------------------------
    def upsert(self, table, row, geom=None):
        """Insert or update one row by the table's key column."""
        meta = self._tables[table]
        cols = [c for c in meta["columns"] if c in row]
        values = [row[c] for c in cols]
        if meta["geom"]:
            cols = ["geom"] + cols
            values = [encode_geometry(geom, meta["srs_id"])] + values
        placeholders = ",".join("?" for _ in cols)
        quoted = ",".join('"%s"' % c for c in cols)
        sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (table, quoted, placeholders)
        key = meta["key"]
        if key:
            updates = ",".join('"%s"=excluded."%s"' % (c, c) for c in cols if c != key)
            sql += ' ON CONFLICT("%s") DO UPDATE SET %s' % (key, updates or '"%s"=excluded."%s"' % (key, key))
        with self._lock:
            self.conn.execute(sql, values)
            self._touch(table, geom)

    def insert(self, table, row, geom=None):
        meta = self._tables[table]
        cols = [c for c in meta["columns"] if c in row]
        values = [row[c] for c in cols]
        if meta["geom"]:
            cols = ["geom"] + cols
            values = [encode_geometry(geom, meta["srs_id"])] + values
        sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (
            table, ",".join('"%s"' % c for c in cols), ",".join("?" for _ in cols))
        with self._lock:
            cur = self.conn.execute(sql, values)
            self._touch(table, geom)
            return cur.lastrowid

    def delete_where(self, table, column, value):
        with self._lock:
            self.conn.execute('DELETE FROM "%s" WHERE "%s"=?' % (table, column), (value,))

    def _touch(self, table, geom):
        if geom is not None and not geom.is_empty:
            minx, miny, maxx, maxy = geom.bounds
            self.conn.execute(
                """UPDATE gpkg_contents SET last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                   min_x=MIN(COALESCE(min_x, ?), ?), min_y=MIN(COALESCE(min_y, ?), ?),
                   max_x=MAX(COALESCE(max_x, ?), ?), max_y=MAX(COALESCE(max_y, ?), ?)
                   WHERE table_name=?""",
                (minx, minx, miny, miny, maxx, maxx, maxy, maxy, table))
        else:
            self.conn.execute("UPDATE gpkg_contents SET last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name=?",
                              (table,))

    def transaction(self):
        return _Tx(self)

    # -- reads ---------------------------------------------------------------------
    def rows(self, table, where=None, params=()):
        sql = 'SELECT * FROM "%s"' % table
        if where:
            sql += " WHERE " + where
        with self._lock:
            out = []
            for r in self.conn.execute(sql, params).fetchall():
                d = dict(r)
                if "geom" in d:
                    d["geom"] = decode_geometry(d["geom"])
                out.append(d)
            return out

    def close(self):
        with self._lock:
            self.conn.close()


class _Tx(object):
    def __init__(self, gp):
        self.gp = gp

    def __enter__(self):
        self.gp._lock.acquire()
        self.gp.conn.execute("BEGIN")
        return self.gp

    def __exit__(self, exc_type, exc, tb):
        try:
            self.gp.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.gp._lock.release()
        return False
