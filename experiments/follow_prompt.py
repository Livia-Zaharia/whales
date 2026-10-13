"""Instrument-aware default captions shared by composition and batch preparation."""

from .follow_models import model_registry


def ensemble_caption(species, config: dict) -> str:
    registry = model_registry()
    names = registry["instrument_prompt_names"]
    instruments = sorted({names.get(config["species"][name]["instrument"]["name"],
                                    config["species"][name]["instrument"]["name"])
                          for name in species})
    return registry["caption_template"].replace("{instruments}", ", ".join(instruments))[:400]
