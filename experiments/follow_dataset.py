"""Portable orchestra inputs: Parquet measurements and lossless FLAC archives.

Building and publishing are explicit research operations. Server setup only
downloads a pinned prepared bundle; it never fetches the upstream corpora.
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
from typing import Annotated

import polars as pl
import typer

from .data import DEFAULT_DATA, ROOT, Layout, digest, read_json, write_json

PIN = ROOT / "resources" / "follow-dataset.json"
FORMAT_VERSION = 1
PREPARATION_VERSION = 1  # bump when phrase measurement/preparation changes
JSON_FIELDS = ("provenance", "units", "counts", "instrument", "stems")
app = typer.Typer(help=__doc__, no_args_is_help=True)


def preparation_hash(config: dict) -> str:
    """Composition settings and seeds do not invalidate measured recordings."""
    keys = ("sample_rate", "sources", "species", "perturbation", "response")
    payload = {key: config[key] for key in keys}
    payload["species"] = {name: {key: value for key, value in species.items()
                                  if key not in ("caption", "lego_track", "spectrogram_hz")}
                          for name, species in config["species"].items()}
    encoded = json.dumps({"version": PREPARATION_VERSION, "config": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def safe_path(name: str) -> Path:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Invalid dataset path: {name!r}")
    return Path(*path.parts)


def prepared_current(layout: Layout, config: dict) -> bool:
    try:
        manifest = read_json(layout.interim / "follow" / "prepared.json")
        matches = manifest.get("preparation_sha256") == preparation_hash(config)
        legacy = "preparation_sha256" not in manifest and manifest.get("config_sha256") == config["config_sha256"]
        if not matches and not legacy:
            return False
        sources = {source["id"]: source for source in manifest["sources"]}
        return all(source["id"] in sources and all(
            (layout.output / "follow" / safe_path(stem)).is_file()
            for stem in sources[source["id"]]["stems"].values()
        ) for source in config["sources"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def portable(value, layout: Layout):
    if isinstance(value, dict):
        return {key: portable(item, layout) for key, item in value.items()}
    if isinstance(value, list):
        return [portable(item, layout) for item in value]
    if isinstance(value, str):
        for root, prefix in ((layout.root.resolve(), "data"), (ROOT, ".")):
            if value.startswith(str(root) + "/"):
                return str(Path(prefix) / Path(value).relative_to(root))
    return value


def parquet_sources(sources: list[dict], folder: Path):
    rows, events = [], []
    for source in sources:
        row = {key: value for key, value in source.items() if key not in ("events", "perturbed_events")}
        for key in JSON_FIELDS:
            row[key] = json.dumps(row[key], separators=(",", ":"), ensure_ascii=False)
        rows.append(row)
        for variant, field in (("measured", "events"), ("perturbed", "perturbed_events")):
            for index, event in enumerate(source[field]):
                entry = {"source_id": source["id"], "variant": variant, "index": index, "keys": list(event)}
                entry.update(event)
                entry["at_s"] = event["time_s"] if event["kind"] == "click" else None
                entry["time_s"] = event["time_s"] if event["kind"] == "tonal" else None
                events.append(entry)
    schema = {"source_id": pl.String, "variant": pl.String, "index": pl.UInt32, "keys": pl.List(pl.String),
              "kind": pl.String, "at_s": pl.Float64, "start_s": pl.Float64, "end_s": pl.Float64,
              "time_s": pl.List(pl.Float64), "hz": pl.List(pl.Float64), "group": pl.Int64,
              "label": pl.String, "origin": pl.String, "contour": pl.String, "cut": pl.String}
    unknown = {key for event in events for key in event} - schema.keys()
    if unknown:
        raise ValueError(f"Unknown measured event fields: {sorted(unknown)}")
    pl.DataFrame(rows, infer_schema_length=None).write_parquet(folder / "sources.parquet", compression="zstd")
    pl.DataFrame(events, schema=schema).write_parquet(folder / "events.parquet", compression="zstd")


def read_prepared(folder: Path) -> dict:
    metadata = read_json(folder / "dataset.json")
    sources = pl.read_parquet(folder / "sources.parquet").to_dicts()
    by_id = {source["id"]: source for source in sources}
    for source in sources:
        for key in JSON_FIELDS:
            source[key] = json.loads(source[key])
        source["events"], source["perturbed_events"] = [], []
    for row in pl.read_parquet(folder / "events.parquet").sort("source_id", "variant", "index").to_dicts():
        event = {key: row[key] for key in row["keys"]}
        if row["kind"] == "click":
            event["time_s"] = row["at_s"]
        field = "events" if row["variant"] == "measured" else "perturbed_events"
        by_id[row["source_id"]][field].append(event)
    return metadata | {"sources": sources}


def flac(wav: Path, destination: Path):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav),
                    "-c:a", "flac", "-compression_level", "8", "-threads", "1", str(destination)], check=True)


def build_bundle(layout: Layout, config: dict, folder: Path, prepared_config: Path | None = None) -> Path:
    manifest = read_json(layout.interim / "follow" / "prepared.json")
    if not prepared_current(layout, config):
        # A legacy manifest may hash older composition settings. Verify its
        # actual preparation config rather than silently relabeling measurements.
        old = read_json(prepared_config) if prepared_config else None
        if not old or digest(prepared_config) != manifest["config_sha256"] or preparation_hash(old) != preparation_hash(config):
            raise ValueError("Prepared recordings are stale; run 'main.py follow prepare' or supply their --prepared-config.")
    wanted = {source["id"] for source in config["sources"]}
    sources = portable([source for source in manifest["sources"] if source["id"] in wanted], layout)
    if {source["id"] for source in sources} != wanted:
        raise ValueError("Some configured recordings have not been prepared")
    folder.mkdir(parents=True, exist_ok=True)
    output = layout.output / "follow"
    with tempfile.TemporaryDirectory(prefix="orchestra-flac-") as scratch, \
            tarfile.open(folder / "recordings.tar", "w") as recordings, \
            tarfile.open(folder / "stems.tar", "w") as stems:
        for source in sources:
            for name, path in list(source["stems"].items()):
                relative = safe_path(path).with_suffix(".flac")
                target = Path(scratch) / "clip.flac"
                flac(output / safe_path(path), target)
                if name == "animal":
                    recordings.add(target, arcname=f"{source['id']}.flac")
                    credit = Path(scratch) / "credit.json"
                    # Flat scalar columns give the Hub audio viewer one stable
                    # schema across providers with different provenance records.
                    write_json(credit, {key: source[key] for key in ("id", "species", "title", "note", "playback_note")} | {
                        "provenance_json": json.dumps(source["provenance"], ensure_ascii=False),
                    })
                    recordings.add(credit, arcname=f"{source['id']}.json")
                else:
                    stems.add(target, arcname=relative.as_posix())
                source["stems"][name] = relative.as_posix()
    parquet_sources(sources, folder)
    write_json(folder / "dataset.json", {key: value for key, value in manifest.items() if key != "sources"} | {
        "config_sha256": config["config_sha256"], "preparation_sha256": preparation_hash(config),
    })
    catalog = read_json(output / "catalog.json")
    if catalog["config_sha256"] != config["config_sha256"]:
        raise ValueError("Catalog is stale; run 'main.py follow catalog' before packaging")
    playback = {catalog["countries"]}
    for source in catalog["sources"]:
        for files in source["files"].values():
            playback.update(files[key] for key in ("audio", "image"))
    with tarfile.open(folder / "playback.tar", "w") as archive:
        for name in sorted(playback):
            archive.add(output / safe_path(name), arcname=name)
    write_json(folder / "catalog.json", catalog)
    files = ("dataset.json", "sources.parquet", "events.parquet", "recordings.tar", "stems.tar", "playback.tar", "catalog.json")
    bundle = {"format_version": FORMAT_VERSION, "preparation_sha256": preparation_hash(config),
              "source_count": len(sources), "files": {name: {"bytes": (folder / name).stat().st_size, "sha256": digest(folder / name)} for name in files}}
    write_json(folder / "bundle.json", bundle)
    total = sum(item["bytes"] for item in bundle["files"].values())
    (folder / "README.md").write_text(
        "---\nconfigs:\n- config_name: recordings\n  default: true\n  data_files: recordings.tar\n"
        "- config_name: sources\n  data_files: sources.parquet\n- config_name: events\n  data_files: events.parquet\n"
        "tags:\n- audio\n- bioacoustics\n- whales\n- dolphins\n---\n\n# Whale and Dolphin Orchestra: prepared phrases\n\n"
        f"{len(sources)} extracted recording clips with measured phrases, lossless FLAC audio, and app playback assets. "
        f"The complete runtime bundle is {total / 2**20:.1f} MiB.\n\n"
        "Input: recording excerpts, resampled to 48 kHz with the playback gain stated in each record. "
        "`recordings.tar` is a WebDataset archive of FLAC clips and source credits. Original providers, "
        "recording identifiers, excerpt offsets and source hashes are retained in the provenance.\n\n"
        "Schema: `sources.parquet` contains source metadata and onsets; `events.parquet` contains measured "
        "and perturbed phrase timing and pitch contours as Float64 arrays. Nested source provenance and "
        "annotation records use JSON columns. These measurements do not decode animal meaning.\n\n"
        "Output: `stems.tar` contains FLAC guides, perturbed guides and deterministic responses. "
        "`playback.tar` contains MP3 previews, spectrograms and map geometry. Models, source corpora, "
        "daily recording archives and generated ACE-Step combinations are excluded.\n\n"
        "The app downloads the pinned files in `bundle.json`, verifies their hashes, and restores "
        "its prepared manifest without repeating extraction or measurement.\n\n"
        "Project: https://github.com/Livia-Zaharia/whales\n", encoding="utf-8")
    return folder / "bundle.json"


def unpack(archive: Path, output: Path, recordings: bool = False):
    with tarfile.open(archive, "r:") as stream:
        for member in stream:
            relative = safe_path(member.name)
            if not member.isfile():
                raise ValueError("Dataset archives must contain regular files only")
            if recordings:
                if relative.suffix == ".json":
                    continue  # credits are already in the Parquet metadata
                if len(relative.parts) != 1 or relative.suffix != ".flac":
                    raise ValueError("Invalid recording in dataset")
                relative = Path(relative.stem) / "animal.flac"
            if relative.suffix not in (".flac", ".mp3", ".webp", ".geojson"):
                raise ValueError(f"Unexpected runtime file: {relative}")
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)


def install_bundle(folder: Path, layout: Layout, config: dict):
    bundle = read_json(folder / "bundle.json")
    if bundle["format_version"] != FORMAT_VERSION or bundle["preparation_sha256"] != preparation_hash(config):
        raise ValueError("Prepared dataset does not match the current measurement settings")
    for name, item in bundle["files"].items():
        path = folder / safe_path(name)
        if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise ValueError(f"Dataset checksum failed: {name}")
    prepared = read_prepared(folder)
    with tempfile.TemporaryDirectory(prefix="orchestra-install-", dir=folder) as scratch:
        staged = Path(scratch)
        unpack(folder / "recordings.tar", staged, recordings=True)
        unpack(folder / "stems.tar", staged)
        unpack(folder / "playback.tar", staged)
        for source in prepared["sources"]:
            for stem in source["stems"].values():
                if not (staged / safe_path(stem)).is_file():
                    raise ValueError(f"Missing prepared stem: {stem}")
        shutil.copyfile(folder / "catalog.json", staged / "catalog.json")
        output = layout.output / "follow"
        for path in staged.rglob("*"):
            if path.is_file():
                target = output / path.relative_to(staged)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    # Publish the readiness marker only after all audio and playback files exist.
    write_json(layout.interim / "follow" / "prepared.json", prepared)


def download_bundle(layout: Layout, config: dict, pin: Path = PIN):
    from huggingface_hub import snapshot_download

    release = read_json(pin)
    if release["preparation_sha256"] != preparation_hash(config):
        raise ValueError("Publish a prepared dataset for the changed measurement settings; startup will not fetch raw corpora")
    layout.interim.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="orchestra-download-", dir=layout.interim) as scratch:
        folder = Path(scratch)
        snapshot_download(release["repo_id"], repo_type="dataset", revision=release["revision"], local_dir=folder,
                          allow_patterns=["bundle.json", "dataset.json", "sources.parquet", "events.parquet", "recordings.tar", "stems.tar", "playback.tar", "catalog.json"], max_workers=2)
        if digest(folder / "bundle.json") != release["bundle_sha256"]:
            raise ValueError("Dataset manifest checksum failed")
        install_bundle(folder, layout, config)


@app.command("build")
def build_command(
    data_dir: Annotated[Path, typer.Option(help="Local data root with extracted recordings.")] = DEFAULT_DATA,
    output_dir: Annotated[Path, typer.Option(help="Destination for the portable dataset.")] = DEFAULT_DATA / "output" / "orchestra-dataset",
    prepared_config: Annotated[Path | None, typer.Option(exists=True, dir_okay=False, help="Original config of a legacy prepared manifest.")] = None,
):
    """Package existing extracted audio and measurements; no upstream downloads."""
    from .follow import load_config

    typer.echo(build_bundle(Layout(data_dir), load_config(), output_dir, prepared_config))


@app.command("publish")
def publish_command(
    repo_id: Annotated[str, typer.Argument(help="Hugging Face dataset namespace/name.")],
    folder: Annotated[Path, typer.Option(exists=True, file_okay=False)] = DEFAULT_DATA / "output" / "orchestra-dataset",
):
    """Upload a built dataset and pin its immutable revision for server setup."""
    from huggingface_hub import HfApi

    bundle = read_json(folder / "bundle.json")
    for name, item in bundle["files"].items():
        if digest(folder / safe_path(name)) != item["sha256"]:
            raise ValueError(f"Dataset checksum failed before upload: {name}")
    api = HfApi()
    api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
    commit = api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=folder,
                               allow_patterns=["README.md", "bundle.json", "dataset.json", "*.parquet", "*.tar", "catalog.json"],
                               commit_message="Publish compact prepared orchestra phrases")
    write_json(PIN, {"repo_id": repo_id, "revision": commit.oid, "bundle_sha256": digest(folder / "bundle.json"),
                     "preparation_sha256": bundle["preparation_sha256"], "bytes": sum(file["bytes"] for file in bundle["files"].values())})
    typer.echo(f"https://huggingface.co/datasets/{repo_id}/tree/{commit.oid}")


@app.command("download")
def download_command(data_dir: Annotated[Path, typer.Option(help="Local data root to populate.")] = DEFAULT_DATA):
    """Install the pinned prepared inputs without downloading source corpora."""
    from .follow import load_config

    download_bundle(Layout(data_dir), load_config())
    typer.echo(data_dir / "interim" / "follow" / "prepared.json")
