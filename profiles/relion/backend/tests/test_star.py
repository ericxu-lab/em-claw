from pathlib import Path

import pytest

from relion_mcp._relion_metadata_types import (
    RELION_METADATA_CANONICAL_LABELS,
    RELION_METADATA_LABEL_TYPES,
)
from relion_mcp.star import (
    StarFormatError,
    job_output_edges,
    parse_star_block,
    particle_star_row_summary,
    particle_star_summary,
    pipeline_processes,
    relion_metadata_loop_digest,
)


def test_pipeline_parser(tmp_path: Path):
    star = tmp_path / "default_pipeline.star"
    star.write_text(
        """data_pipeline_processes

loop_
_rlnPipeLineProcessName #1
_rlnPipeLineProcessStatusLabel #2
Import/job001/ Scheduled
MotionCorr/job002/ Succeeded

""",
        encoding="utf-8",
    )
    assert pipeline_processes(star)[1] == {"name": "MotionCorr/job002/", "status": "Succeeded"}


def test_output_edges(tmp_path: Path):
    star = tmp_path / "job_pipeline.star"
    star.write_text(
        """data_pipeline_output_edges

loop_
_rlnPipeLineEdgeFromProcess #1
_rlnPipeLineEdgeToNode #2
Refine3D/job019/ Refine3D/job019/run_half1_class001_unfil.mrc
Refine3D/job019/ Refine3D/job019/run_data.star

""",
        encoding="utf-8",
    )
    assert job_output_edges(star) == [
        "Refine3D/job019/run_half1_class001_unfil.mrc",
        "Refine3D/job019/run_data.star",
    ]


def test_particle_star_summary_handles_non_contiguous_classes(tmp_path: Path):
    star = tmp_path / "particles.star"
    star.write_text(
        """data_optics

loop_
_rlnOpticsGroup #1
_rlnVoltage #2
1 200.0
2 300.0

data_particles

loop_
_rlnImageName #1
_rlnClassNumber #2
_rlnOpticsGroup #3
000001@particles.mrcs 1 1
000002@particles.mrcs 3 1
000003@particles.mrcs 5 2
000004@particles.mrcs 1 2

""",
        encoding="utf-8",
    )

    summary = particle_star_summary(star)

    assert summary.particle_count == 4
    assert summary.class_counts == {1: 2, 3: 1, 5: 1}
    assert summary.optics_group_counts == {"1": 2, "2": 2}
    assert summary.class_optics_group_counts == {
        1: {"1": 1, "2": 1},
        3: {"1": 1},
        5: {"2": 1},
    }
    assert summary.duplicate_image_name_count == 0
    assert parse_star_block(star, "optics") == {
        "rlnOpticsGroup": ["1", "2"],
        "rlnVoltage": ["200.0", "300.0"],
    }


def test_particle_star_summary_reports_duplicate_images(tmp_path: Path):
    star = tmp_path / "duplicates.star"
    star.write_text(
        """data_particles

loop_
_rlnImageName #1
_rlnClassNumber #2
000001@particles.mrcs 1
000001@particles.mrcs 3

""",
        encoding="utf-8",
    )

    summary = particle_star_summary(star)

    assert summary.duplicate_image_name_count == 1
    assert summary.duplicate_image_name_examples == ("000001@particles.mrcs",)


def test_particle_star_summary_requires_class_column(tmp_path: Path):
    star = tmp_path / "missing-class.star"
    star.write_text(
        """data_particles

loop_
_rlnImageName #1
000001@particles.mrcs

""",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="rlnClassNumber"):
        particle_star_summary(star)


