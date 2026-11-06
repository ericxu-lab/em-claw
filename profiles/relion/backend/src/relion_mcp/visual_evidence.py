from __future__ import annotations

import copy
import hashlib
import math
import os
import secrets
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .settings import Settings


class VisualEvidenceError(ValueError):
    """Raised when rendered evidence cannot be cached or published safely."""


EVIDENCE_DIRECTORY = PurePosixPath(".relion-mcp/initial-model-evidence")
EVIDENCE_REPORT_TYPES: dict[str, frozenset[str]] = {
    "map_multiview": frozenset({"relion_map_report", "relion_3d_classes"}),
    "projection_comparison": frozenset({"relion_map_projection_comparison"}),
    "late_iteration_comparison": frozenset(
        {"relion_map_comparison", "relion_3d_classes"}
    ),
}
_ELIGIBLE_REPORT_TYPES = frozenset().union(*EVIDENCE_REPORT_TYPES.values())
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SHA256_HEX_LENGTH = 64


@dataclass(frozen=True)
class _CachedEvidence:
    png: bytes
    metadata: dict[str, Any]
    sha256: str
    report_type: str
    project: Path
    project_device: int
    project_inode: int
    registered_at: float


def _positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive and finite.")
    return number


def _positive_integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _canonical_project_from_metadata(metadata: Mapping[str, Any]) -> Path:
    value = metadata.get("project_dir")
    if not isinstance(value, str) or not value:
        raise VisualEvidenceError(
            "Eligible visual evidence metadata must contain a nonempty project_dir."
        )
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise VisualEvidenceError(
            "Visual evidence project_dir must be an absolute path."
        )
    try:
        project = raw.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise VisualEvidenceError(
            f"Visual evidence project_dir is unavailable: {raw}"
        ) from exc
    if not project.is_dir():
        raise VisualEvidenceError(
            f"Visual evidence project_dir is not a directory: {project}"
        )
    return project


def _validate_image_metadata(
    metadata: Mapping[str, Any], *, sha256: str, size_bytes: int
) -> None:
    image = metadata.get("image")
    if image is None:
        return
    if not isinstance(image, Mapping):
        raise VisualEvidenceError("Visual evidence image metadata must be an object.")
    declared_hash = image.get("sha256")
    if declared_hash is not None and declared_hash != sha256:
        raise VisualEvidenceError(
            "Visual evidence PNG does not match metadata image.sha256."
        )
    declared_size = image.get("png_bytes")
    if declared_size is not None and declared_size != size_bytes:
        raise VisualEvidenceError(
            "Visual evidence PNG does not match metadata image.png_bytes."
        )


