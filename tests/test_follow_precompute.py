import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments.data import Layout, read_json, write_json
from experiments.follow import app, load_config
from experiments.follow_precompute import combination_specs, duo_specs, precompute_combinations, precompute_trios, run_local_base
from experiments.follow_models import generation_paths, model_registry, music_model
from typer.testing import CliRunner

HAS_AUDIO = all(importlib.util.find_spec(name) for name in ("numpy", "scipy", "soundfile", "matplotlib", "PIL")) and shutil.which("ffmpeg")


class DuoSelectionTests(unittest.TestCase):
    def test_twenty_selectable_recordings_make_190_unordered_pairs(self):
        config = {"sources": [{"id": str(i)} for i in range(20)] + [{"id": "study-only", "map": False}]}
        specs = duo_specs(config, {str(i): {} for i in range(20)})
        self.assertEqual(len(specs), 190)
        pairs = {tuple(p["source"] for p in spec["parts"]) for spec in specs}
        self.assertEqual(len(pairs), 190)
        self.assertTrue(all(a != b for a, b in pairs))
        self.assertTrue(all([p["offset_s"] for p in spec["parts"]] == [0, 0] for spec in specs))

    def test_missing_sources_are_reported_before_any_rendering(self):
        config = {"sources": [{"id": "a"}, {"id": "b"}]}
        with self.assertRaisesRegex(ValueError, "Unprepared sources: b"):
            duo_specs(config, {"a": {}})
        with self.assertRaisesRegex(ValueError, "Unknown map sources: c"):
            duo_specs(config, {"a": {}, "b": {}}, ["a", "c"])
        with self.assertRaisesRegex(ValueError, "at least two"):
            duo_specs(config, {"a": {}, "b": {}}, ["a"])


