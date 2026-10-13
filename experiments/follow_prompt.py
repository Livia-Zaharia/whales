"""Configured model-specific captions shared by composition and batch preparation."""

from .follow_models import model_registry, music_model


def ensemble_caption(species, config: dict, registry: dict | None = None) -> str:
    registry = registry or model_registry()
    names = registry["instrument_prompt_names"]
    instruments = sorted({names.get(config["species"][name]["instrument"]["name"],
                                    config["species"][name]["instrument"]["name"])
                          for name in species})
    return registry["caption_template"].replace("{instruments}", ", ".join(instruments))[:400]


def default_caption(species, config: dict, model_variant: str | None = None) -> str:
    registry = model_registry()
    model_variant = model_variant or registry["default_model"]
    model = music_model(model_variant, registry)
    fallback = "legacy" if model["engine"] == "ace-step" else "instruments"
    profile = registry.get("caption_profiles", {}).get(model_variant, fallback)
    species = set(species)
    if profile == "legacy":
        return (config["species"][next(iter(species))]["caption"] if len(species) == 1
                else config["combination"]["caption"])[:400]
    if profile != "instruments":
        raise ValueError(f"Unknown caption profile for {model_variant}: {profile}")
    return ensemble_caption(species, config, registry)
