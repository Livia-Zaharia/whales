import unittest

from experiments.follow import load_config
from experiments.follow_combine import ace_job, plan


class InstrumentPromptTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.prepared = {source: {"species": species, "duration_s": 30, "register_shift_octaves": 0}
                         for source, species in (("bell", "minke whale"), ("horn", "right whale"),
                                                 ("theremin", "spinner dolphin"), ("bow", "humpback"))}
        self.spec = {"parts": [{"source": source} for source in ("horn", "theremin", "bell")],
                     "ace": {"task": "cover"}}

    def test_prompt_names_selected_instruments_without_the_generic_backing(self):
        combo = plan(self.spec, self.config, self.prepared, model_variant="stable-audio-3-small")
        self.assertEqual(combo["ace"]["caption"],
                         "Slow, spacious ambient chamber music featuring bells, horn, theremin. "
                         "Use only these instruments; preserve the guide's timing and melodic contours. Instrumental, no vocals.")
        for unwanted in ("strings", "mallets", "pads"):
            self.assertNotIn(unwanted, combo["ace"]["caption"])
        self.assertEqual(combo, plan(self.spec | {"parts": list(reversed(self.spec["parts"]))}, self.config,
                                     self.prepared, model_variant="stable-audio-3-small"))

    def test_one_species_has_one_instrument_even_with_repeated_recordings(self):
        combo = plan(self.spec | {"parts": [{"source": "bow"}, {"source": "bow"}]}, self.config,
                     self.prepared, model_variant="stable-audio-3-small")
        self.assertIn("featuring bowed strings.", combo["ace"]["caption"])
        self.assertNotIn("piano", combo["ace"]["caption"])
        self.assertEqual(combo["ace"]["caption"].count("bowed strings"), 1)

    def test_custom_caption_stays_explicit_and_changes_the_piece_id(self):
        automatic = plan(self.spec, self.config, self.prepared, model_variant="stable-audio-3-small")
        custom = plan(self.spec | {"ace": {"task": "cover", "caption": "Solo piano by request"}}, self.config, self.prepared)
        self.assertEqual(custom["ace"]["caption"], "Solo piano by request")
        self.assertNotEqual(custom["id"], automatic["id"])
        legacy = plan(self.spec | {"ace": {"task": "cover", "caption": self.config["combination"]["caption"]}},
                      self.config, self.prepared)
        self.assertNotEqual(legacy["id"], automatic["id"])

    def test_base_restores_the_earlier_caption_and_id(self):
        explicit = self.spec | {"ace": {"task": "cover", "caption": self.config["combination"]["caption"]}}
        expected = plan(explicit, self.config, self.prepared)
        self.assertEqual(plan(self.spec, self.config, self.prepared, model_variant="base"), expected)
        solo = plan(self.spec | {"parts": [{"source": "bow"}]}, self.config, self.prepared, model_variant="base")
        self.assertEqual(solo["ace"]["caption"], self.config["species"]["humpback"]["caption"])

    def test_turbo_and_stable_share_the_instrument_focused_default(self):
        turbo = plan(self.spec, self.config, self.prepared, model_variant="turbo")
        stable = plan(self.spec, self.config, self.prepared, model_variant="stable-audio-3-small")
        self.assertEqual(turbo, stable)
        self.assertIn("featuring bells, horn, theremin.", turbo["ace"]["caption"])

    def test_shared_custom_captions_keep_the_same_id_across_models(self):
        spec = self.spec | {"ace": {"task": "cover", "caption": "The same explicit comparison prompt"}}
        self.assertEqual(len({plan(spec, self.config, self.prepared, model_variant=model)["id"]
                              for model in ("base", "turbo", "stable-audio-3-small")}), 1)

    def test_profiles_can_be_changed_in_app_config(self):
        from unittest.mock import patch
        from experiments.follow_models import model_registry
        registry = model_registry()
        registry["caption_profiles"]["base"] = "instruments"
        with patch("experiments.follow_prompt.model_registry", return_value=registry):
            combo = plan(self.spec, self.config, self.prepared, model_variant="base")
        self.assertIn("featuring bells, horn, theremin.", combo["ace"]["caption"])

    def test_model_job_receives_the_resolved_prompt_and_same_seed(self):
        from pathlib import Path
        combo = plan(self.spec, self.config, self.prepared)
        job = ace_job(combo, self.config, Path("/tmp/test-ensemble"))
        self.assertEqual(job["caption"], combo["ace"]["caption"])
        self.assertEqual(job["seed"], self.config["seed"])
