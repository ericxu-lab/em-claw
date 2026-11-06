from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import mrcfile
import numpy as np
import pytest

from relion_mcp import visuals
from relion_mcp.settings import ConfigurationError, Settings
from relion_mcp.visuals import (
    VisualError,
    render_2d_class_pages,
    render_particle_boxes,
    validate_2d_class_review,
)


def _fixture(tmp_path: Path, count: int = 5) -> tuple[Settings, Path]:
    project = tmp_path / 'project'
    project.mkdir()
    classes = project / 'Class2D'
    classes.mkdir()
    rng = np.random.default_rng(10)
    with mrcfile.new(classes / 'classes.mrcs') as handle:
        handle.set_data(rng.normal(size=(count, 16, 16)).astype(np.float32))
    (classes / 'model.star').write_text(
        'data_model_classes\n\nloop_\n_rlnReferenceImage #1\n_rlnClassScore #2\n'
        + ''.join(f'{i + 1}@classes.mrcs {i / count}\n' for i in range(count))
    )
    return replace(Settings.from_env(), mode='plan', project_roots=(tmp_path,),
                   visual_max_side_px=700, visual_max_panels=64), project


def test_all_hundred_classes_render_and_stale_or_missing_pages_fail(tmp_path: Path) -> None:
    settings, project = _fixture(tmp_path, 100)
    pages = render_2d_class_pages(settings, str(project), 'Class2D/model.star', max_classes=64)
    assert len(pages) == 2
    assert [len(page.metadata['rendered_classes']) for page in pages] == [64, 36]
    assert all(page.png.startswith(b'\x89PNG') for page in pages)
    metadata = [page.metadata for page in pages]
    complete = validate_2d_class_review(settings, str(project), 'Class2D/model.star', metadata)
    assert complete['valid']
    assert complete['rendered_class_numbers'] == list(range(1, 101))
    incomplete = validate_2d_class_review(settings, str(project), 'Class2D/model.star', metadata[:1])
    assert not incomplete['valid']
    assert incomplete['missing_class_numbers'] == list(range(65, 101))
    with mrcfile.open(project / 'Class2D/classes.mrcs', mode='r+') as handle:
        handle.data[0, 0, 0] += 1
    stale = validate_2d_class_review(settings, str(project), 'Class2D/model.star', metadata)
    assert not stale['valid']
    assert any('changed' in issue for issue in stale['issues'])


def test_class_pagination_preserves_sort_and_explicit_subset(tmp_path: Path) -> None:
    settings, project = _fixture(tmp_path)
    pages = render_2d_class_pages(settings, str(project), 'Class2D/model.star',
                                max_classes=2, selected_class_numbers=[1, 3, 5], sort_by='score')
    ids = [row['class_number'] for page in pages for row in page.metadata['rendered_classes']]
    assert ids == [5, 3, 1]
    assert not pages[-1].metadata['pagination']['complete']
    result = validate_2d_class_review(settings, str(project), 'Class2D/model.star',
                                    [page.metadata for page in pages])
    assert result['missing_class_numbers'] == [2, 4]
    with pytest.raises(VisualError, match='nonempty'):
        render_2d_class_pages(settings, str(project), 'Class2D/model.star', selected_class_numbers=[])


def test_particle_box_pixels_indices_and_source_are_preserved(tmp_path: Path) -> None:
    settings, project = _fixture(tmp_path)
    particle_star = project / 'particles.star'
    particle_star.write_text('data_particles\n\nloop_\n_rlnImageName #1\n'
                            '1@Class2D/classes.mrcs\n3@Class2D/classes.mrcs\n5@Class2D/classes.mrcs\n')
    before = (project / 'Class2D/classes.mrcs').read_bytes()
    rendered = render_particle_boxes(settings, str(project), 'particles.star', particle_numbers=[2])
    row = rendered.metadata['rendered_particles'][0]
    assert row['particle_number'] == 2
    assert row['stack_index'] == 3
    with mrcfile.open(project / 'Class2D/classes.mrcs') as handle:
        assert row['mean'] == pytest.approx(float(handle.data[2].mean()))
    assert row['shape_yx'] == [16, 16]
    assert len(row['pixels_sha256']) == 64
    assert (project / 'Class2D/classes.mrcs').read_bytes() == before
    for numbers in ([0], [4], [1, 1], []):
        with pytest.raises(VisualError):
            render_particle_boxes(settings, str(project), 'particles.star', particle_numbers=numbers)


def test_particle_box_rejects_escape_and_invalid_stack_index(tmp_path: Path) -> None:
    settings, project = _fixture(tmp_path)
    outside = tmp_path / 'outside.mrcs'
    outside.write_bytes((project / 'Class2D/classes.mrcs').read_bytes())
    (project / 'escape.mrcs').symlink_to(outside)
    source = project / 'particles.star'
    source.write_text('data_particles\n\nloop_\n_rlnImageName #1\n1@escape.mrcs\n')
    with pytest.raises(ConfigurationError, match='escapes'):
        render_particle_boxes(settings, str(project), 'particles.star')
    source.write_text('data_particles\n\nloop_\n_rlnImageName #1\n0@Class2D/classes.mrcs\n')
    with pytest.raises(VisualError):
        render_particle_boxes(settings, str(project), 'particles.star')


def test_coverage_rejects_substituted_stack_and_image_index(tmp_path: Path) -> None:
    settings, project = _fixture(tmp_path, 2)
    page = render_2d_class_pages(settings, str(project), 'Class2D/model.star')[0]
    page.metadata['rendered_classes'][0]['stack_index'] = 2
    wrong_index = validate_2d_class_review(settings, str(project), 'Class2D/model.star', [page.metadata])
    assert not wrong_index['valid']
    assert wrong_index['missing_class_numbers'] == [1]
    alternative = project / 'Class2D/alternative.mrcs'
    alternative.write_bytes((project / 'Class2D/classes.mrcs').read_bytes())
    page = render_2d_class_pages(settings, str(project), 'Class2D/model.star',
                                 classes_mrcs='Class2D/alternative.mrcs')[0]
    substituted = validate_2d_class_review(settings, str(project), 'Class2D/model.star', [page.metadata])
    assert not substituted['valid']
    assert substituted['missing_class_numbers'] == [1, 2]


def test_pagination_rejects_stack_changed_between_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, project = _fixture(tmp_path, 2)
    original = visuals.render_2d_classes
    calls = 0

    def changing_renderer(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            with mrcfile.open(project / 'Class2D/classes.mrcs', mode='r+') as handle:
                handle.data[0, 0, 0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(visuals, 'render_2d_classes', changing_renderer)
    with pytest.raises(VisualError, match='stack changed'):
        render_2d_class_pages(settings, str(project), 'Class2D/model.star', max_classes=1)


@pytest.mark.parametrize('changed_source', ['star', 'stack'])
def test_particle_boxes_reject_sources_changed_during_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_source: str,
) -> None:
    settings, project = _fixture(tmp_path)
    source = project / 'particles.star'
    source.write_text('data_particles\n\nloop_\n_rlnImageName #1\n1@Class2D/classes.mrcs\n')
    original = visuals._downsample_2d

    def changing_downsample(*args, **kwargs):
        if changed_source == 'star':
            source.write_text(source.read_text().replace('1@', '2@'))
        else:
            with mrcfile.open(project / 'Class2D/classes.mrcs', mode='r+') as handle:
                handle.data[0, 0, 0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(visuals, '_downsample_2d', changing_downsample)
    with pytest.raises(VisualError, match='changed during rendering'):
        render_particle_boxes(settings, str(project), 'particles.star')
