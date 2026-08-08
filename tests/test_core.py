from __future__ import annotations

import json
import tempfile
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
        self.assertEqual([item["kind"] for item in launch["capabilities"]], ["provided_file"])
        self.assertFalse(any("host" in item or "port" in item for item in launch["capabilities"]))
        artifact = launch["capabilities"][0]
        self.assertEqual(len(artifact["sha256"]), 64)
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
        self.assertEqual(self.engine.reset(launch["episode_id"])["status"], "reset")
        self.assertEqual(self.engine.reset(launch["episode_id"])["status"], "reset")

    def test_handoff_produces_copyable_file_invocation(self):
        launch = self.engine.start("fixture-reverse-file", 9)
        handoff = self.engine.handoff(launch["episode_id"], "http://127.0.0.1:8095/v1", "magi", True)
        self.assertIn("--artifact", handoff["argv"])
        self.assertIn("--approve", handoff["argv"])
        self.assertIn("--endpoint", handoff["argv"])
        self.assertEqual(handoff["argv"][0], ".venv/bin/luc1-magi")
        self.assertIn("--model-timeout", handoff["argv"])
        self.assertIn("--otel-endpoint", handoff["argv"])
        self.assertIn("--otel-project", handoff["argv"])
        self.assertEqual(handoff["telemetry"], {
            "endpoint": "http://127.0.0.1:6006/v1/traces", "project": "luc1-magi"})
        self.assertNotIn("--capability-config", handoff["argv"])
        self.assertNotIn("--allow-program", handoff["argv"])
        self.assertEqual(handoff["working_directory"], "~/Luciv3")

    def test_service_handoff_uses_registered_container_identity(self):
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
        handoff = self.engine.handoff(launch["episode_id"],
                                      "http://127.0.0.1:8095/v1", "magi", True)
        self.assertEqual(handoff["network"], "luc1-magi-lab")
        self.assertEqual(handoff["targets"][0]["host"], "colosseum-test-1234-web")
        self.assertIn("--lab-container", handoff["argv"])
        self.assertEqual(handoff["capability_recommendations"]["capabilities"],
                         ["web_observation"])
        self.assertNotIn("127.0.0.1", handoff["targets"][0]["host"])

    def test_human_launch_output_leads_with_actionable_fields(self):
        launch = self.engine.start("fixture-reverse-file", 10)
        launch["handoff"] = self.engine.handoff(launch["episode_id"], "http://127.0.0.1:8095/v1", "magi", True)
        launch["next_actions"] = {"stop": f"magi-colosseum stop {launch['episode_id']}",
                                  "reset": f"magi-colosseum reset {launch['episode_id']}"}
        output = human(launch)
        self.assertIn(f"Episode: {launch['episode_id']}", output)
        self.assertIn("Run Luc1-MAGI:", output)
        self.assertIn("magi-colosseum stop", output)

    def test_start_seed_is_optional(self):
        args = parser().parse_args(["start", "fixture-reverse-file"])
        self.assertIsNone(args.seed)

    def test_file_scenario_certification_exercises_full_lifecycle(self):
        result = self.engine.certify("fixture-reverse-file", 12)
        self.assertEqual(result["status"], "certified")
        self.assertEqual([item["id"] for item in result["checks"]],
                         ["validate", "build", "readiness", "stop", "reset"])


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
            delivered = Path(launch["capabilities"][0]["local_path"])
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
            self.assertIn("CTF-Forge", result["failed"][0]["error"])


class ManifestSecurityTests(unittest.TestCase):
    def test_bundled_scenarios_validate(self):
        scenarios = Catalog([SCENARIOS]).all()
        self.assertEqual({item.id for item in scenarios}, {"fixture-reverse-file", "fixture-network-service"})

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

if __name__ == "__main__":
    unittest.main()
