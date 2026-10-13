import copy
import json
from pathlib import Path
import tempfile
import unittest

from experiments.follow_models import generation_paths, model_fingerprint, model_registry


class MusicModelsTest(unittest.TestCase):
    def test_default_models_recommendations_and_display_copy_do_not_change_audio_cache(self):
        registry = model_registry()
        self.assertEqual(registry["default_model"], "base")
        self.assertEqual(registry["primary_model"], "base")
        self.assertEqual([m["id"] for m in registry["models"] if m["recommended"]], ["base", "stable-audio-3-small"])
        base = registry["models"][0]
        self.assertEqual(model_fingerprint(base), model_fingerprint(base | {"label": "Base edited", "characteristics": ["Slower"], "recommended": False}))
        self.assertNotEqual(model_fingerprint(base), model_fingerprint(base | {"steps": 40}))
        paths = generation_paths(Path("piece"), 123, "base", base)
        self.assertIn("123-base-", paths["wav"].name)
        self.assertEqual(paths["manifest"].stem, paths["wav"].stem)
        self.assertNotEqual(paths["wav"], generation_paths(Path("piece"), 124, "base", base)["wav"])

    def test_invalid_registry_cannot_use_unknown_defaults_duplicate_ids_or_unsafe_paths(self):
        registry = model_registry()
        broken = [registry | {"default_model": "missing"}, registry | {"models": [registry["models"][0]] * 2}]
        unsafe = copy.deepcopy(registry)
        unsafe["models"][0]["id"] = "../base"
        broken.append(unsafe)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.json"
            for config in broken:
                path.write_text(json.dumps({"music": config}))
                with self.assertRaises(ValueError):
                    model_registry(path)
