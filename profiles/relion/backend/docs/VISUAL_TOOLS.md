# Image-returning RELION tools

This document describes MCP image transport and renderer implementation. See
the canonical [tool API](API.md) for arguments/defaults, the
[configuration reference](CONFIGURATION.md) for payload limits, and
[development guide](DEVELOPMENT.md) for test commands.

## Transport contract

Returning a local PNG path does not attach pixels to the model. Each scientific
visual tool returns:

```text
RELION MRC/MRCS/STAR input
        |
        v
pure Python renderer (mrcfile, NumPy, Matplotlib)
        |
        v
PNG bytes in memory
        |
        v
CallToolResult.content[0] = ImageContent(image/png)
CallToolResult.content[1] = TextContent(provenance and metrics)
```

The implementation leaves `structured_content` unset so the image-first rich
content remains authoritative. The trailing JSON text contains
project-relative source paths, rendering parameters, metrics, dimensions,
encoded byte size, and image SHA-256. Clients never need to dereference a local
report path.

Eligible InitialModel reports are also cached by exact PNG SHA-256 in the
running MCP process. This does not write a file during rendering. After direct
inspection, `relion_publish_initial_model_evidence` can materialize those exact
bytes as a project-relative evidence artifact through the normal write gates.

The wrapper is intentionally small:

```python
from mcp.server.mcpserver import Image
from mcp.types import CallToolResult, TextContent

from .visuals import RenderedVisual, metadata_text


def _visual_tool_result(rendered: RenderedVisual) -> CallToolResult:
    return CallToolResult(
        content=[
            Image(data=rendered.png, format="png").to_image_content(),
            TextContent(type="text", text=metadata_text(rendered)),
        ]
    )
```

Do not replace this with a dictionary containing only `png_path`, and do not
echo base64 image data into text or logs.

## Renderer boundary

[`visuals.py`](../src/relion_mcp/visuals.py) keeps rendering independent of MCP
transport. Every renderer returns:

```python
@dataclass(frozen=True)
class RenderedVisual:
    png: bytes
    metadata: dict[str, Any]
```

Renderers:

- resolve inputs beneath both `RELION_PROJECT_ROOTS` and the selected project;
- reject traversal and symlink escapes;
- memory-map MRC input where practical and downsample before plotting;
- apply explicit percentile display limits;
- label every class, map, threshold, and source;
- return one bounded composite PNG per call;
- enforce dimension, byte-size, and panel-count limits;
- hash the encoded image for provenance; and
- never edit or overwrite RELION products.

One composite keeps labels spatially attached to panels and bounds model
context. A focused second render is preferable for borderline classes or
regions.

## Image-producing tools

The server has exactly twelve read-only image-producing tools. The smoke-test
verifier is a separate read-only non-image tool.

### `relion_visual_smoke_test`

Creates a randomized four-quadrant PNG challenge. Each token exists only in the
pixels. The server retains expected values in memory for ten minutes and allows
three verification attempts. `relion_verify_visual_smoke_test` accepts the
`challenge_id` and labels keyed by `top_left`, `top_right`, `bottom_left`, and
`bottom_right`; it returns structured verification data without revealing the
expected values.

The orchestrator [skill](../.agents/skills/relion-spa-orchestrator/SKILL.md)
owns the exact smoke-test trigger and scientific response to unavailable visual
access.

### `relion_render_2d_classes`

Reads a model STAR and its `rlnReferenceImage` MRCS stack, then returns a
labelled class-average sheet with occupancy, estimated resolution, and ranker
score when available. The default emits every requested class as image/text
page pairs, using `max_classes` as the per-page limit. A caller may supply an
explicit stack or select class numbers for a focused report. Source hashes,
per-page missing IDs and combined pagination coverage accompany each image.
Use `all_classes=false` for a single bounded sheet. Before exact selection,
combine page metadata through `relion_validate_2d_class_review`; its source and
coverage check does not replace actual inspection of the attached pixels.

### `relion_compare_map_projections`

