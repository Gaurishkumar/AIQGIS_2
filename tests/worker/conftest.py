import copy
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "worker"))

import bpa_worker  # noqa: E402,F401  (adds common/ to sys.path)
from bpa_common.config import load_config  # noqa: E402

SAMPLE_XLSX = os.environ.get("BPA_SAMPLE_XLSX", r"C:\Users\dhana\Downloads\Outsource Excel Input_4425.xlsx")
MODEL = os.path.join(ROOT, "data", "models", "sam2.1_hiera_base_plus.pt")


@pytest.fixture(scope="session")
def base_cfg():
    cfg = load_config(os.path.join(ROOT, "config", "config.yaml"))
    # Tests never depend on client imagery registered in this project (config/imagery_sources.yaml).
    cfg["imagery"]["sources_file"] = None
    cfg["imagery"]["providers"] = [p for p in cfg["imagery"].get("providers") or [] if not p.get("added_by")]
    return cfg


@pytest.fixture
def cfg(base_cfg):
    return copy.deepcopy(base_cfg)


@pytest.fixture(scope="session")
def synthetic(tmp_path_factory, base_cfg):
    """Rendered 0.3 m scene + Overture-like reference data (shared by the session)."""
    from bpa_worker import synthetic as S
    d = tmp_path_factory.mktemp("synthetic")
    scene = S.render_scene(str(d / "ortho.tif"))
    S.write_reference(str(d / "ref"), scene)
    return {"dir": str(d), "scene": scene, "ortho": str(d / "ortho.tif"), "ref": str(d / "ref"),
            "records": S.survey_records(scene), "truth": S.truth_by_survey(scene)}


def synthetic_cfg(base_cfg, synthetic, imagery=True, allow=True, licence="SYNTHETIC-TEST-001"):
    c = copy.deepcopy(base_cfg)
    c["reference"]["dir"] = synthetic["ref"]
    c["reference"]["files"]["addresses"] = None
    c["imagery"]["providers"] = []
    if imagery:
        c["imagery"]["providers"] = [{"id": "synthetic_ortho", "type": "local_raster", "paths": [synthetic["ortho"]],
                                      "allow_automated_analysis": allow, "licence_ref": licence,
                                      "capture_date": "2025-01-15"}]
    return c


needs_model = pytest.mark.skipif(not os.path.isfile(MODEL), reason="SAM 2.1 checkpoint not downloaded")
needs_sample = pytest.mark.skipif(not os.path.isfile(SAMPLE_XLSX), reason="client sample workbook not available")
