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
        combo = plan(self.spec, self.config, self.prepared)
        self.assertEqual(combo["ace"]["caption"],
                         "Slow, spacious ambient chamber music featuring bells, horn, theremin. "
                         "Use only these instruments; preserve the guide's timing and melodic contours. Instrumental, no vocals.")
        for unwanted in ("strings", "mallets", "pads"):
            self.assertNotIn(unwanted, combo["ace"]["caption"])
        self.assertEqual(combo, plan(self.spec | {"parts": list(reversed(self.spec["parts"]))}, self.config, self.prepared))

    def test_one_species_has_one_instrument_even_with_repeated_recordings(self):
        combo = plan(self.spec | {"parts": [{"source": "bow"}, {"source": "bow"}]}, self.config, self.prepared)
        self.assertIn("featuring bowed strings.", combo["ace"]["caption"])
        self.assertNotIn("piano", combo["ace"]["caption"])
        self.assertEqual(combo["ace"]["caption"].count("bowed strings"), 1)

    def test_custom_caption_stays_explicit_and_changes_the_piece_id(self):
        automatic = plan(self.spec, self.config, self.prepared)
        custom = plan(self.spec | {"ace": {"task": "cover", "caption": "Solo piano by request"}}, self.config, self.prepared)
        self.assertEqual(custom["ace"]["caption"], "Solo piano by request")
        self.assertNotEqual(custom["id"], automatic["id"])
        legacy = plan(self.spec | {"ace": {"task": "cover", "caption": self.config["combination"]["caption"]}},
                      self.config, self.prepared)
        self.assertNotEqual(legacy["id"], automatic["id"])

    def test_model_job_receives_the_resolved_prompt_and_same_seed(self):
        from pathlib import Path
        combo = plan(self.spec, self.config, self.prepared)
        job = ace_job(combo, self.config, Path("/tmp/test-ensemble"))
        self.assertEqual(job["caption"], combo["ace"]["caption"])
        self.assertEqual(job["seed"], self.config["seed"])