def test_particle_star_row_summary_accepts_fresh_extraction_without_classes(
    tmp_path: Path,
):
    star = tmp_path / "extracted.star"
    star.write_text(
        """data_particles

loop_
_rlnImageName #1
_rlnOpticsGroup #2
000001@particles.mrcs 1
000002@particles.mrcs 2

""",
        encoding="utf-8",
    )

    summary = particle_star_row_summary(star)

    assert summary.particle_count == 2
    assert summary.optics_group_counts == {"1": 1, "2": 1}
    assert summary.duplicate_image_name_count == 0

    reordered = tmp_path / "extracted-reordered.star"
    reordered.write_text(
        """data_particles

loop_
_rlnOpticsGroup #1
_rlnImageName #2
2 000002@particles.mrcs
1 000001@particles.mrcs

""",
        encoding="utf-8",
    )
    reordered_summary = particle_star_row_summary(reordered)
    assert (
        reordered_summary.row_metadata_multiset_digest
        == summary.row_metadata_multiset_digest
    )


def test_particle_star_row_summary_accepts_unnamed_block_only_when_opted_in(
    tmp_path: Path,
):
    particle_loop = """loop_
_rlnImageName #1
_rlnOpticsGroup #2
000001@particles.mrcs 1
000002@particles.mrcs 2

"""
    named = tmp_path / "named.star"
    unnamed = tmp_path / "unnamed.star"
    named.write_text("data_particles\n\n" + particle_loop, encoding="utf-8")
    unnamed.write_text("data_\n\n" + particle_loop, encoding="utf-8")

    with pytest.raises(StarFormatError, match="Missing data_particles"):
        particle_star_row_summary(unnamed)

    assert particle_star_row_summary(
        unnamed,
        allow_unnamed_particle_block=True,
    ) == particle_star_row_summary(named)


@pytest.mark.parametrize(
    "contents",
    [
        "data_other\n\ndata_\n\nloop_\n_rlnImageName #1\n000001@particles.mrcs\n\n",
        "data_\n\nloop_\n_rlnImageName #1\n000001@particles.mrcs\n\ndata_other\n",
    ],
)
def test_particle_star_row_summary_rejects_other_blocks_around_unnamed_sidecar(
    tmp_path: Path,
    contents: str,
):
    star = tmp_path / "removed.star"
    star.write_text(contents, encoding="utf-8")

    with pytest.raises(StarFormatError, match="Unexpected"):
        particle_star_row_summary(star, allow_unnamed_particle_block=True)


def test_particle_row_digest_normalizes_relion_numeric_presentation(
    tmp_path: Path,
):
    def write_particle(path: Path, coordinate: str, origin: str) -> None:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnCoordinateX #2\n"
            "_rlnOriginXAngst #3\n"
            f"000001@particles.mrcs {coordinate} {origin}\n",
            encoding="utf-8",
        )

    compact = tmp_path / "compact.star"
    relion_formatted = tmp_path / "relion-formatted.star"
    mutated = tmp_path / "mutated.star"
    write_particle(compact, "100", "0")
    write_particle(relion_formatted, "100.000000", "0.000000")
    write_particle(mutated, "101.000000", "0.000000")

    compact_digest = particle_star_row_summary(compact).row_metadata_multiset_digest
    assert (
        particle_star_row_summary(relion_formatted).row_metadata_multiset_digest
        == compact_digest
    )
    assert particle_star_row_summary(mutated).row_metadata_multiset_digest != compact_digest


def test_relion_5_metadata_type_snapshot_is_complete():
    canonical_types = {
        label: label_type
        for label, label_type in RELION_METADATA_LABEL_TYPES.items()
        if label not in RELION_METADATA_CANONICAL_LABELS
    }

    assert len(canonical_types) == 611
    assert {
        label_type: sum(value == label_type for value in canonical_types.values())
        for label_type in (
            "string",
            "int",
            "bool",
            "double",
            "double_vector",
            "int_vector",
            "unknown",
        )
    } == {
        "string": 123,
        "int": 104,
        "bool": 53,
        "double": 313,
        "double_vector": 16,
        "int_vector": 1,
        "unknown": 1,
    }
    assert RELION_METADATA_LABEL_TYPES["rlnMicrographName"] == "string"
    assert RELION_METADATA_LABEL_TYPES["rlnOddZernike"] == "double_vector"


