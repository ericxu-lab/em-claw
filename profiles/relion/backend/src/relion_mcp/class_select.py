from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import TextIO

from .star import StarFormatError, _tokens, parse_star_block

PARTICLE_NODE_TYPE = "ParticleGroupMetadata.star.relion"
CLASS_AVERAGES_NODE_TYPE = "Image2DGroupMetadata.star.relion.classaverages"
_OUTPUT_JOB_PATTERN = re.compile(r"External/job[0-9]+/?")


class ClassSelectionError(ValueError):
    """Raised when a GUI-equivalent class selection cannot be materialized safely."""


def _normalise_classes(raw: str) -> tuple[int, ...]:
    if not isinstance(raw, str) or not raw.strip():
        raise ClassSelectionError("--classes must contain one or more comma-separated integers.")
    values: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not re.fullmatch(r"[1-9][0-9]*", item):
            raise ClassSelectionError(f"Invalid selected class number: {item!r}.")
        values.append(int(item))
    if len(values) != len(set(values)):
        raise ClassSelectionError("Selected class numbers may not contain duplicates.")
    if len(values) > 256:
        raise ClassSelectionError("At most 256 classes may be selected in one job.")
    return tuple(sorted(values))


def _resolve_project_input(project: Path, raw: str, field: str) -> tuple[str, Path]:
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ClassSelectionError(f"{field} must be a project-relative path.")
    candidate = project.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ClassSelectionError(f"{field} is unavailable: {candidate}.") from exc
    if resolved == project or project not in resolved.parents or not resolved.is_file():
        raise ClassSelectionError(f"{field} must resolve to a file inside the project.")
    return relative.as_posix(), resolved


def _resolve_output_dir(project: Path, raw: str) -> tuple[str, Path]:
    relative = PurePosixPath(raw)
    normalized = relative.as_posix().rstrip("/") + "/"
    if not _OUTPUT_JOB_PATTERN.fullmatch(normalized):
        raise ClassSelectionError("--o must name a RELION External/jobNNN/ directory.")
    candidate = project.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise ClassSelectionError("The output job directory may not be a symbolic link.")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ClassSelectionError(f"The output job directory is unavailable: {candidate}.") from exc
    if project not in resolved.parents or not resolved.is_dir():
        raise ClassSelectionError("The output job directory must exist inside the project.")
    return normalized, resolved


def _star_token(value: str) -> str:
    if not value:
        return "''"
    if not any(character.isspace() for character in value) and not value.startswith("#"):
        return value
    if "\n" in value or "\r" in value:
        raise StarFormatError("Multiline STAR values are not supported in class selection rows.")
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    raise StarFormatError("A STAR value containing both quote styles cannot be serialized safely.")


def _write_row(handle: TextIO, row: list[str]) -> None:
    handle.write(" ".join(_star_token(value) for value in row) + "\n")


