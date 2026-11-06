from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath

# Each rule is (job_types, filename_pattern). ``job_types`` restricts a rule to
# paths whose leading RELION pipeline job-type directory (e.g. "Class3D",
# "Refine3D") is one of the given names; ``None`` means the filename alone is
# unambiguous regardless of job type. Filenames such as ``particles.star`` or
# ``run_data.star`` are reused verbatim by several unrelated job types, so the
# job-type directory is required to disambiguate them -- matching on filename
# alone conflates, for example, an InitialModel job's `run_class001.mrc` with
# a genuinely refined Refine3D/PostProcess map, or a Class3D job's
# `run_data.star` with a Refine3D job's.
PATTERNS: dict[str, tuple[tuple[frozenset[str] | None, str], ...]] = {
    "imported_movies": ((frozenset({"Import"}), r"(?:movies|micrographs)\.star$"),),
    "motion_corrected_micrographs": (
        (frozenset({"MotionCorr"}), r"corrected_micrographs\.star$"),
        (frozenset({"MotionCorr"}), r"micrographs\.star$"),
    ),
    "ctf_estimates": ((frozenset({"CtfFind"}), r"micrographs_ctf\.star$"),),
    "selected_micrographs": (
        (frozenset({"Select"}), r"micrographs_split\d*\.star$"),
        (frozenset({"Select"}), r"micrographs\.star$"),
    ),
    "picked_particles": (
        (frozenset({"AutoPick"}), r"coords_suffix_.*\.star$"),
        (frozenset({"AutoPick"}), r"autopick.*\.star$"),
        (frozenset({"ManualPick"}), r"manualpick.*\.star$"),
    ),
    "extracted_particles": ((frozenset({"Extract"}), r"particles\.star$"),),
    "selected_particles": ((frozenset({"Select"}), r"particles\.star$"),),
    "classified_particles": (
        (frozenset({"Class2D", "Class3D"}), r"run(?:_it\d+)?_data\.star$"),
    ),
    "two_d_classes": (
        (frozenset({"Class2D"}), r"run(?:_it\d+)?_model\.star$"),
        (frozenset({"Class2D"}), r"classes\.mrcs$"),
    ),
    "initial_volume": (
        (frozenset({"InitialModel"}), r"run(?:_it\d+)?_class\d+\.mrc$"),
    ),
    "refined_volume": (
        (frozenset({"PostProcess"}), r"postprocess\.mrc$"),
        (frozenset({"Refine3D"}), r"run(?:_it\d+)?_class\d+\.mrc$"),
    ),
    "refine3d_particles": (
        (frozenset({"Refine3D"}), r"run_data\.star$"),
        (frozenset({"Refine3D"}), r"particles\.star$"),
    ),
    "half_maps": (
        (None, r"half1_.*unfil\.mrc$"),
        (None, r"half2_.*unfil\.mrc$"),
    ),
    "mask": ((frozenset({"MaskCreate"}), r"mask\.mrc$"),),
    "fsc_curve": ((frozenset({"PostProcess"}), r"postprocess\.star$"),),
    "ctf_refined_particles": (
        (frozenset({"CtfRefine"}), r"particles_ctf_refine\.star$"),
        (frozenset({"CtfRefine"}), r"particles\.star$"),
    ),
    "polished_particles": (
        (frozenset({"Polish"}), r"shiny\.star$"),
        (frozenset({"Polish"}), r"particles\.star$"),
    ),
    "polishing_parameters": (
        (frozenset({"Polish"}), r"^opt_params.*$"),
        (frozenset({"Polish"}), r"^params.*$"),
    ),
    "local_resolution_map": ((frozenset({"LocalRes"}), r"locres.*\.mrc$"),),
    "topaz_model": ((frozenset({"AutoPick"}), r"model.*\.(?:sav|pt|pth)$"),),
}


def _job_type(path: str) -> str | None:
    """Return the leading RELION pipeline job-type directory, if present.

    A bare filename with no directory component (e.g. a caller-supplied
    filename fragment with no pipeline path) has no discoverable job type, so
    job-type-restricted rules fall back to matching on filename alone rather
    than being rejected outright.
    """
    parts = PurePosixPath(path).parts
    if len(parts) < 2:
        return None
    return parts[0]


def classify_output(path: str) -> list[str]:
    filename = PurePosixPath(path).name
    job_type = _job_type(path)
    job_type_fold = job_type.casefold() if job_type is not None else None
    result = []
    for result_type, rules in PATTERNS.items():
        for job_types, pattern in rules:
            if (
                job_types is not None
                and job_type_fold is not None
                and job_type_fold not in {name.casefold() for name in job_types}
            ):
                continue
            if re.search(pattern, filename, flags=re.IGNORECASE):
                result.append(result_type)
                break
    return result


def classify_outputs(paths: Iterable[str]) -> list[dict[str, object]]:
    return [{"path": path, "result_types": classify_output(path)} for path in paths]


def best_output(paths: Iterable[str], expected_types: Iterable[str]) -> dict[str, object] | None:
    wanted = list(expected_types)
    candidates = classify_outputs(paths)
    for result_type in wanted:
        for candidate in candidates:
            if result_type in candidate["result_types"]:
                return candidate
    return None