def test_particle_row_digest_keeps_numeric_looking_strings_exact(tmp_path: Path):
    def write_particle(path: Path, micrograph_name: str) -> None:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnMicrographName #2\n"
            f"000001@particles.mrcs {micrograph_name}\n",
            encoding="utf-8",
        )

    integer_spelling = tmp_path / "integer-string.star"
    decimal_spelling = tmp_path / "decimal-string.star"
    write_particle(integer_spelling, "100")
    write_particle(decimal_spelling, "100.000000")

    assert (
        particle_star_row_summary(integer_spelling).row_metadata_multiset_digest
        != particle_star_row_summary(decimal_spelling).row_metadata_multiset_digest
    )


@pytest.mark.parametrize(
    ("left_value", "right_value"),
    [
        ("mic#one", "mic#two"),
        (r"mic\one", "micone"),
    ],
)
def test_particle_row_digest_preserves_relion_unquoted_string_characters(
    tmp_path: Path,
    left_value: str,
    right_value: str,
):
    def digest(path: Path, value: str) -> str:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnMicrographName #2\n"
            f"000001@particles.mrcs {value}\n",
            encoding="utf-8",
        )
        return particle_star_row_summary(path).row_metadata_multiset_digest

    assert digest(tmp_path / "left.star", left_value) != digest(
        tmp_path / "right.star",
        right_value,
    )


def test_particle_row_digest_matches_relion_simplified_quoted_whitespace(
    tmp_path: Path,
):
    def digest(path: Path, value: str) -> str:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnMicrographName #2\n"
            f"000001@particles.mrcs {value}\n",
            encoding="utf-8",
        )
        return particle_star_row_summary(path).row_metadata_multiset_digest

    assert digest(tmp_path / "input.star", '"mic  one"') == digest(
        tmp_path / "native-output.star",
        '"mic one"',
    )


def test_particle_row_digest_drops_relion_bel_before_tokenizing(tmp_path: Path):
    def digest(path: Path, value: str) -> str:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnGroupName #2\n"
            f"000001@particles.mrcs {value}\n",
            encoding="utf-8",
        )
        return particle_star_row_summary(path).row_metadata_multiset_digest

    assert digest(tmp_path / "bel.star", '"mic\x07"one"') == digest(
        tmp_path / "plain.star",
        '"mic"one"',
    )


def test_particle_rows_preserve_empty_quoted_optional_string(tmp_path: Path):
    star = tmp_path / "empty-string.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnGroupName #2\n"
        '000001@particles.mrcs ""\n',
        encoding="utf-8",
    )

    assert particle_star_row_summary(star).particle_count == 1


@pytest.mark.parametrize(
    "value",
    ['"#comment-shaped"', "'mic\" one'"],
    ids=["leading-hash", "embedded-quote-before-space"],
)
def test_particle_rows_reject_strings_native_writer_cannot_round_trip(
    tmp_path: Path,
    value: str,
):
    star = tmp_path / "unsafe-string-writer.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnGroupName #2\n"
        f"000001@particles.mrcs {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="string writer cannot safely round-trip"):
        particle_star_row_summary(star)


def test_particle_rows_accept_literal_quote_inside_unquoted_string(tmp_path: Path):
    star = tmp_path / "literal-quote.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnGroupName #2\n"
        '000001@particles.mrcs abc"def\n',
        encoding="utf-8",
    )

    assert particle_star_row_summary(star).particle_count == 1


def test_particle_row_digest_normalizes_relion_vector_writes(tmp_path: Path):
    def write_particle(
        path: Path,
        double_vector: str,
        int_vector: str,
    ) -> None:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnOddZernike #2\n"
            "_rlnTomoVisibleFrames #3\n"
            f"000001@particles.mrcs {double_vector} {int_vector}\n",
            encoding="utf-8",
        )

    verbose = tmp_path / "verbose-vectors.star"
    native = tmp_path / "native-vectors.star"
    changed = tmp_path / "changed-vectors.star"
    write_particle(verbose, "[1.00000000000,2.50000000000]", "[+01,002]")
    write_particle(native, "[1,2.5]", "[1,2]")
    write_particle(changed, "[1,2.75]", "[1,2]")

    verbose_digest = particle_star_row_summary(verbose).row_metadata_multiset_digest
    assert particle_star_row_summary(native).row_metadata_multiset_digest == verbose_digest
    assert particle_star_row_summary(changed).row_metadata_multiset_digest != verbose_digest


