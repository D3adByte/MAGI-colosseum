from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from magi_colosseum.catalog import Catalog
from magi_colosseum.engine import Engine
from magi_colosseum.errors import BuildError, ReadinessTimeout, StartError, ValidationError
from magi_colosseum.model import load_scenario
from magi_colosseum.runners import ComposeRunner, FileRunner
from magi_colosseum.importers.vulhub import import_vulhub
from magi_colosseum.importers.dojo import import_dojo
from magi_colosseum.cli import human, parser
from magi_colosseum.web import ASSETS, Jobs, _scenario_detail, create_app

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "scenarios"


class FileLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(Catalog([SCENARIOS]), Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_file_only_episode_has_artifact_and_no_fake_endpoint(self):
        launch = self.engine.start("fixture-reverse-file", 1234)
        self.assertEqual(launch["status"], "ready")
        self.assertEqual([item["kind"] for item in launch["artifacts"]], ["provided_file"])
        self.assertEqual(launch["targets"], [])
        artifact = launch["artifacts"][0]
        self.assertEqual(len(artifact["sha256"]), 64)
        self.assertEqual(launch["prompt"],
                         f"Challenge file is located at {artifact['local_path']}")
    def test_agent_projection_excludes_provenance_and_scoring(self):
        launch = self.engine.start("fixture-reverse-file", 3)
        agent = self.engine.describe(launch["episode_id"], "agent")
        public = self.engine.describe(launch["episode_id"], "public")
        self.assertNotIn('"expected"', json.dumps(agent))
        self.assertNotIn("success", json.dumps(agent))
        self.assertNotIn("provenance", json.dumps(agent))
        self.assertNotIn("objective", public["scenario"])

    def test_reset_is_idempotent(self):
        launch = self.engine.start("fixture-reverse-file", 4)
        artifact = Path(launch["artifacts"][0]["local_path"])
        artifact.chmod(0o644)
        artifact.write_bytes(b"changed")
        reset = self.engine.reset(launch["episode_id"])
        self.assertEqual(reset["status"], "ready")
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["artifacts"][0]["sha256"],
                         "a6607d04782881e61a72e7ef1de3e3acc028a13432f162bcbc0ea010f30a6eb2")
        self.assertEqual(self.engine.reset(launch["episode_id"])["status"], "ready")

    def test_stop_deletes_staged_artifact(self):
        launch = self.engine.start("fixture-reverse-file", 6)
        artifact = Path(launch["artifacts"][0]["local_path"])
        self.assertTrue(artifact.exists())
        self.assertEqual(self.engine.stop(launch["episode_id"])["status"], "stopped")
        self.assertFalse(artifact.exists())

    def test_service_target_uses_registered_container_identity(self):
        launch = self.engine.start("fixture-reverse-file", 13)
        episode = self.engine.store.get(launch["episode_id"])
        episode["launch"]["capabilities"].append({
            "kind": "web_endpoint", "service": "web",
            "container_id": "colosseum-test-1234-web",
            "host": "colosseum-test-1234-web", "port": 8080,
            "protocol": "http", "network": "luc1-magi-lab",
            "url": "http://colosseum-test-1234-web:8080/",
        })
        self.engine.store.put(episode)
        briefing = self.engine.describe(launch["episode_id"], "agent")
        self.assertEqual(briefing["targets"][0]["host"], "colosseum-test-1234-web")
        self.assertNotIn("127.0.0.1", briefing["targets"][0]["host"])
        self.assertEqual(briefing["prompt"].splitlines()[0],
                         "Web service is running at http://colosseum-test-1234-web:8080/")

    def test_compose_container_name_does_not_reveal_scenario(self):
        name = ComposeRunner._container_name("ep-7865e2051d4ba92633932f9e", 1)
        self.assertEqual(name, "colosseum-7865e2051d4ba926-target-1")
        self.assertNotIn("cve", name)

    def test_human_launch_output_leads_with_actionable_fields(self):
        launch = self.engine.start("fixture-reverse-file", 10)
        launch["next_actions"] = {"stop": f"magi-colosseum stop {launch['episode_id']}",
                                  "reset": f"magi-colosseum reset {launch['episode_id']}"}
        output = human(launch)
        self.assertIn(f"Episode: {launch['episode_id']}", output)
        self.assertIn("Artifact:", output)
        self.assertIn("magi-colosseum stop", output)

    def test_start_seed_is_optional(self):
        args = parser().parse_args(["start", "fixture-reverse-file"])
        self.assertIsNone(args.seed)

    def test_ctl_command_is_available(self):
        self.assertEqual(parser().parse_args(["ctl"]).command, "ctl")

    def test_episode_store_hides_stopped_by_default(self):
        ready = self.engine.start("fixture-reverse-file", 15)
        stopped = self.engine.start("fixture-reverse-file", 16)
        self.engine.stop(stopped["episode_id"])
        visible = [item["episode_id"] for item in self.engine.store.list()]
        archived = [item["episode_id"] for item in self.engine.store.list(True)]
        self.assertIn(ready["episode_id"], visible)
        self.assertNotIn(stopped["episode_id"], visible)
        self.assertIn(stopped["episode_id"], archived)

    def test_engine_can_start_from_a_worker_thread(self):
        result = []
        failure = []

        def start():
            try:
                result.append(self.engine.start("fixture-reverse-file", 18))
            except Exception as exc:
                failure.append(exc)

        worker = threading.Thread(target=start)
        worker.start()
        worker.join()
        self.assertEqual(failure, [])
        self.assertEqual(result[0]["status"], "ready")
        self.assertEqual(self.engine.store.get(result[0]["episode_id"])["status"], "ready")

    def test_random_start_reports_progress(self):
        progress = []
        launch = self.engine.start_random(5, category="reverse-engineering",
                                          progress=progress.append)
        self.assertEqual(launch["status"], "ready")
        self.assertTrue(any(message.startswith("validating") for message in progress))
        self.assertTrue(any(message.startswith("selected") for message in progress))
        self.assertTrue(any(message.startswith("ready") for message in progress))