def _filter_particle_star(
    source: Path,
    destination: Path,
    selected_classes: tuple[int, ...],
) -> dict[str, object]:
    selected_set = set(selected_classes)
    found_block = False
    found_loop = False
    columns: list[str] = []
    class_index: int | None = None
    image_index: int | None = None
    class_counts: Counter[int] = Counter()
    selected_counts: Counter[int] = Counter()
    seen_images: set[str] = set()
    duplicate_examples: list[str] = []
    row_buffer: list[str] = []
    state = "prefix"

    with source.open("r", encoding="utf-8", errors="replace") as input_handle, destination.open(
        "w", encoding="utf-8"
    ) as output_handle:
        for line in input_handle:
            stripped = line.strip()
            if state == "prefix":
                output_handle.write(line)
                if stripped == "data_particles":
                    found_block = True
                    state = "before_loop"
                continue

            if state == "before_loop":
                output_handle.write(line)
                if stripped.startswith("data_") and stripped != "data_particles":
                    raise StarFormatError(f"Missing particle loop in data_particles block of {source}")
                if stripped == "loop_":
                    found_loop = True
                    state = "columns"
                continue

            if state == "columns":
                if not stripped or stripped.startswith("#"):
                    output_handle.write(line)
                    continue
                if stripped.startswith("_"):
                    columns.append(stripped.split()[0][1:])
                    output_handle.write(line)
                    continue
                if not columns:
                    raise StarFormatError(f"Particle loop in {source} has no columns")
                required = {"rlnImageName", "rlnClassNumber"}
                missing = sorted(required - set(columns))
                if missing:
                    raise StarFormatError(
                        f"data_particles in {source} is missing required columns: {', '.join(missing)}"
                    )
                class_index = columns.index("rlnClassNumber")
                image_index = columns.index("rlnImageName")
                state = "rows"

            if state == "rows":
                if not stripped:
                    if row_buffer:
                        raise StarFormatError(f"Incomplete particle row in {source}")
                    output_handle.write(line)
                    state = "suffix"
                    continue
                if stripped.startswith("#"):
                    if row_buffer:
                        raise StarFormatError(f"Comment interrupts a particle row in {source}")
                    output_handle.write(line)
                    continue
                if stripped.startswith(("data_", "loop_", "_")):
                    if row_buffer:
                        raise StarFormatError(f"Incomplete particle row in {source}")
                    output_handle.write(line)
                    state = "suffix"
                    continue
                row_buffer.extend(_tokens(line))
                width = len(columns)
                while len(row_buffer) >= width:
                    row = row_buffer[:width]
                    del row_buffer[:width]
                    assert class_index is not None and image_index is not None
                    try:
                        class_number = int(row[class_index])
                    except ValueError as exc:
                        raise StarFormatError(
                            f"Invalid rlnClassNumber {row[class_index]!r} in {source}"
                        ) from exc
                    if class_number < 1:
                        raise StarFormatError(f"rlnClassNumber must be positive in {source}")
                    image_name = row[image_index]
                    if image_name in seen_images:
                        if len(duplicate_examples) < 10 and image_name not in duplicate_examples:
                            duplicate_examples.append(image_name)
                    else:
                        seen_images.add(image_name)
                    class_counts[class_number] += 1
                    if class_number in selected_set:
                        selected_counts[class_number] += 1
                        _write_row(output_handle, row)
                continue

            output_handle.write(line)

    if not found_block:
        raise StarFormatError(f"Missing data_particles block in {source}")
    if not found_loop:
        raise StarFormatError(f"Missing particle loop in data_particles block of {source}")
    if row_buffer:
        raise StarFormatError(f"Incomplete particle row in {source}")
    if duplicate_examples:
        raise StarFormatError(
            "Input particle STAR contains duplicate rlnImageName values (examples: "
            + ", ".join(duplicate_examples)
            + ")."
        )
    missing_classes = [value for value in selected_classes if selected_counts[value] == 0]
    if missing_classes:
        raise StarFormatError(
            f"Requested classes are absent from the input particle STAR: {missing_classes}."
        )
    return {
        "source_particle_count": sum(class_counts.values()),
        "source_class_counts": dict(sorted(class_counts.items())),
        "selected_class_counts": {
            value: selected_counts[value] for value in selected_classes
        },
        "selected_particle_count": sum(selected_counts.values()),
        "available_class_count": max(class_counts),
    }


def _write_backup_selection(
    destination: Path,
    selected_classes: tuple[int, ...],
    class_count: int,
) -> None:
    selected = set(selected_classes)
    with destination.open("w", encoding="utf-8") as handle:
        handle.write("# version 50001\n\ndata_\n\nloop_\n_rlnSelected #1\n")
        for class_number in range(1, class_count + 1):
            handle.write(f"{1 if class_number in selected else 0:12d}\n")
        handle.write("\n")


