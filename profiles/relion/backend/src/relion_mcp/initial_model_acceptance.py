from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import mrcfile
import numpy as np
from PIL import Image, UnidentifiedImageError

from .catalog import CatalogError, safe_project_relative
from .settings import ConfigurationError, Settings
from .star import StarFormatError, parse_star_block, particle_star_row_summary

ACCEPTANCE_SCHEMA = "relion.initial-model-acceptance/v1"
ACCEPTANCE_DIRECTORY = ".relion-mcp/initial-model-acceptances"
ACCEPTANCE_FILENAME = re.compile(r"acceptance-([0-9a-f]{24})\.json")
# A refined consensus map from outside RELION (for example a CryoSPARC
# refinement) is not an InitialModel seed, so it carries its own hash-bound
# record type in the same directory instead of a fabricated InitialModel record.
EXTERNAL_REFERENCE_SCHEMA = "relion.external-reference-acceptance/v1"
EXTERNAL_REFERENCE_FILENAME = re.compile(r"external-([0-9a-f]{24})\.json")
EXTERNAL_PROVENANCE_REQUIRED = ("source_software", "source_job", "rationale")
EXTERNAL_PROVENANCE_OPTIONAL = ("source_path", "source_sha256")
POSE_LINEAGE_MODES = frozenset({"poses_from_reference_refinement", "independent_of_reference"})
POSE_ANGLE_COLUMNS = ("rlnAngleRot", "rlnAngleTilt", "rlnAnglePsi")
POSE_ORIGIN_COLUMN_SETS = (
    ("rlnOriginXAngst", "rlnOriginYAngst"),
    ("rlnOriginX", "rlnOriginY"),
)
REQUIRED_EVIDENCE_ARTIFACTS = frozenset(
    {"map_multiview", "projection_comparison", "late_iteration_comparison"}
)
INDEPENDENT_ROOT_SOURCE_TYPES = frozenset(
    {"map_pixels", "particle_class_metadata", "late_iteration_convergence"}
)
REQUIRED_ASSESSMENT_VALUES: dict[str, Any] = {
    "seed_status": "SEED_READY",
    "reference_utility": "credible",
    "confidence": "high",
    "decision": "auto_accept",
    "next_action": "advance_primary",
    "approval_flag": True,
    "visual_access": "direct",
    "late_iteration_stable": True,
    "multiple_contours_checked": True,
    "boundary_clearance_pass": True,
    "clipping_detected": False,
    "major_artifact_detected": False,
}


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _payload_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalise_relative_path(value: str, *, role: str) -> str:
    try:
        checked = safe_project_relative(value)
    except CatalogError as exc:
        raise CatalogError(f"{role}: {exc}") from exc
    path = PurePosixPath(checked)
    if path == PurePosixPath(".") or any(part in {"", "."} for part in path.parts):
        raise CatalogError(f"{role} must name a project-relative file.")
    return path.as_posix()


def _resolve_regular_file(
    settings: Settings,
    project: Path,
    value: str,
    *,
    role: str,
    suffix: str | None = None,
    allow_empty: bool = False,
) -> tuple[str, Path]:
    relative = _normalise_relative_path(value, role=role)
    raw_path = project / Path(relative)
    if raw_path.is_symlink():
        raise CatalogError(f"{role} may not be a symbolic link: {relative}")
    try:
        path = settings.resolve_project_subpath(project, relative, must_exist=True)
    except ConfigurationError as exc:
        raise CatalogError(f"{role}: {exc}") from exc
    if not path.is_file():
        raise CatalogError(f"{role} must be a regular file: {relative}")
    if suffix is not None and path.suffix.lower() != suffix:
        raise CatalogError(f"{role} must have suffix {suffix}: {relative}")
    if not allow_empty and path.stat().st_size <= 0:
        raise CatalogError(f"{role} must not be empty: {relative}")
    return relative, path


