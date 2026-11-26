"""Writing the plan Nuvie sends into the config folder, as pure Python (no Home Assistant imports).

The plan arrives as the same zip as Export → Home Assistant (minus the integration
and the model): nuvie/nuvie_home.json, nuvie/dashboard/*.yaml, nuvie/README.md and
www/nuvie/*.svg. Only those paths are written, each one atomically, so a bad or
hostile zip can't write anywhere else, and the card never reads a half-written file.

The plan images are also kept in nuvie/plan/, where the integration serves them to
signed-in users (/api/nuvie/plan/<floor_id>.svg): Home Assistant only serves www/
(/local) when that folder existed when it started, so the card doesn't rely on it.
www/nuvie/ is still written, for the stock picture-elements cards.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

# what the plan zip may contain: (folder, allowed suffixes)
ALLOWED = {
    ("nuvie",): {".json", ".md"},
    ("nuvie", "dashboard"): {".yaml"},
    ("www", "nuvie"): {".svg"},
    ("nuvie", "plan"): {".svg"},
}
# where the integration keeps (and serves) the plan images, and where a manual export puts them
PLAN_FOLDER = ("nuvie", "plan")
WWW_FOLDER = ("www", "nuvie")
# a storey's id in the plan (haSlug + a number): no dots or slashes, so it can't leave the folder
FLOOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_FILE = 20 * 1024 * 1024
MAX_TOTAL = 60 * 1024 * 1024


class BundleError(ValueError):
    """The zip isn't a Nuvie plan."""


def _allowed(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or len(p.parts) < 2:
        return None
    suffixes = ALLOWED.get(tuple(p.parts[:-1]))
    return p if suffixes and p.suffix in suffixes and not p.name.startswith(".") else None


def read_bundle(data: bytes) -> dict[str, bytes]:
    """The zip's files (path → bytes). Paths outside the allowed folders are skipped."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise BundleError("not a zip") from err
    files: dict[str, bytes] = {}
    total = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            p = _allowed(info.filename)
            if p is None:
                continue
            if info.file_size > MAX_FILE:
                raise BundleError(f"{info.filename} is too big")
            total += info.file_size
            if total > MAX_TOTAL:
                raise BundleError("the plan is too big")
            files[str(p)] = zf.read(info)
    if "nuvie/nuvie_home.json" not in files:
        raise BundleError("no nuvie/nuvie_home.json in the plan")
    return files


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temporary file in the same folder, then rename over the old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def with_plan_copies(files: dict[str, bytes]) -> dict[str, bytes]:
    """The plan's files plus a copy of each www/nuvie/<floor>.svg in nuvie/plan/ (unless the zip has one)."""
    out = dict(files)
    for name, data in files.items():
        p = PurePosixPath(name)
        if tuple(p.parts[:-1]) == WWW_FOLDER and p.suffix == ".svg":
            out.setdefault("/".join((*PLAN_FOLDER, p.name)), data)
    return out


def write_bundle(config_dir: Path, files: dict[str, bytes]) -> list[str]:
    """Write the plan's files (with the plan images' copies), the manifest last; remove plan images and
    cards the new plan no longer has.

    Returns the paths written.
    """
    files = with_plan_copies(files)
    names = sorted(files, key=lambda n: n == "nuvie/nuvie_home.json")
    for name in names:
        write_atomic(config_dir / name, files[name])
    for folder, suffix in ((("nuvie", "dashboard"), ".yaml"), (WWW_FOLDER, ".svg"), (PLAN_FOLDER, ".svg")):
        d = config_dir.joinpath(*folder)
        if not d.is_dir():
            continue
        for old in d.iterdir():
            rel = "/".join((*folder, old.name))
            if old.is_file() and old.suffix == suffix and rel not in files:
                old.unlink()
    return names


def plan_image_paths(config_dir: Path, floor_id: str) -> list[Path]:
    """Where a storey's plan image may be, best first: nuvie/plan/ (the integration's copy), then
    www/nuvie/ (an export unzipped by hand). Empty when floor_id isn't a plan id (like "../x")."""
    if not isinstance(floor_id, str) or not FLOOR_ID_RE.fullmatch(floor_id):
        return []
    return [config_dir.joinpath(*folder, f"{floor_id}.svg") for folder in (PLAN_FOLDER, WWW_FOLDER)]
