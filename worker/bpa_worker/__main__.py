"""Command line entry point: ``python -m bpa_worker <command>``."""

import argparse
import json
import logging
import os
import sys

from . import __version__  # noqa: F401  (sets up sys.path for bpa_common)


def _load_cfg(path):
    from bpa_common.config import load_config
    return load_config(path)


def _survey_points(cfg, excel):
    from bpa_common.excel_io import read_records
    from bpa_common import constants as C
    records, _ = read_records(excel, cfg)
    return [(r["longitude"], r["latitude"]) for r in records if r["state"] == C.IMPORTED]


def cmd_prepare_reference(args):
    from bpa_common.config import get, resolve_path
    from .prepare import prepare_reference
    cfg = _load_cfg(args.config)
    ref_dir = args.reference_dir or resolve_path(cfg, get(cfg, "reference.dir"))
    points = _survey_points(cfg, args.excel)
    types = args.types.split(",") if args.types else None
    manifest = prepare_reference(cfg, points, ref_dir, types=types, release=args.release,
                                 margin_m=args.margin, mode=args.mode, link_m=args.link,
                                 refresh=args.refresh)
    print(json.dumps({k: v for k, v in manifest.items() if k != "boxes"}, indent=2))


def cmd_register_reference(args):
    from bpa_common.config import get, resolve_path
    from .prepare import register_file
    cfg = _load_cfg(args.config)
    ref_dir = args.reference_dir or resolve_path(cfg, get(cfg, "reference.dir"))
    m = register_file(ref_dir, args.key, os.path.abspath(args.path), args.source, args.licence, args.release or "")
    print(json.dumps(m["files"][args.key], indent=2))


def cmd_serve(args):
    import uvicorn
    from .api import create_app
    cfg = _load_cfg(args.config)
    host = args.host or cfg.get("worker", {}).get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        sys.exit("Refusing to listen on %s: the worker binds to localhost only (use --allow-remote for server mode)." % host)
    port = int(args.port or cfg.get("worker", {}).get("port", 8765))
    app = create_app(cfg, load_model=not args.no_model)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower(), access_log=False)


def cmd_process(args):
    from .headless import process_workbook
    cfg = _load_cfg(args.config)
    summary = process_workbook(cfg, args.excel, args.batch_dir, load_model=not args.no_model,
                               batch_id=args.batch_id, auto_reference=args.auto_reference)
    print(json.dumps(summary, indent=2, default=str))


def cmd_export(args):
    from .headless import export_batch
    cfg = _load_cfg(args.config)
    rep = export_batch(cfg, args.batch_dir, args.output, review_gpkg=args.review_gpkg)
    print(json.dumps({k: v for k, v in rep.items() if k != "exported_ids"}, indent=2, default=str))


def cmd_imagery_plan(args):
    from .headless import imagery_plan
    cfg = _load_cfg(args.config)
    plan = imagery_plan(cfg, args.batch_dir, args.output)
    print(json.dumps(plan, indent=2, default=str))


def cmd_benchmark(args):
    from .segmentation import benchmark_models
    cfg = _load_cfg(args.config)
    print(json.dumps(benchmark_models(cfg, crops_dir=args.crops, repeats=args.repeats), indent=2))


def cmd_calibrate(args):
    from .calibration import calibrate_from_ground_truth
    cfg = _load_cfg(args.config)
    rep = calibrate_from_ground_truth(cfg, args.batch_dir, args.ground_truth, args.output_dir,
                                      target_precision=args.target_precision)
    print(json.dumps(rep, indent=2, default=str))


def cmd_learn(args):
    from .learn import learn_from_review
    cfg = _load_cfg(args.config)
    print(json.dumps(learn_from_review(cfg, args.review_gpkg, args.reference_dir), indent=2))


def cmd_evaluate(args):
    from .calibration import evaluate_batch
    cfg = _load_cfg(args.config)
    print(json.dumps(evaluate_batch(cfg, args.batch_dir, args.ground_truth, review_gpkg=args.review_gpkg),
                     indent=2, default=str))