Reads one cubic map plus explicit one-based rows from a 2D class model STAR and
places each selected class average beside an independently fitted synthetic map
projection, magenta/green overlay, and signed residual. The map and class stack
must have matching physical pixel sizes and fields of view. Both sides receive
the declared physical low-pass before bounded, common-grid analysis; metadata
records source hashes, sampling, filtering, the 512 MiB stack and 1024-pixel
class-side resource caps, search bounds, fitted orientation
and shift, NCC diagnostics, runner-up separation, and pairwise best-direction
separations. No source artifact is written or modified.

InitialModel acceptance requires at least three explicitly selected classes that
the caller has established are credible, distinct 2D appearances from the seed
population. Inspect the attached class, projection, overlay, and residual
pixels. A class ID, NCC, runner-up margin, fitted direction, or the metadata's
count check is diagnostic only: no score or threshold auto-accepts a match or
seed. A central-slab mean or threshold-occupancy panel from
`relion_render_map_report` or `relion_render_3d_classes` is not a synthetic
projection and must never be represented as this comparison.

### `relion_render_particle_boxes`

Reads actual extraction-stack images referenced by a particle STAR. Explicit
one-based particle row numbers support targeted diagnosis; the default is an
evenly spaced sample, not a scientifically stratified quality estimate. The
tool does not align, invert or normalize the pixels. Reports source STAR and
per-box pixel hashes, indices, dimensions and intensity statistics. Display
contrast is scaled per panel; existing extraction normalization cannot be
undone by this renderer.

### `relion_render_pick_overlays`

Reads explicit micrograph/coordinate pairs or a coordinate-index STAR. It marks
accepted picks in green and below-threshold picks in red and reports FOM,
nearest-neighbour, count, and edge-proximity summaries. Coordinates use the
RELION upper-left pixel convention. `particle_diameter_pix` must describe the
displayed micrograph grid.

### `relion_render_3d_classes`

Reads class-map references from a model STAR and returns orthogonal central
slices with shared display normalization. When `include_mip=true`, the legacy
argument enables a Z central-slab mean with a threshold-occupancy contour; it
does not produce a raw maximum-intensity projection. Metadata includes
size-aware 26-neighbour component summaries and pairwise raw and commonly
Gaussian-filtered similarity/overlap measurements.

Set `display_lowpass_A` to render all selected classes with one physical radial
Fourier low-pass, applied in memory before downsampling. Its two-shell
raised-cosine edge mirrors RELION's filter convention. The report title and
metadata identify the filtered view, cutoff, physical grid, and read-only
provenance. Use a separate default call to retain direct raw-map evidence.

Every rendered map must have the same shape, voxel size, origin, start indices,
and MRC axis mapping. Candidate maps must also already share a scientifically
meaningful frame; the renderer checks the physical grid but does not align
maps. The fixed Gaussian comparison filter is expressed in downsampled voxels
and is a display diagnostic, not a physical downstream low-pass or biological
interpretation.

### `relion_compare_maps`

Performs deterministic, report-only local registration of one moving candidate
map onto a fixed reference map. Inputs are canonicalized to physical Z/Y/X and
must have the same physical shape and isotropic voxel sampling. Unlike the
ordinary renderers, matching MRC origins or starts is not assumed: those header
fields are recorded, while the density transform is estimated explicitly. The
optional soft mask must occupy the reference grid.

The caller must declare a physical alignment low-pass. Filtering occurs before
bounded center-preserving analysis resampling, and metadata distinguishes the
requested cutoff from any coarser effective cutoff imposed by the analysis
Nyquist limit. The tool fits one candidate-to-reference translation or local
rigid transform, then applies that exact transform without refitting to every
requested common physical comparison low-pass. The PNG uses shared density
limits, weighted 3D metric-threshold contours, support-masked symmetric
before/after z-score differences, and a compact fixed-transform row for every
requested cutoff. The JSON reports weighted pre/post Pearson correlation,
z-score RMS difference, threshold Dice/Jaccard, metric support, the centered
canonical-grid transform and homogeneous index affine, interpolation, bounds,
convergence, canonical voxel hashes, one pixel-pair evidence root, and a
separate analysis ID.