def test_particle_row_digest_canonicalizes_relion_legacy_label_alias(tmp_path: Path):
    def write_particle(path: Path, label: str, value: str) -> None:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            f"_{label} #2\n"
            f"000001@particles.mrcs {value}\n",
            encoding="utf-8",
        )

    legacy = tmp_path / "legacy.star"
    canonical = tmp_path / "canonical.star"
    write_particle(legacy, "rlnKullbackLeibnerDivergence", "1.25")
    write_particle(canonical, "rlnKullbackLeiblerDivergence", "1.250000")

    assert (
        particle_star_row_summary(legacy).row_metadata_multiset_digest
        == particle_star_row_summary(canonical).row_metadata_multiset_digest
    )


def test_particle_rows_reject_logically_duplicate_relion_aliases(tmp_path: Path):
    star = tmp_path / "duplicate-alias.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnKullbackLeiblerDivergence #2\n"
        "_rlnKullbackLeibnerDivergence #3\n"
        "000001@particles.mrcs 1.0 1.0\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="duplicate column labels"):
        particle_star_row_summary(star)


@pytest.mark.parametrize(
    ("label", "value", "label_type"),
    [
        ("rlnClassNumber", "1.5", "integer"),
        ("rlnEnabled", "2", "boolean"),
        ("rlnOddZernike", "[1,not-a-number]", "double-vector"),
        ("rlnTomoVisibleFrames", "[1,2.5]", "integer-vector"),
        ("rlnCoordinateX", "١", "double"),
        ("rlnClassNumber", "١", "integer"),
        ("rlnOddZernike", "[١]", "double-vector"),
        ("rlnTomoVisibleFrames", "[١]", "integer-vector"),
        ("rlnOddZernike", "[\u00a01]", "double-vector"),
        ("rlnTomoVisibleFrames", "[\u00a01]", "integer-vector"),
    ],
)
def test_particle_rows_reject_invalid_recognized_relion_types(
    tmp_path: Path,
    label: str,
    value: str,
    label_type: str,
):
    star = tmp_path / f"invalid-{label}.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        f"_{label} #2\n"
        f"000001@particles.mrcs {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match=label_type):
        particle_star_row_summary(star)


def test_optics_digest_rejects_non_ascii_numeric_digits(tmp_path: Path):
    star = tmp_path / "unicode-digit-optics.star"
    star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        "_rlnVoltage #2\n"
        "1 ١\n\n",
        encoding="utf-8",
    )
    optics = parse_star_block(star, "optics", strict_loops=True)
    assert optics is not None

    with pytest.raises(StarFormatError, match="double metadata.*rlnVoltage"):
        relion_metadata_loop_digest(optics, block_name="optics")


def test_particle_rows_wrap_python_integer_digit_limit(tmp_path: Path):
    star = tmp_path / "oversized-integer-text.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnParticleId #2\n"
        f"000001@particles.mrcs {'9' * 5000}\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="Invalid RELION integer metadata"):
        particle_star_row_summary(star)


def test_particle_rows_accept_only_exact_empty_relion_vectors(tmp_path: Path):
    exact = tmp_path / "exact-empty-vectors.star"
    exact.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnOddZernike #2\n"
        "_rlnTomoVisibleFrames #3\n"
        "000001@particles.mrcs [] []\n",
        encoding="utf-8",
    )

    assert particle_star_row_summary(exact).particle_count == 1

    for label in ("rlnOddZernike", "rlnTomoVisibleFrames"):
        spaced = tmp_path / f"spaced-empty-{label}.star"
        spaced.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            f"_{label} #2\n"
            '000001@particles.mrcs "[ ]"\n',
            encoding="utf-8",
        )
        with pytest.raises(StarFormatError, match="vector"):
            particle_star_row_summary(spaced)