def cmd_verify_reference(args):
    from bpa_common.config import get, resolve_path
    from .reference import verify_manifest
    cfg = _load_cfg(args.config)
    ref_dir = args.reference_dir or resolve_path(cfg, get(cfg, "reference.dir"))
    print(json.dumps(verify_manifest(ref_dir), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="bpa_worker", description="AI Building Polygon Automation worker")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)

    def add_cfg(sp):
        sp.add_argument("--config", required=True, help="config.yaml (project or frozen batch config)")

    sp = sub.add_parser("prepare-reference", help="S0: download Overture data for the survey area")
    add_cfg(sp)
    sp.add_argument("--excel", required=True)
    sp.add_argument("--reference-dir")
    sp.add_argument("--types", help="comma list: building,segment,place,address")
    sp.add_argument("--release", help="Overture release (default: latest)")
    sp.add_argument("--margin", type=float)
    sp.add_argument("--mode", choices=["project", "clusters"])
    sp.add_argument("--link", type=float, help="cluster link distance in metres")
    sp.add_argument("--refresh", action="store_true", help="re-download every area (default: only uncovered areas)")
    sp.set_defaults(func=cmd_prepare_reference)

    sp = sub.add_parser("register-reference", help="add historic.gpkg / heights.tif to the manifest")
    add_cfg(sp)
    sp.add_argument("--key", required=True, choices=["historic", "heights"])
    sp.add_argument("--path", required=True)
    sp.add_argument("--source", required=True)
    sp.add_argument("--licence", required=True)
    sp.add_argument("--release")
    sp.add_argument("--reference-dir")
    sp.set_defaults(func=cmd_register_reference)

    sp = sub.add_parser("verify-reference", help="check reference files against manifest SHA-256")
    add_cfg(sp)
    sp.add_argument("--reference-dir")
    sp.set_defaults(func=cmd_verify_reference)

    sp = sub.add_parser("serve", help="run the HTTP worker on 127.0.0.1")
    add_cfg(sp)
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--no-model", action="store_true", help="do not load SAM (footprint path only)")
    sp.add_argument("--allow-remote", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("process", help="headless batch run: import, match, segment, validate")
    add_cfg(sp)
    sp.add_argument("--excel", required=True)
    sp.add_argument("--batch-dir", required=True)
    sp.add_argument("--batch-id")
    sp.add_argument("--no-model", action="store_true")
    sp.add_argument("--auto-reference", action="store_true",
                    help="first download reference data for any survey points not yet covered")
    sp.set_defaults(func=cmd_process)

    sp = sub.add_parser("export", help="export approved records from review.gpkg to a workbook copy")
    add_cfg(sp)
    sp.add_argument("--batch-dir", required=True)
    sp.add_argument("--output")
    sp.add_argument("--review-gpkg")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("imagery-plan", help="cluster records that need imagery (purchase plan)")
    add_cfg(sp)
    sp.add_argument("--batch-dir", required=True)
    sp.add_argument("--output")
    sp.set_defaults(func=cmd_imagery_plan)

    sp = sub.add_parser("benchmark", help="week-1 SAM benchmark on cached crops")
    add_cfg(sp)
    sp.add_argument("--crops")
    sp.add_argument("--repeats", type=int, default=3)
    sp.set_defaults(func=cmd_benchmark)

    sp = sub.add_parser("calibrate", help="fit identity/confidence models on ground truth")
    add_cfg(sp)
    sp.add_argument("--batch-dir", required=True)
    sp.add_argument("--ground-truth", required=True, help="GeoPackage with survey_id + true polygon")
    sp.add_argument("--output-dir", required=True)
    sp.add_argument("--target-precision", type=float, default=0.98)
    sp.set_defaults(func=cmd_calibrate)

    sp = sub.add_parser("learn", help="add a batch's approved polygons to the trusted reference")
    add_cfg(sp)
    sp.add_argument("--review-gpkg", required=True)
    sp.add_argument("--reference-dir")
    sp.set_defaults(func=cmd_learn)

    sp = sub.add_parser("evaluate", help="pilot metrics per path against ground truth")
    add_cfg(sp)
    sp.add_argument("--batch-dir", required=True)
    sp.add_argument("--ground-truth", required=True)
    sp.add_argument("--review-gpkg", help="evaluate final (reviewed) buildings instead of proposals")
    sp.set_defaults(func=cmd_evaluate)

    args = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