The fitter evaluates exact identity and several deterministic local starts.
Non-identical selected fits receive five spatial block-jackknife pose probes;
exact canonical voxel identity is recorded directly. The probes test pose
repeatability and held-out correlation gain, but never replace the transform
frozen for reported metrics. Meaninglessly tiny translation/rotation bounds are
rejected. Together these guards prevent a forced identity or chance correlation
increase from unrelated maps without defining a universal Pearson threshold.

This is not a global SO(3) search, a hand detector, or a flexible alignment.
Only `aligned` and `identity_optimal` are accepted registration statuses. Bound
contact, insufficient valid overlap, optimizer failure, competing poses,
unstable jackknife poses, or non-generalizing held-out gain is unassessable,
not evidence that maps are distinct. Fitting and scoring one pair can raise
correlation and remains one correlated canonical `map_pixels` root; origin/start
header clones therefore cannot manufacture independent roots, while an axis-map
change correctly changes canonical physical voxel order. Half-map/iteration
reproducibility and an independent channel are still needed for high
confidence. Different strongly low-passed starting references do not block
descriptive final-map registration, but they confound causal attribution and
cannot replace a common-reference matched A/B/A∪B control. No aligned map or
other project file is written.

### `relion_render_map_report`

Returns central slices, three central-slab means with threshold-occupancy
contours, an approximate threshold-surface point view, a density histogram,
voxel size, and map statistics. Connectedness metadata reports the raw
26-neighbour component count alongside dominant-component voxel and excess-
density fractions, 95% coverage counts, and a heuristic significant-component
count. These metrics are computed on the downsampled rendering volume; raw
component count alone is not evidence that a map is fragmented.

With `display_lowpass_A`, filtering occurs on the full-resolution MRC data in
memory before rendering and all displayed metrics describe that filtered view.
The source MRC is not written or modified. Metadata records the Å cutoff,
raised-cosine transition, voxel sampling, axis mapping, and that this is a
display-only view rather than a reference-bias experiment.

### `relion_render_mask_report`

Returns map/mask overlays plus volume fraction, soft-edge fraction, connected
components, boundary contact, credible-density clipping, and approximate
clearance diagnostics. Map and mask shape, voxel size, origin, start indices,
and MRC axis mapping must match.

### `relion_render_fsc_report`

Parses a RELION post-processing STAR and plots corrected, masked, unmasked, and
phase-randomized FSC curves when present, including 0.143 and 0.5 threshold
estimates.

### `relion_scan_symmetry`

Performs an exploratory, read-only symmetry search on two independently
reconstructed C1 half-maps. The half-maps, and the optional mask when supplied,
must have identical physical grids. The required `analysis_lowpass_A` is
applied consistently in memory so the scan does not interpret unsupported
high-frequency detail or alter any source artifact.
Different filesystem identities and content hashes are enforced, but actual
random-half provenance remains a caller responsibility.
When bounded grid resampling forces a coarser cutoff, the PNG and metadata show
the requested and effective low-pass values separately; all scores refer to
the effective value.

The search is deliberately bounded. Stage 1 covers common rotational orders
through `common_max_order`. A high-order sentinel surveys the remaining range
through `max_order`; at most two reproducible sentinel anchors open narrow
Stage 2 follow-ups bounded by `stage2_window` (1–8). One anchor follows the strongest
peak, while a second may preserve a same-axis composite order supported by
harmonic divisor closure. The report is capped by `max_candidates`. This
protects against both an open-ended order sweep and missing a prime or a
composite high order hidden behind stronger subgroup peaks. Harmonic
relationships can still make several orders look plausible, so the PNG and
metadata present a shortlist and diagnostics, not an accepted symmetry.
The minimum one-order neighborhood prevents a neighboring harmonic sentinel
from silently excluding an otherwise supported prime order.
The ambiguity status is computed before `max_candidates` truncates the
detailed shortlist, so reducing the display cap cannot turn an ambiguous scan
into an apparently unique result.

