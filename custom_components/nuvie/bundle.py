"""Writing the plan Nuvie sends into the config folder, as pure Python (no Home Assistant imports).

The plan arrives as the same zip as Export → Home Assistant (minus the integration
and the model): nuvie/nuvie_home.json, nuvie/dashboard/*.yaml, nuvie/README.md and
www/nuvie/*.svg. Only those paths are written, each one atomically, so a bad or
hostile zip can't write anywhere else, and the card never reads a half-written file.
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

# what the plan zip may contain: (folder, allowed suffixes)
ALLOWED = {
    ("nuvie",): {".json", ".md"},
    ("nuvie", "dashboard"): {".yaml"},
    ("www", "nuvie"): {".svg"},
}
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


def write_bundle(config_dir: Path, files: dict[str, bytes]) -> list[str]:
    """Write the plan's files, the manifest last; remove plan images and cards the new plan no longer has.

    Returns the paths written.
    """
    names = sorted(files, key=lambda n: n == "nuvie/nuvie_home.json")
    for name in names:
        write_atomic(config_dir / name, files[name])
    for folder, suffix in ((("nuvie", "dashboard"), ".yaml"), (("www", "nuvie"), ".svg")):
        d = config_dir.joinpath(*folder)
        if not d.is_dir():
            continue
        for old in d.iterdir():
            rel = "/".join((*folder, old.name))
            if old.is_file() and old.suffix == suffix and rel not in files:
                old.unlink()
    return names