@pytest.mark.parametrize(
    ("label", "marker"),
    [
        ("rlnAngleRot", "."),
        ("rlnClassNumber", "?"),
        ("rlnEnabled", "."),
        ("rlnOddZernike", "?"),
        ("rlnTomoVisibleFrames", "."),
    ],
)
def test_particle_rows_reject_null_markers_for_recognized_nonstring_types(
    tmp_path: Path,
    label: str,
    marker: str,
):
    star = tmp_path / f"null-{label}.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        f"_{label} #2\n"
        f"000001@particles.mrcs {marker}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        StarFormatError,
        match=rf"does not safely parse STAR null marker.*{label}",
    ):
        particle_star_row_summary(star)


@pytest.mark.parametrize(
    ("source_value", "stabilized_value"),
    [
        ("100000.0000004", "100000.000000"),
        ("0.00099999996", "0.001000"),
        ("-100000.000004", "-100000.00000"),
        ("-0.00099999996", "-0.00100"),
    ],
)
def test_particle_row_digest_stabilizes_relion_double_writer_thresholds(
    tmp_path: Path,
    source_value: str,
    stabilized_value: str,
):
    def write_particle(path: Path, value: str) -> None:
        path.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnCoordinateX #2\n"
            f"000001@particles.mrcs {value}\n",
            encoding="utf-8",
        )

    source = tmp_path / "source.star"
    stabilized = tmp_path / "stabilized.star"
    write_particle(source, source_value)
    write_particle(stabilized, stabilized_value)

    assert (
        particle_star_row_summary(source).row_metadata_multiset_digest
        == particle_star_row_summary(stabilized).row_metadata_multiset_digest
    )


def test_particle_row_digest_preserves_relion_negative_zero(tmp_path: Path):
    def digest(value: str) -> str:
        star = tmp_path / (value.replace("-", "negative-") + ".star")
        star.write_text(
            "data_particles\n\nloop_\n"
            "_rlnImageName #1\n"
            "_rlnCoordinateX #2\n"
            f"000001@particles.mrcs {value}\n",
            encoding="utf-8",
        )
        return particle_star_row_summary(star).row_metadata_multiset_digest

    assert digest("-0") == digest("-0.000000")
    assert digest("-0") != digest("0")


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("rlnRandomSubset", "1000000000000"),
        ("rlnCoordinateX", "1e100"),
        ("rlnCoordinateX", "1e-100"),
    ],
)
def test_particle_rows_reject_lossy_relion_scalar_writer_truncation(
    tmp_path: Path,
    label: str,
    value: str,
):
    star = tmp_path / f"lossy-writer-{label}-{value}.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        f"_{label} #2\n"
        f"000001@particles.mrcs {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        StarFormatError,
        match=r"12-character .* writer payload.*changing its parsed value",
    ):
        particle_star_row_summary(star)


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("rlnRandomSubset", "999999999999"),
        ("rlnCoordinateX", "1e99"),
        ("rlnCoordinateX", "1e-99"),
        # Fixed-point threshold output is 13 characters, but RELION only
        # truncates a trailing zero and therefore preserves its numeric value.
        ("rlnCoordinateX", "100000.000000"),
    ],
)
def test_particle_rows_accept_lossless_relion_scalar_writer_boundaries(
    tmp_path: Path,
    label: str,
    value: str,
):
    star = tmp_path / f"writer-boundary-{label}-{value}.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        f"_{label} #2\n"
        f"000001@particles.mrcs {value}\n",
        encoding="utf-8",
    )

    assert particle_star_row_summary(star).particle_count == 1


def test_parse_star_block_strict_physical_rows_are_opt_in(tmp_path: Path):
    star = tmp_path / "split-loop.star"
    star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        "_rlnImagePixelSize #2\n"
        "_rlnVoltage #3\n"
        "# header-phase comment remains accepted\n"
        "1\n"
        "1.31 300.0\n",
        encoding="utf-8",
    )

    assert parse_star_block(star, "optics") == {
        "rlnOpticsGroup": ["1"],
        "rlnImagePixelSize": ["1.31"],
        "rlnVoltage": ["300.0"],
    }
    with pytest.raises(StarFormatError, match="one physical line"):
        parse_star_block(star, "optics", strict_loops=True)


