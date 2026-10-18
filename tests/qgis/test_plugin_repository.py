"""The private plugin repository is read by QGIS's own Plugin Manager code.

  "C:\\Program Files\\QGIS 4.2.2\\bin\\python-qgis.bat" tests\\qgis\\test_plugin_repository.py

Builds plugins.xml + zip with tools/build_plugin.py --repo, registers it as a repository
(file:// URL, as for a shared folder; an https:// server behaves the same), lets
pyplugin_installer fetch it and checks the plugin is listed with the right id and version
and that its download contains the whole plugin.
"""

import configparser
import os
import shutil
import sys
import tempfile
import time
import zipfile

from harness import ROOT, Check, app

app()

from qgis.core import QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_plugin  # noqa: E402


def main():
    check = Check()
    repo = tempfile.mkdtemp(prefix="bpa_repo_")
    url = "file:///" + repo.replace(os.sep, "/") + "/"
    z = build_plugin.build()
    xml = build_plugin.make_repository(z, repo, url)
    meta = configparser.ConfigParser(interpolation=None)
    meta.read(os.path.join(build_plugin.PLUGIN, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    check(os.path.isfile(xml) and os.path.isfile(os.path.join(repo, "building_polygon_automation.%s.zip" % version)),
          "repository folder has plugins.xml and the versioned zip")

    from pyplugin_installer.installer_data import Repositories, plugins, repositories
    s = QgsSettings()
    s.setValue("app/plugin_repositories/BPA private/url", url + "plugins.xml")
    s.setValue("app/plugin_repositories/BPA private/enabled", True)
    repositories.load()
    for key in list(repositories.mRepositories):
        if key != "BPA private":
            repositories.mRepositories[key]["enabled"] = False     # never touch the internet in a test
    repositories.requestFetching("BPA private")
    end = time.time() + 30
    while time.time() < end and repositories.mRepositories["BPA private"]["state"] == Repositories.STATE_LOADING:
        QCoreApplication.processEvents()
        time.sleep(0.05)
    state = repositories.mRepositories["BPA private"]["state"]
    check(state == Repositories.STATE_LOADED,
          "QGIS Plugin Manager loads the repository (state %s %s)" % (state, repositories.mRepositories["BPA private"]["error"]))
    listed = [p for p in plugins.repoCache.get("BPA private", []) if p["id"] == "building_polygon_automation"]
    check(len(listed) == 1, "plugin is listed under its folder id (searchable in Plugins > Manage and Install)")
    if listed:
        p = listed[0]
        check(p["version_available"] == version and p["name"] == "Building Polygon Automation",
              "name and version %s shown" % version)
        local = p["download_url"].replace("file:///", "")
        check(os.path.isfile(local), "download link points at the zip")
        names = zipfile.ZipFile(local).namelist()
        need = ["building_polygon_automation/%s" % f for f in
                ("metadata.txt", "plugin.py", "entry_dialog.py", "imagery_dialog.py", "bpa_common/explain.py",
                 "bpa_common/entry.py")]
        check(all(n in names for n in need), "zip contains the plugin with the new modules")
        check(not any(n.endswith((".pyc", "__pycache__/")) for n in names), "no compiled caches in the zip")
    s.remove("app/plugin_repositories/BPA private")
    shutil.rmtree(repo, ignore_errors=True)
    return check.done()


if __name__ == "__main__":
    sys.exit(main())
