"""Filesystem-backed artifacts living in `ai_forecast_exports_dir`.

Used by the admin UI to surface CSV inputs and PNG result charts produced by
the offline AI-forecast build pipeline (`scripts/build_ai_cfg.py` etc.).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Final, Literal

from fastapi import HTTPException, status

from app.core.config import get_settings

_EXTENSION_KIND: Final[dict[str, str]] = {
    ".csv": "csv",
    ".png": "png",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".json": "json",
    ".html": "html",
    ".txt": "text",
}

ArtifactKind = Literal["csv", "png", "image", "json", "html", "text", "other"]


class ArtifactInfo(dict[str, object]):
    pass


def _exports_root() -> Path:
    settings = get_settings()
    return Path(settings.ai_forecast_exports_dir).resolve()


def _classify(extension: str) -> ArtifactKind:
    return _EXTENSION_KIND.get(extension.lower(), "other")  # type: ignore[return-value]


def _to_info(path: Path, root: Path) -> ArtifactInfo:
    stat = path.stat()
    info: ArtifactInfo = ArtifactInfo()
    info["filename"] = path.name
    info["sizeBytes"] = stat.st_size
    info["modifiedAt"] = stat.st_mtime
    info["kind"] = _classify(path.suffix)
    info["relativePath"] = str(path.relative_to(root))
    return info


def list_artifacts(prefix: str | None = None) -> list[ArtifactInfo]:
    root = _exports_root()
    if not root.is_dir():
        return []

    entries: list[ArtifactInfo] = []
    needle = prefix.strip() if prefix else None
    for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        if needle and not path.name.startswith(needle):
            continue
        entries.append(_to_info(path, root))
    return entries


def resolve_artifact_path(filename: str) -> Path:
    """Return absolute path to an artifact, rejecting traversal attempts."""

    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid artifact filename",
        )

    root = _exports_root()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Artifact path is outside of exports directory",
        ) from exc

    if not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )

    return candidate


def guess_media_type(path: Path) -> str:
    media, _ = mimetypes.guess_type(path.name)
    return media or "application/octet-stream"