@pytest.mark.parametrize(
    "marker",
    ["data_next", "loop_", "_rlnVoltage #1"],
    ids=["data-block", "loop", "label"],
)
def test_parse_star_block_strict_requires_blank_before_row_phase_marker(
    tmp_path: Path,
    marker: str,
):
    star = tmp_path / "unterminated-optics.star"
    star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        "_rlnVoltage #2\n"
        "1 300.0\n"
        f"{marker}\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="blank-line terminator"):
        parse_star_block(star, "optics", strict_loops=True)


@pytest.mark.parametrize(
    ("separator", "marker"),
    [
        ("", "data_next"),
        ("", "loop_"),
        ("\n", "data_next"),
        ("\n", "loop_"),
    ],
)
def test_parse_star_block_strict_rejects_marker_in_first_row_position(
    tmp_path: Path,
    marker: str,
    separator: str,
):
    star = tmp_path / "empty-unterminated-optics.star"
    star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        f"{separator}"
        f"{marker}\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="blank-line terminator"):
        parse_star_block(star, "optics", strict_loops=True)


def test_parse_star_block_strict_rejects_second_loop_in_named_block(
    tmp_path: Path,
):
    star = tmp_path / "multiple-optics-loops.star"
    star.write_text(
        "data_optics\n\n"
        "loop_\n_rlnVoltage #1\n300.0\n\n"
        "loop_\n_rlnVoltage #1\n200.0\n\n",
        encoding="utf-8",
    )

    assert parse_star_block(star, "optics") == {"rlnVoltage": ["200.0"]}
    with pytest.raises(StarFormatError, match="exactly one loop"):
        parse_star_block(star, "optics", strict_loops=True)


def test_parse_star_block_strict_rejects_scalar_list_before_loop(tmp_path: Path):
    star = tmp_path / "scalar-then-optics-loop.star"
    star.write_text(
        "data_optics\n\n"
        "_rlnOpticsGroup 9\n\n"
        "loop_\n"
        "_rlnOpticsGroup #1\n"
        "_rlnVoltage #2\n"
        "1 300.0\n\n",
        encoding="utf-8",
    )

    assert parse_star_block(star, "optics") == {
        "rlnOpticsGroup": ["1"],
        "rlnVoltage": ["300.0"],
    }
    with pytest.raises(StarFormatError, match="no scalar metadata entries"):
        parse_star_block(star, "optics", strict_loops=True)


def test_parse_star_block_strict_requires_one_loop(tmp_path: Path):
    star = tmp_path / "empty-optics.star"
    star.write_text("data_optics\n\n# comment only\n\n", encoding="utf-8")

    with pytest.raises(StarFormatError, match="exactly one loop"):
        parse_star_block(star, "optics", strict_loops=True)


@pytest.mark.parametrize(
    "marker",
    ["data_next", "loop_", "_rlnVoltage #1"],
    ids=["data-block", "loop", "label"],
)
def test_particle_star_row_summary_requires_blank_before_row_phase_marker(
    tmp_path: Path,
    marker: str,
):
    star = tmp_path / "unterminated-particles.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnOpticsGroup #2\n"
        "000001@particles.mrcs 1\n"
        f"{marker}\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="blank-line terminator"):
        particle_star_row_summary(star)


@pytest.mark.parametrize(
    ("separator", "marker"),
    [
        ("", "data_next"),
        ("", "loop_"),
        ("\n", "data_next"),
        ("\n", "loop_"),
    ],
)
def test_particle_star_row_summary_rejects_marker_in_first_row_position(
    tmp_path: Path,
    marker: str,
    separator: str,
):
    star = tmp_path / "empty-unterminated-particles.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        f"{separator}"
        f"{marker}\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="blank-line terminator"):
        particle_star_row_summary(star)


