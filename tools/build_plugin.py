"""Build (and optionally install) the QGIS plugin.

  python tools/build_plugin.py            -> sync shared code, dist/building_polygon_automation-<v>.zip
  python tools/build_plugin.py --install  -> also copy into the QGIS 4 / QGIS 3 default profile
  python tools/build_plugin.py --install --profile-dir <...>/python/plugins
  python tools/build_plugin.py --repo <folder> --repo-url https://downloads.example.com/bpa/
                                          -> private plugin repository (plugins.xml + zip) for the
                                             QGIS Plugin Manager; upload the folder to that URL

The shared package (common/bpa_common) is vendored into the plugin so the
plugin runs in QGIS's own Python without the worker environment.
"""

import argparse
import configparser
import datetime as dt
import os
import shutil
import sys
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "qgis_plugin", "building_polygon_automation")
COMMON = os.path.join(ROOT, "common", "bpa_common")
NAME = "building_polygon_automation"
SKIP = ("__pycache__", ".pyc", ".pyo")


def sync_common():
    dst = os.path.join(PLUGIN, "bpa_common")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(COMMON, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dst


def make_icon(path):
    if os.path.exists(path):
        return
    try:
        import numpy as np
        import cv2
    except ImportError:
        return
    img = np.zeros((64, 64, 4), np.uint8)
    cv2.rectangle(img, (2, 2), (61, 61), (160, 90, 30, 255), -1)
    pts = np.array([[14, 50], [14, 22], [32, 12], [50, 22], [50, 50]], np.int32)
    cv2.fillPoly(img, [pts], (40, 200, 255, 255))
    cv2.polylines(img, [pts], True, (255, 255, 255, 255), 3)
    cv2.circle(img, (47, 47), 8, (60, 200, 60, 255), -1)
    cv2.imwrite(path, img)


def build():
    sync_common()
    make_icon(os.path.join(PLUGIN, "icon.png"))
    meta = configparser.ConfigParser()
    meta.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    out = os.path.join(dist, "%s-%s.zip" % (NAME, version))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(PLUGIN):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for f in files:
                if f.endswith(SKIP):
                    continue
                full = os.path.join(root, f)
                z.write(full, os.path.join(NAME, os.path.relpath(full, PLUGIN)))
    return out


def read_metadata():
    meta = configparser.ConfigParser(interpolation=None)
    meta.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    return meta["general"]


def make_repository(zip_path, out_dir, base_url):
    """Private plugin repository: plugins.xml + the zip, in the layout the QGIS Plugin Manager reads.

    QGIS takes the plugin id from the zip name (before the first dot), so the zip is published as
    building_polygon_automation.<version>.zip. ``base_url``: where ``out_dir`` will be served
    (https://... for a server, or file:///C:/... for a shared/local folder).
    """
    g = read_metadata()
    version = g["version"]
    os.makedirs(out_dir, exist_ok=True)
    zip_name = "%s.%s.zip" % (NAME, version)
    shutil.copyfile(zip_path, os.path.join(out_dir, zip_name))
    shutil.copyfile(os.path.join(PLUGIN, "icon.png"), os.path.join(out_dir, "%s.png" % NAME))
    base = base_url if base_url.endswith("/") else base_url + "/"
    today = dt.date.today().isoformat()

    def el(tag, value, cdata=False):
        value = "" if value is None else str(value)
        return "    <%s>%s</%s>\n" % (tag, "<![CDATA[%s]]>" % value if cdata else escape(value), tag)

    body = ['<?xml version="1.0" encoding="UTF-8"?>\n<plugins>\n',
            '  <pyqgis_plugin name="%s" version="%s" plugin_id="1">\n' % (escape(g["name"]), escape(version))]
    for tag, value, cdata in (
            ("description", g.get("description"), True), ("about", g.get("about"), True),
            ("version", version, False), ("qgis_minimum_version", g.get("qgisMinimumVersion"), False),
            ("qgis_maximum_version", g.get("qgisMaximumVersion"), False), ("homepage", g.get("homepage"), False),
            ("file_name", zip_name, False), ("icon", base + "%s.png" % NAME, False),
            ("author_name", g.get("author"), False), ("download_url", base + zip_name, False),
            ("uploaded_by", g.get("author"), False), ("create_date", today, False), ("update_date", today, False),
            ("experimental", g.get("experimental", "False"), False), ("deprecated", "False", False),
            ("tracker", g.get("tracker"), False), ("repository", g.get("repository"), False),
            ("tags", g.get("tags"), False), ("changelog", g.get("changelog"), True), ("category", g.get("category"), False),
            ("downloads", 0, False), ("average_vote", 0, False), ("rating_votes", 0, False),
            ("external_dependencies", "", False), ("server", "False", False)):
        body.append(el(tag, value, cdata))
    body.append("  </pyqgis_plugin>\n</plugins>\n")
    xml = os.path.join(out_dir, "plugins.xml")
    with open(xml, "w", encoding="utf-8") as fh:
        fh.write("".join(body))
    return xml


def default_profile_dirs():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~/.local/share")
    out = []
    for v in ("QGIS4", "QGIS3"):
        d = os.path.join(appdata, "QGIS", v, "profiles", "default")
        if os.path.isdir(d):
            out.append(os.path.join(d, "python", "plugins"))
    return out


def install(zip_path, plugin_dirs):
    done = []
    for d in plugin_dirs:
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, NAME)
        if os.path.exists(target):
            shutil.rmtree(target)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(d)
        done.append(target)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--profile-dir", action="append", help="QGIS python/plugins folder (repeatable)")
    ap.add_argument("--repo", help="write a private plugin repository (plugins.xml + zip) into this folder")
    ap.add_argument("--repo-url", help="URL the --repo folder is served from (https://... or file:///...)")
    args = ap.parse_args()
    z = build()
    print("built", z)
    if args.repo:
        url = args.repo_url or "file:///" + os.path.abspath(args.repo).replace(os.sep, "/")
        print("repository", make_repository(z, args.repo, url))
        print("QGIS: Plugins > Manage and Install > Settings > Add... URL = %splugins.xml" % (
            url if url.endswith("/") else url + "/"))
    if args.install:
        dirs = args.profile_dir or default_profile_dirs()
        if not dirs:
            sys.exit("No QGIS profile found; pass --profile-dir")
        for t in install(z, dirs):
            print("installed", t)


if __name__ == "__main__":
    main()