class VisualEvidenceCache:
    """A process-local, bounded cache for reviewed visual report bytes.

    The PNG digest is the lookup token.  Report provenance stays in memory and
    is revalidated when bytes are materialized, so a token from one project or
    report type cannot be repurposed for another acceptance channel.
    """

    def __init__(
        self,
        *,
        max_entries: int = 32,
        max_total_bytes: int = 96 * 1024 * 1024,
        ttl_seconds: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = _positive_integer(max_entries, name="max_entries")
        self._max_total_bytes = _positive_integer(
            max_total_bytes, name="max_total_bytes"
        )
        self._ttl_seconds = _positive_number(ttl_seconds, name="ttl_seconds")
        if not callable(clock):
            raise TypeError("clock must be callable.")
        self._clock = clock
        self._entries: OrderedDict[str, _CachedEvidence] = OrderedDict()
        self._total_bytes = 0
        self._lock = threading.RLock()

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise VisualEvidenceError(
                "Visual evidence cache clock returned an invalid value."
            )
        return float(value)

    def _purge_expired(self, now: float) -> None:
        expired = [
            digest
            for digest, entry in self._entries.items()
            if now - entry.registered_at >= self._ttl_seconds
        ]
        for digest in expired:
            entry = self._entries.pop(digest)
            self._total_bytes -= len(entry.png)

    def _evict_to_fit(self, incoming_size: int) -> None:
        while self._entries and (
            len(self._entries) >= self._max_entries
            or self._total_bytes + incoming_size > self._max_total_bytes
        ):
            _, entry = self._entries.popitem(last=False)
            self._total_bytes -= len(entry.png)

    def register(self, png: bytes, metadata: Mapping[str, Any]) -> str:
        """Cache one eligible project-bound visual and return its PNG SHA-256."""
        if not isinstance(png, bytes):
            raise VisualEvidenceError("Visual evidence PNG must be bytes.")
        if not png.startswith(_PNG_SIGNATURE):
            raise VisualEvidenceError("Visual evidence must contain PNG bytes.")
        if len(png) > self._max_total_bytes:
            raise VisualEvidenceError(
                "Visual evidence PNG exceeds the cache byte bound."
            )
        if not isinstance(metadata, Mapping):
            raise VisualEvidenceError("Visual evidence metadata must be an object.")
        report_type = metadata.get("report_type")
        if (
            not isinstance(report_type, str)
            or report_type not in _ELIGIBLE_REPORT_TYPES
        ):
            raise VisualEvidenceError(
                "Visual evidence report_type is not eligible for InitialModel evidence."
            )
        project = _canonical_project_from_metadata(metadata)
        try:
            project_stat = project.stat()
        except OSError as exc:
            raise VisualEvidenceError(
                "Visual evidence project_dir identity could not be read."
            ) from exc
        digest = hashlib.sha256(png).hexdigest()
        _validate_image_metadata(metadata, sha256=digest, size_bytes=len(png))
        try:
            copied_metadata = copy.deepcopy(dict(metadata))
        except Exception as exc:  # pragma: no cover - defensive for exotic mappings
            raise VisualEvidenceError(
                "Visual evidence metadata could not be copied."
            ) from exc

        now = self._now()
        with self._lock:
            self._purge_expired(now)
            existing = self._entries.get(digest)
            if existing is not None and (
                existing.project != project or existing.report_type != report_type
            ):
                raise VisualEvidenceError(
                    "The PNG digest is already bound to different visual evidence provenance."
                )
            if existing is not None:
                self._total_bytes -= len(existing.png)
                del self._entries[digest]
            self._evict_to_fit(len(png))
            self._entries[digest] = _CachedEvidence(
                png=png,
                metadata=copied_metadata,
                sha256=digest,
                report_type=report_type,
                project=project,
                project_device=project_stat.st_dev,
                project_inode=project_stat.st_ino,
                registered_at=now,
            )
            self._total_bytes += len(png)
        return digest

    def _lookup(self, digest: str) -> _CachedEvidence:
        if not isinstance(digest, str) or len(digest) != _SHA256_HEX_LENGTH:
            raise VisualEvidenceError(
                "image_sha256 must be a lowercase SHA-256 digest."
            )
        try:
            parsed = bytes.fromhex(digest)
        except ValueError as exc:
            raise VisualEvidenceError(
                "image_sha256 must be a lowercase SHA-256 digest."
            ) from exc
        if len(parsed) != 32 or digest != digest.lower():
            raise VisualEvidenceError(
                "image_sha256 must be a lowercase SHA-256 digest."
            )
        now = self._now()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(digest)
            if entry is None:
                raise VisualEvidenceError(
                    "Visual evidence cache miss or expiry; render the report again before publishing."
                )
            return entry

    def publish(
        self,
        settings: Settings,
        project_dir: str,
        evidence_kind: str,
        image_sha256: str,
        *,
        confirm: bool,
    ) -> dict[str, Any]:
        """Publish cached visual bytes through an idempotent, no-replace write."""
        settings.assert_execution_enabled(confirm)
        project = settings.validate_project_path(project_dir)
        if evidence_kind not in EVIDENCE_REPORT_TYPES:
            raise VisualEvidenceError(
                f"evidence_kind must be one of {sorted(EVIDENCE_REPORT_TYPES)}."
            )
        entry = self._lookup(image_sha256)
        try:
            project_stat = project.stat()
        except OSError as exc:
            raise VisualEvidenceError(
                "The canonical project identity could not be verified."
            ) from exc
        if entry.project != project or (
            entry.project_device,
            entry.project_inode,
        ) != (project_stat.st_dev, project_stat.st_ino):
            raise VisualEvidenceError(
                "Cached visual evidence belongs to a different canonical project."
            )
        if entry.report_type not in EVIDENCE_REPORT_TYPES[evidence_kind]:
            raise VisualEvidenceError(
                f"Report type {entry.report_type!r} is not compatible with "
                f"evidence_kind {evidence_kind!r}."
            )
        if hashlib.sha256(entry.png).hexdigest() != entry.sha256:
            raise VisualEvidenceError(
                "Cached visual evidence failed its integrity check."
            )

        filename = f"{evidence_kind}-{entry.sha256[:24]}.png"
        _publish_no_replace(project, filename, entry)
        relative_path = (EVIDENCE_DIRECTORY / filename).as_posix()
        return {
            "path": relative_path,
            "sha256": entry.sha256,
            "size_bytes": len(entry.png),
            "report_type": entry.report_type,
        }

    def clear(self) -> None:
        """Drop process-local entries (primarily useful for process teardown/tests)."""
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired(self._now())
            return len(self._entries)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_evidence_directory(
    project: Path, *, project_device: int, project_inode: int
) -> int:
    """Open/create the evidence directory without following either component."""
    flags = _directory_flags()
    current_fd = os.open(project, flags)
    try:
        project_stat = os.fstat(current_fd)
        if (project_stat.st_dev, project_stat.st_ino) != (
            project_device,
            project_inode,
        ):
            raise VisualEvidenceError(
                "The canonical project changed before visual evidence publication."
            )
        for component in EVIDENCE_DIRECTORY.parts:
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _hash_open_file(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest(), size
        digest.update(chunk)
        size += len(chunk)


def _validate_existing(
    directory_fd: int, filename: str, entry: _CachedEvidence
) -> None:
    try:
        file_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise VisualEvidenceError(
            "Existing visual evidence could not be inspected safely."
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise VisualEvidenceError(
            "The visual evidence destination already exists and is not a regular file."
        )
    if stat.S_IMODE(file_stat.st_mode) & 0o222:
        raise VisualEvidenceError(
            "Existing visual evidence is not immutable (write bits are set)."
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise VisualEvidenceError(
            "Existing visual evidence could not be opened safely."
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
        if (file_stat.st_dev, file_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise VisualEvidenceError(
                "The visual evidence destination changed during validation."
            )
        digest, size = _hash_open_file(descriptor)
    finally:
        os.close(descriptor)
    if digest != entry.sha256 or size != len(entry.png):
        raise VisualEvidenceError(
            "Conflicting visual evidence already exists at the content-addressed path."
        )


def _publish_no_replace(project: Path, filename: str, entry: _CachedEvidence) -> None:
    directory_fd: int | None = None
    temporary_name = f".evidence-{secrets.token_hex(12)}.tmp"
    temporary_created = False
    try:
        directory_fd = _open_evidence_directory(
            project,
            project_device=entry.project_device,
            project_inode=entry.project_inode,
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        temporary_created = True
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(entry.png)
                handle.flush()
                os.fsync(handle.fileno())
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        except FileExistsError:
            _validate_existing(directory_fd, filename, entry)
    except VisualEvidenceError:
        raise
    except OSError as exc:
        raise VisualEvidenceError(
            f"Could not publish visual evidence without replacement: {exc}"
        ) from exc
    finally:
        if directory_fd is not None:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            os.close(directory_fd)


VISUAL_EVIDENCE_CACHE = VisualEvidenceCache()


def register_visual_evidence(png: bytes, metadata: Mapping[str, Any]) -> str:
    """Register an eligible rendered visual in the process-local cache."""
    return VISUAL_EVIDENCE_CACHE.register(png, metadata)


def register_visual_evidence_if_eligible(
    png: bytes, metadata: Mapping[str, Any]
) -> str | None:
    """Register InitialModel-compatible reports and ignore unrelated visuals."""
    if metadata.get("report_type") not in _ELIGIBLE_REPORT_TYPES:
        return None
    return register_visual_evidence(png, metadata)


def publish_visual_evidence(
    settings: Settings,
    project_dir: str,
    evidence_kind: str,
    image_sha256: str,
    *,
    confirm: bool,
) -> dict[str, Any]:
    """Publish a registered visual as one InitialModel evidence artifact."""
    return VISUAL_EVIDENCE_CACHE.publish(
        settings,
        project_dir,
        evidence_kind,
        image_sha256,
        confirm=confirm,
    )