def test_particle_star_row_summary_rejects_scalar_list_before_loop(tmp_path: Path):
    star = tmp_path / "scalar-then-particle-loop.star"
    star.write_text(
        "data_particles\n\n"
        "_rlnImageName ignored@particles.mrcs\n\n"
        "loop_\n"
        "_rlnImageName #1\n"
        "000001@particles.mrcs\n\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="first data construct"):
        particle_star_row_summary(star)


def test_particle_star_row_summary_wraps_malformed_quoted_token(tmp_path: Path):
    star = tmp_path / "malformed-particle-quote.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        '"unterminated@particles.mrcs\n',
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="Malformed STAR token syntax"):
        particle_star_row_summary(star)


def test_parse_star_block_strict_wraps_malformed_quoted_token(tmp_path: Path):
    star = tmp_path / "malformed-optics-quote.star"
    star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        '"unterminated\n',
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="Malformed STAR token syntax"):
        parse_star_block(star, "optics", strict_loops=True)


def test_parse_star_block_strict_skips_semicolon_header_comment(tmp_path: Path):
    star = tmp_path / "semicolon-optics-header.star"
    star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        "_rlnVoltage #2\n"
        "; header comment\n"
        "1 300.0\n\n",
        encoding="utf-8",
    )

    assert parse_star_block(star, "optics", strict_loops=True) == {
        "rlnOpticsGroup": ["1"],
        "rlnVoltage": ["300.0"],
    }


def test_particle_star_row_summary_skips_semicolon_header_comment(tmp_path: Path):
    star = tmp_path / "semicolon-particle-header.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnOpticsGroup #2\n"
        "; header comment\n"
        "000001@particles.mrcs 1\n\n",
        encoding="utf-8",
    )

    assert particle_star_row_summary(star).particle_count == 1


@pytest.mark.parametrize("header", ["_rlnVoltage garbage", "_rlnVoltage#1"])
def test_parse_star_block_strict_rejects_malformed_loop_header(
    tmp_path: Path,
    header: str,
):
    star = tmp_path / "malformed-optics-header.star"
    star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        f"{header}\n"
        "1 300.0\n\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="malformed column declaration"):
        parse_star_block(star, "optics", strict_loops=True)


@pytest.mark.parametrize("header", ["_rlnImageName garbage", "_rlnImageName#1"])
def test_particle_star_row_summary_rejects_malformed_loop_header(
    tmp_path: Path,
    header: str,
):
    star = tmp_path / "malformed-particle-header.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        f"{header}\n"
        "000001@particles.mrcs\n\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="malformed loop column declaration"):
        particle_star_row_summary(star)


def test_native_bound_readers_reject_nbsp_before_header_comment(tmp_path: Path):
    particle_star = tmp_path / "nbsp-particle-header.star"
    particle_star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName\u00a0#1\n"
        "000001@particles.mrcs\n\n",
        encoding="utf-8",
    )
    with pytest.raises(StarFormatError, match="malformed loop column declaration"):
        particle_star_row_summary(particle_star)

    optics_star = tmp_path / "nbsp-optics-header.star"
    optics_star.write_text(
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup\u00a0#1\n"
        "1\n\n",
        encoding="utf-8",
    )
    with pytest.raises(StarFormatError, match="malformed column declaration"):
        parse_star_block(optics_star, "optics", strict_loops=True)