def _artifact_record(
    settings: Settings,
    project: Path,
    value: str,
    *,
    role: str,
    suffix: str | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    relative, path = _resolve_regular_file(
        settings,
        project,
        value,
        role=role,
        suffix=suffix,
        allow_empty=allow_empty,
    )
    return {
        "path": relative,
        "sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _as_finite_positive(value: object, path: str, errors: list[dict[str, str]]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(_issue("invalid_field", path, f"{path} must be a positive number."))
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        errors.append(_issue("invalid_field", path, f"{path} must be a positive finite number."))
        return None
    return number


def _validate_assessment(value: object) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(value, Mapping):
        return [_issue("invalid_field", "assessment", "assessment must be an object.")]
    for key, expected in REQUIRED_ASSESSMENT_VALUES.items():
        if value.get(key) != expected:
            errors.append(
                _issue(
                    "acceptance_criterion_failed",
                    f"assessment.{key}",
                    f"assessment.{key} must equal {expected!r}.",
                )
            )
    views = value.get("distinct_2d_views_matched")
    if isinstance(views, bool) or not isinstance(views, int) or views < 3:
        errors.append(
            _issue(
                "acceptance_criterion_failed",
                "assessment.distinct_2d_views_matched",
                "At least three distinct compatible 2D appearances must be matched.",
            )
        )
    gaps = value.get("critical_gaps")
    if not isinstance(gaps, list) or gaps:
        errors.append(
            _issue(
                "critical_gaps_present",
                "assessment.critical_gaps",
                "critical_gaps must be an empty list before Class3D.",
            )
        )
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append(
            _issue(
                "invalid_field",
                "assessment.rationale",
                "assessment.rationale must be a non-empty string.",
            )
        )
    roots = value.get("independent_root_sources")
    if (
        not isinstance(roots, list)
        or len(roots) < 2
        or any(not isinstance(root, str) for root in roots)
        or len(set(roots)) != len(roots)
        or not set(roots).issubset(INDEPENDENT_ROOT_SOURCE_TYPES)
    ):
        errors.append(
            _issue(
                "independent_evidence_missing",
                "assessment.independent_root_sources",
                "independent_root_sources must contain at least two distinct values from "
                f"{sorted(INDEPENDENT_ROOT_SOURCE_TYPES)}.",
            )
        )
    return errors


def _validate_geometry(value: object) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(value, Mapping):
        return [_issue("invalid_field", "geometry", "geometry must be an object.")]
    extent = value.get("expected_particle_extent_A")
    minimum: float | None = None
    maximum: float | None = None
    if (
        not isinstance(extent, Sequence)
        or isinstance(extent, (str, bytes, bytearray))
        or len(extent) != 2
    ):
        errors.append(
            _issue(
                "invalid_field",
                "geometry.expected_particle_extent_A",
                "expected_particle_extent_A must be [minimum, maximum].",
            )
        )
    else:
        minimum = _as_finite_positive(
            extent[0], "geometry.expected_particle_extent_A[0]", errors
        )
        maximum = _as_finite_positive(
            extent[1], "geometry.expected_particle_extent_A[1]", errors
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            errors.append(
                _issue(
                    "invalid_field",
                    "geometry.expected_particle_extent_A",
                    "Expected particle extent minimum may not exceed maximum.",
                )
            )
    mask = _as_finite_positive(value.get("mask_diameter_A"), "geometry.mask_diameter_A", errors)
    pixel = _as_finite_positive(value.get("pixel_size_A"), "geometry.pixel_size_A", errors)
    clearance = _as_finite_positive(
        value.get("boundary_clearance_A"), "geometry.boundary_clearance_A", errors
    )
    box = value.get("box_size_pix")
    if isinstance(box, bool) or not isinstance(box, int) or box <= 0:
        errors.append(
            _issue(
                "invalid_field",
                "geometry.box_size_pix",
                "geometry.box_size_pix must be a positive integer.",
            )
        )
        box = None
    if mask is not None and maximum is not None and mask <= maximum:
        errors.append(
            _issue(
                "geometry_mismatch",
                "geometry.mask_diameter_A",
                "The mask diameter must exceed the maximum expected particle extent.",
            )
        )
    if (
        box is not None
        and pixel is not None
        and mask is not None
        and clearance is not None
        and box * pixel <= mask + 2.0 * clearance
    ):
        errors.append(
            _issue(
                "geometry_mismatch",
                "geometry.boundary_clearance_A",
                "The physical box must exceed the mask diameter by twice the declared boundary clearance.",
            )
        )
    return errors


def _validate_reference_semantics(
    reference_path: Path,
    reference_relative: str,
    model_path: Path,
    geometry: object,
) -> list[dict[str, str]]:
    """Validate that the accepted reference is a real, matching RELION volume."""
    errors: list[dict[str, str]] = []
    if not isinstance(geometry, Mapping):
        return errors
    box_size = geometry.get("box_size_pix")
    pixel_size = geometry.get("pixel_size_A")
    try:
        with mrcfile.mmap(reference_path, mode="r", permissive=False) as volume:
            data = volume.data
            if data is None or data.ndim != 3:
                errors.append(
                    _issue(
                        "invalid_reference_volume",
                        "reference_volume",
                        "The accepted reference must decode as a three-dimensional MRC volume.",
                    )
                )
            else:
                shape = tuple(int(value) for value in data.shape)
                if (
                    isinstance(box_size, bool)
                    or not isinstance(box_size, int)
                    or shape != (box_size, box_size, box_size)
                ):
                    errors.append(
                        _issue(
                            "geometry_mismatch",
                            "geometry.box_size_pix",
                            f"Reference MRC shape {shape} does not match the declared cubic box size {box_size!r}.",
                        )
                    )
                if not np.issubdtype(data.dtype, np.floating):
                    errors.append(
                        _issue(
                            "invalid_reference_volume",
                            "reference_volume",
                            f"Reference MRC density must use a real floating-point mode, got {data.dtype}.",
                        )
                    )
                elif not bool(np.isfinite(data).all()):
                    errors.append(
                        _issue(
                            "invalid_reference_volume",
                            "reference_volume",
                            "Reference MRC density contains non-finite values.",
                        )
                    )
                elif float(np.min(data)) == float(np.max(data)):
                    errors.append(
                        _issue(
                            "invalid_reference_volume",
                            "reference_volume",
                            "Reference MRC density is constant and cannot be assessed as an initial model.",
                        )
                    )
            voxel_size = tuple(
                float(value)
                for value in (
                    volume.voxel_size.x,
                    volume.voxel_size.y,
                    volume.voxel_size.z,
                )
            )
            if any(not math.isfinite(value) or value <= 0 for value in voxel_size):
                errors.append(
                    _issue(
                        "invalid_reference_volume",
                        "reference_volume",
                        f"Reference MRC has invalid voxel size {voxel_size}.",
                    )
                )
            elif not all(
                math.isclose(value, voxel_size[0], rel_tol=1e-4, abs_tol=1e-5)
                for value in voxel_size[1:]
            ):
                errors.append(
                    _issue(
                        "geometry_mismatch",
                        "geometry.pixel_size_A",
                        f"Reference MRC voxel size is anisotropic: {voxel_size}.",
                    )
                )
            elif (
                isinstance(pixel_size, bool)
                or not isinstance(pixel_size, (int, float))
                or not math.isclose(
                    voxel_size[0],
                    float(pixel_size),
                    rel_tol=1e-4,
                    abs_tol=1e-5,
                )
            ):
                errors.append(
                    _issue(
                        "geometry_mismatch",
                        "geometry.pixel_size_A",
                        f"Reference MRC voxel size {voxel_size[0]:.6g} A does not match the declared pixel size {pixel_size!r} A.",
                    )
                )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(
            _issue(
                "invalid_reference_volume",
                "reference_volume",
                f"Could not decode the accepted reference as MRC: {exc}",
            )
        )

    try:
        general = parse_star_block(model_path, "model_general")
        classes = parse_star_block(model_path, "model_classes")
    except (OSError, StarFormatError) as exc:
        errors.append(
            _issue(
                "invalid_initial_model",
                "initial_model_artifacts.model_star",
                f"Could not parse InitialModel model metadata: {exc}",
            )
        )
        return errors
    if not isinstance(general, Mapping) or str(
        general.get("rlnReferenceDimensionality")
    ) != "3":
        errors.append(
            _issue(
                "invalid_initial_model",
                "initial_model_artifacts.model_star",
                "InitialModel model metadata must declare rlnReferenceDimensionality=3.",
            )
        )
    if isinstance(general, Mapping) and general.get("rlnPixelSize") is not None:
        try:
            model_pixel_size = float(str(general["rlnPixelSize"]))
        except ValueError:
            model_pixel_size = math.nan
        if (
            isinstance(pixel_size, bool)
            or not isinstance(pixel_size, (int, float))
            or not math.isfinite(model_pixel_size)
            or not math.isclose(
                model_pixel_size,
                float(pixel_size),
                rel_tol=1e-4,
                abs_tol=1e-5,
            )
        ):
            errors.append(
                _issue(
                    "geometry_mismatch",
                    "geometry.pixel_size_A",
                    "InitialModel model pixel size does not match the declared reference pixel size.",
                )
            )
    references = classes.get("rlnReferenceImage") if isinstance(classes, Mapping) else None
    if isinstance(references, str):
        references = [references]
    if not isinstance(references, list) or reference_relative not in {
        str(value) for value in references
    }:
        errors.append(
            _issue(
                "reference_model_mismatch",
                "reference_volume",
                "The selected reference is not listed by rlnReferenceImage in the final model STAR.",
            )
        )
    return errors


def _validate_particle_star_file(path: Path, role: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    try:
        summary = particle_star_row_summary(path)
    except (OSError, StarFormatError) as exc:
        return [_issue("invalid_particle_star", role, f"Could not parse {role}: {exc}")]
    if summary.particle_count < 1:
        errors.append(
            _issue("invalid_particle_star", role, f"{role} must contain particle rows.")
        )
    if summary.duplicate_image_name_count:
        errors.append(
            _issue(
                "duplicate_particle_identity",
                role,
                f"{role} contains {summary.duplicate_image_name_count} duplicate rlnImageName values.",
            )
        )
    return errors


def _validate_png_evidence(path: Path, role: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError(f"decoded format is {image.format!r}, not PNG")
            width, height = image.size
            image.verify()
        with Image.open(path) as image:
            image.load()
            extrema = image.getextrema()
            if isinstance(extrema, tuple) and len(extrema) == 2 and all(
                isinstance(value, (int, float)) for value in extrema
            ):
                varying = extrema[0] != extrema[1]
            else:
                varying = any(low != high for low, high in extrema)
        if width < 16 or height < 16:
            errors.append(
                _issue(
                    "invalid_visual_evidence",
                    role,
                    f"Visual evidence must be at least 16x16 pixels, got {width}x{height}.",
                )
            )
        if not varying:
            errors.append(
                _issue(
                    "invalid_visual_evidence",
                    role,
                    "Visual evidence is constant and contains no inspectable image variation.",
                )
            )
    except (
        OSError,
        RuntimeError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        errors.append(
            _issue(
                "invalid_visual_evidence",
                role,
                f"Could not decode required visual evidence as PNG: {exc}",
            )
        )
    return errors


def _raise_acceptance_errors(context: str, errors: list[dict[str, str]]) -> None:
    if not errors:
        return
    detail = "; ".join(f"{item['code']}: {item['message']}" for item in errors)
    raise CatalogError(f"{context}: {detail}")


def _validate_lineage_shape(value: object) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(value, Mapping):
        return [
            _issue("invalid_field", "particle_lineage", "particle_lineage must be an object.")
        ]
    mode = value.get("mode")
    if mode not in {"same_particles", "initial_model_subset_of_classification"}:
        errors.append(
            _issue(
                "invalid_field",
                "particle_lineage.mode",
                "particle_lineage.mode must be same_particles or initial_model_subset_of_classification.",
            )
        )
    if mode == "initial_model_subset_of_classification" and not isinstance(
        value.get("evidence"), Mapping
    ):
        errors.append(
            _issue(
                "particle_lineage_mismatch",
                "particle_lineage.evidence",
                "Subset lineage requires a hash-bound evidence artifact.",
            )
        )
    return errors


def _job_options(path: Path) -> dict[str, str]:
    try:
        block = parse_star_block(path, "joboptions_values")
    except (OSError, StarFormatError) as exc:
        raise CatalogError(f"Could not parse RELION job options from {path}: {exc}") from exc
    if not isinstance(block, Mapping):
        raise CatalogError(f"Missing data_joboptions_values in {path}.")
    variables = block.get("rlnJobOptionVariable")
    values = block.get("rlnJobOptionValue")
    if isinstance(variables, str):
        variables = [variables]
    if isinstance(values, str):
        values = [values]
    if not isinstance(variables, list) or not isinstance(values, list) or len(variables) != len(values):
        raise CatalogError(f"Invalid data_joboptions_values in {path}.")
    names = [str(key) for key in variables]
    if len(names) != len(set(names)):
        raise CatalogError(f"Duplicate native option names in {path}.")
    return {key: str(value) for key, value in zip(names, values, strict=True)}


def _require_initial_model_source(
    settings: Settings,
    project: Path,
    *,
    reference_volume: str,
    initial_model_particles: str,
    model_star: str,
    optimiser_star: str,
    data_star: str,
) -> dict[str, dict[str, Any]]:
    reference = _artifact_record(
        settings, project, reference_volume, role="reference_volume", suffix=".mrc"
    )
    initial_particles = _artifact_record(
        settings,
        project,
        initial_model_particles,
        role="initial_model_particles",
        suffix=".star",
    )
    model = _artifact_record(settings, project, model_star, role="model_star", suffix=".star")
    optimiser = _artifact_record(
        settings, project, optimiser_star, role="optimiser_star", suffix=".star"
    )
    data = _artifact_record(settings, project, data_star, role="data_star", suffix=".star")

    model_path = PurePosixPath(model["path"])
    source_dir = model_path.parent
    if len(source_dir.parts) != 2 or source_dir.parts[0] != "InitialModel" or re.fullmatch(
        r"job[0-9]+", source_dir.parts[1]
    ) is None:
        raise CatalogError("model_star must be inside InitialModel/jobNNN/.")
    for role, artifact in (
        ("reference_volume", reference),
        ("optimiser_star", optimiser),
        ("data_star", data),
    ):
        if PurePosixPath(artifact["path"]).parent != source_dir:
            raise CatalogError(f"{role} must come from the same InitialModel job as model_star.")

    model_match = re.fullmatch(r"(run_it[0-9]+)_model\.star", model_path.name)
    if model_match is None:
        raise CatalogError("model_star must be a final run_itNNN_model.star artifact.")
    prefix = model_match.group(1)
    if PurePosixPath(optimiser["path"]).name != f"{prefix}_optimiser.star":
        raise CatalogError("optimiser_star must match the final model iteration.")
    if PurePosixPath(data["path"]).name != f"{prefix}_data.star":
        raise CatalogError("data_star must match the final model iteration.")
    if re.fullmatch(rf"{re.escape(prefix)}_class[0-9]+\.mrc", PurePosixPath(reference["path"]).name) is None:
        raise CatalogError("reference_volume must be an explicit class map from the final model iteration.")
    selected_iteration = int(prefix.removeprefix("run_it"))
    complete_iterations: list[int] = []
    source_path = settings.resolve_project_subpath(project, source_dir.as_posix(), must_exist=True)
    for candidate in source_path.glob("run_it*_model.star"):
        match = re.fullmatch(r"run_it([0-9]+)_model\.star", candidate.name)
        if match is None:
            continue
        candidate_prefix = f"run_it{match.group(1)}"
        if (
            (source_path / f"{candidate_prefix}_optimiser.star").is_file()
            and (source_path / f"{candidate_prefix}_data.star").is_file()
            and any(source_path.glob(f"{candidate_prefix}_class[0-9]*.mrc"))
        ):
            complete_iterations.append(int(match.group(1)))
    if not complete_iterations or selected_iteration != max(complete_iterations):
        raise CatalogError(
            "reference/model/optimiser/data must come from the latest complete InitialModel iteration."
        )

    job_star_path = (source_dir / "job.star").as_posix()
    success_path = (source_dir / "RELION_JOB_EXIT_SUCCESS").as_posix()
    job_star = _artifact_record(
        settings, project, job_star_path, role="initial_model_job_star", suffix=".star"
    )
    success = _artifact_record(
        settings,
        project,
        success_path,
        role="initial_model_success_marker",
        allow_empty=True,
    )
    _, resolved_job_star = _resolve_regular_file(
        settings, project, job_star_path, role="initial_model_job_star", suffix=".star"
    )
    try:
        job_block = parse_star_block(resolved_job_star, "job")
    except (OSError, StarFormatError) as exc:
        raise CatalogError(f"Could not parse InitialModel job metadata: {exc}") from exc
    if not isinstance(job_block, Mapping) or job_block.get("rlnJobTypeLabel") != "relion.initialmodel":
        raise CatalogError("The source job.star is not a RELION InitialModel job.")
    options = _job_options(resolved_job_star)
    if options.get("fn_img") != initial_particles["path"]:
        raise CatalogError(
            "initial_model_particles does not match fn_img in the InitialModel job.star."
        )
    return {
        "reference_volume": reference,
        "initial_model_particles": initial_particles,
        "model_star": model,
        "optimiser_star": optimiser,
        "data_star": data,
        "job_star": job_star,
        "success_marker": success,
    }


def _acceptance_directory(settings: Settings, project: Path, *, create: bool) -> Path:
    metadata_raw = project / ".relion-mcp"
    if metadata_raw.is_symlink():
        raise CatalogError("The .relion-mcp metadata directory may not be a symbolic link.")
    if metadata_raw.exists() and not metadata_raw.is_dir():
        raise CatalogError("The .relion-mcp metadata path must be a directory.")
    if create:
        metadata_raw.mkdir(exist_ok=True)
    acceptance_raw = metadata_raw / "initial-model-acceptances"
    if acceptance_raw.is_symlink():
        raise CatalogError("The initial-model acceptance directory may not be a symbolic link.")
    if acceptance_raw.exists() and not acceptance_raw.is_dir():
        raise CatalogError("The initial-model acceptance path must be a directory.")
    if create:
        acceptance_raw.mkdir(exist_ok=True)
    return settings.resolve_project_subpath(
        project,
        ACCEPTANCE_DIRECTORY,
        must_exist=not create,
    )


def _publish_record(
    settings: Settings,
    project: Path,
    payload: Mapping[str, Any],
    *,
    prefix: str,
) -> tuple[str, str]:
    """Write one read-only, content-addressed record without clobbering."""
    digest = _payload_digest(payload)
    record = dict(payload)
    record["record_sha256"] = digest
    directory = _acceptance_directory(settings, project, create=True)
    relative_path = f"{ACCEPTANCE_DIRECTORY}/{prefix}-{digest[:24]}.json"
    output = settings.resolve_project_subpath(project, relative_path)
    encoded = json.dumps(record, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{prefix}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o444)
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError:
            if output.is_symlink() or not output.is_file():
                raise CatalogError("The acceptance record path is not a regular file.")
            try:
                existing = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CatalogError("The existing acceptance record is unreadable.") from exc
            if existing != record:
                raise CatalogError("A conflicting acceptance record already exists.")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return relative_path, digest


def record_initial_model_acceptance(
    settings: Settings,
    project_dir: str,
    reference_volume: str,
    initial_model_particles: str,
    classification_particles: str,
    model_star: str,
    optimiser_star: str,
    data_star: str,
    evidence_artifacts: dict[str, str],
    assessment: dict[str, Any],
    geometry: dict[str, Any],
    particle_lineage: dict[str, Any],
    confirm: bool,
) -> dict[str, Any]:
    """Write one content-addressed, hash-bound InitialModel acceptance record."""
    settings.assert_execution_enabled(confirm)
    project = settings.validate_project_path(project_dir)
    errors = _validate_assessment(assessment) + _validate_geometry(geometry)
    if errors:
        detail = "; ".join(f"{item['code']}: {item['message']}" for item in errors)
        raise CatalogError(f"InitialModel acceptance criteria were not met: {detail}")
    if not isinstance(evidence_artifacts, dict):
        raise CatalogError("evidence_artifacts must be an object of named project-relative paths.")
    missing_evidence = sorted(REQUIRED_EVIDENCE_ARTIFACTS.difference(evidence_artifacts))
    if missing_evidence:
        raise CatalogError(f"Missing required InitialModel evidence artifacts: {missing_evidence}")

    source = _require_initial_model_source(
        settings,
        project,
        reference_volume=reference_volume,
        initial_model_particles=initial_model_particles,
        model_star=model_star,
        optimiser_star=optimiser_star,
        data_star=data_star,
    )
    classification = _artifact_record(
        settings,
        project,
        classification_particles,
        role="classification_particles",
        suffix=".star",
    )
    reference_path = settings.resolve_project_subpath(
        project, source["reference_volume"]["path"], must_exist=True
    )
    model_path = settings.resolve_project_subpath(
        project, source["model_star"]["path"], must_exist=True
    )
    initial_particles_path = settings.resolve_project_subpath(
        project, source["initial_model_particles"]["path"], must_exist=True
    )
    classification_particles_path = settings.resolve_project_subpath(
        project, classification["path"], must_exist=True
    )
    structural_errors = _validate_reference_semantics(
        reference_path,
        source["reference_volume"]["path"],
        model_path,
        geometry,
    )
    structural_errors.extend(
        _validate_particle_star_file(initial_particles_path, "initial_model_particles")
    )
    if classification_particles_path != initial_particles_path:
        structural_errors.extend(
            _validate_particle_star_file(
                classification_particles_path, "classification_particles"
            )
        )
    _raise_acceptance_errors(
        "InitialModel source artifacts failed structural validation", structural_errors
    )
    evidence: dict[str, dict[str, Any]] = {}
    for name, path in sorted(evidence_artifacts.items()):
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
            raise CatalogError(f"Unsafe evidence artifact name: {name!r}")
        evidence[name] = _artifact_record(
            settings,
            project,
            path,
            role=f"evidence_artifacts.{name}",
            suffix=".png" if name in REQUIRED_EVIDENCE_ARTIFACTS else None,
        )
    required_paths = [evidence[name]["path"] for name in REQUIRED_EVIDENCE_ARTIFACTS]
    if len(set(required_paths)) != len(required_paths):
        raise CatalogError("Required evidence channels must use distinct artifact paths.")
    required_hashes = [evidence[name]["sha256"] for name in REQUIRED_EVIDENCE_ARTIFACTS]
    if len(set(required_hashes)) != len(required_hashes):
        raise CatalogError("Required evidence channels must contain distinct image content.")
    evidence_errors: list[dict[str, str]] = []
    for name in sorted(REQUIRED_EVIDENCE_ARTIFACTS):
        evidence_path = settings.resolve_project_subpath(
            project, evidence[name]["path"], must_exist=True
        )
        evidence_errors.extend(
            _validate_png_evidence(evidence_path, f"evidence_artifacts.{name}")
        )
    _raise_acceptance_errors(
        "InitialModel evidence failed structural validation", evidence_errors
    )

    if not isinstance(particle_lineage, dict):
        raise CatalogError("particle_lineage must be an object.")
    mode = particle_lineage.get("mode")
    lineage: dict[str, Any] = {"mode": mode, "evidence": None}
    initial = source["initial_model_particles"]
    if mode == "same_particles":
        if initial["path"] != classification["path"] or initial["sha256"] != classification["sha256"]:
            raise CatalogError(
                "particle_lineage_mismatch: same_particles requires the exact same path and SHA-256."
            )
    elif mode == "initial_model_subset_of_classification":
        evidence_path = particle_lineage.get("evidence_path")
        if not isinstance(evidence_path, str) or not evidence_path:
            raise CatalogError(
                "particle_lineage_mismatch: subset lineage requires evidence_path."
            )
        lineage["evidence"] = _artifact_record(
            settings,
            project,
            evidence_path,
            role="particle_lineage.evidence_path",
        )
    else:
        raise CatalogError(
            "particle_lineage.mode must be same_particles or initial_model_subset_of_classification."
        )

    payload: dict[str, Any] = {
        "schema": ACCEPTANCE_SCHEMA,
        "reference_volume": source["reference_volume"],
        "initial_model_particles": initial,
        "classification_particles": classification,
        "initial_model_artifacts": {
            key: source[key]
            for key in (
                "model_star",
                "optimiser_star",
                "data_star",
                "job_star",
                "success_marker",
            )
        },
        "evidence_artifacts": evidence,
        "assessment": dict(assessment),
        "geometry": dict(geometry),
        "particle_lineage": lineage,
    }
    relative_path, digest = _publish_record(settings, project, payload, prefix="acceptance")
    return {
        "valid": True,
        "schema": ACCEPTANCE_SCHEMA,
        "project_dir": str(project),
        "initial_model_acceptance": relative_path,
        "acceptance_record": relative_path,
        "record_sha256": digest,
        "reference_volume": source["reference_volume"],
        "classification_particles": classification,
        "mask_diameter_A": float(geometry["mask_diameter_A"]),
    }


def _record_path(settings: Settings, project: Path, acceptance_record: str) -> tuple[str, Path]:
    relative = _normalise_relative_path(acceptance_record, role="acceptance_record")
    pure = PurePosixPath(relative)
    if pure.parent.as_posix() != ACCEPTANCE_DIRECTORY or (
        ACCEPTANCE_FILENAME.fullmatch(pure.name) is None
        and EXTERNAL_REFERENCE_FILENAME.fullmatch(pure.name) is None
    ):
        raise CatalogError(
            f"acceptance_record must be a content-addressed file under {ACCEPTANCE_DIRECTORY}/."
        )
    raw = project / Path(relative)
    if raw.is_symlink():
        raise CatalogError("acceptance_record may not be a symbolic link.")
    path = settings.resolve_project_subpath(project, relative, must_exist=True)
    if not path.is_file():
        raise CatalogError("acceptance_record must be a regular file.")
    if path.stat().st_size <= 0 or path.stat().st_size > 1024 * 1024:
        raise CatalogError("acceptance_record must be a nonempty JSON file no larger than 1 MiB.")
    return relative, path


def _verify_artifact_entry(
    settings: Settings,
    project: Path,
    value: object,
    path: str,
    errors: list[dict[str, str]],
    hash_cache: dict[Path, str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(_issue("invalid_record", path, f"{path} must be an artifact object."))
        return
    relative = value.get("path")
    sha256 = value.get("sha256")
    size = value.get("size_bytes")
    if not isinstance(relative, str) or not isinstance(sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", sha256
    ) is None or isinstance(size, bool) or not isinstance(size, int) or size < 0:
        errors.append(_issue("invalid_record", path, f"{path} has invalid artifact metadata."))
        return
    try:
        checked, resolved = _resolve_regular_file(
            settings,
            project,
            relative,
            role=path,
            allow_empty=True,
        )
    except (CatalogError, OSError) as exc:
        errors.append(_issue("artifact_unavailable", path, str(exc)))
        return
    if checked != relative:
        errors.append(_issue("invalid_record", f"{path}.path", "Artifact path is not canonical."))
    current_size = resolved.stat().st_size
    if current_size != size:
        errors.append(
            _issue(
                "artifact_size_mismatch",
                path,
                f"Artifact size changed from {size} to {current_size}: {relative}",
            )
        )
    try:
        current_hash = hash_cache.get(resolved)
        if current_hash is None:
            current_hash = _file_sha256(resolved)
            hash_cache[resolved] = current_hash
    except OSError as exc:
        errors.append(_issue("artifact_unavailable", path, str(exc)))
        return
    if current_hash != sha256:
        role_code = {
            "reference_volume": "reference_sha256_mismatch",
            "classification_particles": "particles_sha256_mismatch",
        }.get(path, "artifact_sha256_mismatch")
        errors.append(
            _issue(role_code, path, f"Artifact SHA-256 changed: {relative}")
        )


def validate_initial_model_acceptance(
    settings: Settings,
    project_dir: str,
    acceptance_record: str,
    *,
    reference_volume: str | None = None,
    classification_particles: str | None = None,
    mask_diameter_A: float | None = None,
    alignment: bool | None = None,
    _hash_cache: dict[Path, str] | None = None,
) -> dict[str, Any]:
    """Validate an acceptance record and optionally bind it to one Class3D launch.

    The record is either an InitialModel acceptance or an external refined
    reference acceptance; ``alignment`` only constrains the latter.
    """
    project = settings.validate_project_path(project_dir)
    relative, path = _record_path(settings, project, acceptance_record)
    errors: list[dict[str, str]] = []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "acceptance_record": relative,
            "errors": [_issue("invalid_record", "acceptance_record", str(exc))],
        }
    if not isinstance(loaded, dict):
        return {
            "valid": False,
            "acceptance_record": relative,
            "errors": [_issue("invalid_record", "acceptance_record", "Record must be an object.")],
        }
    if loaded.get("schema") == EXTERNAL_REFERENCE_SCHEMA:
        return _validate_external_reference_record(
            settings,
            project,
            relative,
            loaded,
            reference_volume=reference_volume,
            classification_particles=classification_particles,
            mask_diameter_A=mask_diameter_A,
            alignment=alignment,
            hash_cache={} if _hash_cache is None else _hash_cache,
        )
    expected_keys = {
        "schema",
        "reference_volume",
        "initial_model_particles",
        "classification_particles",
        "initial_model_artifacts",
        "evidence_artifacts",
        "assessment",
        "geometry",
        "particle_lineage",
        "record_sha256",
    }
    if set(loaded) != expected_keys:
        errors.append(
            _issue(
                "invalid_record",
                "acceptance_record",
                f"Record keys must be exactly {sorted(expected_keys)}.",
            )
        )
    if loaded.get("schema") != ACCEPTANCE_SCHEMA:
        errors.append(_issue("invalid_record", "schema", f"schema must be {ACCEPTANCE_SCHEMA}."))
    claimed_digest = loaded.get("record_sha256")
    payload = {key: value for key, value in loaded.items() if key != "record_sha256"}
    actual_digest = _payload_digest(payload)
    name_match = ACCEPTANCE_FILENAME.fullmatch(PurePosixPath(relative).name)
    if claimed_digest != actual_digest:
        errors.append(
            _issue("record_sha256_mismatch", "record_sha256", "Record content digest is invalid.")
        )
    if name_match is None or name_match.group(1) != actual_digest[:24]:
        errors.append(
            _issue("record_sha256_mismatch", "acceptance_record", "Record filename does not match its content digest.")
        )
    errors.extend(_validate_assessment(loaded.get("assessment")))
    errors.extend(_validate_geometry(loaded.get("geometry")))
    errors.extend(_validate_lineage_shape(loaded.get("particle_lineage")))

    hash_cache = {} if _hash_cache is None else _hash_cache
    for key in ("reference_volume", "initial_model_particles", "classification_particles"):
        _verify_artifact_entry(settings, project, loaded.get(key), key, errors, hash_cache)
    initial_artifacts = loaded.get("initial_model_artifacts")
    required_initial = {"model_star", "optimiser_star", "data_star", "job_star", "success_marker"}
    if not isinstance(initial_artifacts, Mapping) or set(initial_artifacts) != required_initial:
        errors.append(
            _issue(
                "invalid_record",
                "initial_model_artifacts",
                f"initial_model_artifacts keys must be exactly {sorted(required_initial)}.",
            )
        )
    else:
        for key in sorted(required_initial):
            _verify_artifact_entry(
                settings,
                project,
                initial_artifacts[key],
                f"initial_model_artifacts.{key}",
                errors,
                hash_cache,
            )
    evidence = loaded.get("evidence_artifacts")
    if not isinstance(evidence, Mapping):
        errors.append(
            _issue("invalid_record", "evidence_artifacts", "evidence_artifacts must be an object.")
        )
    else:
        missing = sorted(REQUIRED_EVIDENCE_ARTIFACTS.difference(evidence))
        if missing:
            errors.append(
                _issue(
                    "incomplete_evidence",
                    "evidence_artifacts",
                    f"Missing required evidence artifacts: {missing}",
                )
            )
        for key, artifact in sorted(evidence.items()):
            if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None:
                errors.append(
                    _issue(
                        "invalid_record",
                        "evidence_artifacts",
                        f"Unsafe evidence artifact name: {key!r}",
                    )
                )
            _verify_artifact_entry(
                settings,
                project,
                artifact,
                f"evidence_artifacts.{key}",
                errors,
                hash_cache,
            )
        required_names = sorted(REQUIRED_EVIDENCE_ARTIFACTS)
        required_entries = [evidence.get(name) for name in required_names]
        if all(isinstance(entry, Mapping) for entry in required_entries):
            required_paths = [entry.get("path") for entry in required_entries]
            required_hashes = [entry.get("sha256") for entry in required_entries]
            if all(isinstance(value, str) for value in required_paths) and len(
                set(required_paths)
            ) != len(required_paths):
                errors.append(
                    _issue(
                        "incomplete_evidence",
                        "evidence_artifacts",
                        "Required evidence channels must use distinct artifact paths.",
                    )
                )
            if all(isinstance(value, str) for value in required_hashes) and len(
                set(required_hashes)
            ) != len(required_hashes):
                errors.append(
                    _issue(
                        "incomplete_evidence",
                        "evidence_artifacts",
                        "Required evidence channels must contain distinct image content.",
                    )
                )
            for name, entry in zip(
                required_names, required_entries, strict=True
            ):
                relative_path = entry.get("path")
                if not isinstance(relative_path, str):
                    continue
                try:
                    _, resolved_evidence = _resolve_regular_file(
                        settings,
                        project,
                        relative_path,
                        role=f"evidence_artifacts.{name}",
                        suffix=".png",
                    )
                except (CatalogError, OSError) as exc:
                    errors.append(
                        _issue(
                            "invalid_visual_evidence",
                            f"evidence_artifacts.{name}",
                            str(exc),
                        )
                    )
                    continue
                errors.extend(
                    _validate_png_evidence(
                        resolved_evidence, f"evidence_artifacts.{name}"
                    )
                )

    reference_entry = loaded.get("reference_volume")
    initial_particles_entry = loaded.get("initial_model_particles")
    classification_entry = loaded.get("classification_particles")
    if (
        isinstance(reference_entry, Mapping)
        and isinstance(initial_particles_entry, Mapping)
        and isinstance(classification_entry, Mapping)
        and isinstance(initial_artifacts, Mapping)
        and all(
            isinstance(entry, Mapping)
            for entry in (
                initial_artifacts.get("model_star"),
                initial_artifacts.get("optimiser_star"),
                initial_artifacts.get("data_star"),
            )
        )
    ):
        source_paths = {
            "reference_volume": reference_entry.get("path"),
            "initial_model_particles": initial_particles_entry.get("path"),
            "classification_particles": classification_entry.get("path"),
            "model_star": initial_artifacts["model_star"].get("path"),
            "optimiser_star": initial_artifacts["optimiser_star"].get("path"),
            "data_star": initial_artifacts["data_star"].get("path"),
        }
        if all(isinstance(value, str) for value in source_paths.values()):
            try:
                _require_initial_model_source(
                    settings,
                    project,
                    reference_volume=source_paths["reference_volume"],
                    initial_model_particles=source_paths["initial_model_particles"],
                    model_star=source_paths["model_star"],
                    optimiser_star=source_paths["optimiser_star"],
                    data_star=source_paths["data_star"],
                )
                resolved_reference = settings.resolve_project_subpath(
                    project, source_paths["reference_volume"], must_exist=True
                )
                resolved_model = settings.resolve_project_subpath(
                    project, source_paths["model_star"], must_exist=True
                )
                resolved_initial_particles = settings.resolve_project_subpath(
                    project, source_paths["initial_model_particles"], must_exist=True
                )
                resolved_classification = settings.resolve_project_subpath(
                    project, source_paths["classification_particles"], must_exist=True
                )
                errors.extend(
                    _validate_reference_semantics(
                        resolved_reference,
                        source_paths["reference_volume"],
                        resolved_model,
                        loaded.get("geometry"),
                    )
                )
                errors.extend(
                    _validate_particle_star_file(
                        resolved_initial_particles, "initial_model_particles"
                    )
                )
                if resolved_classification != resolved_initial_particles:
                    errors.extend(
                        _validate_particle_star_file(
                            resolved_classification, "classification_particles"
                        )
                    )
            except (CatalogError, ConfigurationError, OSError) as exc:
                errors.append(
                    _issue(
                        "invalid_initial_model",
                        "initial_model_artifacts",
                        f"InitialModel source structure is no longer valid: {exc}",
                    )
                )
    lineage = loaded.get("particle_lineage")
    initial = loaded.get("initial_model_particles")
    classification = loaded.get("classification_particles")
    if isinstance(lineage, Mapping) and isinstance(initial, Mapping) and isinstance(classification, Mapping):
        if lineage.get("mode") == "same_particles":
            if initial.get("path") != classification.get("path") or initial.get("sha256") != classification.get("sha256"):
                errors.append(
                    _issue(
                        "particle_lineage_mismatch",
                        "particle_lineage",
                        "same_particles does not bind the exact same path and SHA-256.",
                    )
                )
        elif lineage.get("mode") == "initial_model_subset_of_classification":
            _verify_artifact_entry(
                settings,
                project,
                lineage.get("evidence"),
                "particle_lineage.evidence",
                errors,
                hash_cache,
            )

    errors.extend(
        _class3d_binding_errors(
            loaded,
            reference_volume=reference_volume,
            classification_particles=classification_particles,
            mask_diameter_A=mask_diameter_A,
        )
    )
    return {
        "valid": not errors,
        "schema": loaded.get("schema"),
        "project_dir": str(project),
        "acceptance_record": relative,
        "record_sha256": loaded.get("record_sha256"),
        "errors": errors,
    }


def _class3d_binding_errors(
    loaded: Mapping[str, Any],
    *,
    reference_volume: str | None,
    classification_particles: str | None,
    mask_diameter_A: float | None,
) -> list[dict[str, str]]:
    """Bind a record to the exact reference, particles, and diameter of one Class3D job."""
    errors: list[dict[str, str]] = []
    if reference_volume is not None:
        expected_reference = _normalise_relative_path(reference_volume, role="reference_volume")
        recorded_reference = loaded.get("reference_volume")
        if not isinstance(recorded_reference, Mapping) or recorded_reference.get("path") != expected_reference:
            errors.append(
                _issue(
                    "reference_path_mismatch",
                    "reference_volume.path",
                    f"Acceptance is not bound to Class3D reference {expected_reference}.",
                )
            )
    if classification_particles is not None:
        expected_particles = _normalise_relative_path(
            classification_particles, role="classification_particles"
        )
        recorded_particles = loaded.get("classification_particles")
        if not isinstance(recorded_particles, Mapping) or recorded_particles.get("path") != expected_particles:
            errors.append(
                _issue(
                    "particle_lineage_mismatch",
                    "classification_particles.path",
                    f"Acceptance is not bound to Class3D particles {expected_particles}.",
                )
            )
    if mask_diameter_A is not None:
        if isinstance(mask_diameter_A, bool) or not isinstance(mask_diameter_A, (int, float)) or not math.isfinite(float(mask_diameter_A)):
            errors.append(_issue("geometry_mismatch", "mask_diameter_A", "Class3D mask diameter is invalid."))
        else:
            geometry = loaded.get("geometry")
            recorded_mask = geometry.get("mask_diameter_A") if isinstance(geometry, Mapping) else None
            if isinstance(recorded_mask, bool) or not isinstance(recorded_mask, (int, float)) or not math.isclose(
                float(recorded_mask), float(mask_diameter_A), rel_tol=0.0, abs_tol=1e-6
            ):
                errors.append(
                    _issue(
                        "geometry_mismatch",
                        "geometry.mask_diameter_A",
                        "Acceptance mask diameter does not match the rendered Class3D particle diameter.",
                    )
                )
    return errors


def _external_volume_geometry(path: Path) -> tuple[int | None, float | None, list[dict[str, str]]]:
    """Measure the cubic box and isotropic voxel size of a usable reference density."""
    errors: list[dict[str, str]] = []
    box: int | None = None
    pixel: float | None = None
    try:
        with mrcfile.mmap(path, mode="r", permissive=False) as volume:
            data = volume.data
            if data is None or data.ndim != 3:
                errors.append(
                    _issue(
                        "invalid_reference_volume",
                        "reference_volume",
                        "The external reference must decode as a three-dimensional MRC volume.",
                    )
                )
            else:
                shape = tuple(int(value) for value in data.shape)
                if len(set(shape)) != 1:
                    errors.append(
                        _issue(
                            "geometry_mismatch",
                            "reference_volume",
                            f"The external reference must be cubic, got shape {shape}.",
                        )
                    )
                else:
                    box = shape[0]
                if not np.issubdtype(data.dtype, np.floating):
                    errors.append(
                        _issue(
                            "invalid_reference_volume",
                            "reference_volume",
                            f"Reference MRC density must use a real floating-point mode, got {data.dtype}.",
                        )
                    )
                elif not bool(np.isfinite(data).all()):
                    errors.append(
                        _issue(
                            "invalid_reference_volume",
                            "reference_volume",
                            "Reference MRC density contains non-finite values.",
                        )
                    )
                elif float(np.min(data)) == float(np.max(data)):
                    errors.append(
                        _issue(
                            "invalid_reference_volume",
                            "reference_volume",
                            "Reference MRC density is constant.",
                        )
                    )
            voxel_size = tuple(
                float(value)
                for value in (volume.voxel_size.x, volume.voxel_size.y, volume.voxel_size.z)
            )
            if any(not math.isfinite(value) or value <= 0 for value in voxel_size):
                errors.append(
                    _issue(
                        "invalid_reference_volume",
                        "reference_volume",
                        f"Reference MRC has invalid voxel size {voxel_size}.",
                    )
                )
            elif not all(
                math.isclose(value, voxel_size[0], rel_tol=1e-4, abs_tol=1e-5)
                for value in voxel_size[1:]
            ):
                errors.append(
                    _issue(
                        "geometry_mismatch",
                        "reference_volume",
                        f"Reference MRC voxel size is anisotropic: {voxel_size}.",
                    )
                )
            else:
                pixel = voxel_size[0]
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(
            _issue(
                "invalid_reference_volume",
                "reference_volume",
                f"Could not decode the external reference as MRC: {exc}",
            )
        )
    return box, pixel, errors


def _star_column(block: Mapping[str, Any], name: str) -> list[str] | None:
    value = block.get(name)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return None


def _measure_external_geometry(
    reference_path: Path,
    particles_path: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Require every particle optics group to share the reference box and pixel size."""
    box, pixel, errors = _external_volume_geometry(reference_path)
    try:
        optics = parse_star_block(particles_path, "optics")
    except (OSError, StarFormatError) as exc:
        optics = None
        errors.append(
            _issue(
                "invalid_particle_star",
                "classification_particles",
                f"Could not parse the particle optics block: {exc}",
            )
        )
    sizes = _star_column(optics, "rlnImageSize") if isinstance(optics, Mapping) else None
    pixels = _star_column(optics, "rlnImagePixelSize") if isinstance(optics, Mapping) else None
    group_count = 0
    if not sizes or not pixels or len(sizes) != len(pixels):
        errors.append(
            _issue(
                "invalid_particle_star",
                "classification_particles",
                "The particle STAR needs a data_optics loop with rlnImageSize and rlnImagePixelSize.",
            )
        )
    else:
        group_count = len(sizes)
        for index, (size_text, pixel_text) in enumerate(zip(sizes, pixels, strict=True), start=1):
            try:
                size = int(size_text)
                group_pixel = float(pixel_text)
            except ValueError:
                errors.append(
                    _issue(
                        "invalid_particle_star",
                        "classification_particles",
                        f"Optics row {index} has a non-numeric image size or pixel size.",
                    )
                )
                continue
            if box is not None and size != box:
                errors.append(
                    _issue(
                        "geometry_mismatch",
                        "geometry.box_size_pix",
                        f"Optics row {index} image size {size} does not match the reference box {box}.",
                    )
                )
            if pixel is not None and not math.isclose(
                group_pixel, pixel, rel_tol=1e-4, abs_tol=1e-5
            ):
                errors.append(
                    _issue(
                        "geometry_mismatch",
                        "geometry.pixel_size_A",
                        f"Optics row {index} pixel size {group_pixel:.6g} A does not match the reference voxel size {pixel:.6g} A.",
                    )
                )
    geometry = {
        "box_size_pix": box,
        "pixel_size_A": None if pixel is None else round(pixel, 6),
        "optics_group_count": group_count,
    }
    return geometry, errors


def _particle_star_columns(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    try:
        summary = particle_star_row_summary(path)
    except (OSError, StarFormatError) as exc:
        return (), [
            _issue(
                "invalid_particle_star",
                "classification_particles",
                f"Could not parse classification_particles: {exc}",
            )
        ]
    errors: list[dict[str, str]] = []
    if summary.particle_count < 1:
        errors.append(
            _issue(
                "invalid_particle_star",
                "classification_particles",
                "classification_particles must contain particle rows.",
            )
        )
    if summary.duplicate_image_name_count:
        errors.append(
            _issue(
                "duplicate_particle_identity",
                "classification_particles",
                f"classification_particles contains {summary.duplicate_image_name_count} duplicate rlnImageName values.",
            )
        )
    return summary.columns, errors


def _validate_external_provenance(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        return [_issue("invalid_field", "reference_provenance", "provenance must be an object.")]
    errors: list[dict[str, str]] = []
    allowed = set(EXTERNAL_PROVENANCE_REQUIRED) | set(EXTERNAL_PROVENANCE_OPTIONAL)
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        errors.append(
            _issue(
                "invalid_field",
                "reference_provenance",
                f"Unknown provenance fields {unknown}; allowed fields are {sorted(allowed)}.",
            )
        )
    for key in EXTERNAL_PROVENANCE_REQUIRED:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > 2000:
            errors.append(
                _issue(
                    "invalid_field",
                    f"reference_provenance.{key}",
                    f"provenance.{key} must be a non-empty string of at most 2000 characters.",
                )
            )
    source_path = value.get("source_path")
    if source_path is not None and (
        not isinstance(source_path, str) or not source_path.strip() or len(source_path) > 4096
    ):
        errors.append(
            _issue(
                "invalid_field",
                "reference_provenance.source_path",
                "provenance.source_path must be a non-empty string when present.",
            )
        )
    source_sha256 = value.get("source_sha256")
    if source_sha256 is not None and (
        not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
    ):
        errors.append(
            _issue(
                "invalid_field",
                "reference_provenance.source_sha256",
                "provenance.source_sha256 must be a lowercase SHA-256 hex digest when present.",
            )
        )
    return errors


def _validate_pose_lineage_shape(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Mapping) or set(value) != {"mode"} or value.get("mode") not in POSE_LINEAGE_MODES:
        return [
            _issue(
                "invalid_field",
                "pose_lineage",
                f"pose_lineage must be exactly {{'mode': one of {sorted(POSE_LINEAGE_MODES)}}}.",
            )
        ]
    return []


def _pose_column_errors(pose_lineage: object, columns: Sequence[str]) -> list[dict[str, str]]:
    if not isinstance(pose_lineage, Mapping) or pose_lineage.get("mode") != "poses_from_reference_refinement":
        return []
    present = set(columns)
    missing = [name for name in POSE_ANGLE_COLUMNS if name not in present]
    if not any(set(group) <= present for group in POSE_ORIGIN_COLUMN_SETS):
        missing.append("rlnOriginXAngst/rlnOriginYAngst")
    if not missing:
        return []
    return [
        _issue(
            "pose_lineage_mismatch",
            "pose_lineage",
            f"poses_from_reference_refinement requires particle pose columns; missing {missing}.",
        )
    ]


def _mask_fits_box_errors(geometry: Mapping[str, Any], mask: float | None) -> list[dict[str, str]]:
    box = geometry.get("box_size_pix")
    pixel = geometry.get("pixel_size_A")
    if mask is None or not isinstance(box, int) or not isinstance(pixel, (int, float)):
        return []
    if mask > box * float(pixel):
        return [
            _issue(
                "geometry_mismatch",
                "geometry.mask_diameter_A",
                f"Mask diameter {mask:g} A exceeds the {box * float(pixel):.4g} A particle box.",
            )
        ]
    return []


def _reject_initial_model_reference(relative: str) -> None:
    if PurePosixPath(relative).parts[0] == "InitialModel":
        raise CatalogError(
            "An InitialModel output is a de novo seed, not an external refined reference; "
            "use relion_record_initial_model_acceptance."
        )


def record_external_reference(
    settings: Settings,
    project_dir: str,
    reference_volume: str,
    classification_particles: str,
    mask_diameter_A: float,
    provenance: dict[str, Any],
    pose_lineage: dict[str, Any],
    confirm: bool,
) -> dict[str, Any]:
    """Write one content-addressed record admitting a refined external map as a Class3D reference."""
    settings.assert_execution_enabled(confirm)
    project = settings.validate_project_path(project_dir)
    errors = _validate_external_provenance(provenance) + _validate_pose_lineage_shape(pose_lineage)
    mask = _as_finite_positive(mask_diameter_A, "geometry.mask_diameter_A", errors)
    _raise_acceptance_errors("External reference criteria were not met", errors)
    reference = _artifact_record(
        settings, project, reference_volume, role="reference_volume", suffix=".mrc"
    )
    _reject_initial_model_reference(reference["path"])
    particles = _artifact_record(
        settings,
        project,
        classification_particles,
        role="classification_particles",
        suffix=".star",
    )
    reference_path = settings.resolve_project_subpath(project, reference["path"], must_exist=True)
    particles_path = settings.resolve_project_subpath(project, particles["path"], must_exist=True)
    geometry, structural = _measure_external_geometry(reference_path, particles_path)
    columns, particle_errors = _particle_star_columns(particles_path)
    structural.extend(particle_errors)
    structural.extend(_pose_column_errors(pose_lineage, columns))
    structural.extend(_mask_fits_box_errors(geometry, mask))
    _raise_acceptance_errors("External reference failed structural validation", structural)
    geometry["mask_diameter_A"] = mask
    payload: dict[str, Any] = {
        "schema": EXTERNAL_REFERENCE_SCHEMA,
        "reference_volume": reference,
        "classification_particles": particles,
        "reference_provenance": {
            key: provenance[key]
            for key in (*EXTERNAL_PROVENANCE_REQUIRED, *EXTERNAL_PROVENANCE_OPTIONAL)
            if provenance.get(key) is not None
        },
        "geometry": geometry,
        "pose_lineage": {"mode": pose_lineage["mode"]},
    }
    relative_path, digest = _publish_record(settings, project, payload, prefix="external")
    return {
        "valid": True,
        "schema": EXTERNAL_REFERENCE_SCHEMA,
        "project_dir": str(project),
        "initial_model_acceptance": relative_path,
        "acceptance_record": relative_path,
        "record_sha256": digest,
        "reference_volume": reference,
        "classification_particles": particles,
        "geometry": geometry,
        "pose_lineage": payload["pose_lineage"],
    }


def _validate_external_reference_record(
    settings: Settings,
    project: Path,
    relative: str,
    loaded: Mapping[str, Any],
    *,
    reference_volume: str | None,
    classification_particles: str | None,
    mask_diameter_A: float | None,
    alignment: bool | None,
    hash_cache: dict[Path, str],
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    expected_keys = {
        "schema",
        "reference_volume",
        "classification_particles",
        "reference_provenance",
        "geometry",
        "pose_lineage",
        "record_sha256",
    }
    if set(loaded) != expected_keys:
        errors.append(
            _issue(
                "invalid_record",
                "acceptance_record",
                f"Record keys must be exactly {sorted(expected_keys)}.",
            )
        )
    payload = {key: value for key, value in loaded.items() if key != "record_sha256"}
    actual_digest = _payload_digest(payload)
    if loaded.get("record_sha256") != actual_digest:
        errors.append(
            _issue("record_sha256_mismatch", "record_sha256", "Record content digest is invalid.")
        )
    name_match = EXTERNAL_REFERENCE_FILENAME.fullmatch(PurePosixPath(relative).name)
    if name_match is None or name_match.group(1) != actual_digest[:24]:
        errors.append(
            _issue(
                "record_sha256_mismatch",
                "acceptance_record",
                "Record filename does not match its content digest.",
            )
        )
    errors.extend(_validate_external_provenance(loaded.get("reference_provenance")))
    pose_lineage = loaded.get("pose_lineage")
    errors.extend(_validate_pose_lineage_shape(pose_lineage))
    for key in ("reference_volume", "classification_particles"):
        _verify_artifact_entry(settings, project, loaded.get(key), key, errors, hash_cache)

    recorded_geometry = loaded.get("geometry")
    if not isinstance(recorded_geometry, Mapping):
        errors.append(_issue("invalid_record", "geometry", "geometry must be an object."))
        recorded_geometry = {}
    recorded_mask = _as_finite_positive(
        recorded_geometry.get("mask_diameter_A"), "geometry.mask_diameter_A", errors
    )
    reference_entry = loaded.get("reference_volume")
    particles_entry = loaded.get("classification_particles")
    reference_relative = reference_entry.get("path") if isinstance(reference_entry, Mapping) else None
    particles_relative = particles_entry.get("path") if isinstance(particles_entry, Mapping) else None
    if isinstance(reference_relative, str) and isinstance(particles_relative, str):
        try:
            _reject_initial_model_reference(reference_relative)
            reference_path = settings.resolve_project_subpath(
                project, reference_relative, must_exist=True
            )
            particles_path = settings.resolve_project_subpath(
                project, particles_relative, must_exist=True
            )
            measured, geometry_errors = _measure_external_geometry(reference_path, particles_path)
            errors.extend(geometry_errors)
            for key in ("box_size_pix", "optics_group_count"):
                if measured.get(key) != recorded_geometry.get(key):
                    errors.append(
                        _issue(
                            "geometry_mismatch",
                            f"geometry.{key}",
                            f"Recorded {key} {recorded_geometry.get(key)!r} no longer matches measured {measured.get(key)!r}.",
                        )
                    )
            recorded_pixel = recorded_geometry.get("pixel_size_A")
            measured_pixel = measured.get("pixel_size_A")
            if (
                isinstance(recorded_pixel, bool)
                or not isinstance(recorded_pixel, (int, float))
                or measured_pixel is None
                or not math.isclose(float(recorded_pixel), measured_pixel, rel_tol=1e-4, abs_tol=1e-5)
            ):
                errors.append(
                    _issue(
                        "geometry_mismatch",
                        "geometry.pixel_size_A",
                        f"Recorded pixel size {recorded_pixel!r} no longer matches measured {measured_pixel!r}.",
                    )
                )
            errors.extend(_mask_fits_box_errors(measured, recorded_mask))
            columns, particle_errors = _particle_star_columns(particles_path)
            errors.extend(particle_errors)
            errors.extend(_pose_column_errors(pose_lineage, columns))
        except (CatalogError, ConfigurationError, OSError) as exc:
            errors.append(
                _issue(
                    "invalid_reference_volume",
                    "reference_volume",
                    f"External reference is no longer valid: {exc}",
                )
            )

    errors.extend(
        _class3d_binding_errors(
            loaded,
            reference_volume=reference_volume,
            classification_particles=classification_particles,
            mask_diameter_A=mask_diameter_A,
        )
    )
    if alignment is False and (
        not isinstance(pose_lineage, Mapping)
        or pose_lineage.get("mode") != "poses_from_reference_refinement"
    ):
        errors.append(
            _issue(
                "pose_lineage_mismatch",
                "pose_lineage.mode",
                "Class3D without alignment keeps the input poses, so they must come from the "
                "refinement that produced this reference (poses_from_reference_refinement).",
            )
        )
    return {
        "valid": not errors,
        "schema": loaded.get("schema"),
        "project_dir": str(project),
        "acceptance_record": relative,
        "record_sha256": loaded.get("record_sha256"),
        "errors": errors,
    }


def require_initial_model_acceptance(
    settings: Settings,
    project: Path,
    acceptance_record: str | None,
    *,
    reference_volume: str,
    classification_particles: str,
    mask_diameter_A: float,
    alignment: bool = True,
) -> dict[str, Any]:
    if not acceptance_record:
        raise CatalogError(
            "Initial-model acceptance gate blocked Class3D: acceptance_record_missing: "
            "Class3D requires a validated initial_model_acceptance record. "
            "Create one with relion_record_initial_model_acceptance after the InitialModel evidence checkpoint, "
            "or with relion_record_external_reference for a refined consensus map from outside RELION."
        )
    result = validate_initial_model_acceptance(
        settings,
        str(project),
        acceptance_record,
        reference_volume=reference_volume,
        classification_particles=classification_particles,
        mask_diameter_A=mask_diameter_A,
        alignment=alignment,
    )
    if not result["valid"]:
        details = "; ".join(
            f"{item['code']}: {item['message']}" for item in result["errors"]
        )
        raise CatalogError(
            f"Initial-model acceptance gate blocked Class3D: {details}"
        )
    return result


def find_initial_model_acceptance(
    settings: Settings,
    project: Path,
    *,
    reference_volume: str,
    classification_particles: str,
    mask_diameter_A: float,
    alignment: bool = True,
) -> dict[str, Any]:
    try:
        directory = _acceptance_directory(settings, project, create=False)
    except Exception as exc:
        raise CatalogError(
            "Initial-model acceptance gate blocked Class3D: acceptance_record_missing: "
            "no InitialModel acceptance records are available for this Class3D job."
        ) from exc
    candidates = sorted(directory.glob("acceptance-*.json")) + sorted(
        directory.glob("external-*.json")
    )
    if not candidates:
        raise CatalogError(
            "Initial-model acceptance gate blocked Class3D: acceptance_record_missing: "
            "no InitialModel acceptance records are available for this Class3D job."
        )
    hash_cache: dict[Path, str] = {}
    failures: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        relative = candidate.relative_to(project).as_posix()
        try:
            result = validate_initial_model_acceptance(
                settings,
                str(project),
                relative,
                reference_volume=reference_volume,
                classification_particles=classification_particles,
                mask_diameter_A=mask_diameter_A,
                alignment=alignment,
                _hash_cache=hash_cache,
            )
        except (CatalogError, ConfigurationError, OSError) as exc:
            failures.append(
                {"record": relative, "errors": [_issue("invalid_record", relative, str(exc))]}
            )
            continue
        if result["valid"]:
            matches.append(result)
        else:
            failures.append({"record": relative, "errors": result["errors"]})
    if matches:
        selected = matches[0]
        selected["matching_record_count"] = len(matches)
        return selected
    codes = sorted(
        {
            issue.get("code", "invalid_record")
            for failure in failures
            for issue in failure["errors"]
        }
    )
    raise CatalogError(
        "Initial-model acceptance gate blocked Class3D: no valid acceptance record "
        "matches the registered job"
        + (f"; validation codes: {', '.join(codes)}" if codes else ".")
    )


def registered_class3d_inputs(
    settings: Settings,
    project: Path,
    job: str,
) -> dict[str, Any] | None:
    job_star_relative = f"{job.rstrip('/')}/job.star"
    raw = project / Path(job_star_relative)
    if not raw.exists():
        if PurePosixPath(job).parts[0] == "Class3D":
            raise CatalogError(f"Registered Class3D job is missing job.star: {job}")
        return None
    _, job_star = _resolve_regular_file(
        settings,
        project,
        job_star_relative,
        role="registered_job_star",
        suffix=".star",
    )
    try:
        job_block = parse_star_block(job_star, "job")
    except (OSError, StarFormatError) as exc:
        raise CatalogError(f"Could not parse registered job metadata for {job}: {exc}") from exc
    label = job_block.get("rlnJobTypeLabel") if isinstance(job_block, Mapping) else None
    if label != "relion.class3d":
        if PurePosixPath(job).parts[0] == "Class3D":
            raise CatalogError(f"Class3D path does not contain a relion.class3d job: {job}")
        return None
    options = _job_options(job_star)
    reference = options.get("fn_ref")
    particles = options.get("fn_img")
    diameter = options.get("particle_diameter")
    if not reference or not particles or diameter is None:
        raise CatalogError(f"Registered Class3D job has incomplete gate inputs: {job}")
    try:
        mask = float(diameter)
    except ValueError as exc:
        raise CatalogError(f"Registered Class3D particle_diameter is invalid: {diameter!r}") from exc
    if not math.isfinite(mask) or mask <= 0:
        raise CatalogError(f"Registered Class3D particle_diameter is invalid: {diameter!r}")
    return {
        "job": job,
        "reference_volume": reference,
        "classification_particles": particles,
        "mask_diameter_A": mask,
        "alignment": options.get("dont_skip_align", "Yes") != "No",
    }
