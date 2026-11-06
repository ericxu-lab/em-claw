from pathlib import Path

import mrcfile
import numpy as np
import pytest

from relion_mcp import visuals
from relion_mcp.settings import ConfigurationError, Settings


def _settings(root: Path) -> Settings:
    return Settings(
        mode="plan",
        project_roots=(root.resolve(),),
        pipeliner_executable="relion_pipeliner",
        image_handler_executable="relion_image_handler",
        pipeline_name="default",
        add_job_timeout_seconds=90,
        utility_timeout_seconds=900,
        visual_max_side_px=800,
        visual_max_png_bytes=2_000_000,
        visual_max_panels=16,
    )


def _micrograph(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.new(path) as handle:
        handle.set_data(np.arange(1024, dtype=np.float32).reshape((32, 32)))


@pytest.mark.parametrize("path_form", ["project_relative", "absolute", "star_relative", "symlink"])
def test_particle_star_overlays_filter_exact_micrograph_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_form: str
) -> None:
    project = tmp_path / "project"
    micrographs = [project / f"Micrographs/{name}.mrc" for name in ("a", "b", "empty")]
    for path in micrographs:
        _micrograph(path)
    source = project / "Select/job001/particles.star"
    source.parent.mkdir(parents=True)
    names = [f"Micrographs/{name}.mrc" for name in ("a", "b")]
    if path_form == "absolute":
        names = [str(path) for path in micrographs[:2]]
    elif path_form == "star_relative":
        names = [f"../../{name}" for name in names]
    elif path_form == "symlink":
        alias = project / "Micrographs/a_alias.mrc"
        alias.symlink_to(micrographs[0])
        names[0] = "Micrographs/a_alias.mrc"
    source.write_text(
        "data_particles\n\nloop_\n_rlnCoordinateX #1\n_rlnCoordinateY #2\n"
        "_rlnMicrographName #3\n_rlnAutopickFigureOfMerit #4\n"
        f"5 7 {names[0]} -4\n21 25 {names[1]} 2\n12 14 {names[0]} 1\n",
        encoding="utf-8",
    )
    original_source = source.read_bytes()
    plotted = []
    original_finish = visuals._finish

    def capture(figure, metadata, settings):
        for axis in figure.axes:
            plotted.append(sorted(
                tuple(point)
                for collection in axis.collections
                for point in collection.get_offsets()
            ))
        return original_finish(figure, metadata, settings)

    monkeypatch.setattr(visuals, "_finish", capture)
    rendered = visuals.render_pick_overlays(
        _settings(tmp_path),
        str(project),
        pairs=[
            {"micrograph": str(path), "coordinates": "Select/job001/particles.star"}
            for path in micrographs
        ],
        particle_diameter_pix=4,
        fom_threshold=0,
        max_micrographs=3,
        columns=3,
    )
    records = rendered.metadata["rendered_micrographs"]
    assert [record["total_picks"] for record in records] == [2, 1, 0]
    assert [record["accepted_picks"] for record in records] == [1, 1, 0]
    assert [record["rejected_picks"] for record in records] == [1, 0, 0]
    assert plotted == [[(5, 7), (12, 14)], [(21, 25)], []]
    assert source.read_bytes() == original_source


def test_coordinate_only_star_remains_usable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    micrograph = project / "Micrographs/a.mrc"
    _micrograph(micrograph)
    source = project / "coordinates.star"
    source.write_text(
        "data_coordinates\n\nloop_\n_rlnCoordinateX #1\n_rlnCoordinateY #2\n5 7\n12 14\n",
        encoding="utf-8",
    )
    rendered = visuals.render_pick_overlays(
        _settings(tmp_path), str(project),
        pairs=[{"micrograph": "Micrographs/a.mrc", "coordinates": "coordinates.star"}],
        particle_diameter_pix=4,
    )
    record = rendered.metadata["rendered_micrographs"][0]
    assert record["total_picks"] == 2
    assert record["accepted_picks"] == 2
    assert record["fom_available"] is False


@pytest.mark.parametrize("escape_form", ["absolute", "symlink"])
def test_particle_micrograph_references_cannot_escape_project(
    tmp_path: Path, escape_form: str
) -> None:
    project = tmp_path / "project"
    _micrograph(project / "Micrographs/a.mrc")
    outside = tmp_path / "outside.mrc"
    _micrograph(outside)
    outside_name = str(outside)
    if escape_form == "symlink":
        (project / "Micrographs/outside.mrc").symlink_to(outside)
        outside_name = "Micrographs/outside.mrc"
    (project / "particles.star").write_text(
        "data_particles\n\nloop_\n_rlnCoordinateX #1\n_rlnCoordinateY #2\n"
        f"_rlnMicrographName #3\n5 7 Micrographs/a.mrc\n21 25 {outside_name}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="escapes the approved RELION project"):
        visuals.render_pick_overlays(
            _settings(tmp_path), str(project),
            pairs=[{"micrograph": "Micrographs/a.mrc", "coordinates": "particles.star"}],
            particle_diameter_pix=4,
        )