def _write_selected_class_averages(
    source_model: Path,
    destination: Path,
    selected_classes: tuple[int, ...],
) -> tuple[bool, int]:
    general = parse_star_block(source_model, "model_general") or {}
    dimensionality = str(general.get("rlnReferenceDimensionality", ""))
    classes = parse_star_block(source_model, "model_classes")
    if not classes:
        raise StarFormatError(f"Missing data_model_classes block in {source_model}")
    columns = list(classes)
    if "rlnReferenceImage" not in classes:
        raise StarFormatError(f"data_model_classes in {source_model} lacks rlnReferenceImage")
    references = classes["rlnReferenceImage"]
    if isinstance(references, str):
        references = [references]
    selected_set = set(selected_classes)
    selected_rows: list[int] = []
    available: set[int] = set()
    for index, reference in enumerate(references):
        match = re.match(r"0*([1-9][0-9]*)@", str(reference))
        class_number = int(match.group(1)) if match else index + 1
        available.add(class_number)
        if class_number in selected_set:
            selected_rows.append(index)
    missing = sorted(selected_set - available)
    if missing:
        raise StarFormatError(f"Requested classes are absent from {source_model}: {missing}")
    class_count = max(available)
    if dimensionality and dimensionality != "2":
        return False, class_count
    if not dimensionality and not all(
        str(references[index]).lower().endswith(".mrcs") for index in selected_rows
    ):
        return False, class_count

    with destination.open("w", encoding="utf-8") as handle:
        handle.write("# version 50001\n\ndata_\n\nloop_\n")
        for column_index, column in enumerate(columns, start=1):
            handle.write(f"_{column} #{column_index}\n")
        for row_index in selected_rows:
            row: list[str] = []
            for column in columns:
                values = classes[column]
                if isinstance(values, str):
                    values = [values]
                if row_index >= len(values):
                    raise StarFormatError(
                        f"Column {column} has fewer rows than rlnReferenceImage in {source_model}"
                    )
                row.append(str(values[row_index]))
            _write_row(handle, row)
        handle.write("\n")
    return True, class_count


def _write_output_nodes(output_dir_relative: str, destination: Path, include_classes: bool) -> None:
    prefix = output_dir_relative.rstrip("/") + "/"
    rows = [(prefix + "particles.star", PARTICLE_NODE_TYPE)]
    if include_classes:
        rows.append((prefix + "class_averages.star", CLASS_AVERAGES_NODE_TYPE))
    with destination.open("w", encoding="utf-8") as handle:
        handle.write(
            "# version 50001\n\ndata_output_nodes\n\nloop_\n"
            "_rlnPipeLineNodeName #1\n_rlnPipeLineNodeTypeLabel #2\n"
        )
        for row in rows:
            _write_row(handle, list(row))
        handle.write("\n")


