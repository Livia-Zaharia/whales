import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from experiments import follow_hf


def wav(path):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 800)


class StatusTests(unittest.TestCase):
    def test_quota_exhaustion_disables_a_running_space(self):
        with patch.object(follow_hf, "hf_json", side_effect=[
            {"host": "https://test.hf.space", "runtime": {"stage": "RUNNING"}}, {"current": 0},
        ]):
            status = follow_hf.space_status("owner/music")
        self.assertFalse(status["available"])
        self.assertEqual(status["stage"], "QUOTA_EXHAUSTED")

    def test_offline_hf_keeps_cpu_available(self):
        with patch.dict(os.environ, {"ORCHESTRA_HF_SPACE": "owner/music"}, clear=True), \
                patch.object(follow_hf, "hf_json", side_effect=OSError("offline")), \
                patch.object(follow_hf, "local_runtime", return_value={"device": "cuda", "cuda_available": True}):
            runtime = follow_hf.runtime_info()
        self.assertTrue(runtime["ace_available"])
        self.assertEqual(runtime["device"], "cpu")
        self.assertFalse(runtime["hf_available"])

    def test_warm_hf_works_without_a_local_model(self):
        with patch.dict(os.environ, {"ORCHESTRA_HF_SPACE": "owner/music"}, clear=True), \
                patch.object(follow_hf, "space_status", return_value={"available": True, "stage": "RUNNING"}), \
                patch.object(follow_hf, "local_runtime", return_value={"device": None, "cuda_available": False}):
            runtime = follow_hf.runtime_info()
        self.assertTrue(runtime["ace_available"])
        self.assertEqual(runtime["device"], "hf")

    def test_explicit_cpu_never_probes_hf(self):
        with patch.dict(os.environ, {"ORCHESTRA_HF_SPACE": "owner/music", "ORCHESTRA_ACE_BACKEND": "cpu"}, clear=True), \
                patch.object(follow_hf, "space_status") as remote, \
                patch.object(follow_hf, "local_runtime", return_value={"device": "cuda", "cuda_available": True}):
            runtime = follow_hf.runtime_info()
        remote.assert_not_called()
        self.assertEqual(runtime["device"], "cpu")


