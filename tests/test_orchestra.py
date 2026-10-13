import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import orchestra
import typer
from experiments.data import Layout


class EnvTests(unittest.TestCase):
    def load(self, text: str, environ: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text(text, encoding="utf-8")
            with mock.patch.object(orchestra, "ENV", path), mock.patch.dict(os.environ, environ or {}, clear=True):
                orchestra.load_env()
                return dict(os.environ)

    def test_values_quotes_and_comments(self):
        env = self.load('A=1 # note\nexport B="two words"\n# C=3\nD=\nE=\'x # y\'\n')
        self.assertEqual(env["A"], "1")
        self.assertEqual(env["B"], "two words")
        self.assertEqual(env["E"], "x # y")
        self.assertNotIn("C", env)
        self.assertNotIn("D", env)  # blank values stay unset, so HF_TOKEN= means no token

    def test_environment_wins_over_the_file(self):
        self.assertEqual(self.load("ORCHESTRA_PORT=3071\n", {"ORCHESTRA_PORT": "4000"})["ORCHESTRA_PORT"], "4000")

    def test_relative_hf_home_resolves_against_the_repository(self):
        self.assertEqual(self.load("HF_HOME=data/interim/huggingface\n")["HF_HOME"], str(orchestra.ROOT / "data/interim/huggingface"))

    def test_template_sets_every_orchestra_setting(self):
        keys = orchestra.env_keys(orchestra.TEMPLATE, commented=False)
        self.assertLessEqual({"ORCHESTRA_PORT", "ORCHESTRA_HOST", "ORCHESTRA_ACE"}, keys)
        self.assertNotIn("CUDA_VISIBLE_DEVICES", keys)  # optional, shown commented out


class AceSetupTests(unittest.TestCase):
    def test_auto_without_gpu_does_not_install_packages_or_download_models(self):
        with tempfile.TemporaryDirectory() as folder:
            layout = Layout(Path(folder))
            with mock.patch.object(orchestra, "gpu", return_value=None), \
                    mock.patch.object(orchestra, "run") as run:
                orchestra.ace_step(orchestra.Report(), orchestra.Ace.auto, layout)
            self.assertFalse((layout.interim / orchestra.ACE_DIR).exists())
        run.assert_not_called()

    def test_weight_downloads_exclude_unused_turbo_and_language_model(self):
        download = mock.Mock()
        modules = {
            "huggingface_hub": SimpleNamespace(snapshot_download=download),
            "acestep.model_downloader": SimpleNamespace(check_model_exists=lambda name, root: False),
        }
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(sys, "argv", ["weights", "/ace", "base", "model-repo", "model-revision", "main-repo", "main-revision"]):
            exec(orchestra.WEIGHTS, {})
        self.assertEqual(download.call_count, 2)
        main, model = download.call_args_list
        self.assertEqual(main.args, ("main-repo",))
        self.assertEqual(main.kwargs["revision"], "main-revision")
        self.assertEqual(main.kwargs["allow_patterns"], ["vae/*", "Qwen3-Embedding-0.6B/*"])
        self.assertEqual(model.args, ("model-repo",))
        self.assertEqual(model.kwargs["revision"], "model-revision")

    def setup_cpu(self, folder, existing=False, install_ok=True):
        layout = Layout(Path(folder))
        root = layout.interim / orchestra.ACE_DIR
        root.mkdir(parents=True)
        python = root / ".venv" / "bin" / "python"
        if existing:
            python.parent.mkdir(parents=True)
            python.touch()
        report = orchestra.Report()

        def execute(command, **kwargs):
            if command[1:2] == ["venv"]:
                python.parent.mkdir(parents=True, exist_ok=True)
                python.touch()
            return install_ok

        with mock.patch.object(orchestra, "ROOT", Path(folder)), \
                mock.patch.object(orchestra, "gpu", return_value=None), \
                mock.patch.object(orchestra, "output", return_value=orchestra.ACE_COMMIT), \
                mock.patch.object(orchestra, "run", side_effect=execute) as run:
            orchestra.ace_step(report, orchestra.Ace.yes, layout)
        return run, report

    def test_cpu_setup_uses_cpu_wheels_without_upstream_cuda_sync(self):
        with tempfile.TemporaryDirectory() as folder:
            run, report = self.setup_cpu(folder)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:4], [orchestra.UV, "venv", "--python", "3.12"])
        self.assertIn("--no-cache", commands[1])
        self.assertEqual(commands[1][6:8], ["--torch-backend", "cpu"])
        self.assertTrue(commands[1][-1].endswith("ace-cpu-requirements.txt"))
        self.assertEqual(len(commands), 3)  # environment, dependencies, pinned weights
        self.assertEqual(report.warnings, [])

    def test_cpu_setup_retries_dependencies_in_an_existing_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            run, report = self.setup_cpu(folder, existing=True)
        self.assertEqual(run.call_args_list[0].args[0][1:3], ["pip", "install"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(report.warnings, [])

    def test_failed_cpu_install_stops_before_weight_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            run, report = self.setup_cpu(folder, existing=True, install_ok=False)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("Installing ACE-Step dependencies failed", report.warnings[0])


class RecordingSetupTests(unittest.TestCase):
    def test_fresh_setup_uses_prepared_dataset_without_upstream_or_audio_commands(self):
        config = {"sources": [{"id": "dolphin"}], "config_sha256": "current"}
        with tempfile.TemporaryDirectory() as folder:
            report = orchestra.Report()
            layout = Layout(Path(folder))

            def install(data, settings):
                from experiments.data import write_json
                write_json(data.output / "follow" / "catalog.json", {"config_sha256": "current"})

            with mock.patch.object(orchestra, "load_config", return_value=config), \
                    mock.patch.object(orchestra, "prepared_current", return_value=False), \
                    mock.patch.object(orchestra, "download_bundle", side_effect=install) as download, \
                    mock.patch.object(orchestra, "run") as run:
                orchestra.recordings(report, layout)
        download.assert_called_once_with(layout, config)
        run.assert_not_called()
        self.assertEqual(report.warnings, [])


class BuildTests(unittest.TestCase):
    def test_failed_build_stops_start_before_downloads_or_server_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("ORCHESTRA_PORT=3070\n")
            with mock.patch.object(orchestra, "running", return_value=None), \
                    mock.patch.object(orchestra, "ENV", env), \
                    mock.patch.object(orchestra, "tools", return_value="/bun"), \
                    mock.patch.object(orchestra, "port_free"), \
                    mock.patch.object(orchestra, "demo_bundle"), \
                    mock.patch.object(orchestra, "packages"), \
                    mock.patch.object(orchestra, "run", return_value=False), \
                    mock.patch.object(orchestra, "recordings") as recordings, \
                    mock.patch.object(orchestra, "ace_step") as ace, \
                    mock.patch.object(orchestra, "serve") as serve:
                with self.assertRaises(typer.Exit):
                    orchestra.start(3070, "127.0.0.1", orchestra.Ace.no)
        recordings.assert_not_called()
        ace.assert_not_called()
        serve.assert_not_called()

    def test_start_rebuilds_an_existing_server_without_restarting_it(self):
        with tempfile.TemporaryDirectory() as folder:
            built = Path(folder) / "server.js"
            built.touch()
            with mock.patch.object(orchestra, "running", return_value={"url": "http://localhost:3070", "pid": 123}), \
                    mock.patch.object(orchestra, "tools", return_value="/bun"), \
                    mock.patch.object(orchestra, "packages"), \
                    mock.patch.object(orchestra, "BUILT_SERVER", built), \
                    mock.patch.object(orchestra, "run", return_value=True) as run, \
                    mock.patch.object(orchestra, "serve") as serve, \
                    mock.patch.object(orchestra, "recordings") as recordings:
                orchestra.start(3070, "127.0.0.1", orchestra.Ace.no)
        run.assert_called_once_with(["/bun", "run", "build"], cwd=orchestra.APP)
        serve.assert_not_called()
        recordings.assert_not_called()

    def test_process_detection_accepts_the_built_app_but_not_other_bun_servers(self):
        command = b"/bun\0" + str(orchestra.BUILD / "releases" / "release-test" / "server.js").encode() + b"\0"
        with mock.patch.object(orchestra, "alive", return_value=True), \
                mock.patch.object(Path, "read_bytes", return_value=command):
            self.assertTrue(orchestra.ours(123))
        with mock.patch.object(orchestra, "alive", return_value=True), \
                mock.patch.object(Path, "read_bytes", return_value=b"/bun\0/other-app/server.js\0"):
            self.assertFalse(orchestra.ours(123))

    def test_failed_dataset_never_falls_back_to_downloading_corpora(self):
        with tempfile.TemporaryDirectory() as folder:
            report = orchestra.Report()
            with mock.patch.object(orchestra, "load_config", return_value={"sources": []}), \
                    mock.patch.object(orchestra, "prepared_current", return_value=False), \
                    mock.patch.object(orchestra, "download_bundle", side_effect=OSError("offline")), \
                    mock.patch.object(orchestra, "run") as run:
                orchestra.recordings(report, Layout(Path(folder)))
        run.assert_not_called()
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("Startup does not download source corpora", report.warnings[0])

    def test_changed_composition_only_refreshes_catalog(self):
        config = {"sources": [{"id": "dolphin"}], "config_sha256": "new-composition"}
        with tempfile.TemporaryDirectory() as folder:
            report = orchestra.Report()
            layout = Layout(Path(folder))
            with mock.patch.object(orchestra, "load_config", return_value=config), \
                    mock.patch.object(orchestra, "prepared_current", return_value=True), \
                    mock.patch.object(orchestra, "download_bundle") as download, \
                    mock.patch.object(orchestra, "run", return_value=True) as run:
                orchestra.recordings(report, layout)
        download.assert_not_called()
        run.assert_called_once_with(orchestra.PYTHON + ["main.py", "follow", "catalog", "--data-dir", str(layout.root)])

    def test_ready_setup_needs_no_downloads_or_python_subprocess(self):
        from experiments.data import write_json
        config = {"sources": [{"id": "dolphin"}], "config_sha256": "current"}
        with tempfile.TemporaryDirectory() as folder:
            layout = Layout(Path(folder))
            write_json(layout.output / "follow" / "catalog.json", {"config_sha256": "current"})
            with mock.patch.object(orchestra, "load_config", return_value=config), \
                    mock.patch.object(orchestra, "prepared_current", return_value=True), \
                    mock.patch.object(orchestra, "download_bundle") as download, \
                    mock.patch.object(orchestra, "run") as run:
                orchestra.recordings(orchestra.Report(), layout)
        download.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
