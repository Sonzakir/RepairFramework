import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path


class RunWriter:
    """Creates and writes the three standard artifacts for one evaluation run directory."""

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @classmethod
    def create(cls, runs_dir: Path) -> "RunWriter":
        """Create the next run_NNN directory under runs_dir and return a writer for it."""
        runs_dir.mkdir(parents=True, exist_ok=True)
        next_id = 1
        for child in runs_dir.iterdir():
            if not child.is_dir() or not child.name.startswith("run_"):
                continue
            suffix = child.name.removeprefix("run_")
            if suffix.isdigit():
                next_id = max(next_id, int(suffix) + 1)
        run_dir = runs_dir / f"run_{next_id:03d}"
        run_dir.mkdir()
        return cls(run_dir)

    def write_json(self, filename: str, data: dict) -> None:
        (self._run_dir / filename).write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

    def log(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with (self._run_dir / "execution.log").open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] {message}\n")


def serialize_localization_result(result) -> dict:
    """Convert a LocalizationResult to a JSON-safe dict for results.json."""
    return {
        "backend": result.backend,
        "bug": dataclasses.asdict(result.bug),
        "ranked_locations": [
            dataclasses.asdict(loc) for loc in result.ranked_locations
        ],
        "metadata": _serialize_metadata(result.metadata),
    }


def _serialize_metadata(metadata: dict) -> dict:
    """Recursively convert a localization metadata dict to a JSON-safe dict.

    Metadata may nest sub-result metadata (e.g. the hybrid backend embeds each
    backend's ``all_metrics`` under ``sbfl_metadata``/``mbfl_metadata``), and
    those inner tables still hold ``RankedLocation`` dataclass instances.  A
    plain top-level pass would leave them un-serialized, so this walks the whole
    structure and turns any dataclass instance into a dict.
    """
    return {key: _to_json_safe(value) for key, value in metadata.items()}


def _to_json_safe(value):
    """Recursively convert dataclasses / nested containers into JSON-safe values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {key: _to_json_safe(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(inner) for inner in value]
    return value