class RemoteTests(unittest.TestCase):
    def run_remote(self, cold=False, quota=2400, health_revision=None):
        from orchestra import ACE_COMMIT

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            generated = root / "download.wav"
            wav(generated)
            job = {"id": "one", "task": "cover", "track": None, "src_audio": str(root / "guide.wav"),
                   "caption": "ambient", "duration": 10, "seed": 42, "inference_steps": 50,
                   "guidance_scale": 7, "audio_cover_strength": .6, "output": str(root / "result.wav")}
            spec = {"jobs": [job], "results": str(root / "results.json"), "model": "acestep-v15-base",
                    "main_revision": "main", "model_revision": "base"}
            health = {"ready": True, "models": [spec["model"]], "ace_git_revision": health_revision or ACE_COMMIT,
                      "main_revision": "main", "model_revisions": {spec["model"]: "base"}}
            client = Mock()
            metadata = {"model": spec["model"], "seed_used": 42, "backend": "hf-zerogpu"}
            client.submit.side_effect = [Mock(result=Mock(return_value=health)),
                                         Mock(result=Mock(return_value=(str(generated), metadata)))]
            client_module = SimpleNamespace(Client=Mock(return_value=client), handle_file=lambda value: {"path": value})
            warm = {"available": True, "stage": "RUNNING", "quota_remaining_s": quota}
            stages = ([{"available": False, "stage": "SLEEPING", "host": "https://test.hf.space"}] if cold else []) + [warm, warm]
            with patch.dict(os.environ, {"ORCHESTRA_HF_SPACE": "owner/music"}, clear=True), \
                    patch.dict(sys.modules, {"gradio_client": client_module}), \
                    patch.object(follow_hf, "space_status", side_effect=stages), \
                    patch.object(follow_hf, "hf_json", return_value={}) as wake, \
                    patch.object(follow_hf.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                follow_hf.remote_jobs(spec)
            self.assertTrue(Path(job["output"]).exists())
            self.assertEqual(json.loads(Path(spec["results"]).read_text())["jobs"]["one"]["seed_used"], 42)
            client.close.assert_called_once()
            return client, wake, job

    def test_cold_space_is_woken_and_exact_parameters_are_sent(self):
        client, wake, job = self.run_remote(cold=True)
        wake.assert_called_once_with("https://test.hf.space/config")
        args = client.submit.call_args.args
        self.assertEqual(args[0], {"path": job["src_audio"]})
        payload = json.loads(args[1])
        self.assertEqual(payload["seed"], 42)
        self.assertEqual(payload["audio_cover_strength"], .6)
        self.assertNotIn("output", payload)

    def test_mismatched_code_never_generates(self):
        with self.assertRaisesRegex(ValueError, "pinned code"):
            self.run_remote(health_revision="different")

    def test_insufficient_included_quota_never_generates(self):
        with self.assertRaisesRegex(ValueError, "included HF"):
            self.run_remote(quota=1)


FAKE_RUNNER = '''
import json, pathlib, sys, time, wave
spec=json.loads(pathlib.Path(sys.argv[1]).read_text())
time.sleep(DELAY)
if FAIL:
    raise SystemExit(1)
results={"jobs":{}}
for job in spec["jobs"]:
    path=pathlib.Path(job["output"])
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\\0\\0"*800)
    if BAD:
        path.write_bytes(b"broken")
    results["jobs"][job["id"]]=job|{"seed_used":42,"device_type":spec.get("device","cuda")}
pathlib.Path(spec["results"]).write_text(json.dumps(results))
'''


class RaceTests(unittest.TestCase):
    def race(self, hf_delay=.01, cpu_delay=.5, hf_fail=False, cpu_fail=False, hf_bad=False):
        popen = subprocess.Popen
        processes = []
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            spec = {"ace_root": scratch, "model": "acestep-v15-base", "offload": True,
                    "quantization": "int8_weight_only", "results": str(root / "results.json"),
                    "jobs": [{"id": "one", "output": str(root / "winner.wav"), "seed": 42}]}
            cpu = root / "cpu.py"
            hf = root / "hf.py"
            for path, delay, fail, bad in [(cpu, cpu_delay, cpu_fail, False), (hf, hf_delay, hf_fail, hf_bad)]:
                path.write_text(f"DELAY={delay!r}\nFAIL={fail!r}\nBAD={bad!r}\n" + FAKE_RUNNER)

            def launch(command, **kwargs):
                path = hf if "experiments.follow_hf" in command else cpu
                process = popen([sys.executable, str(path), command[-1]], **kwargs)
                processes.append(process)
                return process

            with patch.object(follow_hf.subprocess, "Popen", side_effect=launch), \
                    contextlib.redirect_stdout(io.StringIO()):
                follow_hf.race_backends(spec, root / "jobs.json", Path(sys.executable), cpu)
            result = json.loads(Path(spec["results"]).read_text())
            self.assertTrue(all(p.poll() is not None for p in processes))
            self.assertEqual(Path(spec["jobs"][0]["output"]).read_bytes()[:4], b"RIFF")
            self.assertEqual(result["jobs"]["one"]["output"], spec["jobs"][0]["output"])
            self.assertFalse(list(root.glob("ace-race-*")))
            return result

    def test_hf_wins_and_cpu_process_is_stopped(self):
        result = self.race()
        self.assertEqual(result["race_winner"], "hf-zerogpu")

    def test_cpu_wins_during_a_cold_start(self):
        result = self.race(hf_delay=.5, cpu_delay=.01)
        self.assertEqual(result["race_winner"], "local-cpu")
        self.assertEqual(result["jobs"]["one"]["device_type"], "cpu")

    def test_remote_failure_does_not_interrupt_cpu(self):
        result = self.race(hf_fail=True, cpu_delay=.1)
        self.assertEqual(result["race_winner"], "local-cpu")

    def test_invalid_remote_audio_cannot_win(self):
        result = self.race(hf_bad=True, cpu_delay=.1)
        self.assertEqual(result["race_winner"], "local-cpu")

    def test_both_failures_report_error_instead_of_publishing(self):
        with self.assertRaisesRegex(ValueError, "both backends"):
            self.race(hf_fail=True, cpu_fail=True, cpu_delay=.02)


if __name__ == "__main__":
    unittest.main()
