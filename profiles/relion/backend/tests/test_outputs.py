from __future__ import annotations

import pytest

from relion_mcp.outputs import best_output, classify_output


@pytest.mark.parametrize(
    "path",
    [
        "Polish/job001/opt_params_all_groups.txt",
        "Polish/job001/params_fit.txt",
        "OPT_PARAMS_ALL_GROUPS.TXT",
    ],
)
def test_classifies_polishing_parameter_files(path: str) -> None:
    assert classify_output(path) == ["polishing_parameters"]


@pytest.mark.parametrize(
    "path",
    [
        "Polish/opt_params_archive/job001/shiny.star",
        "Polish/params/job001/unrelated.txt",
        "Polish/job001/notparams.txt",
        "Polish/job001/myparams_notes.txt",
        "Polish/job001/adopt_params_backup.txt",
    ],
)
def test_polishing_parameter_directories_do_not_classify_unrelated_files(
    path: str,
) -> None:
    assert "polishing_parameters" not in classify_output(path)


def test_best_output_prefers_the_first_expected_type() -> None:
    paths = [
        "Polish/job001/shiny.star",
        "Polish/job002/opt_params_all_groups.txt",
    ]

    result = best_output(paths, ["polishing_parameters", "polished_particles"])

    assert result == {
        "path": "Polish/job002/opt_params_all_groups.txt",
        "result_types": ["polishing_parameters"],
    }


def test_best_output_does_not_reward_ambiguous_classifications() -> None:
    paths = [
        "PostProcess/job001/postprocess.mrc",
        "InitialModel/job002/run_class001.mrc",
    ]

    result = best_output(paths, ["refined_volume", "initial_volume"])

    assert result == {
        "path": "PostProcess/job001/postprocess.mrc",
        "result_types": ["refined_volume"],
    }


def test_best_output_keeps_the_first_candidate_when_scores_tie() -> None:
    paths = [
        "Polish/job001/opt_params_all_groups.txt",
        "Polish/job002/params_fit.txt",
    ]

    result = best_output(paths, ["polishing_parameters"])

    assert result == {
        "path": "Polish/job001/opt_params_all_groups.txt",
        "result_types": ["polishing_parameters"],
    }