class ImporterTests(unittest.TestCase):
    @mock.patch("magi_colosseum.importers.vulhub.subprocess.run")
    def test_vulhub_normalizes_into_lifecycle_scenario(self, run):
        run.return_value = mock.Mock(returncode=0, stdout=json.dumps({
            "services": {"web": {"image": "fixture@sha256:" + "a" * 64,
                                  "ports": [{"target": 8000, "published": "8000"}]}}
        }), stderr="")
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            progress = []
            source = Path(source_dir)
            (source / "flask" / "ssti").mkdir(parents=True)
            (source / "flask" / "ssti" / "docker-compose.yml").write_text("services: {}\n")
            result = import_vulhub(source, Path(output_dir), "flask/ssti", progress.append)
            self.assertEqual(result["imported_count"], 1)
            scenario = load_scenario(Path(output_dir) / "vulhub-flask-ssti")
            self.assertEqual(scenario.manifest["runtime"]["services"][0]["capability"], "web_endpoint")
            compose = json.loads((Path(output_dir) / "vulhub-flask-ssti" /
                                  "compose.normalized.json").read_text())
            self.assertEqual(compose["networks"], {
                "default": {"external": True, "name": "luc1-magi-lab"}})
            self.assertEqual(compose["services"]["web"]["networks"], ["default"])
            self.assertNotIn("ports", compose["services"]["web"])
            self.assertTrue(any("[1/1]" in message for message in progress))
            self.assertIn("1 imported", progress[-1])

    @mock.patch("magi_colosseum.importers.vulhub.subprocess.run")
    def test_vulhub_classifies_shell_only_lab_as_unsupported(self, run):
        run.return_value = mock.Mock(returncode=0, stdout=json.dumps({
            "services": {"tool": {"image": "fixture@sha256:" + "b" * 64}}
        }), stderr="")
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            source = Path(source_dir)
            (source / "tool").mkdir()
            (source / "tool" / "compose.yaml").write_text("services: {}\n")
            result = import_vulhub(source, Path(output_dir))
            self.assertEqual(result["failed_count"], 0)
            self.assertEqual(result["unsupported_count"], 1)
            self.assertEqual(result["unsupported"][0]["reason"], "no_agent_endpoint")

    def test_dojo_imports_forged_file_only_challenge(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            task = Path(source_dir) / "Example CTF" / "reversing" / "tiny"
            (task / "files" / "nested").mkdir(parents=True)
            (task / "files" / "nested" / "challenge.bin").write_bytes(b"fixture")
            (task / "challenge.json").write_text(json.dumps({
                "name": "Tiny Reverse", "category": "rev", "compose": False,
                "description": "Analyze the supplied binary.",
                "files": ["files/nested/challenge.bin"], "flag": "flag{hidden}",
            }))
            result = import_dojo(Path(source_dir), Path(output_dir))
            self.assertEqual(result["imported_count"], 1)
            scenario = load_scenario(Path(output_dir) / result["imported"][0]["scenario_id"])
            self.assertEqual(scenario.manifest["runtime"]["runner"], "file")
            self.assertEqual(scenario.manifest["category"], "reverse")
            self.assertEqual(scenario.manifest["artifacts"][0]["name"],
                             "files/nested/challenge.bin")
            self.assertNotIn("flag{hidden}", json.dumps(scenario.agent_view()))
            engine = Engine(Catalog([Path(output_dir)]), Path(output_dir) / "state")
            launch = engine.start(scenario.id, 11)
            delivered = Path(launch["artifacts"][0]["local_path"])
            self.assertEqual(delivered.read_bytes(), b"fixture")
            self.assertEqual(delivered.relative_to(Path(output_dir) / "state").parts[-3:],
                             ("files", "nested", "challenge.bin"))

    def test_dojo_rejects_declared_path_traversal(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            task = Path(source_dir) / "bad"
            task.mkdir()
            (task / "challenge.json").write_text(json.dumps({
                "name": "Bad", "compose": False, "files": ["../../secret"]
            }))
            result = import_dojo(Path(source_dir), Path(output_dir))
            self.assertEqual(result["imported_count"], 0)
            self.assertEqual(result["failed_count"], 1)
            self.assertIn("escapes", result["failed"][0]["error"])

    def test_unforged_dojo_tree_is_reported_clearly(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            result = import_dojo(Path(source_dir), Path(output_dir))
            self.assertEqual(result["status"], "not_forged")
            self.assertIn("module.yml", result["failed"][0]["error"])

    def test_dojo_imports_safe_files_from_raw_archive_modules(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            event = Path(source_dir) / "samplectf2026"
            task = event / "tinyrev"
            task.mkdir(parents=True)
            (event / "module.yml").write_text(
                "id: samplectf2026\nname: Sample CTF\nchallenges:\n"
                "  - id: tinyrev\n    name: REV - 100 - Tiny Rev\n")
            (task / "chall.bin").write_bytes(b"player input")
            (task / "flagCheck").write_bytes(b"secret evaluator")
            (task / ".flag.sha256").write_text("secret hash")
            (task / "DESCRIPTION.md").write_text("description")
            result = import_dojo(Path(source_dir), Path(output_dir))
            self.assertEqual(result["imported_count"], 1)
            self.assertEqual(result["format"], "raw-archive")
            scenario = load_scenario(Path(output_dir) / "dojo-samplectf2026-tinyrev")
            self.assertEqual(scenario.manifest["category"], "reverse-engineering")
            self.assertEqual(scenario.manifest["difficulty"], "unspecified")
            self.assertEqual(scenario.manifest["source"]["upstream_points"], 100)
            self.assertEqual([item["source"] for item in scenario.manifest["artifacts"]],
                             ["chall.bin"])
            self.assertNotIn("flagCheck", json.dumps(scenario.manifest))

    def test_dojo_decodes_html_entities_in_raw_titles(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            event = Path(source_dir) / "samplectf"
            task = event / "casino"
            task.mkdir(parents=True)
            (event / "module.yml").write_text(
                "id: samplectf\nname: Sample CTF\nchallenges:\n"
                "  - id: casino\n    name: CRYPTO - 120 - &aring;CTF CasinO\n")
            (task / "casino.bin").write_bytes(b"challenge")
            result = import_dojo(Path(source_dir), Path(output_dir))
            scenario = load_scenario(Path(output_dir) / result["imported"][0]["scenario_id"])
            self.assertEqual(scenario.manifest["title"], "åCTF CasinO")


class ManifestSecurityTests(unittest.TestCase):
    def test_bundled_scenarios_validate(self):
        scenarios = Catalog([SCENARIOS]).all()
        self.assertEqual({item.id for item in scenarios}, {"fixture-reverse-file", "fixture-network-service"})

    def test_catalog_cache_can_be_refreshed(self):
        catalog = Catalog([SCENARIOS])
        first = catalog.all()
        self.assertIsNotNone(catalog._cache)
        catalog.refresh()
        self.assertIsNone(catalog._cache)
        self.assertEqual([item.id for item in catalog.all()], [item.id for item in first])

    def test_stale_import_does_not_break_valid_catalog_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "stale"
            stale.mkdir()
            (stale / "scenario.json").write_text("{}")
            catalog = Catalog([Path(directory), SCENARIOS])
            self.assertEqual(catalog.get("fixture-reverse-file").id, "fixture-reverse-file")

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = json.loads((SCENARIOS / "fixture-reverse-file" / "scenario.json").read_text())
            manifest["artifacts"][0]["source"] = "../../etc/passwd"
            (root / "scenario.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValidationError):
                load_scenario(root)

    def test_privileged_compose_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = json.loads((SCENARIOS / "fixture-network-service" / "scenario.json").read_text())
            manifest["build"]["inputs"] = []
            (root / "scenario.json").write_text(json.dumps(manifest))
            (root / "compose.normalized.json").write_text(json.dumps({
                "services": {"bad": {"privileged": True, "networks": ["default"]}},
                "networks": {"default": {"external": True, "name": "luc1-magi-lab"}},
            }))
            with self.assertRaises(ValidationError):
                load_scenario(root)

    def test_default_bridge_compose_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = json.loads((SCENARIOS / "fixture-network-service" / "scenario.json").read_text())
            manifest["build"]["inputs"] = []
            (root / "scenario.json").write_text(json.dumps(manifest))
            (root / "compose.normalized.json").write_text(json.dumps({
                "services": {"gateway": {"networks": ["default"]}},
                "networks": {"default": {"external": False}},
            }))
            with self.assertRaisesRegex(ValidationError, "luc1-magi-lab"):
                load_scenario(root)


class RunnerContractTests(unittest.TestCase):
    def test_both_runners_expose_common_methods(self):
        methods = {"validate", "build", "start", "status", "stop", "reset"}
        for runner in (FileRunner(), ComposeRunner()):
            self.assertTrue(all(callable(getattr(runner, method, None)) for method in methods))

    @mock.patch("magi_colosseum.runners.subprocess.run")
    @mock.patch("magi_colosseum.runners.shutil.which", return_value="/usr/bin/docker")
    def test_broken_compose_validation_is_normalized(self, _which, run):
        run.side_effect = [mock.Mock(returncode=0, stdout="true\n", stderr=""),
                           mock.Mock(returncode=1, stdout="", stderr="broken compose")]
        scenario = load_scenario(SCENARIOS / "fixture-network-service")
        result = ComposeRunner().validate(scenario)
        self.assertEqual(result, {"status": "invalid", "runner": "compose", "errors": ["broken compose"]})

    @mock.patch("magi_colosseum.runners.subprocess.run")
    def test_broken_build_is_typed(self, run):
        run.return_value = mock.Mock(returncode=1, stderr="compiler failed")
        scenario = load_scenario(SCENARIOS / "fixture-network-service")
        with self.assertRaises(BuildError):
            ComposeRunner().build(scenario)

    def test_readiness_timeout_is_typed(self):
        scenario = load_scenario(SCENARIOS / "fixture-network-service")
        scenario.manifest["readiness"]["timeout_seconds"] = 0
        runner = ComposeRunner()
        with mock.patch.object(runner, "_container_ip", return_value="127.0.0.1"):
            with self.assertRaises(ReadinessTimeout):
                runner._wait_ready(
                    scenario, {"runner_state": {"container_names": {"gateway": "target"}}},
                    [{"service": "gateway", "host": "target", "port": 1}])

    @mock.patch("magi_colosseum.runners.subprocess.run")
    @mock.patch("magi_colosseum.runners.shutil.which", return_value="/usr/bin/docker")
    def test_non_internal_lab_network_is_rejected(self, _which, run):
        run.return_value = mock.Mock(returncode=0, stdout="false\n", stderr="")
        scenario = load_scenario(SCENARIOS / "fixture-network-service")
        result = ComposeRunner().validate(scenario)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("must be internal", result["errors"][0])

    def test_partial_start_attempts_cleanup(self):
        class FailingRunner(FileRunner):
            stopped = False
            def start(self, scenario, episode, episode_dir):
                raise StartError("partial failure")
            def stop(self, scenario, episode, episode_dir):
                self.stopped = True
                return {"status": "stopped"}

        runner = FailingRunner()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                "magi_colosseum.engine.RUNNERS", {"file": runner}, clear=False):
            engine = Engine(Catalog([SCENARIOS]), Path(directory))
            with self.assertRaises(StartError):
                engine.start("fixture-reverse-file", 5)
            self.assertTrue(runner.stopped)


class WebDashboardTests(unittest.TestCase):
    def test_dashboard_serves_catalog_and_scenario_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine(Catalog([SCENARIOS]), root / "state", root / "work")
            app = create_app(engine)
            details = _scenario_detail(engine.catalog.get("fixture-reverse-file"))
            self.assertTrue(any(route.path == "/api/catalog" for route in app.routes))
            self.assertIn("MAGI Colosseum", (ASSETS / "index.html").read_text())
            self.assertTrue((ASSETS / "app.css").is_file())
            self.assertTrue((ASSETS / "app.js").is_file())
            self.assertEqual(details["category"], "reverse-engineering")
            self.assertIn("Analyze the supplied", details["objective"])

    def test_dashboard_start_runs_as_background_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = Engine(Catalog([SCENARIOS]), root / "state", root / "work")
            jobs = Jobs()
            job = jobs.submit("start", lambda update: engine.start(
                "fixture-reverse-file", 21))
            for _ in range(100):
                status = jobs.get(job["id"])
                if status["status"] in {"complete", "failed"}:
                    break
                threading.Event().wait(.01)
            jobs.close()
            self.assertEqual(status["status"], "complete")
            self.assertIn("Challenge file is located at", status["result"]["prompt"])

if __name__ == "__main__":
    unittest.main()
