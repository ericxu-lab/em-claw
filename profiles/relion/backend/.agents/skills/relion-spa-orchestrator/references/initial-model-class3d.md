# Initial Model and Class3D

Use this reference for de novo InitialModel, preparation and treatment of a 3D
reference, Class3D, and 3D-class assessment/selection. Apply the parent
[orchestrator](../SKILL.md), and also read its routed symmetry/mask reference
whenever symmetry or a focused mask enters the plan.

## Contents

- [Entry conditions](#entry-conditions)
- [Recoverable seed construction](#recoverable-seed-construction)
- [Initial-model parameter rules](#initial-model-parameter-rules)
- [Initial-model diagnostic ladder](#initial-model-diagnostic-ladder)
- [3D assessment evidence contract](#3d-assessment-evidence-contract)
- [Initial-model assessment](#initial-model-assessment)
- [InitialModel acceptance interlock](#initialmodel-acceptance-interlock)
- [Poor-map recovery](#poor-map-recovery)
- [Reference treatment](#reference-treatment)
- [Class3D parameter rules](#class3d-parameter-rules)
- [3D-class assessment and selection](#3d-class-assessment-and-selection)
- [Similar-class singleton-versus-union refinement adjudication](#similar-class-singleton-versus-union-refinement-adjudication)
- [Golden K=2 assessment scenario](#golden-k2-assessment-scenario)
- [Budgets and upstream routing](#budgets-and-upstream-routing)

## Entry conditions

Before InitialModel or Class3D, verify and record:

- the exact finalized, assessed 2D-selected particle STAR intended for Class3D,
  plus any separately balanced or capped particle STAR intended only for
  InitialModel, with path, SHA-256, extraction/selection provenance, and an
  audited exact-or-subset relation between them;
- particle count, 2D cleanliness, plausible view diversity, optics-group
  coverage, pixel size, box size, contrast, and CTF treatment;
- particle/mask diameter and nonzero box clearance;
- the objective: junk-sink modelling, consensus cleaning, view separation, or
  heterogeneity discovery;
- reference source, map/particle grids and frames, pixel/box dimensions,
  filtering history, hand, symmetry, and absolute-greyscale status;
- direct map-rendering access for every morphology-dependent conclusion; and
- approved K/T/search/reference/symmetry groups and remaining budgets.

An assessed credible provisional particle set or reference may satisfy these
entry conditions under `primary_fallback` for InitialModel diagnosis and
recovery. It cannot cross into Class3D. The InitialModel-to-Class3D boundary
requires the high-confidence `initial_model_accepted` checkpoint below. Keep
provisional inputs out of `accepted_outputs` and reassess their utility at every
stage. References to an accepted parent below mean use the best accepted
compatible parent when one exists, otherwise use a technically valid assessed
provisional primary only for work that does not cross this interlock.

Do not tune K or T to compensate for junk-dominated particles, missing views,
wrong polarity, clipping, inconsistent CTF treatment, or mismatched reference
geometry. Return to the earliest invalid state.

Keep the InitialModel reference role separate from the Class3D particle role.
An angularly balanced subset may be used to construct an initial map, but an
InitialModel class assignment is not an accepted particle-cleaning decision.
Class3D must use the full appropriate particle set: the exact accepted
2D-selected particles after documented quality exclusions, including
defensible minority views. Finalize and fingerprint that population before
accepting the initial model. Prove that the InitialModel seed particles are
either exactly that population or a duplicate-free documented subset of it;
an earlier bootstrap, LoG-label, held-out, or still-evolving population does
not qualify. Do not silently restrict Class3D to particles assigned to the
chosen InitialModel class.

For newly prepared particles, confirm the particle-preparation retention audit
and bounded recovery-trigger assessment before this handoff. Use the inclusive
assessed reconstruction candidates, not just the high-precision particles
curated as Topaz training labels. A credible first 2D result needs no extra
cleaning round when that audit identifies no supported residual problem.

## Recoverable seed construction

Use the InitialModel handoff ledger defined in
[particle preparation](particle-preparation.md#early-2d-cleaning-policy). Build
each seed from the exact **seed-eligible** particles for one structural
population, drawing on several credible 2D appearances when the data support
them. Preserve plausible minority views within that population. Sharpness,
class population, occupancy, or ranker score may describe a candidate but must
never be the sole reason to include or exclude it.

Keep **seed-eligible**, **uncertain**, and **clear junk** identities separate.
Record the original inventory and fingerprint, complete selection history,
exact role membership, `seed_branch_id`, and the exact balanced or capped
`materialized_seed_ids`. Exclusion from a materialized seed is not permanent
particle rejection: preserve uncertain particles and eligible particles omitted
for balance or cost so later evidence can recover them. Never move particles to
clear junk unless their canonical `proven_junk` disposition already satisfies
the parent rejection rules.

Keep clearly incompatible structural populations in separate seed branches.
Do not mix them to increase support, and do not mistake an unusual or
low-occupancy projection for incompatibility without direct evidence. A branch
may contain common and plausible minority viewing directions of the same
population. Materialize every seed through the typed exact-selection path and
prove its rows are a duplicate-free subset of that branch's seed-eligible
inventory. If balancing or capping requires an unavailable row-selection
operation, record a `contract_gap` rather than editing a STAR file or inventing
a shell path. Preserve every other branch as an ordered inactive fallback.

## Initial-model parameter rules

Use C1 during InitialModel optimization. The default sequence is C1
InitialModel, gold-standard C1 refinement, candidate-symmetry identification,
then a matched symmetry-constrained refinement within its authorized scope. Preserve
the C1 map and refined half-map baseline. A pre-existing independently
validated symmetry within the task may be an exception only when the
matched C1 baseline remains available.

Treat the workflow's K=1 value as a `tutorial_prior`. For this strategy, prefer
K=2 as the first InitialModel attempt when the accepted 2D particles have
enough support for two classes. Record K=2 as an evidence-selectable
`strategy_intent` override, not as a rewritten metadata or tutorial value.

| Rule ID | Prerequisite and condition | Atomic action | Required validation |
| --- | --- | --- | --- |
| `initial_model_k2_baseline` | Accepted 2D particles pass the entry checks and have adequate support for two classes. | Start with `num_classes: 2`, `regularisation_parameter: 4`, `number_of_vdam_mini_batches: 100`, and `symmetry: C1`. Treat both classes as unknown diagnostic partitions; do not preassign a junk sink. | Inspect both classes. At least one coherent connected low-resolution map must converge without fragmenting all signal into tiny or unstable classes. Advance to Class3D only after an evidence-complete credible reference passes `initial_model_accepted`; a routine reproducibility rerun is not otherwise mandatory. Use the targeted initialization exception below only when a consequential ambiguity warrants it. |
| `initial_model_reduce_to_k1` | Both K=2 classes are weak or unstable; the 2D, geometry, CTF, centering, and view-coverage checks still pass; and insufficient per-class support or artificial splitting is the supported primary diagnosis. | Rerun with `num_classes: 1`; hold T, VDAM limit, input particle STAR, geometry, symmetry, and resource-science hybrids fixed. | Pooling the signal produces a coherent, stable map without merely hiding an upstream failure. Return upstream if K=1 remains pathological. |
| `initial_model_increase_classes_after_k2` | K=2 reveals coherent, stable, meaningful populations that remain demonstrably mixed, and no credible initial reference is yet available. | Optionally test `num_classes: 3` or `4` with T normally fixed at 4 only when particle support, SNR, resources, approval, and the remaining attempt budget permit it. | Added classes are coherent and stable across late batches rather than noise splits; stop if classes become tiny, redundant, or unstable. |
| `initial_model_lower_T` | Global geometry is plausible but density remains noisy or streaky after coherent improvement has plateaued; verified geometry and directional coverage do not better explain the artifacts. | Lower `regularisation_parameter` from 4 toward 2–3 while holding K and the VDAM limit fixed. Do not use T to rescue fundamentally incorrect geometry. | Coherence and solvent behavior improve without erasing stable structure. |
| `initial_model_fix_mask_geometry` | Verified particle extent shows a mask-clearance mismatch while box, rescale, and pixel geometry remain valid. | Recalculate `mask_diameter_A` as `derived_geometry` before K/T/batch tuning and propagate invalidation. If extraction geometry is also wrong, correct the coupled geometry state upstream instead of changing only the mask. | Connected density, boundary clearance, and late-batch stability improve. |
| `initial_model_increase_vdam_batches` | A plausible 100-mini-batch result is still changing and late batches show coherent structural improvement. | Launch a preserved attempt with up to 200 `number_of_vdam_mini_batches`; hold K and T fixed. | Map identity and diagnostics stabilize; stop at a plateau or any stable pathological morphology. |
| `initial_model_stop_stable_pathology` | Late batches stabilize to diffuse, streaky, fragmented, or otherwise implausible global morphology after geometry checks. | Stop extending the same solution and route through the diagnostic ladder. Change one supported parameter family or return upstream; test initialization sensitivity only when the targeted exception below applies. | Direct map evidence and convergence history support the diagnosis and distinguish a convergence limit from a partitioning, regularisation, geometry, or input failure. |
| `initial_model_fix_low_frequency_ctf_behaviour` | The accepted particle branch used ignore-until-first-CTF-peak for verified low-frequency pathology. | Match that CTF treatment only after the native InitialModel option has a typed, render-tested mapping; otherwise record a contract gap. | Reconstruction remains coherent without low-frequency collapse and downstream CTF treatment stays consistent. |
| `initial_model_return_for_view_coverage` | Before a repeat, accepted 2D particles demonstrably lack directional coverage; do not wait for multiple InitialModel failures. | Stop K/T tuning and activate the recorded particle-selection/picking view-diversity fallback. | Improved view coverage exists before starting a new InitialModel input-data state. |

Keep `flatten_solvent: true`, CTF correction, and the standard positive-density
prior for ordinary SPA. Do not change them for a prettier map. Restrict tilt
priors to particles with justified filament, sphere, or manifold provenance;
the current neutral operation does not expose the native tilt-prior width.
Do not raise initial-model T above an approved 2–4 envelope autonomously.

## Initial-model diagnostic ladder

Use this ladder to choose one evidence-supported branch, not as a serial list
of experiments. Before any InitialModel repeat, directly inspect the accepted
2D classes and record whether they show:

- protein-like features commensurate with the target size and attainable SNR;
- multiple plausible viewing directions, interpreted in the context of the
  sample and any independently supported symmetry;
- consistent centering, no material clipping, and valid box/mask geometry;
- limited residual junk and contamination; and
- for applicable membrane-protein samples, limited detergent/micelle
  dominance rather than protein-free micelle classes.

Return to selection, centering/re-extraction, or 2D classification when these
checks fail. Do not tune InitialModel to compensate for an invalid particle
set. Treat late-batch stability within one run as convergence evidence. Use
subsequent InitialModel attempts to test supported parameter or initialization
hypotheses, not routine repeats of an already credible result. A stable
pathological solution is evidence against adding batches; diagnose a K, T,
geometry, input, or initialization hypothesis before the next attempt.
Near-spherical density is pathological only when it conflicts
with expected particle dimensions/composition, 2D projections, or independent
evidence.

| Observation | Route |
| --- | --- |
| K=2 gives one coherent, stable map plus a visually and structurally supported junk/damaged-particle sink. | Treat the coherent map as the primary reference candidate regardless of whether it has the largest occupancy. Preserve both maps, apply the full assessment, and cross into Class3D only after the candidate and full final 2D particle set pass `initial_model_accepted`. |
| K=2 gives two coherent distinct maps. | Preserve both as possible populations. If evidence supports one credible reference primary, rank it and keep the other inactive. At `medium` confidence, use `primary_fallback` only for further InitialModel assessment or recovery; do not continue into Class3D. Inspect the missing acceptance evidence before requesting input. Do not call either map junk from occupancy alone, and do not increase K merely because two populations exist. |
| Both K=2 classes are weak or unstable, the upstream checks pass, and low per-class support or artificial splitting explains the result. | Apply `initial_model_reduce_to_k1`; do not change T, batches, geometry, or particles in the same attempt. |
| Both K=2 classes are weak or pathological and the 2D, geometry, CTF, centering, or view-coverage checks fail. | Apply the earliest upstream remedy; do not use K=1 to conceal an invalid particle state. |
| Both K=2 classes show the same stable pathology without a supported low-particle explanation. | Diagnose the next parameter family or return upstream; a targeted initialization diagnostic needs a recorded hypothesis, and K changes must not oscillate without new evidence. |
| K=2 separates coherent, stable, meaningful populations but one or more remain demonstrably mixed and no credible reference exists. | Apply `initial_model_increase_classes_after_k2` with K=3 or K=4 only if the added partition answers the recorded hypothesis. |
| Global geometry is plausible and late batches are still improving coherently. | Apply `initial_model_increase_vdam_batches`; do not change T or K. |
| Global geometry is plausible but noise/streaking remains after improvement plateaus. | Apply `initial_model_lower_T` only after excluding view-coverage and geometry causes. |
| The K=1 fallback gives a coherent, plausible, stable map. | Apply the InitialModel reference-utility assessment. Preserve it as a provisional primary while incomplete, and advance to Class3D only after the high-confidence acceptance interlock passes. |
| The K=1 fallback remains weak or pathological. | Do not cycle back to K=2 unchanged. If the map is promising but still improving, test the VDAM limit; if improvement plateaued with noise/streaking, test T. Otherwise return upstream or record `defer` and retain a usable fallback; ask only if essential user input prevents all useful progress. |
| A K=3–4 test yields an evidence-supported coherent reference. | Preserve every class, rank one reference primary, and move remaining heterogeneity separation to Class3D only after `initial_model_accepted` passes. |
| A K=3–4 test creates tiny, redundant, or unstable fragments. | Reject that K branch. Return to the best-supported lower-K evidence and test batches or T only when its convergence history supports that diagnosis; otherwise return upstream. |

Use the K=2/T=4/100-batch strategy baseline as the first attempt, then allocate
up to four additional evidence-driven configurations by default. Rebalance this
agent allocation only for a new supported hypothesis within the finite overall
campaign limit; no additional user confirmation is needed. Change exactly one
scientific parameter family per attempt and preserve every result. Use K=1 to
test over-partitioning, K=3–4 to test unresolved meaningful mixtures, up to 200
VDAM batches to test a promising solution that is still improving, and T
toward 2–3 to test plateaued noisy or streaky density. Choose only the action
supported by the preceding result; do not run this list serially or as a
Cartesian grid. Do not spend an attempt on an unchanged repeat without a
predeclared initialization-sensitivity hypothesis. Stop the search
as soon as one candidate passes the complete `initial_model_accepted` interlock
and move remaining heterogeneity separation to Class3D. A merely credible but
incompletely evidenced candidate remains provisional. Treat K, T, and the VDAM limit as
separate `evidence_selectable` diagnostic groups; treat mask and box/rescale
values as `derived_geometry`.

A targeted independently initialized attempt may hold K, T, geometry, and input
fixed when conflicting coarse envelopes, unstable assignments, or suspected
initialization bias would change a consequential reference or particle choice.
Predeclare the comparison and exit condition, charge it to the finite campaign
budget, and match map identities rather than class numbers. Follow the
[particle-preparation initialization rule](particle-preparation.md#bounded-recovery-before-topaz-or-3d)
for typed-control and provenance requirements: no general executable seed
mapping is currently exposed. Do not invent a seed option or count late-batch
stability as independent initialization. This exception is diagnostic, not a
mandatory repeat before advancing a credible reference.

## 3D assessment evidence contract

Keep four assessment axes separate. Do not let a weakness or difference on one
axis silently determine another:

| Axis | Question | Allowed conclusion |
| --- | --- | --- |
| Reference utility | Is this map a safe, sufficiently coherent low-resolution seed for the intended downstream job? | `credible`, `ambiguous`, or `unusable` for that named downstream role. |
| Morphology quality | What connected envelope, internal density, solvent behavior, artifacts, and resolution-appropriate features are present? | Descriptive quality evidence and explicit defects; not a biological-state label. |
| Inter-class relation | After valid matching, are two classes the same, distinct, or uninterpretable, and is the difference stable? | `same`, `distinct`, or `unassessable`, with a relation confidence. Difference alone does not make either class junk. |
| Biological interpretation | Does independent evidence support a conformation, composition, oligomer, ligand state, orientation population, or other biological meaning? | `supported`, `unknown`, or `unsupported`; never infer a supported interpretation from class separation alone. |

Use the following evidence contract for both InitialModel and Class3D:

- Inspect the raw reconstruction and a display-only downstream-low-pass view.
  For an InitialModel candidate, use the planned Class3D reference low-pass or
  another recorded conservative common filter. Preserve the raw map and record
  the filter; a prettier filtered view is not a reference-bias experiment.
- Before any pairwise image, contour, correlation, overlap, or difference
  claim, verify compatible hand and sampling, then call `relion_compare_maps`.
  Fit one candidate-to-reference transform at a declared physical low-pass and
  hold it fixed across matched filters and contours. Inspect the aligned image
  and record the transform, bounds/status, mask, valid overlap, source hashes,
  and pre/post metrics. Matching MRC headers or a near-identity fitted transform
  must be reported and verified; neither may be assumed to prove a common
  density frame. A failed, boundary-limited, or insufficient-overlap fit makes
  the relation `unassessable`. Do not compare independently auto-scaled panels
  or independently optimized transforms as though they were quantitative
  matches.
- Inspect central and signal-centred slices, projections, and multiple
  isosurface views. A central slice can miss an off-centre domain and a maximum
  projection can hide a pore, overlap disconnected objects, or saturate real
  differences.
- Make connectedness size-aware and resolution-aware. At each recorded contour,
  report the largest-component density or volume fraction and the number and
  sizes of significant components using 26-neighbour connectivity. Define the
  minimum significant physical volume in Angstrom cubed from the sampling,
  displayed resolution, and smallest plausible domain; retain and list smaller
  islands as speckle sensitivity rather than counting every one-voxel island as
  a fragmented molecule. Raw component count may be reported only as a
  threshold-sensitivity descriptor. Never reject a map as fragmented from raw
  count when one component contains nearly all credible density.
- Interpret topology conditionally. A continuous central pore may be expected
  for a channel, ring, or assembly; a multi-lobed envelope may be expected from
  oligomeric or multi-domain composition. Treat either as a defect only when
  matched contours, expected dimensions, 2D projections, solvent behavior, or
  independent evidence show that it is an unsupported closed shell, missing
  mass, mask imprint, split object, or other artifact. Record evidence for and
  against both interpretations.
- Separate between-class from within-class heterogeneity. Stable distinct maps
  demonstrate a between-class relation. Internal blur, mutually incompatible
  density within one class, unstable assignments, or residual mixtures in its
  particles support within-class heterogeneity. Do not diagnose one from the
  other.
- Match late class identities with the fixed transform and matched-filter
  similarity/overlap reported by `relion_compare_maps`, not class number. Class
  labels may permute. Track occupancy and, when available, particle-assignment
  transitions and orientation distributions along each matched identity. A
  change in K is a new model-complexity experiment; call it recurrence only
  after an explicit correspondence shows that the same assessed feature
  recurred.
- Record evidence roots as required by the parent skill. Renders, surfaces,
  histograms, component metrics, and correlations derived from one MRC remain
  one `map_pixels` root. Late iterations from one optimization establish
  convergence but are not an independently initialized branch. Use evidence
  with genuinely different failure modes before a high-confidence rejection.

## Initial-model assessment

Require:

- final optimiser/model/data STAR files;
- every final `run_it*_classNNN.mrc` for a multi-class attempt, with the exact
  class-map path selected for downstream use;
- raw and downstream-low-pass orthogonal and signal-centred slices,
  projections, and multi-view isosurfaces at several matched contours;
- compatible low-resolution projection-to-class comparisons covering several
  distinct credible 2D appearances from the same seed population, with the
  compared class IDs, physical sampling, field of view, centering, contrast
  convention, and common filter recorded;
- map dimensions, size-aware connectedness, significant-component sizes,
  solvent behavior, boundary contact, estimated resolution, and occupancy for
  multi-class runs;
- per-class reference utility and morphology dispositions, pairwise
  inter-class relations, and `biological_interpretation: unknown` unless an
  independent protected interpretation is supported;
- identity-matched convergence across late batches, occupancy trajectories,
  available assignment/orientation stability, and the response to every
  evidence-driven parameter change already performed; and
- the SHA-256 fingerprints of the selected map, the finalized Class3D particle
  STAR, the seed particle STAR, and the
  exact-or-subset lineage audit connecting the two particle states; and
- optional suitably filtered fit to independent structure evidence.

Use compatible existing evidence before requesting a new calculation. When an
orientation-matched comparison is missing, call
`relion_compare_map_projections` with at least three explicit
`selected_class_numbers` already judged to be credible, distinct 2D appearances
from the relevant seed population. Together they must cover several distinct
credible 2D appearances. Use a shared physical pixel size and field of view,
compatible centering and contrast convention, and the same conservative
physical low-pass; preserve and identify the unfiltered sources. Inspect the
attached class, fitted projection, overlay, and residual pixels and judge
whether the coarse envelope and major density arrangements explain every
selected appearance. Do not demand a pixel-perfect match or unsupported
high-resolution features. The projections and class averages remain one
morphology channel unless another independent root corroborates them.

The synthetic projections from `relion_compare_map_projections` are distinct
from central-slab means and threshold-occupancy panels in map or 3D-class
reports. A central-slab or occupancy view is not a reprojected class match;
never represent it as one. Class number, best-fit direction, NCC, runner-up
margin, pairwise direction separation, or `count_requirement_met` is diagnostic
only. There is no score threshold or metadata flag that auto-accepts a match or
seed. If fewer than three selected appearances are credible and distinct after
direct inspection, record the evidence gap rather than inventing a match or
launching an unrelated validation job.

The InitialModel acceptance bar is reference utility, not final-map quality.
A candidate may be `credible` when its conservative downstream-low-pass view has
a coherent global envelope of plausible dimensions, its dominant morphology
and identity are stable, and no severe clipping, unsupported high-frequency
detail, solvent collapse, or reference imprint makes it unsafe as a low-bias
seed. It need not resolve final domains, establish symmetry or hand, be the
largest class, or be biologically interpretable. Modest raw-map streaking,
small speckles, a plausible pore, or composition-consistent lobes do not by
themselves make a candidate unusable when the coherent envelope survives
matched filtering and contour perturbation.

Morphology red flags include filter-persistent streaks that dominate the
envelope, unsupported closed shells, solvent-filled or solvent-only objects,
significant unexplained disconnected domains, preferred-direction elongation
that conflicts with 2D views, implausible detail, and identity or assignment
switching. Treat near-spherical, annular, hollow-looking, or multi-lobed density
as conditional evidence under the shared topology rule, not a categorical
failure. Record severity, contour/filter dependence, evidence for a plausible
structural explanation, and whether the defect actually prevents downstream
reference use.

Do not call a multi-class initial model a biological state without independent
evidence. Do not preassign extra classes as junk sinks; classify them from map,
particle, and convergence evidence. Occupancy is supporting evidence, not a
quality rank: the largest class is not automatically the best reference.
Preserve plausible alternatives, but advance one ranked primary. As soon as one
class is `credible`, stop demanding cosmetic improvement and complete the
acceptance interlock; credibility alone does not authorize Class3D. If one
class is `credible` and another is `ambiguous` or `unusable`, preserve the
credible map as the ranked primary and the others as fallbacks, but keep the
checkpoint `UNCERTAIN` until every required acceptance field passes at high
confidence. Never use `primary_fallback` to cross into Class3D. Use `defer`
while acquiring missing acceptance evidence or applying a supported recovery.
Reject the whole InitialModel set only when every class independently meets the
parent reject-all guard.

Assign exactly one stage-local `seed_status` to every InitialModel candidate:

- `SEED_READY`: `reference_utility: credible`; the map has plausible physical
  dimensions, no gross artifact that prevents reference use, and low-resolution
  projections consistent with at least three distinct credible 2D appearances
  from its population under the compatibility rules above. Its late identity
  is stable, all required evidence is assessable, confidence is `high`, and the
  hash-bound `initial_model_accepted` record below is complete.
- `UNCERTAIN`: `reference_utility: ambiguous`, or a credible map has not yet
  passed the acceptance interlock. Evidence is missing, incompatible,
  conflicting, below high confidence, `unknown`, or `unassessable`, including
  a missing compatible projection-to-class comparison, an unfinished final
  particle population, or an unaudited seed lineage. Insufficient evidence
  always means `UNCERTAIN`, never readiness or rejection.
- `REJECT_CANDIDATE`: `reference_utility: unusable`; observed gross artifacts,
  implausible dimensions, or reproducible disagreement with the relevant 2D
  evidence makes the map unsuitable for the seed role. This is a preserved
  candidate-map status, not permission to delete or permanently reject its
  particles.

Keep `seed_status` separate from the parent `decision`, `confidence`,
`next_action`, and class `disposition` enums. `REJECT_CANDIDATE` permits a final
`reject`/`auto_reject` only when the normal high-confidence and reject-all
guards also pass. `SEED_READY` means provisionally usable as a low-resolution
starting reference, not a validated final structure. It authorizes Class3D only
through the matching `initial_model_accepted` record and the exact finalized
particle population named there. Preserve the original inventory, uncertain
particles, omitted eligible particles, and alternate seed branches for recovery.

Do not require high-resolution detail, a repeated initialization, or an
additional Class3D or Refine3D job to assign `SEED_READY`. Existing compatible
evidence and inexpensive read-only renders are sufficient when they meet the
criteria. Run a targeted additional job only when a consequential recorded
hypothesis needs it under the recovery policy below.

## InitialModel acceptance interlock

`initial_model_accepted` is a hard launch gate, not a descriptive label. The
existence of an MRC, a successful InitialModel job, nominal resolution,
occupancy, or connectedness alone never satisfies it. Before registering,
starting, or launching any Class3D job, call
`relion_record_initial_model_acceptance` to validate and publish one acceptance
artifact with these top-level fields. Mirror the returned artifact path and
`record_sha256` under `initial_model_acceptance` in `RELION_RUN_STATE.yaml`:

```yaml
schema: relion.initial-model-acceptance/v1
reference_volume: {path: null, sha256: null, size_bytes: null}
initial_model_particles: {path: null, sha256: null, size_bytes: null}
classification_particles: {path: null, sha256: null, size_bytes: null}
initial_model_artifacts:
  model_star: {path: null, sha256: null, size_bytes: null}
  optimiser_star: {path: null, sha256: null, size_bytes: null}
  data_star: {path: null, sha256: null, size_bytes: null}
  job_star: {path: null, sha256: null, size_bytes: null}
  success_marker: {path: null, sha256: null, size_bytes: 0}
evidence_artifacts:
  map_multiview: {path: null, sha256: null, size_bytes: null}
  projection_comparison: {path: null, sha256: null, size_bytes: null}
  late_iteration_comparison: {path: null, sha256: null, size_bytes: null}
assessment:
  seed_status: SEED_READY
  reference_utility: credible
  confidence: high
  decision: auto_accept
  next_action: advance_primary
  approval_flag: true
  visual_access: direct
  distinct_2d_views_matched: 3
  late_iteration_stable: true
  multiple_contours_checked: true
  boundary_clearance_pass: true
  clipping_detected: false
  major_artifact_detected: false
  independent_root_sources:
    - map_pixels
    - particle_class_metadata
    - late_iteration_convergence
  critical_gaps: []
  rationale: null  # required non-empty acceptance rationale
geometry:
  mask_diameter_A: null
  expected_particle_extent_A: [null, null]
  box_size_pix: null
  pixel_size_A: null
  boundary_clearance_A: null
particle_lineage:
  mode: same_particles  # or initial_model_subset_of_classification
  evidence: null        # hash-bound artifact for subset mode
record_sha256: null
```

The null and empty values above show shape only; the recording tool requires
the dataset-derived values and a nonempty rationale, and it generates all
artifact metadata plus `record_sha256` itself.

Persist each immutable record below the project at
`.relion-mcp/initial-model-acceptances/acceptance-<prefix>.json`, where
`<prefix>` is the first 24 lowercase hexadecimal characters of the SHA-256 of
the canonical serialized payload before the generated `record_sha256` field is
added. This location is MCP-owned: never create, edit, rename, or overwrite an
acceptance artifact directly. The recording tool
validates and hashes the supplied InitialModel particles, finalized Class3D
particles, model/optimiser/data STAR files, evidence artifacts, assessment,
geometry, and lineage before publishing it. Pass its returned project-relative
path as `initial_model_acceptance` to every Class3D add/run wrapper. A
start-only recovery call may auto-find a stored record, but it must revalidate
that the record matches the registered job's live inputs.

Create the three PNG evidence paths only with
`relion_publish_initial_model_evidence`. Render and directly inspect an eligible
map-multiview, projection-comparison, or late-iteration report, then pass that
exact report's `image.sha256` and matching `evidence_kind` to the publisher with
`confirm: true`. The report cache is bound to the canonical project and current
MCP process; after a restart, miss, or expiry, render and inspect again. Use the
returned project-relative `path` in `evidence_artifacts`. Never create, copy,
rename, edit, or substitute these content-addressed evidence PNGs manually.
Publication proves which PNG bytes were inspected, not that their scientific
interpretation passes the gate.

Validate every field against the live Class3D inputs. The selected
`reference_volume.path` and SHA-256 must identify the exact inspected class map,
not a generic InitialModel output suggestion. The Class3D mask diameter and
reference geometry must agree with the recorded geometry.
`classification_particles` must be the finalized full assessed 2D-selected
population, including defensible minority views; `initial_model_particles` must
be either exactly that STAR (`particle_lineage.mode: same_particles`) or an
audited duplicate-free subset
(`particle_lineage.mode: initial_model_subset_of_classification`). The subset
lineage evidence must prove row identity and unchanged optics/CTF metadata, not
merely compatible counts or filenames. If a full Topaz or other intended
picking/selection branch is still running, its final population is not yet
fixed and this checkpoint cannot pass from an earlier bootstrap branch.

Require at least three compatible low-resolution projection-to-class matches
covering explicitly selected, distinct credible 2D appearances from the seed
population, inspected in the `relion_compare_map_projections` pixels at recorded
common sampling, field of view, centering, contrast convention, and physical
low-pass. Do not derive this claim from the fitted score or count metadata. The
three required evidence-artifact keys must resolve to distinct files published
from the exact inspected reports, but distinct files do not by themselves
establish independent evidence roots. Require directly inspected multi-contour
map evidence,
plausible geometry with nonzero boundary clearance, stable late-iteration map
identity supported by a hash-bound `late_iteration_comparison` artifact that
compares at least two late iterations, at least two independent root sources
under the parent policy, named from `map_pixels`, `particle_class_metadata`,
and `late_iteration_convergence`, `assessment.visual_access: direct`, and no
unresolved severe artifact. Formal non-convergence or material late-map,
assignment, orientation, or occupancy drift makes `late_iteration_stable`
false. Any required value that is missing, stale, `unknown`, or `unassessable`,
any nonempty `assessment.critical_gaps`, or any confidence below `high` fails
closed as `UNCERTAIN`.

The only passing checkpoint decision is `assessment.decision: auto_accept` with
`assessment.confidence: high` and `assessment.next_action: advance_primary`. A
passing record also requires `assessment.approval_flag: true`. A
medium-confidence `primary_fallback` may preserve and diagnose the candidate
but cannot enter Class3D. Any change to the reference bytes, final particle
STAR, seed-particle STAR, lineage audit, pixel/box/origin/hand/symmetry
compatibility, mask diameter, or particle-extent geometry invalidates the
record and requires reassessment.
Class3D planning/rendering must surface the gate status. Add/run wrappers and
start-only recovery must compare their actual particle, reference, and
mask-diameter/geometry inputs to the record and reject a missing or mismatched
gate.

## Poor-map recovery

Define scientific InitialModel failure narrowly: a completed attempt whose
assessed candidates contain no `SEED_READY` map. Handle scheduler, resource,
missing-output, and other execution failures under the parent failure classes
instead of calling them poor maps.

Before another scientific attempt, review the observed map defects, the
existing 2D evidence, particle preparation and geometry, and every prior
InitialModel attempt on this particle-and-geometry state. Record observations
separately from hypotheses. Observations describe inspected artifacts and
measurements; hypotheses state a proposed cause and must link to supporting and
contrary evidence.

Propose at most three evidence-supported alternatives for that failure
checkpoint and recommend exactly one affordable, reversible next action. Draw
it from the earliest supported action family: change seed composition,
targeted 2D classification, extraction/centring correction, or an
initialization/symmetry correction consistent with the C1 and symmetry policy.
Do not use unsupported symmetry as a rescue or combine a symmetry change with
another main scientific change.

For every alternative record:

- the hypothesis and exact evidence for and against it;
- the proposed change and the main factor held responsible;
- the expected improvement and a predeclared stop condition;
- estimated computational cost and its charge to the remaining campaign
  budget; and
- the particle-preservation plan, including seed-eligible, uncertain, omitted,
  clear-junk, and alternate-population identities.

Rank the alternatives, mark one `recommended_action`, and execute it when it is
inside the processing scope and budget. Change one main factor when practical,
hold unrelated scientific settings fixed, then reassess with the same
inexpensive readiness criteria and record the new `seed_status`, evidence,
cost, and outcome in `initial_model_recovery`. Do not automatically tighten
selection, discard uncertainty, extend iterations, or repeat a failed or
unchanged approach. When evidence cannot support an action, identify and, when
available, acquire the cheapest missing observation before proposing more
computation.

## Reference treatment

Before Class3D, require exact compatibility among particle and reference pixel
size, box, origin/frame, hand, and symmetry convention. Prefer explicit,
provenance-recorded rescaling/reboxing over silently relying on
`resize_reference_if_needed`. Inspect the rescaled map before use.

Rank accepted candidate maps by their reference utility for the named Class3D
objective. Prefer the highest-ranked accepted, reference-suitable map with a
coherent reproducible low-frequency envelope and no unresolved reference or
mask artifact. A credible provisional InitialModel primary may be preserved and
diagnosed under `primary_fallback`, but it may not seed Class3D. Only the exact
reference and finalized particle state bound by a passing
`initial_model_accepted` record may cross this boundary. Preserve alternatives
without recording them as accepted. Do not define `best_reference` by job recency, sharpened
appearance, or nominal FSC resolution alone. For broader-pool attrition
recovery, prefer the compatible best accepted pre-symmetry C1 consensus and an
unsharpened reconstruction source; apply the strong reference low-pass below
before classification. Preserve a more-strongly-filtered or de novo C1
fallback when the chosen map came from a narrow state selection and reference
bias or minority-state loss is a material concern.

For a multi-class InitialModel, bind the exact inspected
`run_it*_classNNN.mrc` selected as the reference. Never accept the first
generic `initial_volume` suggestion as a scientific class choice. Bind the
Class3D particle input independently to the full accepted 2D-selected particle
STAR; do not use the selected InitialModel class's particle assignment as the
Class3D input unless it underwent a separate, evidence-complete particle
selection checkpoint.

Set `reference_map_absolute_greyscale` from provenance:

- use absolute scale only for an unchanged compatible RELION reconstruction
  whose scaling provenance remains valid;
- normally allow rescaling for an external, filtered, transformed, resampled,
  or otherwise altered reference; and
- never choose the flag from first-iteration appearance alone.

The current greyscale field accepts only a workflow default or explicit user
override, not a provenance-derived/strategy source. For an external or
resampled reference, record `false` as the scientifically supported candidate
but do not promote it to an executable override without an allowed explicit
source or a contract change.

The generic `three_d_classification` operation accepts both `initial_volume`
and `refined_volume`, but a workflow step may expose a narrower reference role.
When the selected best reference is a refined volume, use a direct typed
`three_d_classification` job only after its exact input binding and rendered
native options pass the executable-contract gate. Do not relabel the refined
map as an initial volume or bypass the typed contract; record a `contract_gap`
when no validated binding exists.

Start with a strong reference low-pass. Roughly 40–60 Å is a planning range for
smaller particles, and about 70 Å may be appropriate for ribosome-sized
references. These are heuristics, not dataset facts. Increase the Å value when
independent branches reveal reference bias.
Reference-bias evidence requires a complete matched Class3D rerun from the
more strongly filtered reference; post-hoc display filtering of the prior output
does not test bias. If the coarse architecture fails to recur in that matched
branch, stop using the biased reference and activate a de novo C1 InitialModel
fallback rather than increasing K.

Leave the Class3D reference mask empty initially so the spherical particle mask
provides the lower-bias baseline. A focused reference mask, local search,
no-alignment classification, or symmetry relaxation requires a supported
technical hypothesis and an unmasked/global/C1 baseline. A processing or
optimization request covers reversible exploration on an already supported
structural target; record the hypothesis without requesting separate
confirmation.
Generic processing authority does not authorize inventing a biological target
or imposing an unsupported symmetry.
Test symmetry and focused/no-alignment hypotheses in separate branches; never
introduce both together.

## Class3D parameter rules

Use C1 for initial classification unless symmetry evidence and task scope already
satisfy the symmetry policy. Use the full appropriate particle set defined at
entry. Use K around 3–4 and T around 4 as a consensus-cleaning planning prior;
use K around 4–6 only when reproducible evidence makes heterogeneity discovery
the stated objective.

| Rule ID | Prerequisite and condition | Atomic action | Required validation |
| --- | --- | --- | --- |
| `three_d_increase_class_count` | Multiple coherent classes remain internally mixed and heterogeneity discovery is the stated objective. | Increase `num_classes` toward 5–6 within particle-count, memory, compute, and approval bounds; hold T fixed. | Added classes are coherent, reproducible, and scientifically distinct rather than noise splits. |
| `three_d_decrease_class_count` | Many classes are tiny, unstable, fragmented, or redundant. | Reduce `num_classes` toward 2–3, or return upstream if junk dominates; hold T fixed. | Stability improves without collapsing defensible distinct states. |
| `three_d_validate_similar_class_union` | One credible pair from the same final Class3D result is not high-confidence `distinct` and passes the staged gate below. | Compare matched singleton refinements first; materialize and refine the exact duplicate-free A ∪ B only when their common-frame comparison is assessable and supports compatibility without distinct/minority-state evidence. A credible medium-confidence relation can justify this reversible experiment. | Promote the union only for a predeclared distinguishable quality gain without loss, smearing, leakage, anisotropy, or state dilution; otherwise retain a singleton primary and continue the requested workflow. |
| `three_d_limit_overfit` | Solvent streaking, implausible high-frequency detail, or non-reproducible fine features appear. | Lower `regularisation_parameter` from 4 toward 2–3, or test `e_step_resolution_limit_A` at 7–12 Å in a separate attempt; never change both first. | Artifacts decrease and coherent density persists across late iterations or a bounded perturbation. |
| `three_d_reduce_reference_bias` | Features follow the start reference but disappear in independent/weaker-reference branches. | Increase `initial_lowpass_A`, for example 50 toward 60–70 Å; hold K/T fixed. | The class architecture re-emerges without unsupported reference detail. |
| `three_d_fix_reference_greyscale` | Reference provenance shows the absolute-scale flag is inconsistent. | Correct only `reference_map_absolute_greyscale`; do not combine this diagnosis with low-pass or K/T changes. | First-iteration scaling and likelihood behavior stabilize without density-amplitude pathology. |
| `three_d_blush_candidate` | A small or low-SNR complex overfits under standard regularization and the validated environment can execute Blush. | Compare one `use_blush_regularisation: true` branch with a preserved matched standard branch. | Blush improves coherent density and solvent behavior without erasing real heterogeneity or inventing detail. |
| `three_d_increase_iterations` | Late assignments/maps are changing meaningfully and quality continues to improve. | Increase `number_of_iterations` within budget while holding K, T, symmetry, and reference treatment fixed. | Class identities and diagnostics stabilize; stop at a plateau. |
| `three_d_adjust_initial_sampling` | Particle scale/symmetry and compute support finer global coverage; discretization, not model complexity, limits assignments. | Move `initial_angular_sampling` one supported step finer only when the operation contract authorizes the change; hold offsets/K/T fixed. | Stable assignments and reproducible structural information justify added compute. |
| `three_d_fix_centering_search` | Reference/particle geometry is correct but measured residual offsets exceed the search range. | Adjust `initial_offset_range_pix` from measured displacement; change `initial_offset_step_pix` separately only if discretization remains limiting. | Poses stabilize without implausible translations or new class mixing. |
| `three_d_use_fast_subsets` | Hundreds of thousands of particles make a full exploratory baseline infeasible. | Enable native fast subsets only after a typed mapping exists; treat it as an exploratory resource/science hybrid. | Confirm the chosen class architecture in a full-data branch before acceptance. |
| `three_d_change_solvent_masking` | A large/high-resolution particle shows edge correlations or zero-mask artifacts. | Test random-noise solvent (`zero_mask_particles: false`) only if the current contract authorizes that change; preserve the zero-mask branch. | Solvent and edge behavior improve while class identity remains reproducible. |
| `three_d_use_focused_classification` | A consensus refinement, trusted poses, and explicit local question exist; global classification cannot isolate local heterogeneity. | Within the processing task, record the supported focused hypothesis and matched unmasked baseline, then pass one validated soft mask through the optional typed `reference_mask` input and verify the rendered native `fn_mask` before execution. | Compare with the unmasked parent; reject mask-shaped classes, boundary leakage, or lost global consistency. |
| `three_d_disable_alignment_focused` | Trusted orientations and a focused mask exist; pose changes obscure local classification. | For a supported reversible no-alignment hypothesis within the processing task, set typed `alignment: false`, verify the rendered native `dont_skip_align == No`, and preserve the matched aligned parent. | Classes reproduce across perturbations and do not mirror mask/reference features. |
| `three_d_use_local_angular_search` | Global orientations are trusted but limited pose adjustment is scientifically required. | Within the named local-search objective, set enable/range as one atomic group after typed mappings exist. | Classes are not trapped near their priors and compare favorably with the global-search parent. |
| `three_d_relax_pseudosymmetry` | Independent evidence supports pseudosymmetry and the reference follows RELION's symmetry-axis convention. | Within the processing task, set symmetry relaxation only after typed mapping exists; preserve C1 and strict-symmetry branches. | Asymmetric features improve without duplicated noise or unstable symmetry-related poses. |

Render `use_blush_regularisation: false` for the first standard branch unless
the Blush condition is already supported. A tutorial `true` value alone is not
evidence. More classes are not justified merely because compute is available.
`skip_padding` and allow-coarser sampling are speed/quality choices, not
evidence that the science improved; confirm important class structure without
exploratory approximations.

Keep `do_ctf_correction: true` when valid per-particle CTF metadata exists.
Disabling CTF correction is a specialized protected hypothesis, not a remedy
for poor classes. Resolve an upstream CTF inconsistency instead.

For any approved no-alignment branch, focused or global, verify that the input
particle STAR retains the trusted orientations intended to remain fixed and
render `alignment: false` explicitly. Preserve an otherwise matched aligned
parent. A global no-alignment branch may omit `reference_mask` only when its
predeclared question does not depend on spatial focus; a focused branch must
bind the validated `reference_mask` and treat it atomically with the alignment
mode. Never infer authorization from the availability of these typed fields.

## 3D-class assessment and selection

Require:

- final model/optimiser/data STAR files and per-class particle STAR outputs;
- raw and common-filter slices, common-frame isosurfaces/projections, and
  several matched contours under the shared evidence contract;
- size-aware connectedness, solvent and boundary artifacts, apparent-detail
  plausibility, occupancy, estimated resolution, and identity-matched
  late-iteration convergence;
- particle-assignment and occupancy trajectories plus class-specific
  orientation distributions when available, so view separation is not
  mistaken for compositional or conformational change;
- pairwise map correlations, contour overlap, and difference evidence from
  `relion_compare_maps` after compatible hand/grid checks, one declared
  alignment low-pass, and one fixed transform across matched filters;
- separate per-class morphology quality, selected-particle utility,
  inter-class relation, and biological-interpretation records; and
- optional suitably filtered independent model/prediction fits.

The Class3D selection bar is stricter than InitialModel reference utility
because accepting a class determines a particle population. A class is
selection-credible only when its matched-filter morphology is coherent and
resolution-appropriate, its significant density is non-pathological, its
identity and particle support are stable, and overfit/reference/mask artifacts
are not a better explanation. Internal estimated resolution and occupancy are
supporting descriptors, not validation. Never select the largest class merely
because it is largest or combine classes solely because global resolutions are
similar.

Codex may reject one obvious junk class only when at least two independent
roots support that class-specific disposition, including direct visual map
evidence plus assignment/convergence evidence, an independent branch, or
another root with a different failure mode. Structured descriptions derived
from the same map do not add independence. Apply the parent reject-all guard
before rejecting a whole run. Raw speckles, an expected pore,
composition-consistent lobes, or difference from another class cannot alone
satisfy rejection. If a class is ambiguous, preserve it without rejection and inspect the missing
evidence or perform a bounded eligible validation. Ambiguity alone does not
require a user interruption.

Assess heterogeneity in two steps. First decide whether each class is internally
coherent or remains within-class mixed using its density, particles,
assignments, and orientation evidence. Then classify each pair as same,
distinct, or unassessable using matched maps and identity tracks. Two coherent,
stable, distinct classes support between-class heterogeneity, but their
biological meanings remain unknown until independent evidence distinguishes
conformation, composition, orientation, damage, or processing artifact.

`same` is an evidential relation, not a Pearson cutoff: after successful
alignment, no reproducible residual exceeds the predeclared margin across the
supported filters and contours, and no independent counterevidence remains.
`distinct` requires spatial residuals that survive transform, filter, and
contour perturbations and are corroborated by identity/convergence,
orientation, half-map, or independent-branch evidence. Alignment failure,
bound contact, insufficient overlap, hand ambiguity, or filter-dependent fitted
transforms yields `unassessable`, never `distinct`. Correlation and overlap are
measurements within that assessment, not relation labels.

When several coherent classes may represent real conformations, compositions,
or orientations, rank one primary using morphology, stability, occupancy,
resolution, and independent biological evidence. Preserve separate maps and
particle STAR files for ordered inactive fallbacks. Activate only the next
fallback if the primary meets its recorded failure trigger. Use
`primary_fallback` at `medium` confidence to continue with a credible
reversible primary through normal downstream stages, without rejecting the
other classes or claiming final scientific acceptance. If candidates cannot be ranked,
first inspect the missing evidence or perform the bounded matched comparison
below when eligible. A user request to investigate multiple states already
authorizes bounded state comparisons; record the objective and budgets. Use
`human_gate` with `await_human` only when an essential unresolved choice or a
new objective outside scope blocks useful progress.

Route materialization of an accepted exact class set or a credible provisional
set continuing the requested workflow through the parent orchestrator's
particle-preparation reference and its typed class-selection planner/runner.
Keep provisional sets out of `accepted_outputs`, preserve every alternative,
and assess the provisional primary at each downstream checkpoint until the
requested endpoint, a campaign limit, or final acceptance is reached. A
requested endpoint may be delivered with a credible assessed provisional map
and explicit limitations; do not ask for final acceptance or run extra jobs
solely to acquire a high-confidence label.
Prefer its one-job GUI-equivalent mode and keep
Select/JoinStar as the recorded fallback. A Class3D filename or class number
alone is not an exact particle selection. Treat symmetry inferred at Class3D as
provisional until the C1 InitialModel/C1 Refine3D/candidate-identification
sequence and a matched symmetry-constrained Refine3D/half-map comparison
satisfy the symmetry evidence policy.

## Similar-class singleton-versus-union refinement adjudication

Use rule `three_d_validate_similar_class_union` to test whether combining two
apparently equivalent Class3D particle populations improves a reconstruction.
It is a staged particle-set hypothesis, not permission to average class maps or
merge every plausible class.

Entry eligibility requires all of the following:

- exactly one pair from the same final Class3D data STAR is under consideration;
- both classes are selection-credible, internally coherent, mutually exclusive,
  sufficiently represented in both random halves, and free of an unresolved
  junk, mask, reference, grid, hand, lineage, or view-separation red flag;
- `relion_compare_maps` has been used on the source maps with a predeclared
  alignment low-pass, fixed-transform filter perturbations, inspected aligned
  pixels, and recorded bounds/status, and no valid evidence establishes a
  high-confidence `distinct` relation; and
- the singleton stage and conditional union stage fit the approval envelope and
  remaining scientific-attempt and compute budgets.

The two matched singleton branches are a bounded diagnostic experiment covered
by a processing or optimization request when all entry checks and budgets pass;
source `same`/`high` is not required merely to gather this evidence. If the
source relation is unresolved, record that uncertainty and the predicted
validation outcome before launch, preserving both populations without
acceptance or rejection. A failed geometry, hand, or provenance check still
blocks the affected branch. Autonomous union materialization additionally
requires a successful, non-boundary-limited common-frame
`relion_compare_maps` comparison of the singleton refined maps, credible
compatible morphology, and no evidence of a distinct population, reproducible
local difference, or defensible minority-state signal. A `medium`-confidence
compatibility assessment can justify this preserved experiment; a `same`
relation at `high` confidence and an evidence root outside map-pixel derivatives
are required for final union promotion, not merely to gather valid evidence.
A high-confidence `distinct` relation established after valid alignment blocks
a union, as does unresolved evidence of distinct/minority states. A source-map
`unassessable` relation permits only the bounded singleton diagnostic stage
when its independent entry checks pass; it does not itself authorize the union.
The source status may be superseded only when the matched singleton refined-map
stage independently passes the assessable compatibility check above. Failed or
unassessable refined-map alignment never authorizes a union.

Do not define "quite similar" with a universal Pearson correlation, overlap,
estimated-resolution, occupancy, or particle-count cutoff. Predeclare the
alignment and comparison filters, contour/enclosed-volume convention,
perturbation, supported frequency range, and decision margin before the
comparison. Similar global resolutions alone never trigger this rule. A group
larger than two or more than one proposed pair from a checkpoint requires a
predeclared objective and compute budget covering that bounded extension;
request new authorization only if it exceeds the existing scope or limits. Reducing K to test
over-partitioning and refining singleton/union sets test different hypotheses;
do not run them serially by default.

Derive the predeclared equivalence margin from the noisier map's supported
frequency range, half-map or late-iteration variability, registration
repeatability, interpolation precision, and mask/filter/contour sensitivity.
It must be fine enough to detect the smallest scientifically relevant retained
domain or state signal and must not claim frequencies unsupported by either
map. If available data cannot bound that margin or provide adequate sensitivity,
the relation remains `unassessable`; do not choose a permissive margin merely to
open the union gate.

Do not run LocalRes independently on all three candidate branches. Use
compatible existing LocalRes evidence during matched adjudication only when it
is already available. Select the primary from the three refined branches before
scheduling LocalRes, then opt in once for that selected map. Run it earlier only
after an explicit user request; missing LocalRes output is not an A/B/A ∪ B
adjudication blocker.

For eligible classes A and B, complete these stages as one bounded scientific
experiment:

1. From the same final Class3D data STAR, call
   `relion_plan_particle_class_selection` and
   `relion_run_particle_class_selection` separately for A and B. Inspect the
   materialized STARs, prove `ids(A) ∩ ids(B) = ∅`, fingerprint both sets, and
   verify that each particle's source row and optics/CTF metadata are
   unchanged, and that its `rlnRandomSubset` value is unchanged. Planner
   counts alone do not prove
   identity or half-set preservation.
2. Run gold-standard C1 Refine3D for A and B with the same neutral, unsharpened,
   strongly low-passed pre-selection C1 reference and source hash. Do not seed
   each singleton from its own class map. Hold grid, hand, initial low-pass,
   greyscale treatment, mask diameter, CTF treatment, regularization,
   angular/offset behavior, padding/solvent behavior, and all other scientific
   controls fixed. Record resource-only differences and never count them as
   evidence. If re-extraction is necessary, apply the identical audited
   geometry/recentering operation to both first. Select the neutral reference
   before inspecting singleton outcomes from a preserved pre-selection C1
   consensus or independently validated de novo C1 InitialModel, then apply the
   declared strong low-pass. If no typed, non-branch-privileging reference can be
   selected or created, record a `contract_gap` and stop; never substitute one
   class's map.
3. Use one broad, non-branch-privileging validated soft global mask, matched
   PostProcess pixel/MTF/B-factor/FSC policy, matched directional-resolution
   policy, and compatible existing local-resolution evidence when available.
   Compare the unsharpened singleton maps with
   `relion_compare_maps`; fit one transform at the declared alignment low-pass
   and keep it fixed for every common-filter and contour sensitivity view.
   Inspect convergence, pose accuracy, particle support, orientation
   distributions, corrected masked/unmasked/phase-randomized FSC,
   global/directional resolution, compatible existing local-resolution
   evidence when available, retained local density, and solvent/edge/corner
   behavior. Stop the union experiment after this stage unless the refined
   relation passes the assessable compatibility check with no distinct or
   minority-state evidence. Continue with the best supported singleton when
   the union is ineligible; do not pause the whole workflow.
4. Only after that gate passes, materialize A ∪ B. Prove
   `ids(A ∪ B) = ids(A) ∪ ids(B)` and `N(A ∪ B) = N(A) + N(B)`, reject
   duplicates, fingerprint the union, and repeat the particle-row, optics/CTF,
   and half-set audit. Run its gold-standard C1 Refine3D/PostProcess with the
   same neutral reference, settings, global mask, and policies used by both
   singletons. The union is the union of particle rows, never an average of
   class maps.

A pre-existing singleton refinement may fill a matched slot only when its exact
particle/half-set fingerprint, common-reference fingerprint, grid, scientific
settings, mask, and PostProcess policy match the declared experiment; otherwise
rerun it. Final maps from refinements that used different strongly low-passed
starting references may still be aligned and descriptively compared. The
reference difference does not invalidate the geometric comparison; record it
as a causal confound. Convergence despite different starts may support
robustness, but divergence cannot by itself attribute a difference to particle
population. Such a comparison may support a formal descriptive geometric
`same`/`high` label when the full evidence contract passes, but that label does
not authorize a causal union claim. The branches cannot fill a matched
A/B/A∪B slot or isolate union benefit unless their particle/half-set
fingerprints, starting-reference fingerprint, settings, mask, and PostProcess
policy match. Strong low-pass suppresses fine-detail imprint but does not prove
equivalence of coarse envelope, centering/orientation, hand, or convergence
basin.

For a group of N classes covered by that bounded objective and budget, apply the same staged rules to
N singleton branches and one conditional exact union. Count the N+1 bundle as
one scientific hypothesis while charging every job to the compute budget.
Treat union-versus-singleton maps and FSCs as correlated evidence because the
union contains the singleton particles; require corroboration from a root
outside those shared particle/half-map derivatives.

Use `relion_render_3d_classes` for source montage and context, and use
`relion_render_map_report` and `relion_render_fsc_report` for per-branch quality.
Use `relion_compare_maps` for every quantitative pairwise source or refined-map
claim. Inspect its aligned image, record its transform/filter/mask/valid overlap
and pre/post metrics, and keep the transform fixed across sensitivity views.
The tool establishes registered measurements, not a `same`/`distinct` decision;
Pearson or overlap alone is insufficient. Do not feed an added union map through
stale Class3D model metadata, and do not repurpose `relion_compare_symmetry` as
a generic map comparator. A failed or incompatible registration is a comparison
blocker and yields `unassessable` rather than an invented metric. Inspect
geometry and registration diagnostics first; the relation's uncertainty alone
does not require a user interruption.

Finally promote A ∪ B only when the matched singleton relation passes
`same`/`high` with independent corroboration and it gives a predeclared,
Fourier-shell-distinguishable
quality improvement over the best singleton in compatible corrected and
unmasked FSC behavior, corroborated by map evidence, compatible existing
LocalRes evidence when available, and a root outside half-map derivatives. It
must remain compatible with both
singleton maps over their supported frequency ranges and introduce no blur,
lost local density, minority-state dilution, reference bias, mask dependence,
half-set leakage, edge/corner artifact, or worsened anisotropy. Increased
particle count, a smaller nominal resolution, a sharper display, masked FSC
alone, or similarity alone is insufficient.

Do not promote the union when singleton refinements diverge into reproducibly
distinct maps, the union smears or loses density present in either singleton,
or it performs worse than the best singleton. If the union is indistinguishable
from the best singleton, combination has not demonstrated a benefit; retain the
best supported singleton as primary. Mark the experiment inconclusive when any
identity/half-set audit, branch, alignment, mask, PostProcess comparison, or
decisive map-similarity evidence is invalid, unavailable, or conflicting.
Resolve recoverable gaps through diagnostics or a bounded correction, or retain
the accepted singleton fallback. Use `human_gate` with `await_human` only when
the essential gap cannot be resolved and no safe useful progress remains;
never lower the final union promotion's `same`/`high` evidence requirements or
the experiment's alignment, compatibility, identity, and half-set requirements.

Record one aggregate checkpoint using the existing decision and next-action
enums, advance one primary only, and preserve every singleton and the union as
ordered state-specific fallbacks or rejected/inconclusive branches as evidence
supports. Keep `biological_interpretation: unknown` without independent
biological evidence. Record the source Class3D job and final STAR, class IDs,
relation evidence roots, set counts/fingerprints/intersections, half-set audit,
common reference/mask/settings, every Refine3D/PostProcess job and any
explicitly opted-in LocalRes job, comparison settings and metrics, branch
dispositions, aggregate decision, activation triggers, and stopping condition.

## Golden K=2 assessment scenario

Use this compact scenario as a decision-regression example. A K=2 InitialModel
produces two classes whose matched late identities each correlate near 0.99
across the final batches, while a successful declared `relion_compare_maps`
call gives a post-alignment Class001-versus-Class002 correlation near 0.47. This
value is illustrative evidence, never a universal relation threshold. At
matched contours, more than 97% of each class's credible density belongs to one
dominant component; dozens of raw one-voxel islands are present. Class001 is
multi-lobed, and Class002 has a central pore or shell-like appearance. The raw
and planned downstream-low-pass views preserve the two different coarse
morphologies.

The correct assessment is:

- raw island counts do not establish fragmentation because size-aware
  connectedness shows one dominant object in each class;
- stable within-identity similarity plus low between-class similarity supports
  a stable `distinct` inter-class relation, not a shared pathology;
- the lobes and pore are assessed conditionally against composition, 2D views,
  solvent behavior, and significant-component topology;
- Class001 may be a `credible` primary reference while Class002 is held
  `ambiguous`; if both remain plausible, rank a reversible primary for further
  acceptance assessment, or gather more evidence before requesting input.
  Medium confidence does not authorize Class3D, and neither outcome authorizes
  reject-all;
- `biological_interpretation` remains `unknown`; and
- Class3D, if `initial_model_accepted` passes, uses the exact fingerprinted full
  accepted 2D particle set rather than only particles assigned to the preferred
  InitialModel class.

## Budgets and upstream routing

- Allocate up to five distinct InitialModel parameter configurations per
  particle-and-geometry state by default. This is an agent planning allocation,
  not a reason to request more permission. Rebalance within the recorded finite
  overall campaign limit when new evidence justifies it, preserving user and
  hardware hard limits.
- Count every completed parameterized InitialModel attempt on the same accepted
  particle and geometry state, including attempts run before the current plan.
  Do not count an allowed infrastructure retry as a scientific configuration.
- Change one scientific parameter family per attempt, choose only the response
  supported by the preceding evidence; use a fixed-settings independent
  initialization only under the targeted diagnostic exception above. End
  InitialModel search only when a candidate passes `initial_model_accepted`;
  a credible provisional reference can end cosmetic optimization but cannot
  seed Class3D while required evidence remains incomplete.
- After the default five unsuccessful configurations, normally end InitialModel
  parameter search and return to the earliest unsupported particle, geometry, CTF, centering,
  or view-coverage state.
- Allocate up to four Class3D attempts per branch by default under the same
  overall campaign limit.
- Stop after two justified Class3D rounds reproduce the same structure without
  meaningful separation improvement.
- Start a new stage attempt counter when the particle set or geometry state
  changes; never reset the overall campaign limit. A stage budget or plateau
  ends that loop, then continue the requested pipeline from the last usable
  primary or fallback. When no useful route remains, return the best supported
  result without asking to extend the budget by default.
- Return to particle preparation when junk, clipping, contrast, CTF
  inconsistency, centering, or view coverage dominates.
- Address ambiguous morphology, missing images, and unrankable coherent states
  with available diagnostics or a bounded validation first. Preserve credible
  candidates and record provisional confidence rather than forcing rejection.
  Use `defer` with `repeat`, `retry`, or branch-local `stop` when no user answer
  is needed; reserve `human_gate` for `await_human`.
- Use `human_gate` with `await_human` only when an essential unresolved choice
  blocks useful progress or a symmetry/focused/local/no-align objective exceeds
  existing authorization.