def materialize_gui_equivalent_selection(
    *,
    project_dir: Path,
    output_dir: str,
    particles_star: str,
    selected_classes: tuple[int, ...],
    expected_particles: int | None = None,
    class_model_star: str | None = None,
) -> dict[str, object]:
    project = project_dir.resolve(strict=True)
    if not project.is_dir():
        raise ClassSelectionError("project_dir must be an existing directory.")
    output_relative, output_path = _resolve_output_dir(project, output_dir)
    particles_relative, particles_path = _resolve_project_input(
        project, particles_star, "--in_parts"
    )
    model_relative: str | None = None
    model_path: Path | None = None
    if class_model_star:
        model_relative, model_path = _resolve_project_input(
            project, class_model_star, "--class-model"
        )

    protected_outputs = (
        "particles.star",
        "backup_selection.star",
        "class_averages.star",
        "selection_provenance.json",
        "RELION_OUTPUT_NODES.star",
        "RELION_JOB_EXIT_SUCCESS",
    )
    existing = [name for name in protected_outputs if (output_path / name).exists()]
    if existing:
        raise ClassSelectionError(
            f"Refusing to overwrite existing class-selection outputs: {existing}."
        )

    temporary_paths: list[Path] = []

    def temporary_path(name: str) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{name}-", suffix=".tmp", dir=output_path)
        os.close(descriptor)
        path = Path(raw_path)
        temporary_paths.append(path)
        return path

    particle_temp = temporary_path("particles")
    backup_temp = temporary_path("backup")
    class_temp = temporary_path("classes") if model_path is not None else None
    nodes_temp = temporary_path("nodes")
    provenance_temp = temporary_path("provenance")
    try:
        summary = _filter_particle_star(particles_path, particle_temp, selected_classes)
        selected_particle_count = int(summary["selected_particle_count"])
        if expected_particles is not None and selected_particle_count != expected_particles:
            raise ClassSelectionError(
                "Selected particle count changed between planning and execution: "
                f"expected {expected_particles}, observed {selected_particle_count}."
            )
        wrote_class_averages = False
        backup_class_count = int(summary["available_class_count"])
        if model_path is not None and class_temp is not None:
            wrote_class_averages, backup_class_count = _write_selected_class_averages(
                model_path,
                class_temp,
                selected_classes,
            )
        _write_backup_selection(
            backup_temp,
            selected_classes,
            backup_class_count,
        )
        _write_output_nodes(output_relative, nodes_temp, wrote_class_averages)
        provenance = {
            "operation": "gui_equivalent_particle_class_selection",
            "execution_model": "relion_external_gui_equivalent",
            "particles_star_input": particles_relative,
            "class_model_star_input": model_relative,
            "selected_class_numbers": list(selected_classes),
            **summary,
            "class_averages_written": wrote_class_averages,
            "preserves_source_particle_order": True,
        }
        provenance_temp.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")

        particle_temp.replace(output_path / "particles.star")
        backup_temp.replace(output_path / "backup_selection.star")
        if wrote_class_averages and class_temp is not None:
            class_temp.replace(output_path / "class_averages.star")
        nodes_temp.replace(output_path / "RELION_OUTPUT_NODES.star")
        provenance_temp.replace(output_path / "selection_provenance.json")
        (output_path / "RELION_JOB_EXIT_SUCCESS").touch(exist_ok=False)
        return provenance
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize an exact class set with RELION GUI-equivalent STAR semantics."
    )
    parser.add_argument("--o", required=True, help="RELION External/jobNNN/ output directory")
    parser.add_argument("--in_parts", required=True, help="Project-relative input particle STAR")
    parser.add_argument("--classes", required=True, help="Comma-separated one-based class IDs")
    parser.add_argument("--expected-particles", type=int, default=None)
    parser.add_argument("--class-model", default=None, help="Optional project-relative model STAR")
    parser.add_argument("--j", default="1", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    output_candidate: Path | None = None
    try:
        project = Path.cwd().resolve(strict=True)
        _, output_candidate = _resolve_output_dir(project, args.o)
        selected = _normalise_classes(args.classes)
        if args.expected_particles is not None and args.expected_particles < 1:
            raise ClassSelectionError("--expected-particles must be positive when provided.")
        result = materialize_gui_equivalent_selection(
            project_dir=project,
            output_dir=args.o,
            particles_star=args.in_parts,
            selected_classes=selected,
            expected_particles=args.expected_particles,
            class_model_star=args.class_model,
        )
    except (ClassSelectionError, OSError, StarFormatError, ValueError) as exc:
        try:
            if (
                output_candidate is not None
                and output_candidate.is_dir()
                and not output_candidate.is_symlink()
                and not (output_candidate / "RELION_JOB_EXIT_SUCCESS").exists()
            ):
                error_path = output_candidate / "selection_error.txt"
                failure_path = output_candidate / "RELION_JOB_EXIT_FAILURE"
                writable_targets = (error_path, failure_path)
                if all(
                    not path.is_symlink() and (not path.exists() or path.is_file())
                    for path in writable_targets
                ):
                    error_path.write_text(str(exc) + "\n", encoding="utf-8")
                    failure_path.touch(exist_ok=True)
        except OSError:
            pass
        parser.exit(1, f"class selection failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