class TrioSelectionTests(unittest.TestCase):
    def test_twenty_recordings_make_1140_distinct_simultaneous_trios(self):
        config = {"sources": [{"id": str(i)} for i in range(20)] + [{"id": "study", "map": False}]}
        specs = combination_specs(config, {str(i): {} for i in range(20)}, 3)
        trios = {tuple(part["source"] for part in spec["parts"]) for spec in specs}
        self.assertEqual(len(trios), 1140)
        self.assertTrue(all(len(set(trio)) == 3 for trio in trios))
        self.assertTrue(all(trio == tuple(sorted(trio)) for trio in trios))
        self.assertTrue(all([part["offset_s"] for part in spec["parts"]] == [0, 0, 0] for spec in specs))

    def test_source_filter_selects_exactly_one_trio_and_reports_invalid_inputs(self):
        config = {"sources": [{"id": name} for name in "abcd"]}
        prepared = {name: {} for name in "abcd"}
        specs = combination_specs(config, prepared, 3, ["d", "a", "c", "a"])
        self.assertEqual([[part["source"] for part in spec["parts"]] for spec in specs], [["a", "c", "d"]])
        with self.assertRaisesRegex(ValueError, "at least 3"):
            combination_specs(config, prepared, 3, ["a", "b"])
        with self.assertRaisesRegex(ValueError, "Unprepared sources: d"):
            combination_specs(config, {name: {} for name in "abc"}, 3)
        with self.assertRaisesRegex(ValueError, "Unknown map sources: x"):
            combination_specs(config, prepared, 3, ["a", "b", "x"])

    def test_cli_dry_run_and_errors(self):
        with patch("experiments.follow_precompute.precompute_trios", return_value=None) as batch:
            result = CliRunner().invoke(app, ["precompute-trios", "--dry-run", "--workers", "2", "--source", "a"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(batch.call_args.args[2], ["a"])
            self.assertEqual(batch.call_args.args[-2:], (2, True))
        with patch("experiments.follow_precompute.precompute_trios", side_effect=ValueError("Unprepared sources: a")):
            result = CliRunner().invoke(app, ["precompute-trios"])
            self.assertEqual(result.exit_code, 2)
            self.assertIn("ERROR: Unprepared sources: a", result.output)

    def test_local_base_uses_one_cuda_runner_even_with_hf_configured(self):
        with tempfile.TemporaryDirectory() as temporary:
            layout = Layout(Path(temporary))
            ace_root = layout.interim / "tools" / "ACE-Step-1.5"
            python = ace_root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            jobs = [{"id": str(i)} for i in range(4)]
            jobs_path = layout.interim / "follow" / "jobs.json"
            results_path = jobs_path.with_name("results.json")

            def generate(command, **kwargs):
                spec = read_json(Path(command[-1]))
                self.assertEqual(spec["jobs"], jobs)
                self.assertEqual(spec["device"], "cuda")
                self.assertEqual(spec["model"], "acestep-v15-base")
                write_json(Path(spec["results"]), {"jobs": {job["id"]: job for job in jobs}})

            with patch.dict(os.environ, {"ORCHESTRA_HF_SPACE": "owner/space", "ORCHESTRA_ACE_BACKEND": "auto"}), \
                    patch("experiments.follow_precompute.subprocess.run", side_effect=generate) as process:
                results = run_local_base(layout, load_config(), jobs, jobs_path, results_path, None, False)
            process.assert_called_once()
            self.assertEqual(len(results["jobs"]), 4)


@unittest.skipUnless(HAS_AUDIO, "Run with uv run --group audio (needs ffmpeg)")
class TrioBatchTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import soundfile as sf

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.layout = Layout(Path(self.temporary.name))
        self.config = load_config()
        python = self.layout.interim / "tools" / "ACE-Step-1.5" / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.touch()
        output = self.layout.output / "follow"
        output.mkdir(parents=True)
        sf.write(output / "source.wav", .1 * np.sin(2 * np.pi * 300 * np.arange(6 * 48000) / 48000), 48000)
        location = {"label": "Test sea", "lon": 0, "lat": 0}
        self.config["sources"] = [{"id": name, "species": "sperm whale", "location": location} for name in "abcd"]
        sources = [{"id": name, "species": "sperm whale", "title": name, "note": "synthetic", "duration_s": 6,
                    "register_shift_octaves": 0, "onsets_s": [1, 2, 3, 4],
                    "events": [{"kind": "click", "time_s": t, "group": 0} for t in [1, 2, 3, 4]],
                    "stems": {"animal": "source.wav", "guide": "source.wav"}} for name in "abcd"]
        write_json(self.layout.interim / "follow" / "prepared.json", {"sources": sources})

    def fake_model(self, layout, config, jobs, jobs_path, results_path, ace_root, offload):
        results = read_json(results_path) if results_path.exists() else {"jobs": {}, "model": "acestep-v15-base"}
        for job in jobs:
            shutil.copyfile(job["src_audio"], job["output"])
            results["jobs"][job["id"]] = job | {"seconds": 0}
            write_json(results_path, results)
        return results

    def checkpoint_path(self):
        return generation_paths(self.layout.interim / "follow" / "batches" / "trios", self.config["seed"],
                                "base", music_model("base"))["results"]

    def test_batch_generates_four_trios_in_one_model_call_and_reuses_them(self):
        with patch("experiments.follow_precompute.run_local_base", side_effect=self.fake_model) as model, \
                patch("experiments.follow_combine.run_ace", side_effect=AssertionError("No per-piece model loading")), \
                contextlib.redirect_stdout(io.StringIO()):
            path = precompute_trios(self.layout, self.config, workers=1)
            index = read_json(path)
            self.assertEqual(path.name, "trios.json")
            self.assertEqual(len(index["pieces"]), 4)
            model.assert_called_once()
            self.assertEqual(len(model.call_args.args[2]), 4)
            for piece in index["pieces"]:
                manifest = read_json(self.layout.output / "follow" / "combos" / piece["id"] / "manifest.json")
                self.assertEqual(len(manifest["parts"]), 3)
                self.assertEqual(manifest["ace"]["variant"], "base")
                self.assertTrue((self.layout.output / "follow" / manifest["stems"]["ace"]["audio"]).exists())
            self.assertEqual(read_json(precompute_trios(self.layout, self.config, workers=1))["pieces"], index["pieces"])
            model.assert_called_once()

    def test_interrupted_batch_resumes_without_regenerating_its_first_audio(self):
        def interrupt(layout, config, jobs, jobs_path, results_path, ace_root, offload):
            self.fake_model(layout, config, jobs[:1], jobs_path, results_path, ace_root, offload)
            raise subprocess.CalledProcessError(1, "ACE-Step")

        with patch("experiments.follow_precompute.run_local_base", side_effect=interrupt), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                precompute_trios(self.layout, self.config, workers=1)
        checkpoint = read_json(self.checkpoint_path())
        first = next(iter(checkpoint["jobs"].values()))
        stamp = Path(first["output"]).stat().st_mtime_ns
        with patch("experiments.follow_precompute.run_local_base", side_effect=self.fake_model) as model, contextlib.redirect_stdout(io.StringIO()):
            path = precompute_trios(self.layout, self.config, workers=1)
        self.assertEqual(len(model.call_args.args[2]), 3)
        self.assertNotIn(first["id"], [job["id"] for job in model.call_args.args[2]])
        self.assertEqual(Path(first["output"]).stat().st_mtime_ns, stamp)
        self.assertEqual(len(read_json(path)["pieces"]), 4)

    def test_base_batch_preserves_turbo_and_stable_outputs_when_the_app_defaults_to_stable(self):
        from experiments.follow_combine import combine, plan, prepared_sources

        spec = combination_specs(self.config, prepared_sources(self.layout), 3, ["a", "b", "c"])[0]

        def one_job(layout, config_or_model, job_or_jobs, jobs_path, results_path, *args):
            jobs = [job_or_jobs] if isinstance(job_or_jobs, dict) else job_or_jobs
            return self.fake_model(layout, self.config, jobs, jobs_path, results_path, None, False)

        with patch("experiments.follow_combine.run_ace", side_effect=one_job), \
                patch("experiments.follow_combine.run_stable", side_effect=one_job), contextlib.redirect_stdout(io.StringIO()):
            combine(self.layout, self.config, spec, model_variant="turbo")
            combine(self.layout, self.config, spec, model_variant="stable-audio-3-small")
        folder = self.layout.output / "follow" / "combos" / plan(spec, self.config, prepared_sources(self.layout))["id"]
        retained = {path: path.read_bytes() for path in folder.glob("generated-*")}
        retained.update({path: path.read_bytes() for path in folder.glob("manifest-*.json")})
        stable_folder = self.layout.output / "follow" / "combos" / plan(
            spec, self.config, prepared_sources(self.layout), model_variant="stable-audio-3-small")["id"]
        retained.update({path: path.read_bytes() for path in stable_folder.glob("generated-*")})
        retained.update({path: path.read_bytes() for path in stable_folder.glob("manifest-*.json")})
        registry = model_registry()
        registry["default_model"] = "stable-audio-3-small"
        with patch("experiments.follow_models.model_registry", return_value=registry), \
                patch("experiments.follow_combine.model_registry", return_value=registry), \
                patch("experiments.follow_precompute.run_local_base", side_effect=self.fake_model) as model, \
                patch("experiments.follow_combine.run_ace", side_effect=AssertionError("No per-piece model loading")), \
                patch("experiments.follow_combine.run_stable", side_effect=AssertionError("The batch selects Base")), \
                contextlib.redirect_stdout(io.StringIO()):
            path = precompute_trios(self.layout, self.config, only=["a", "b", "c"], workers=1)
        model.assert_called_once()
        manifest = read_json(folder / "manifest-base.json")
        self.assertEqual(manifest["generation"]["model_id"], "base")
        self.assertIn("-base-", manifest["stems"]["ace"]["audio"])
        for path, content in retained.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def test_legacy_base_audio_and_preset_names_survive_migration_without_a_model_job(self):
        from experiments.follow_combine import ace_job, combine, plan, prepared_sources

        spec = combination_specs(self.config, prepared_sources(self.layout), 3, ["a", "b", "c"])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            path = combine(self.layout, self.config, spec, run_model=False, model_variant="base")
        combo = plan(spec, self.config, prepared_sources(self.layout))
        job = ace_job(combo, self.config, path.parent)
        shutil.copyfile(job["src_audio"], job["output"])
        write_json(path.parent / "ace-results.json", {"model": "acestep-v15-base", "jobs": {job["id"]: job | {"seconds": 0}}})
        # Keep the original preset's media URLs and metadata alongside the new names.
        from experiments.follow_gallery import listening, MUSIC_BAND
        listening(Path(job["output"]), MUSIC_BAND, self.config["sample_rate"])
        preset = self.layout.output / "follow" / "presets.json"
        write_json(preset, {combo["id"]: {"name": "My saved sea", "saved_at_utc": "2026-10-08"}})
        retained = {file: file.read_bytes() for file in path.parent.glob("ace*")}
        retained[preset] = preset.read_bytes()
        with patch("experiments.follow_precompute.run_local_base") as model, \
                patch("experiments.follow_combine.run_ace", side_effect=AssertionError("Reuse legacy Base music")), \
                contextlib.redirect_stdout(io.StringIO()):
            precompute_trios(self.layout, self.config, only=["a", "b", "c"], workers=1)
        model.assert_not_called()
        for file, content in retained.items():
            self.assertEqual(file.read_bytes(), content, str(file))
        manifest = read_json(path.parent / "manifest-base.json")
        self.assertEqual(manifest["ace"]["variant"], "base")
        self.assertEqual((self.layout.output / "follow" / manifest["stems"]["ace"]["audio"]).with_suffix(".wav").read_bytes(),
                         Path(job["output"]).read_bytes())

    def test_a_model_setting_change_generates_a_new_file_once_and_preserves_the_old_audio(self):
        with patch("experiments.follow_precompute.run_local_base", side_effect=self.fake_model), contextlib.redirect_stdout(io.StringIO()):
            path = precompute_trios(self.layout, self.config, only=["a", "b", "c"], workers=1)
        piece = read_json(path)["pieces"][0]
        folder = self.layout.output / "follow" / "combos" / piece["id"]
        old_manifest = read_json(folder / "manifest-base.json")
        old_audio = self.layout.output / "follow" / old_manifest["stems"]["ace"]["audio"]
        old_bytes = old_audio.read_bytes()
        registry = model_registry()
        base = music_model("base", registry)
        base["steps"] += 1
        with patch("experiments.follow_models.model_registry", return_value=registry), \
                patch("experiments.follow_combine.model_registry", return_value=registry), \
                patch("experiments.follow_precompute.run_local_base", side_effect=self.fake_model) as model, \
                patch("experiments.follow_combine.run_ace", side_effect=AssertionError("The new batch output is already generated")), \
                contextlib.redirect_stdout(io.StringIO()):
            precompute_trios(self.layout, self.config, only=["a", "b", "c"], workers=1)
        model.assert_called_once()
        self.assertEqual(model.call_args.args[2][0]["inference_steps"], base["steps"])
        self.assertNotEqual(read_json(folder / "manifest-base.json")["stems"]["ace"]["audio"], old_manifest["stems"]["ace"]["audio"])
        self.assertEqual(old_audio.read_bytes(), old_bytes)

    def test_dry_run_only_reads_sources_and_saved_results(self):
        before = sorted(str(path) for path in self.layout.root.rglob("*"))
        with patch("experiments.follow_precompute.render_pieces") as render, \
                patch("experiments.follow_precompute.run_local_base") as model, contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertIsNone(precompute_trios(self.layout, self.config, dry_run=True))
        render.assert_not_called()
        model.assert_not_called()
        self.assertEqual(sorted(str(path) for path in self.layout.root.rglob("*")), before)
        self.assertIn("4 trios: 0 finished, 4 pending", log.getvalue())

    def test_missing_model_environment_is_reported_before_preparing_guides(self):
        python = self.layout.interim / "tools" / "ACE-Step-1.5" / ".venv" / "bin" / "python"
        python.unlink()
        with patch("experiments.follow_precompute.render_pieces") as render, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "No ACE-Step environment"):
                precompute_trios(self.layout, self.config)
        render.assert_not_called()

    def test_stable_duos_and_trios_share_one_model_load_and_preserve_the_base_index(self):
        python = self.layout.interim / "tools/Stable-Audio-3/.venv/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        legacy_index = self.layout.output / "follow/duos.json"
        write_json(legacy_index, {"existing_base_duos": True})

        def stable(layout, model, jobs, jobs_path, results_path, *_):
            results = {"model": model["checkpoint"], "jobs": {}}
            for job in jobs:
                shutil.copyfile(job["src_audio"], job["output"])
                results["jobs"][job["id"]] = job | {"model": model["checkpoint"], "seconds": 0,
                    "parameters": {"init_noise_level": model["init_noise_level"]}}
            write_json(results_path, results)
            return results

        with patch("experiments.follow_precompute.run_local_stable", side_effect=stable) as model, \
                patch("experiments.follow_combine.run_stable", side_effect=AssertionError("No per-piece model load")), \
                contextlib.redirect_stdout(io.StringIO()):
            paths = precompute_combinations(self.layout, self.config, [2, 3], workers=1, local=True,
                                           model_variant="stable-audio-3-small")
            model.assert_called_once()
            self.assertEqual(len(model.call_args.args[2]), 10)
            self.assertEqual([len(read_json(path)["pieces"]) for path in paths], [6, 4])
            self.assertTrue(all("stable-audio-3-small" in path.name for path in paths))
            self.assertEqual(read_json(legacy_index), {"existing_base_duos": True})
            for path in paths:
                for piece in read_json(path)["pieces"]:
                    manifest = read_json(self.layout.output / "follow/combos" / piece["id"] / "manifest-stable-audio-3-small.json")
                    self.assertIsNone(manifest["ace"])
                    self.assertEqual(manifest["generation"]["model_id"], "stable-audio-3-small")
                    self.assertIn("playback_gain", manifest["parts"][0])
                    self.assertNotIn("/combos/", manifest["parts"][0]["files"]["animal"])
            precompute_combinations(self.layout, self.config, [2, 3], workers=1, local=True,
                                   model_variant="stable-audio-3-small")
            model.assert_called_once()


if __name__ == "__main__":
    unittest.main()
