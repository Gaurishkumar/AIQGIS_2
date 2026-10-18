"""Configuration loading, lookup and hashing."""

import copy
import hashlib
import json
import os

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with QGIS and the worker env
    yaml = None


class ConfigError(ValueError):
    pass


def load_config(path):
    """Load a YAML config file and resolve relative paths against the project root.

    The project root is the directory that contains the ``config`` folder (or
    the config file's own directory when it is not inside ``config``).
    """
    if yaml is None:
        raise ConfigError("PyYAML is required to read the configuration")
    path = os.path.abspath(path)
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ConfigError("Configuration root must be a mapping: %s" % path)
    cfg.setdefault("_meta", {})
    cfg["_meta"]["path"] = path
    cfg["_meta"]["root"] = project_root_for(path)
    if "frozen" not in cfg:          # a frozen batch config already carries its sources
        merge_imagery_sources(cfg)
    validate_config(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Imagery sources registered from the plugin (client imagery + permission record)
# ---------------------------------------------------------------------------

def imagery_sources_path(cfg):
    """Registered-imagery file; None when imagery.sources_file is set to null (disabled)."""
    return resolve_path(cfg, get(cfg, "imagery.sources_file", "config/imagery_sources.yaml"))


def _read_sources(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return [s for s in (data.get("sources") or []) if isinstance(s, dict) and s.get("id")]


def merge_imagery_sources(cfg):
    """Append the registered sources to imagery.providers (a provider in config.yaml wins on id)."""
    sources = _read_sources(imagery_sources_path(cfg))
    if not sources:
        return cfg
    img = cfg.setdefault("imagery", {}) or {}
    providers = list(img.get("providers") or [])
    ids = {p.get("id") for p in providers}
    providers.extend(copy.deepcopy(s) for s in sources if s["id"] not in ids)
    img["providers"] = providers
    cfg["imagery"] = img
    return cfg


def _replace_by_id(items, spec):
    out = [s for s in items if s.get("id") != spec["id"]]
    out.append(spec)
    return out


def _write_yaml(path, data, header=""):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        if header:
            fh.write(header)
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def save_imagery_source(cfg, spec):
    """Register (or replace, by id) an imagery source for every new batch of this project."""
    if yaml is None:
        raise ConfigError("PyYAML is required to write the configuration")
    path = imagery_sources_path(cfg) or resolve_path(cfg, "config/imagery_sources.yaml")
    sources = _replace_by_id(_read_sources(path), spec)
    _write_yaml(path, {"sources": sources},
                "# Imagery added with 'Add client imagery'. Each entry records who gave permission for\n"
                "# automated processing (licence_ref). Remove an entry to stop using that imagery.\n")
    return path


def add_imagery_to_batch(batch_config_path, spec):
    """Add (or replace) a source in a batch's frozen config so re-processing that batch uses it."""
    if yaml is None:
        raise ConfigError("PyYAML is required to write the configuration")
    with open(batch_config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    img = data.get("imagery") or {}
    img["providers"] = _replace_by_id(list(img.get("providers") or []), spec)
    data["imagery"] = img
    _write_yaml(batch_config_path, data)
    return batch_config_path


def project_root_for(config_path):
    d = os.path.dirname(os.path.abspath(config_path))
    if os.path.basename(d).lower() == "config":
        return os.path.dirname(d)
    # Frozen batch config (batch_<id>/config.yaml): root recorded inside it.
    return d


def resolve_path(cfg, p):
    """Resolve a config path relative to the project root."""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    root = cfg.get("_meta", {}).get("project_root") or cfg.get("_meta", {}).get("root") or os.getcwd()
    return os.path.normpath(os.path.join(root, p))


def get(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def public_dict(cfg):
    """Config without private (_meta) keys, for hashing and freezing."""
    out = copy.deepcopy(cfg)
    out.pop("_meta", None)
    return out


def config_hash(cfg):
    blob = json.dumps(public_dict(cfg), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def freeze_config(cfg, dest_path):
    """Write the effective config to ``dest_path`` (batch folder), keeping the root."""
    if yaml is None:
        raise ConfigError("PyYAML is required to write the configuration")
    data = public_dict(cfg)
    data["frozen"] = {
        "project_root": cfg.get("_meta", {}).get("project_root") or cfg.get("_meta", {}).get("root"),
        "source_config": cfg.get("_meta", {}).get("path"),
        "config_hash": config_hash(cfg),
    }
    tmp = dest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, dest_path)
    return dest_path


def load_frozen_config(path):
    """Load a frozen batch config; relative paths keep resolving to the project root."""
    cfg = load_config(path)
    frozen = cfg.pop("frozen", {}) or {}
    if frozen.get("project_root"):
        cfg["_meta"]["project_root"] = frozen["project_root"]
    cfg["_meta"]["frozen_hash"] = frozen.get("config_hash")
    return cfg


def validate_config(cfg):
    """Fail fast on the settings everything else relies on."""
    required = ["config_version", "excel", "candidates", "scoring", "validation",
                "routing", "corrected_coordinate"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ConfigError("Configuration is missing sections: %s" % ", ".join(missing))
    cols = get(cfg, "excel.columns", {}) or {}
    for key in ("survey_id", "latitude", "longitude"):
        if not cols.get(key):
            raise ConfigError("excel.columns.%s must be set" % key)
    weights = get(cfg, "scoring.weights", {}) or {}
    for key in ("s_dist", "s_name", "s_addr", "s_road", "s_plaus", "s_source"):
        if key not in weights:
            raise ConfigError("scoring.weights.%s must be set" % key)
    total = sum(float(v) for v in weights.values())
    if not 0.99 <= total <= 1.01:
        raise ConfigError("scoring.weights must sum to 1.0 (got %.3f)" % total)
    strategy = get(cfg, "corrected_coordinate.strategy")
    if strategy not in ("centroid_if_inside", "representative_point", "snap_to_boundary"):
        raise ConfigError("Unknown corrected_coordinate.strategy: %r" % strategy)
    lo, hi = get(cfg, "candidates.radius_clamp_m", [25, 100])
    if not lo <= float(get(cfg, "candidates.search_radius_m", 50)) <= hi:
        raise ConfigError("candidates.search_radius_m must lie inside radius_clamp_m")
    for prov in get(cfg, "imagery.providers", []) or []:
        if "id" not in prov or "type" not in prov:
            raise ConfigError("Every imagery provider needs an id and a type")
        if "allow_automated_analysis" not in prov:
            raise ConfigError("Imagery provider %r must declare allow_automated_analysis" % prov["id"])
    return True