The tool does not impose symmetry or write an aligned/symmetrized map. A top
rank is not sufficient for biological or processing acceptance. Preserve the
C1 inputs. Confirm any shortlisted group and axis through C1 InitialModel, C1
Refine3D, then matched candidate Refine3D/PostProcess branches using the same
particles and random halves. Use
`relion_compare_symmetry` only after those branch maps share an auditable RELION
frame, filter, and contouring policy. The scanner searches rotations about the
physical array center. It uses
independent characteristic-axis discovery plus a coarse-to-fine twist search
for `T`, `O`, and abstract `I`. Independently found polyhedral orientations are
compared as complete operator sets so equivalent characteristic axes are not
mistaken for disagreement; `D2` is reported as an unordered three-axis frame.
The scan does not optimize an off-center axis or choose among RELION `I1`–`I4`
conventions. These are explicit report limitations, not silent assumptions.

### `relion_compare_symmetry`

Compares two to eight candidate branch maps using matched central slices,
z-score differences, Pearson correlation, and normalized RMS difference. Maps
must already have identical grids and a common frame; shape, voxel size, origin,
start indices, and MRC axis mapping are checked before comparison. The tool
neither aligns maps nor discovers/imposes symmetry. Use C1 as
`reference_label` and explicitly inspect for density lost from C1, smeared side
chains/domains, split or distorted helices, weakened ligand density, apparent
fractional occupancy, and conformations averaged together. The current
independently autoscaled central-slice rendering is a global triage overview;
matched filtering/contours, both C1 half-maps, and targeted local views remain
required for those conclusions. Use compatible existing local-resolution
evidence when available. Defer a new LocalRes job until the final branch is
selected unless the user explicitly requests it.

### `relion_compare_hands`

Compares an original map with an explicit opposite-hand map or a labelled axis
reflection generated for display. Explicit maps must have identical grids.
Generated opposite-hand crops are reflected after cropping so both panels show
the same physical region. This rendering is a comparison aid, not evidence of
absolute hand.

## Publishing inspected InitialModel evidence

The write-side `relion_publish_initial_model_evidence` tool publishes only PNGs
that an eligible renderer registered in the current process. Use the exact
`image.sha256` from the report that was directly inspected and choose its
matching channel:

| Evidence kind | Eligible report types |
|---|---|
| `map_multiview` | `relion_map_report`, `relion_3d_classes` |
| `projection_comparison` | `relion_map_projection_comparison` |
| `late_iteration_comparison` | `relion_map_comparison`, `relion_3d_classes` |

The publisher verifies the canonical project and report/channel pairing, then
uses a no-replace content-addressed path under
`.relion-mcp/initial-model-evidence/`. It requires run mode, `confirm=true`, and
an approved project root. Cached bytes are process-local and expire; after a
restart, cache miss, expiry, or eviction, render and inspect the report again.
Publication binds the exact inspected PNG but does not validate its scientific
meaning. The three acceptance channels still require distinct files and hashes,
and the late-iteration artifact must actually compare at least two late
iterations before it can support the gate.

## Context and scientific limits

- Keep a call focused on one decision.
- Start with at most 64 2D classes or eight 3D maps per image.
- Use a focused follow-up instead of repeatedly attaching a full report.
- Keep metadata concise and record the returned SHA-256 in project run state.
- Do not ask the model to reinterpret an already assessed image without new
  evidence or a new question.

Image attachment proves only that pixels were transported. Scientific
acceptance still requires the evidence, confidence, reversibility, symmetry,
handedness, and biological-interpretation rules in the orchestrator skill.

For InitialModel and Class3D decisions, the separate read-only
`relion_validate_3d_assessment` tool checks the structured
`relion.3d-assessment/v1` contract and reject-all, evidence-independence,
alignment, confidence, and approval guards. It does not inspect pixels, verify
that submitted evidence is scientifically correct, align maps, infer biology,
or authorize a project write; callers must establish those facts before
submitting the assessment and must still use the normal write gates.

## Tests

Pure renderer coverage is in
[`test_visuals.py`](../tests/test_visuals.py). Image-block transport is covered
by
[`test_mcp_image_transport.py`](../tests/test_mcp_image_transport.py). A client
transport test must assert image content first, text content second, and no
structured content. MCP Inspector can verify the result shape, but only the
randomized challenge verifies that a selected Codex client/model read the
pixels.
