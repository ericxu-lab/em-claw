from __future__ import annotations

import hashlib
import stat
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from relion_mcp.settings import ConfigurationError, Settings
from relion_mcp.visual_evidence import (
    VISUAL_EVIDENCE_CACHE,
    VisualEvidenceCache,
    VisualEvidenceError,
    register_visual_evidence_if_eligible,
)


def _settings(root: Path, *, mode: str = "run") -> Settings:
    return Settings(
        mode=mode,
        project_roots=(root.resolve(),),
        pipeliner_executable="relion_pipeliner",
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
    )


def _png(variant: int) -> bytes:
    output = BytesIO()
    Image.new(
        "RGB",
        (5, 4),
        color=((17 * variant) % 256, (37 * variant) % 256, (71 * variant) % 256),
    ).save(output, format="PNG")
    return output.getvalue()


def _metadata(project: Path, report_type: str, png: bytes) -> dict[str, object]:
    return {
        "report_type": report_type,
        "project_dir": str(project.resolve()),
        "image": {
            "mime_type": "image/png",
            "png_bytes": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
        },
    }


def test_conditional_registration_ignores_unrelated_reports_and_caches_eligible(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unrelated = _png(11)
    eligible = _png(12)
    VISUAL_EVIDENCE_CACHE.clear()
    try:
        assert (
            register_visual_evidence_if_eligible(
                unrelated, _metadata(project, "relion_particle_boxes", unrelated)
            )
            is None
        )
        assert len(VISUAL_EVIDENCE_CACHE) == 0
        assert (
            register_visual_evidence_if_eligible(
                eligible, _metadata(project, "relion_map_report", eligible)
            )
            == hashlib.sha256(eligible).hexdigest()
        )
        assert len(VISUAL_EVIDENCE_CACHE) == 1
    finally:
        VISUAL_EVIDENCE_CACHE.clear()


def test_publish_is_content_addressed_read_only_and_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    png = _png(1)
    cache = VisualEvidenceCache()
    digest = cache.register(png, _metadata(project, "relion_map_report", png))

    first = cache.publish(
        _settings(tmp_path),
        str(project),
        "map_multiview",
        digest,
        confirm=True,
    )

    expected_path = (
        project
        / ".relion-mcp"
        / "initial-model-evidence"
        / f"map_multiview-{digest[:24]}.png"
    )
    first_stat = expected_path.stat()
    assert first == {
        "path": (f".relion-mcp/initial-model-evidence/map_multiview-{digest[:24]}.png"),
        "sha256": digest,
        "size_bytes": len(png),
        "report_type": "relion_map_report",
    }
    assert expected_path.read_bytes() == png
    assert stat.S_IMODE(first_stat.st_mode) == 0o444
    assert list(expected_path.parent.glob(".evidence-*.tmp")) == []

    second = cache.publish(
        _settings(tmp_path),
        str(project),
        "map_multiview",
        digest,
        confirm=True,
    )

    assert second == first
    assert expected_path.stat().st_ino == first_stat.st_ino
    assert expected_path.read_bytes() == png
    assert list(expected_path.parent.glob(".evidence-*.tmp")) == []


@pytest.mark.parametrize(
    ("report_type", "evidence_kind"),
    [
        ("relion_map_report", "map_multiview"),
        ("relion_3d_classes", "map_multiview"),
        ("relion_map_projection_comparison", "projection_comparison"),
        ("relion_map_comparison", "late_iteration_comparison"),
        ("relion_3d_classes", "late_iteration_comparison"),
    ],
)
def test_report_type_allowlist_accepts_only_compatible_channel(
    tmp_path: Path,
    report_type: str,
    evidence_kind: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    png = _png(2)
    cache = VisualEvidenceCache()
    digest = cache.register(png, _metadata(project, report_type, png))

    result = cache.publish(
        _settings(tmp_path),
        str(project),
        evidence_kind,
        digest,
        confirm=True,
    )

    assert result["report_type"] == report_type


def test_register_rejects_unbound_ineligible_or_inconsistent_visuals(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    png = _png(3)
    cache = VisualEvidenceCache()

    with pytest.raises(VisualEvidenceError, match="report_type is not eligible"):
        cache.register(png, _metadata(project, "visual_smoke_test", png))
    with pytest.raises(VisualEvidenceError, match="project_dir"):
        cache.register(
            png,
            {"report_type": "relion_map_report"},
        )
    inconsistent = _metadata(project, "relion_map_report", png)
    assert isinstance(inconsistent["image"], dict)
    inconsistent["image"]["sha256"] = "0" * 64
    with pytest.raises(VisualEvidenceError, match="image.sha256"):
        cache.register(png, inconsistent)


def test_publish_checks_run_confirmation_before_cache_lookup(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    digest = "0" * 64
    cache = VisualEvidenceCache()

    with pytest.raises(ConfigurationError, match="Execution is disabled"):
        cache.publish(
            _settings(tmp_path, mode="plan"),
            str(project),
            "map_multiview",
            digest,
            confirm=True,
        )
    with pytest.raises(ConfigurationError, match="confirm=true"):
        cache.publish(
            _settings(tmp_path),
            str(project),
            "map_multiview",
            digest,
            confirm=False,
        )


def test_publish_fails_clearly_for_cache_miss_type_and_project_mismatch(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    png = _png(4)
    cache = VisualEvidenceCache()
    digest = cache.register(png, _metadata(project, "relion_map_report", png))
    settings = _settings(tmp_path)

    with pytest.raises(VisualEvidenceError, match="cache miss or expiry"):
        cache.publish(
            settings,
            str(project),
            "map_multiview",
            "f" * 64,
            confirm=True,
        )
    with pytest.raises(VisualEvidenceError, match="not compatible"):
        cache.publish(
            settings,
            str(project),
            "projection_comparison",
            digest,
            confirm=True,
        )
    with pytest.raises(VisualEvidenceError, match="different canonical project"):
        cache.publish(
            settings,
            str(other),
            "map_multiview",
            digest,
            confirm=True,
        )


def test_cached_visual_is_bound_to_canonical_project_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    png = _png(10)
    cache = VisualEvidenceCache()
    digest = cache.register(png, _metadata(project, "relion_map_report", png))
    retired_project = tmp_path / "retired-project"
    project.rename(retired_project)
    project.mkdir()

    with pytest.raises(VisualEvidenceError, match="different canonical project"):
        cache.publish(
            _settings(tmp_path),
            str(project),
            "map_multiview",
            digest,
            confirm=True,
        )

    assert not (project / ".relion-mcp").exists()


def test_cache_is_entry_bounded_and_entries_expire(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    now = [100.0]
    cache = VisualEvidenceCache(
        max_entries=2,
        max_total_bytes=1024 * 1024,
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    digests = [
        cache.register(
            png,
            _metadata(project, "relion_map_report", png),
        )
        for png in (_png(5), _png(6), _png(7))
    ]
    settings = _settings(tmp_path)

    assert len(cache) == 2
    with pytest.raises(VisualEvidenceError, match="cache miss or expiry"):
        cache.publish(
            settings,
            str(project),
            "map_multiview",
            digests[0],
            confirm=True,
        )
    now[0] = 110.0
    assert len(cache) == 0
    with pytest.raises(VisualEvidenceError, match="cache miss or expiry"):
        cache.publish(
            settings,
            str(project),
            "map_multiview",
            digests[-1],
            confirm=True,
        )


def test_conflicting_destination_is_never_replaced(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    png = _png(8)
    cache = VisualEvidenceCache()
    digest = cache.register(png, _metadata(project, "relion_map_report", png))
    destination = (
        project
        / ".relion-mcp"
        / "initial-model-evidence"
        / f"map_multiview-{digest[:24]}.png"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"conflict")
    destination.chmod(0o444)

    with pytest.raises(VisualEvidenceError, match="Conflicting visual evidence"):
        cache.publish(
            _settings(tmp_path),
            str(project),
            "map_multiview",
            digest,
            confirm=True,
        )

    assert destination.read_bytes() == b"conflict"
    assert list(destination.parent.glob(".evidence-*.tmp")) == []


def test_symlinked_metadata_directory_cannot_redirect_publication(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".relion-mcp").symlink_to(outside, target_is_directory=True)
    png = _png(9)
    cache = VisualEvidenceCache()
    digest = cache.register(png, _metadata(project, "relion_map_report", png))

    with pytest.raises(VisualEvidenceError, match="without replacement"):
        cache.publish(
            _settings(tmp_path),
            str(project),
            "map_multiview",
            digest,
            confirm=True,
        )

    assert list(outside.iterdir()) == []
