import copy
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

from experiments.data import Layout, digest, read_json, write_json
from experiments.follow_dataset import build_bundle, install_bundle, parquet_sources, preparation_hash, prepared_current, read_prepared, unpack


class PreparedDatasetTests(unittest.TestCase):
    def config(self):
        return {"config_sha256": "config", "sample_rate": 48000, "sources": [{"id": "test"}],
                "species": {"whale": {"band_hz": [50, 500], "caption": "music"}}, "perturbation": {}, "response": {},
                "combination": {}, "seed": 1, "ace_step": {"model": "base"}}

    def source(self):
        tonal = {"kind": "tonal", "start_s": 0.123456789012345, "end_s": 0.5,
                 "time_s": [0.123456789012345, 0.5], "hz": [85.1234567890123, 100.0], "origin": "test", "label": "call"}
        click = {"kind": "click", "time_s": 0.75, "group": 0}
        return {"id": "test", "species": "whale", "title": "Test", "note": "Measured", "duration_s": 1.0,
                "native_rate": 48000, "provenance": {"file": "source.wav", "archive": "daily.tar"}, "units": None,
                "events": [tonal, click], "perturbed_events": [click, tonal], "onsets_s": [0.123456789012345, 0.75],
                "perturbed_onsets_s": [0.75, 0.123456789012345], "counts": {"tonal": 1}, "instrument": {"harmonics": [1.0]},
                "register_shift_octaves": 0, "playback_gain": 1.0, "response_gain": 1.0, "playback_note": "PCM",
                "stems": {"animal": "test/animal.wav", "guide": "test/guide.wav"}}

    def test_composition_seed_caption_and_model_changes_do_not_invalidate_measurements(self):
        original = self.config()
        changed = copy.deepcopy(original)
        changed.update(seed=55, combination={"caption": "new"}, ace_step={"model": "turbo"})
        changed["species"]["whale"]["caption"] = "another caption"
        self.assertEqual(preparation_hash(original), preparation_hash(changed))
        changed["species"]["whale"]["band_hz"] = [100, 1000]
        self.assertNotEqual(preparation_hash(original), preparation_hash(changed))

    def test_parquet_preserves_exact_contours_event_order_and_absent_fields(self):
        source = self.source()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            parquet_sources([source], path)
            write_json(path / "dataset.json", {"sample_rate": 48000})
            restored = read_prepared(path)
        self.assertEqual(restored, {"sample_rate": 48000, "sources": [source]})

    def test_missing_audio_and_changed_preparation_version_are_not_marked_ready(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as folder:
            layout = Layout(Path(folder))
            source = self.source()
            write_json(layout.interim / "follow" / "prepared.json", {"config_sha256": "config", "preparation_sha256": "old", "sources": [source]})
            self.assertFalse(prepared_current(layout, config))
            write_json(layout.interim / "follow" / "prepared.json", {"preparation_sha256": preparation_hash(config), "sources": [source]})
            self.assertFalse(prepared_current(layout, config))
            for stem in source["stems"].values():
                path = layout.output / "follow" / stem
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertTrue(prepared_current(layout, config))

    def test_tar_paths_and_links_are_rejected(self):
        for name, link in (("../escape.flac", False), ("/escape.flac", False), ("test/animal.flac", True)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                archive = Path(folder) / "audio.tar"
                with tarfile.open(archive, "w") as stream:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/tmp/escape"
                    else:
                        member.size = 1
                    stream.addfile(member, None if link else io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    unpack(archive, Path(folder) / "output")

    def test_download_uses_only_pinned_runtime_files_with_bounded_parallelism(self):
        from experiments.follow_dataset import download_bundle
        config = self.config()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pin = root / "pin.json"
            release = {"repo_id": "test/phrases", "revision": "a" * 40, "preparation_sha256": preparation_hash(config), "bundle_sha256": "expected"}
            write_json(pin, release)
            with mock.patch("huggingface_hub.snapshot_download") as download, \
                    mock.patch("experiments.follow_dataset.digest", return_value="expected"), \
                    mock.patch("experiments.follow_dataset.install_bundle"):
                download_bundle(Layout(root / "data"), config, pin)
        args = download.call_args.kwargs
        self.assertEqual(args["revision"], "a" * 40)
        self.assertEqual(args["repo_type"], "dataset")
        self.assertEqual(args["max_workers"], 2)
        self.assertNotIn("README.md", args["allow_patterns"])
        self.assertNotIn("*.tar", args["allow_patterns"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FLAC encoding needs ffmpeg")
    def test_bundle_installs_without_raw_inputs_and_preserves_audio_samples(self):
        import numpy as np
        import soundfile as sf

        config, source = self.config(), self.source()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            layout = Layout(root / "producer")
            output = layout.output / "follow"
            pcm = np.arange(-1000, 1000, dtype=np.int16)
            for stem in source["stems"].values():
                path = output / stem
                path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(path, pcm, 48000, subtype="PCM_16")
            write_json(layout.interim / "follow" / "prepared.json", {"config_sha256": "config", "sample_rate": 48000, "sources": [source]})
            write_json(output / "catalog.json", {"config_sha256": "config", "countries": "world.geojson", "sources": []})
            write_json(output / "world.geojson", {"type": "FeatureCollection", "features": []})
            bundle = root / "bundle"
            build_bundle(layout, config, bundle)
            with tarfile.open(bundle / "recordings.tar") as archive:
                credit = json.load(archive.extractfile("test.json"))
            self.assertEqual(json.loads(credit["provenance_json"]), source["provenance"])
            self.assertTrue(all(isinstance(value, str) for value in credit.values()))
            consumer = Layout(root / "consumer")
            install_bundle(bundle, consumer, config)
            self.assertFalse(consumer.input.exists())
            self.assertTrue(prepared_current(consumer, config))
            restored = read_json(consumer.interim / "follow" / "prepared.json")
            decoded, rate = sf.read(consumer.output / "follow" / restored["sources"][0]["stems"]["animal"], dtype="int16")
            np.testing.assert_array_equal(decoded, pcm)
            self.assertEqual(rate, 48000)
            # Corruption must fail before overwriting a previously installed manifest.
            before = digest(consumer.interim / "follow" / "prepared.json")
            (bundle / "recordings.tar").write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "checksum"):
                install_bundle(bundle, consumer, config)
            self.assertEqual(digest(consumer.interim / "follow" / "prepared.json"), before)


if __name__ == "__main__":
    unittest.main()
