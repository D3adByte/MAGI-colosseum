from __future__ import annotations

import argparse
import json
import os
import shutil
import secrets
import subprocess
import sys
import tempfile
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
    root.add_argument("--workspace-root", type=Path)
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
    start = commands.add_parser("start")
    start.add_argument("scenario", nargs="?")
    start.add_argument("--seed", type=int)
    start.add_argument("--random", action="store_true")
    start.add_argument("--category")
    start.add_argument("--tag")
    for name in ("status", "stop", "reset"):
        item = commands.add_parser(name)
        item.add_argument("episode")
    describe = commands.add_parser("describe")
    describe.add_argument("episode")
    describe.add_argument("--audience", choices=("public", "agent"), default="agent")
    commands.add_parser("doctor")
    ctl = commands.add_parser("ctl", help="open the local operator dashboard")
    ctl.add_argument("--host", default="127.0.0.1",
                     choices=("127.0.0.1", "localhost", "::1"),
                     help="loopback bind address (default: 127.0.0.1)")
    ctl.add_argument("--port", type=int, default=8765,
                     help="dashboard port (default: 8765)")
    ctl.add_argument("--open-browser", action="store_true",
                     help="open the dashboard using the configured system browser")
    return root


def _engine(args) -> Engine:
    project = Path(__file__).resolve().parents[2]
    state = args.state_dir or Path(os.environ.get("MAGI_COLOSSEUM_STATE", project / ".colosseum"))
    workspace_root = args.workspace_root or Path(os.environ.get(
        "MAGI_COLOSSEUM_WORKSPACES", Path(tempfile.gettempdir()) / "magi-colosseum"))
    roots = args.scenario_root or [Path(os.environ.get("MAGI_COLOSSEUM_SCENARIOS", project / "scenarios")),
                                   state / "imports"]
    return Engine(Catalog([path.resolve() for path in roots]), state.resolve(),
                  workspace_root.resolve())


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
    if args.command == "start":
        seed = args.seed if args.seed is not None else secrets.randbits(63)
        progress = lambda message: print(f"colosseum: {message}", file=sys.stderr, flush=True)
        if args.random:
            if args.scenario:
                raise ValidationError("do not provide a scenario with --random")
            result = engine.start_random(seed, args.category, args.tag, progress)
        else:
            if not args.scenario:
                raise ValidationError("scenario is required unless --random is used")
            progress(f"starting {args.scenario}")
            result = engine.start(args.scenario, seed)
            progress(f"ready {result['episode_id']}")
        result["next_actions"] = {
            "stop": f"magi-colosseum stop {result['episode_id']}",
            "reset": f"magi-colosseum reset {result['episode_id']}",
        }
        progress(f"cleanup with: {result['next_actions']['stop']}")
        return result
    if args.command == "status":
        return engine.status(args.episode)
    if args.command == "describe":
        return engine.describe(args.episode, args.audience)
    if args.command == "stop":
        return engine.stop(args.episode)
    if args.command == "reset":
        return engine.reset(args.episode)
    if args.command == "doctor":
        network = _lab_network_status()
        return {"contract_version": CONTRACT_VERSION, "version": __version__,
                "python": sys.version.split()[0], "docker": bool(shutil.which("docker")),
                "lab_network": network,
                "scenario_roots": [str(path) for path in engine.catalog.roots],
                "state_dir": str(engine.state_dir),
                "workspace_root": str(engine.workspace_root),
                "status": "ok" if network.get("status") == "ready" else "degraded"}
    if args.command == "ctl":
        from .web import run_dashboard
        run_dashboard(engine, args.host, args.port, args.open_browser)
        return {"status": "closed"}
    raise AssertionError(args.command)


def human(result: dict) -> str:
    if "scenarios" in result:
        return "\n".join(f"{item['id']:<32} {item['category']:<20} {item['title']}"
                         for item in result["scenarios"])
    if result.get("episode_id") and result.get("next_actions"):
        lines = [
            f"Episode: {result['episode_id']}",
            f"Scenario: {result['scenario_id']}",
            f"Status: {result['status']}",
            f"Seed: {result['seed']}",
        ]
        if result.get("prompt"):
            lines.extend(["", "Agent prompt:", result["prompt"]])
        for target in result.get("targets", []):
            address = target.get("url") or f"{target['host']}:{target['port']}"
            lines.append(f"Target: {address}")
        for artifact in result.get("artifacts", []):
            lines.append(f"Artifact: {artifact['local_path']} ({artifact['sha256']})")
        lines.extend(["", "When finished:", f"  {result['next_actions']['stop']}",
                      f"  {result['next_actions']['reset']}  # wipe and restart"])
        return "\n".join(lines)
    return json.dumps(result, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw
    args = parser().parse_args([item for item in raw if item != "--json"])
    args.json = json_requested
    try:
        result = execute(args)
        if args.command != "ctl":
            print(json.dumps(result, sort_keys=True) if args.json else human(result))
        return 0
    except ColosseumError as exc:
        payload = {"contract_version": CONTRACT_VERSION, "status": "error", "error": exc.as_dict()}
        print(json.dumps(payload, sort_keys=True) if args.json else f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
