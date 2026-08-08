from __future__ import annotations

import argparse
import json
import os
import shutil
import secrets
import subprocess
import sys
from pathlib import Path

from . import CONTRACT_VERSION, __version__
from .catalog import Catalog
from .engine import Engine
from .errors import ColosseumError, ValidationError
from .importers import IMPORTERS, run_importer

LAB_NETWORK = "luc1-magi-lab"


def _lab_network_status() -> dict:
    if shutil.which("docker") is None:
        return {"name": LAB_NETWORK, "exists": False, "internal": False,
                "error": "docker unavailable"}
    proc = subprocess.run(["docker", "network", "inspect", LAB_NETWORK,
                           "--format", "{{json .Internal}}"],
                          capture_output=True, text=True)
    if proc.returncode:
        return {"name": LAB_NETWORK, "exists": False, "internal": False,
                "error": proc.stderr.strip() or "network missing"}
    internal = proc.stdout.strip() == "true"
    return {"name": LAB_NETWORK, "exists": True, "internal": internal,
            "status": "ready" if internal else "invalid"}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="magi-colosseum")
    root.add_argument("--json", action="store_true", help="emit the stable JSON contract")
    root.add_argument("--scenario-root", action="append", type=Path)
    root.add_argument("--state-dir", type=Path)
    commands = root.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    listing = catalog_sub.add_parser("list")
    listing.add_argument("--category")
    listing.add_argument("--tag")
    inspect = catalog_sub.add_parser("inspect")
    inspect.add_argument("scenario")
    importer = catalog_sub.add_parser("import")
    importer.add_argument("corpus", choices=(*IMPORTERS, "all"))
    importer.add_argument("--source", type=Path,
                          help="override the source path for a single corpus")
    importer.add_argument("--filter")
    importer.add_argument("--verbose", action="store_true",
                          help="print import progress to stderr; JSON stays on stdout")
    for name in ("validate", "build"):
        item = commands.add_parser(name)
        item.add_argument("scenario")
    certify = commands.add_parser("certify")
    certify.add_argument("scenario", nargs="?")
    certify.add_argument("--all", action="store_true")
    certify.add_argument("--category")
    certify.add_argument("--tag")
    certify.add_argument("--seed", type=int, default=0)
    certify.add_argument("--verbose", action="store_true")
    start = commands.add_parser("start")
    start.add_argument("scenario", nargs="?")
    start.add_argument("--seed", type=int)
    start.add_argument("--random", action="store_true")
    start.add_argument("--category")
    start.add_argument("--tag")
    start.add_argument("--endpoint", default=os.environ.get("LUC1_MAGI_ENDPOINT", "http://127.0.0.1:8095/v1"))
    start.add_argument("--model", default=os.environ.get("LUC1_MAGI_MODEL", "magi"))
    start.add_argument("--luc1-dir", default=os.environ.get("LUC1_MAGI_HOME", "~/Luciv3"))
    start.add_argument("--model-timeout", type=int, default=600)
    start.add_argument("--otel-endpoint", default=os.environ.get(
        "LUC1_MAGI_OTEL_ENDPOINT", "http://127.0.0.1:6006/v1/traces"))
    start.add_argument("--otel-project", default=os.environ.get(
        "LUC1_MAGI_OTEL_PROJECT", "luc1-magi"))
    start.add_argument("--approve", action="store_true")
    for name in ("status", "stop", "reset"):
        item = commands.add_parser(name)
        item.add_argument("episode")
    describe = commands.add_parser("describe")
    describe.add_argument("episode")
    describe.add_argument("--audience", choices=("public", "agent"), default="agent")
    handoff = commands.add_parser("handoff")
    handoff.add_argument("episode")
    handoff.add_argument("--endpoint")
    handoff.add_argument("--model")
    handoff.add_argument("--luc1-dir", default=os.environ.get("LUC1_MAGI_HOME", "~/Luciv3"))
    handoff.add_argument("--model-timeout", type=int, default=600)
    handoff.add_argument("--otel-endpoint", default=os.environ.get(
        "LUC1_MAGI_OTEL_ENDPOINT", "http://127.0.0.1:6006/v1/traces"))
    handoff.add_argument("--otel-project", default=os.environ.get(
        "LUC1_MAGI_OTEL_PROJECT", "luc1-magi"))
    handoff.add_argument("--approve", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("episode")
    export.add_argument("--output", required=True, type=Path)
    commands.add_parser("doctor")
    return root


def _engine(args) -> Engine:
    project = Path(__file__).resolve().parents[2]
    state = args.state_dir or Path(os.environ.get("MAGI_COLOSSEUM_STATE", project / ".colosseum"))
    roots = args.scenario_root or [Path(os.environ.get("MAGI_COLOSSEUM_SCENARIOS", project / "scenarios")),
                                   state / "imports"]
    return Engine(Catalog([path.resolve() for path in roots]), state.resolve())


def execute(args) -> dict:
    engine = _engine(args)
    if args.command == "catalog":
        if args.catalog_command == "import":
            progress = (lambda message: print(message, file=sys.stderr, flush=True)) \
                if args.verbose else None
            project = Path(__file__).resolve().parents[2]
            default_sources = {
                "vulhub": Path(os.environ.get(
                    "MAGI_COLOSSEUM_VULHUB_SOURCE", project / "vuln-services" / "vulhub")),
                "dojo": Path(os.environ["MAGI_COLOSSEUM_DOJO_SOURCE"])
                if os.environ.get("MAGI_COLOSSEUM_DOJO_SOURCE")
                else project / "corpora" / "ctf-archive",
            }
            if args.corpus == "all":
                if args.source:
                    raise ValidationError("--source cannot be used with 'catalog import all'")
                results = {}
                for name in IMPORTERS:
                    source = default_sources.get(name)
                    if progress:
                        progress(f"{name}: source {source or '<not configured>'}")
                    if source is None or not source.is_dir():
                        results[name] = {"status": "source_missing", "source": str(source or "")}
                    else:
                        results[name] = run_importer(
                            name, source, engine.state_dir / "imports", args.filter, progress)
                return {"contract_version": CONTRACT_VERSION, "corpus": "all",
                        "status": "complete" if all(item.get("status") == "complete"
                                                    for item in results.values()) else "partial",
                        "adapters": results, "implemented_adapters": sorted(IMPORTERS)}
            source = args.source or default_sources.get(args.corpus)
            if source is None:
                raise ValidationError(f"no default source is configured for {args.corpus}")
            return {"contract_version": CONTRACT_VERSION,
                    "corpus": args.corpus,
                    **run_importer(args.corpus, source, engine.state_dir / "imports",
                                   args.filter, progress)}
        if args.catalog_command == "list":
            return {"contract_version": CONTRACT_VERSION,
                    "scenarios": [item.public_view() for item in
                                  engine.catalog.filter(args.category, args.tag)]}
        return {"contract_version": CONTRACT_VERSION,
                "scenario": engine.catalog.get(args.scenario).public_view()}
    if args.command == "validate":
        return engine.validate(args.scenario)
    if args.command == "build":
        return engine.build(args.scenario)
    if args.command == "certify":
        if args.all:
            if args.scenario:
                raise ValidationError("do not provide a scenario with --all")
            scenarios = engine.catalog.filter(args.category, args.tag)
            results = []
            for position, scenario in enumerate(scenarios, 1):
                if args.verbose:
                    print(f"certify: [{position}/{len(scenarios)}] {scenario.id}",
                          file=sys.stderr, flush=True)
                results.append(engine.certify(scenario.id, args.seed))
            passed = sum(item["status"] == "certified" for item in results)
            return {"contract_version": CONTRACT_VERSION, "status": "complete",
                    "certified_count": passed, "failed_count": len(results) - passed,
                    "results": results}
        if not args.scenario:
            raise ValidationError("scenario is required unless --all is used")
        return engine.certify(args.scenario, args.seed)
    if args.command == "start":
        seed = args.seed if args.seed is not None else secrets.randbits(63)
        if args.random:
            if args.scenario:
                raise ValidationError("do not provide a scenario with --random")
            result = engine.start_random(seed, args.category, args.tag)
        else:
            if not args.scenario:
                raise ValidationError("scenario is required unless --random is used")
            result = engine.start(args.scenario, seed)
        handoff = engine.handoff(result["episode_id"], args.endpoint, args.model, args.approve,
                                 args.luc1_dir, args.model_timeout,
                                 args.otel_endpoint, args.otel_project)
        result["handoff"] = handoff
        result["next_actions"] = {
            "launch_luc1": handoff["command"],
            "stop": f"magi-colosseum stop {result['episode_id']}",
            "reset": f"magi-colosseum reset {result['episode_id']}",
        }
        return result
    if args.command == "status":
        return engine.status(args.episode)
    if args.command == "describe":
        return engine.describe(args.episode, args.audience)
    if args.command == "handoff":
        return engine.handoff(args.episode, args.endpoint, args.model, args.approve,
                              args.luc1_dir, args.model_timeout,
                              args.otel_endpoint, args.otel_project)
    if args.command == "stop":
        return engine.stop(args.episode)
    if args.command == "reset":
        return engine.reset(args.episode)
    if args.command == "export":
        return engine.export(args.episode, args.output)
    if args.command == "doctor":
        network = _lab_network_status()
        return {"contract_version": CONTRACT_VERSION, "version": __version__,
                "python": sys.version.split()[0], "docker": bool(shutil.which("docker")),
                "lab_network": network,
                "scenario_roots": [str(path) for path in engine.catalog.roots],
                "state_dir": str(engine.state_dir),
                "status": "ok" if network.get("status") == "ready" else "degraded"}
    raise AssertionError(args.command)


def human(result: dict) -> str:
    if "scenarios" in result:
        return "\n".join(f"{item['id']:<32} {item['category']:<20} {item['title']}"
                         for item in result["scenarios"])
    if result.get("episode_id") and result.get("handoff"):
        lines = [
            f"Episode: {result['episode_id']}",
            f"Scenario: {result['scenario_id']}",
            f"Status: {result['status']}",
            f"Seed: {result['seed']}",
        ]
        for target in result["handoff"].get("targets", []):
            address = target.get("url") or f"{target['host']}:{target['port']}"
            lines.append(f"Target: {address}")
        for artifact in result["handoff"].get("artifacts", []):
            lines.append(f"Artifact: {artifact['local_path']} ({artifact['sha256']})")
        lines.extend(["", "Run Luc1-MAGI:", f"  {result['handoff']['command']}", "",
                      "When finished:", f"  {result['next_actions']['stop']}",
                      f"  {result['next_actions']['reset']}  # full cleanup"])
        return "\n".join(lines)
    return json.dumps(result, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw
    args = parser().parse_args([item for item in raw if item != "--json"])
    args.json = json_requested
    try:
        result = execute(args)
        print(json.dumps(result, sort_keys=True) if args.json else human(result))
        return 0
    except ColosseumError as exc:
        payload = {"contract_version": CONTRACT_VERSION, "status": "error", "error": exc.as_dict()}
        print(json.dumps(payload, sort_keys=True) if args.json else f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