@pytest.mark.parametrize("marker", ["data_optics", "data_optics\t"])
def test_parse_star_block_strict_rejects_target_block_decoy(
    tmp_path: Path,
    marker: str,
):
    prefix = "# " if marker == "data_optics" else ""
    star = tmp_path / "optics-block-decoy.star"
    star.write_text(
        f"{prefix}{marker}\n"
        "data_optics\n\nloop_\n"
        "_rlnOpticsGroup #1\n"
        "1\n\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="reserved data_optics substring"):
        parse_star_block(star, "optics", strict_loops=True)


@pytest.mark.parametrize("marker", ["data_particles", "data_particles\t"])
def test_particle_star_row_summary_rejects_target_block_decoy(
    tmp_path: Path,
    marker: str,
):
    prefix = "# " if marker == "data_particles" else ""
    star = tmp_path / "particle-block-decoy.star"
    star.write_text(
        f"{prefix}{marker}\n"
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "000001@particles.mrcs\n\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="reserved data_particles substring"):
        particle_star_row_summary(star)


def test_parse_star_block_strict_rejects_preloop_substring_decoy(tmp_path: Path):
    star = tmp_path / "optics-loop-decoy.star"
    star.write_text(
        "data_optics\n\n# loop_\nloop_\n"
        "_rlnOpticsGroup #1\n1\n\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="reserved loop_ substring"):
        parse_star_block(star, "optics", strict_loops=True)


def test_particle_star_row_summary_rejects_preloop_substring_decoy(tmp_path: Path):
    star = tmp_path / "particle-loop-decoy.star"
    star.write_text(
        "data_particles\n\n# loop_\nloop_\n"
        "_rlnImageName #1\n000001@particles.mrcs\n\n",
        encoding="utf-8",
    )

    with pytest.raises(StarFormatError, match="reserved loop_ substring"):
        particle_star_row_summary(star)


def test_reserved_substrings_remain_valid_inside_particle_rows(tmp_path: Path):
    star = tmp_path / "reserved-substrings-in-values.star"
    star.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnGroupName #2\n"
        "000001@data_particles_stack.mrcs value_with_loop_suffix\n\n",
        encoding="utf-8",
    )

    assert particle_star_row_summary(star).particle_count == 1


def test_parse_star_block_strict_rejects_invalid_utf8(tmp_path: Path):
    star = tmp_path / "invalid-utf8-optics.star"
    star.write_bytes(
        b"data_optics\n\nloop_\n_rlnOpticsGroup #1\n1\n\n# \x80\n"
    )

    with pytest.raises(StarFormatError, match="Invalid UTF-8"):
        parse_star_block(star, "optics", strict_loops=True)


def test_particle_star_row_summary_rejects_invalid_utf8(tmp_path: Path):
    star = tmp_path / "invalid-utf8-particles.star"
    star.write_bytes(
        b"data_particles\n\nloop_\n_rlnImageName #1\n"
        b"000001@mic\x80.mrcs\n\n"
    )

    with pytest.raises(StarFormatError, match="Invalid UTF-8"):
        particle_star_row_summary(star)


def test_native_bound_readers_reject_nul_bytes(tmp_path: Path):
    particle_star = tmp_path / "nul-particles.star"
    particle_star.write_bytes(
        b"data_particles\n\nloop_\n_rlnImageName #1\n"
        b"000001@mic\x00.mrcs\n\n"
    )
    with pytest.raises(StarFormatError, match="NUL bytes"):
        particle_star_row_summary(particle_star)

    optics_star = tmp_path / "nul-optics.star"
    optics_star.write_bytes(
        b"data_optics\n\nloop_\n_rlnOpticsGroup #1\n1\n\n# \x00\n"
    )
    with pytest.raises(StarFormatError, match="NUL bytes"):
        parse_star_block(optics_star, "optics", strict_loops=True)


def test_particle_row_phase_uses_relion_control_character_simplification(
    tmp_path: Path,
):
    bel_terminator = tmp_path / "bel-terminator.star"
    bel_terminator.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnOpticsGroup #2\n"
        "000001@particles.mrcs 1\n"
        "\x07\n"
        "ignored trailing text\n",
        encoding="utf-8",
    )
    assert particle_star_row_summary(bel_terminator).particle_count == 1

    nbsp_row = tmp_path / "nbsp-row.star"
    nbsp_row.write_text(
        "data_particles\n\nloop_\n"
        "_rlnImageName #1\n"
        "_rlnOpticsGroup #2\n"
        "\u00a0\n"
        "000001@particles.mrcs 1\n\n",
        encoding="utf-8",
    )
    with pytest.raises(StarFormatError, match="one complete particle row"):
        particle_star_row_summary(nbsp_row)
