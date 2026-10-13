import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from experiments.follow_space import SOURCE


class SpaceContractTests(unittest.TestCase):
    def test_startup_shares_components_and_api_preserves_generation_parameters(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            shutil.copyfile(SOURCE / "app.py", root / "app.py")
            settings = {"model": "acestep-v15-base", "model_repo": "base", "model_revision": "base-rev",
                        "main_repo": "main", "main_revision": "main-rev", "ace_git_revision": "code-rev"}
            (root / "model-config.json").write_text(json.dumps(settings))
            base = Mock()
            base.initialize_service.return_value = ("ready", True)
            base.generate_instruction.return_value = "instruction"
            widgets = lambda *args, **kwargs: MagicMock()
            gradio = SimpleNamespace(Blocks=widgets, Markdown=Mock(), Audio=widgets,
                                     Textbox=widgets, Button=widgets, JSON=widgets, Progress=widgets, Error=ValueError)
            download = Mock()
            gpu = Mock(side_effect=lambda **kwargs: lambda function: function)
            generate = Mock(return_value=SimpleNamespace(
                success=True, audios=[{"path": str(root / "music.wav"), "params": {"seed": 42}}],
                status_message="done"))
            components = ["unused-lm"]
            modules = {
                "spaces": SimpleNamespace(GPU=gpu), "gradio": gradio,
                "huggingface_hub": SimpleNamespace(snapshot_download=download),
                "torch": SimpleNamespace(__version__="test", cuda=SimpleNamespace(get_device_name=lambda _: "Test GPU")),
                "acestep.handler": SimpleNamespace(AceStepHandler=lambda: base),
                "acestep.inference": SimpleNamespace(GenerationParams=SimpleNamespace, GenerationConfig=SimpleNamespace,
                                                      generate_music=generate),
                "acestep.llm_inference": SimpleNamespace(LLMHandler=Mock()),
                "acestep.model_downloader": SimpleNamespace(MAIN_MODEL_COMPONENTS=components),
            }
            with patch.dict(sys.modules, modules):
                app = runpy.run_path(str(root / "app.py"))
            self.assertEqual(components, ["vae", "Qwen3-Embedding-0.6B"])
            self.assertEqual(download.call_args_list[0].kwargs["revision"], "main-rev")
            self.assertEqual(download.call_args_list[1].kwargs["revision"], "base-rev")
            self.assertEqual(base.initialize_service.call_args.kwargs["device"], "cuda")
            self.assertFalse(base.initialize_service.call_args.kwargs["compile_model"])
            self.assertIs(app["HANDLERS"]["acestep-v15-base"].vae, app["HANDLERS"]["acestep-v15-turbo"].vae)
            self.assertIs(app["HANDLERS"]["acestep-v15-base"].text_encoder, app["HANDLERS"]["acestep-v15-turbo"].text_encoder)
            health = app["health"]()
            self.assertEqual(health["model_revisions"]["acestep-v15-turbo"], "main-rev")
            job = {"model": "acestep-v15-base", "task": "cover", "track": None, "caption": "ambient",
                   "duration": 10, "seed": 42, "inference_steps": 50, "guidance_scale": 7,
                   "audio_cover_strength": .6}
            _, metadata = app["generate"]("guide.wav", json.dumps(job))
            params = generate.call_args.args[2]
            self.assertEqual(params.src_audio, "guide.wav")
            self.assertEqual(params.seed, 42)
            self.assertEqual(params.inference_steps, 50)
            self.assertFalse(params.thinking)
            self.assertEqual(metadata["model_revision"], "base-rev")
            generate.reset_mock()
            with self.assertRaisesRegex(ValueError, "needs an audio guide"):
                app["generate"](None, json.dumps(job))
            generate.assert_not_called()
            job["model"] = "acestep-v15-turbo"
            _, metadata = app["generate"]("guide.wav", json.dumps(job))
            self.assertEqual(metadata["model_revision"], "main-rev")


if __name__ == "__main__":
    unittest.main()
