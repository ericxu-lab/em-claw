# Particle Preparation

Use this reference for importing CryoSPARC particles for RELION Class3D,
micrograph subset design, LoG/Topaz/reference picking, extraction and
re-extraction, micrograph/particle/class selection, 2D classification, and
exact class-set materialization. Apply the shared decision, authority,
approval, contract, and evidence rules in the parent
[orchestrator](../SKILL.md).

## Contents

- [Entry conditions](#entry-conditions)
- [CryoSPARC particle import for Class3D](#cryosparc-particle-import-for-class3d)
- [Evidence and stage order](#evidence-and-stage-order)
- [Representative subsets and Topaz labels](#representative-subsets-and-topaz-labels)
- [Picking-mode choice](#picking-mode-choice)
- [Picking rules](#picking-rules)
- [Extraction rules](#extraction-rules)
- [Selection rules](#selection-rules)
- [Early 2D cleaning policy](#early-2d-cleaning-policy)
- [Bounded recovery before Topaz or 3D](#bounded-recovery-before-topaz-or-3d)
- [2D-classification rules](#2d-classification-rules)
- [2D assessment and exact class selection](#2d-assessment-and-exact-class-selection)
- [Budgets and upstream routing](#budgets-and-upstream-routing)

## Entry conditions

Before selecting a picking or extraction parameter, verify and record:

- the assessed usable motion-corrected/CTF-estimated micrograph branch and optics
  groups;
- calibrated micrograph pixel size, contrast polarity, dose/CTF provenance,
  and whether particles appear dark or white under the native convention;
- particle minimum, typical, and maximum projected extent in angstroms;
- coordinate source, particle-density expectation, and crowding/duplicate
  risk;
- extraction input pixel size, even box size, optional even rescaled size,
  derived particle pixel size and Nyquist, background circle, and mask margin;
- a representative subset stratified across optics group, defocus, ice
  thickness, acquisition time, contamination, and pick density;
- direct visual access for overlay or class-morphology conclusions; and
- approved parameter groups/ranges and remaining attempt/compute budgets.

A credible assessed `medium`-confidence primary may advance through these
stages with `primary_fallback`, preserved alternatives, and assessment after
each stage. Final scientific acceptance is not a prerequisite for routine
continuation. Where a best accepted parent is available, prefer it; otherwise
use the technically valid assessed provisional primary without relabeling it
as `accepted_outputs`.

Correct polarity, scale, geometry, or severe micrograph contamination before
tuning thresholds, K, T, or convergence. Never merge genuinely different
optics groups to increase particle counts.

## CryoSPARC particle import for Class3D

Use the typed import pair when the accepted particles already live in a
CryoSPARC project and the next scientific stage is RELION Class3D. This is a
data handoff, not a 2D-selection workflow. Do not call `csparc2star.py`
directly, reuse the CryoSPARC 2D-selection pipeline, filter classes, transform
coordinates or poses, or repack stacks as part of this conversion.

Follow this order:

1. Call `relion_environment` and require
   `cryosparc_particle_import.ready=true`. Verify the active source
   authorization policy, resolved pyem Python and script, script SHA-256,
   copy-only policy, and output layout. In a combined EM-Claw session, confirm
   that the RELION MCP captured the live-project inventory at startup. Then
   call `mcp__cryosparc__inspect_project_job` for the named `P#`/`J#` and retain
   its `relion_handoff.source_project_dir` and
   `relion_handoff.allowed_cs_files`. Do not guess an omitted input when
   `relion_handoff.allowed_cs_files_truncated=true`.
2. Choose a new RELION destination beneath `RELION_PROJECT_ROOTS`, a distinct
   authorized CryoSPARC `source_project_dir`, one relative primary
   `particles_cs`, and any relative passthrough CS files in their intended
   merge order. On the combined route, use the returned handoff directory and
   select every CS input from its allowed list; RELION independently requires
   its canonical directory to exactly match `project_dir` in the API-derived
   `{uid, project_dir}` inventory and directory device/inode identities captured
   at MCP startup, with every inventory
   directory resolved through authoritative `Project.dir()`. A standalone or
   RELION-only route instead requires containment beneath
   `RELION_CRYOSPARC_IMPORT_ROOTS` when it uses this bridge. Assign a unique
   `export_name`.
3. Call `relion_render_cryosparc_particle_import` with those source fields. A
   user needs no existing RELION project at this point. If the plan reports
   `bootstrap_required=true`, call `relion_initialize_project` with the exact
   returned bootstrap arguments. Treat initialization and conversion as
   additive writes covered by the requested import; do not request another
   confirmation. Never let conversion create a project implicitly.
4. Render the import again after initialization with the same source project
   and CS inputs. Require `execution_ready=true`; review UID and particle
   counts, passthrough order, source-stack identities, index ranges, box and
   pixel sizes, bytes to copy, CTF validation, the zero-based CryoSPARC to
   one-based RELION optics-group mapping, alignment warnings, pyem argv,
   destination paths, and `plan_sha256`.
5. Call `relion_import_cryosparc_particles` with the same scientific inputs
   and `confirm=true`. Require `status="materialized"`, validated particle and
   optics metadata, unchanged source files, matching source/output stack
   hashes, a manifest hash, the reported publication method, and no-replace
   publication beneath
   `External/CryoSPARC/<export_name>/`.
6. Use the returned `import_job_handoff` first with `relion_render_job` and
   then `relion_run_job`. This separate mapped `import_particles` job registers
   the new `particles.star` in the RELION pipeline; use that Import job output
   as the particle input for the planned Class3D branch.

The additive bundle contains `particles.star`, copied collision-safe
`stacks/*.mrcs`, and `conversion_manifest.json`. Multiple referenced stacks are
valid; do not consolidate them merely to obtain one `.mrcs`. Class3D CTF
metadata is mandatory. A missing complete 3D pose/shift/class-posterior set is
a warning, not an import failure: record it and allow Class3D to establish new
orientations.
Stop on a UID mismatch, source-path or symlink escape, stack/header/index or
sampling mismatch, changed source, existing bundle, pyem failure, invalid STAR,
undefined optics group, insufficient space, or incomplete atomic publication.

## Evidence and stage order

Use this closed evidence loop:

1. Select representative micrographs without discarding rare but usable
   acquisition conditions.
2. Pick with one justified mode and inspect stratified overlays.
3. Measure pick count per micrograph, score/FOM distribution, nearest-neighbour
   distances, edge proximity, clustering, and dependence on defocus, ice,
   optics group, and acquisition time.
4. Extract with verified geometry and inspect raw/normalized boxes.
5. Run a matched 2D validation, score every class with class ranker, and
   inspect every class at least once in a labelled sheet.
6. Use ranker scores as a triage prior, reconcile every class with visual and
   particle evidence, and partition the exact source into retained,
   ambiguous-recoverable, and proven-junk pools. Audit selection losses and run
   a bounded recovery diagnostic when its trigger is met before handoff.

Do not accept a picker from its training loss or visually attractive overlays
alone. Do not accept a final selection without auditing the ranker boundary,
obvious high-score junk, and protected rare-view candidates.

## Representative subsets and Topaz labels

Plan representative micrographs before LoG bootstrap or Topaz training with
`relion_plan_micrograph_subset`. Set explicit `training_count`, `heldout_count`,
and `seed`; choose recorded `strata_columns` and `numeric_bins` from available
optics, defocus, acquisition-time, ice-quality, or density metadata. Never
invent absent acquisition metadata. Inspect the plan's stratum coverage,
omissions, counts, source fingerprint, and disjoint micrograph identities.
When curated label candidates already exist, pass their exact STAR as optional
`particles_star` to both planning and materialization. This partitions those
labels by the chosen micrograph identities even when their source classes span
the full acquisition. Inspect the label-source fingerprint and projected
particle counts in each split before writing. For LoG bootstrap without prior
labels, omit `particles_star` and use the micrograph-only split.
Prefer coverage of usable rare conditions over simply taking the first images.
Audit shared holes or acquisition blocks when that metadata exists. If related
micrographs span the split, record that dependence and do not claim complete
statistical independence from disjoint filenames alone.

Materialize an authorized plan with `relion_materialize_micrograph_subset`,
passing its exact `expected_plan_sha256` and a new `output_name`. This is an
additive manifest/STAR write, not a native RELION job. Preserve the returned
manifest and new training, heldout, and remaining micrograph STAR files under
`External/MicrographSubsets/`; bind their exact paths to typed LoG/Topaz jobs.
With `particles_star` supplied, preserve the additional `training_particles.star`,
`heldout_particles.star`, and `remaining_particles.star` outputs. Verify their
counts and identities partition the supplied labels by micrograph membership,
with particle columns, optics metadata, and existing half assignments unchanged.
Bind the returned training particle STAR together with the training micrograph
STAR to Topaz; do not pass the original full-acquisition label STAR or filter it
by hand. Reserve heldout positives for evaluation, and preserve the remaining
labels for provenance or a separately justified future plan. Positive-only
heldout labels do not establish exhaustive ground truth for every visible pick.
Do not overwrite a split or repeatedly resample to obtain favorable validation. A
changed source or stratification is a new recorded plan.

If using the native micrograph-subset workflow fallback, supply its explicit
positive `subset_size` and retain `randomize_order: true`; the tutorial's ten
images are an example, not an executable default. Audit the resulting strata
and preserve that exact output for comparisons. Native random ordering alone
does not establish stratification, a deterministic seed, or an external
training/heldout split.

Keep two particle roles separate:

- **Topaz training labels:** high-precision, well-centered particles spanning
  credible views and the usable training-micrograph strata. Audit coordinates
  on micrograph overlays; class score alone does not establish label quality.
  Avoid letting one abundant view or easy imaging condition dominate labels.
- **Reconstruction candidates:** the inclusive assessed retained population,
  including credible lower-score, weaker, or minority-view particles. A
  particle unsuitable as a training label need not be unsuitable for 3D.

Record exact label source STARs, selection/recovery provenance, micrograph
identities, counts by class/view family and stratum, and Topaz model identity.
All supplied training labels must belong to the training-micrograph STAR. Use
the typed label partition above when starting from broader existing labels;
after micrograph-only bootstrap, verify that newly curated labels retain that
same training-micrograph membership before binding them.
The external heldout micrographs are disjoint from training and supply no
training labels; Topaz's internal validation split does not replace this test.
Use a fixed training/calibration subset for threshold tuning and preserve the
external heldout subset for evaluation of the chosen model. If heldout results
guide retraining or threshold choice, record that reuse and do not call it an
independent final test. Evaluate overlays, false positives/missed particles,
and downstream 2D quality and coverage rather than training loss alone.

## Picking-mode choice

- Use LoG as the default bootstrap when no trusted training set or diverse 2D
  references exist.
- Train Topaz only from a curated, assessed usable coordinate/particle set. Treat
  Topaz as `labels -> training -> picking -> overlays/statistics -> bounded FOM
  sweep -> matched 2D validation`.
- Use reference picking only with validated, diverse 2D references and an
  explicit reference-bias plan. Preserve the independent LoG or Topaz branch.
- Use manual picking only when the user explicitly opts in.
- Do not combine coordinate sets until duplicates and provenance can be
  resolved without compromising half-set independence.

## Picking rules

Each action is one scientific hypothesis or atomic group. Apply it only after
the parent executable-contract gate passes. If a named control is unavailable,
record the rule as a diagnosis and `contract_gap`; do not simulate it with a
different option.

LoG minimum/maximum diameters and Topaz particle diameter require explicit,
positive finite values derived from measured particle extent and verified
pixel scale; tutorial examples are not executable defaults. Record their
`input_metadata`, `derived_geometry`, `sample_hypothesis`, or `user_override`
source as permitted by the contract. The LoG adjustment and upper thresholds
still accept only workflow defaults or explicit user overrides; autonomous
threshold tuning remains a contract gap until that authority is expanded.

| Rule ID | Prerequisite and condition | Atomic action | Required validation |
| --- | --- | --- | --- |
| `picking_fix_particle_geometry` | Pixel size and particle extent are measured; LoG/Topaz circles systematically mismatch particles. | Change only LoG `log_diameter_min_A`/`log_diameter_max_A` together or Topaz `topaz_particle_diameter_A`; propagate a changed physical diameter to extraction and masks. | Centered overlays cover genuine size variation; pick-density and nearest-neighbour statistics remain plausible; matched 2D classes improve. |
| `picking_fix_polarity` | Acquisition convention and raw-micrograph appearance disagree with the configured picking polarity. | Correct polarity before threshold tuning. Native LoG `log_invert` is not exposed by the current neutral operation, so stop with a contract gap unless another typed, validated path exists. | Picks move from background/contamination to particles across the stratified subset and downstream 2D becomes interpretable. |
| `log_adjust_pick_yield` | Geometry and polarity are correct; real particles are systematically missed or background is over-picked. | Decrease `log_adjust_threshold` by one bounded step for more picks, or increase it for fewer picks; hold diameter and upper threshold fixed. | Stratified overlays, count/micrograph, nearest-neighbour distribution, edge/clustering metrics, and matched 2D quality agree. |
| `log_suppress_high_contrast_contamination` | High-contrast false picks localize to ice, carbon, droplets, or other contamination. | Lower only `log_upper_threshold`; hold the detection threshold and geometry fixed. | Contamination picks decrease without removing genuine high-contrast particles or rare views. |
| `log_adjust_spectral_scale` | Particle scale is correct but weak-signal detection needs a different LoG maximum resolution. | Do not execute with current `lowpass_filter_A`: it maps to native `lowpass`, not LoG `log_maxres`. Record a contract gap pending a corrected and render-tested mapping. | After contract repair, require matched overlays and downstream 2D, with no new high-frequency false picks. |
| `topaz_adjust_fom_threshold` | Model identity errors are absent and FOM separates plausible from implausible picks; extraction is too permissive or sparse. | Evaluate the tutorial prior `minimum_autopick_fom = -3` first for a new model/dataset, then sweep a small bounded set of alternatives using preserved extraction branches; change nothing in the Topaz model. | Overlay morphology, retained fraction, spatial statistics, rare-view coverage, and matched 2D runs support one primary and ordered fallback. |
| `topaz_retrain_systematic_error` | Systematic view-, size-, ice-, defocus-, time-, or contamination-dependent errors persist across defensible FOM thresholds. | Correct/stratify labels or training particles and retrain one preserved branch. Change particle diameter only when measured geometry changed. | Held-out micrographs and matched downstream 2D improve; training completion or loss alone is insufficient. |
| `reference_picker_reduce_bias` | Reference picking is authorized; picks follow reference-like noise or exclude plausible views. | Increase the reference `lowpass_filter_A` value for stronger low-pass filtering while holding other settings fixed. | Independent 2D views emerge without template-shaped noise; compare with the preserved non-reference branch. |
| `reference_picker_adjust_separation` | Physical particle spacing is known; duplicate picks or credible crowded particles are mishandled. | Adjust native minimum inter-particle distance only after a typed mapping exists; the current neutral reference-picker operation does not expose it. | Nearest-neighbour distributions and overlays show duplicates removed without erasing plausible crowded particles. |

For the first FOM evaluation of each Topaz model/dataset pairing, use
`minimum_autopick_fom = -3` with `baseline_source: tutorial_prior`, unless an
explicit user value or a threshold already validated for that exact Topaz
model and dataset supersedes it. Evaluate the full coordinate pool at `-3` at
least read-only: report the exact retained count and its distribution across
the representative strata before choosing any alternative threshold. Do not
select the first FOM value from score percentiles alone.

If full `-3` extraction fits the approved compute and storage budget,
materialize it as a preserved branch. If it does not, record the resource gate
and evaluate `-3` on a representative stratified micrograph subset; use that
same subset for every candidate threshold comparison. A resource gate may
limit full materialization, but it does not scientifically reject the `-3`
baseline.

Do not auto-reject or skip the `-3` baseline solely from retained count,
overlay appearance, FOM percentiles, or nearest-neighbour statistics. Before
accepting another cutoff as primary, compare `-3` and the alternative with
matched 2D classification on the full branches or the same stratified subset,
then assess class quality, retained fraction, view/stratum coverage, and
duplicate or contamination burden. Preserve `-3` as an inactive fallback when
another threshold wins.

Topaz expected particles per micrograph is also unavailable in the current
neutral contract. Treat systematic density errors as evidence for contract
work or label/subset review, not permission to tune an unrelated parameter.

When LoG simultaneously misses weak particles and clusters on high-contrast
contamination, correct geometry and polarity first, then test the upper
contamination cutoff, accept a carbon-clean branch, and only then test the
detection threshold for recall. Never change both thresholds in one attempt.
Verify `log_diameter_min_A <= log_diameter_max_A` and that the rendered
detection threshold remains below the effective upper cutoff.

For two plausible Topaz FOM thresholds, advance the better-supported threshold
with `primary_fallback`, preserve the other, and activate it only if the primary
fails its recorded 2D cleanliness or view-diversity condition. Retrain only
when labels, training subset, particle geometry, or a systematic error pattern
changed materially.

## Extraction rules

Define the geometry state before extraction:

```text
field_width_A = box_size_pix * input_pixel_size_A
output_box_size_pix = rescaled_size_pix if rescale_particles else box_size_pix
output_pixel_size_A = input_pixel_size_A * box_size_pix / output_box_size_pix
output_nyquist_A = 2 * output_pixel_size_A
```

Read `input_pixel_size_A` from the accepted extraction-micrograph optics table
(`rlnMicrographPixelSize`). Do not copy the movie-import pixel size, a prior
particle-stack pixel size, or a tutorial value. Pixel size is metadata-locked;
the agent chooses the even box and, for an exploratory stage, whether a
Nyquist-safe rescale is justified.

Use even box and rescaled dimensions. Make the physical field wide enough for
the maximum particle extent plus observed centering error, credible flexible
density, and a nonzero safety margin. Keep the intended stage resolution
comfortably coarser than the downsampled Nyquist; do not plan at Nyquist.
Verify `background_diameter_pix < box_size_pix` in the same pixel coordinate
system and require all derived physical sizes to be positive.

The workflow's import and extraction geometry is deliberately unbound. Supply
every name reported by `missing_explicit_param_overrides` as an actual
`param_overrides` key and require `execution_ready: true` before rendering or
running the step. Values under `tutorial_example_params` are documentation
only; copying them does not establish dataset geometry. For initial LoG and
Topaz extraction, explicitly derive `box_size_pix` and
`rescale_particles`; when rescaling is selected, also derive
`rescaled_size_pix` and record the resulting pixel size, field width, Nyquist
limit, and intended stage resolution. Supply `background_diameter_pix`
explicitly when the native automatic background choice is unsuitable; never
copy the tutorial example.

For the workflow's post-Class3D `reextract_refined_particles` step, final
high-resolution refinement requires native sampling:
`rescale_particles: false`. Derive the even native box from measured maximum
particle extent, centering/CTF-delocalization allowance, normalization
background, edge clearance, and available resources. A downsampled
post-Class3D pass is a distinct exploratory branch, not the final re-extraction
step; it needs an explicit recorded target-resolution/Nyquist justification and
must be followed by a new native-sampling extraction before final Refine3D,
CTF refinement, polishing, PostProcess, or LocalRes.

The contract permits derived sources for the coupled `box_size_pix`,
`rescale_particles`, `rescaled_size_pix`, and `background_diameter_pix`
geometry group. Guarded workflow steps still require the applicable values to
be supplied as explicit overrides. Submit a coupled geometry change atomically;
do not execute only part of it. The renderer enforces even dimensions,
conditional presence or absence of `rescaled_size_pix`, no upsampling, and a
background diameter smaller than the native box.

| Rule ID | Prerequisite and condition | Atomic action | Required validation |
| --- | --- | --- | --- |
| `extraction_fix_box_geometry` | Extent and pixel size are measured; signal is clipped or the field is excessively solvent-dominated. | Recompute the `box_size_pix`/`rescaled_size_pix` geometry group, changing only the components needed to restore containment while preserving the sampling objective. | Even dimensions, edge clearance, derived pixel size/Nyquist, memory feasibility, downstream mask diameter, and reference grid are all consistent. |
| `extraction_adjust_sampling` | Containment is correct; early classification is oversampled or reconstruction approaches the current downsampled Nyquist. | Downsample for early classification, or re-extract at finer sampling for refinement; enlarge the native box when delocalized CTF signal or offsets require it. | STAR/header pixel size, box size, Nyquist margin, mask geometry, and reference rescaling agree before downstream use. |
| `extraction_fix_background_normalisation` | Normalization is enabled and raw boxes are inspectable; particle signal enters the background circle or too few background pixels make estimates unstable. | Adjust only `background_diameter_pix`, recording which pixel coordinate system it uses. Keep the circle outside credible particle density and inside the box. | Normalized background is flat and particle statistics are stable without subtracting real signal. |
| `extraction_enable_dust_removal` | Isolated extreme bright or dark pixels are directly observed in particle boxes. | Enable only the affected `white_dust_stddev` or `black_dust_stddev`, normally beginning near five standard deviations; leave both disabled when outliers are absent. | Extreme pixels disappear without clipping or reshaping particle density. |
| `extraction_fix_contrast` | Acquisition convention or raw boxes show the current `invert_contrast` setting is wrong. | Correct the contrast convention as metadata/visual truth, not an adaptive aesthetic test. Invalidate affected extracted particles and downstream classifications. | Particle signal has the expected polarity and centering/classification behaves coherently. |
| `extraction_recenter_refined_particles` | Refined coordinate provenance is trusted and particles remain systematically off-center. | Set `reextract_refined_particles` and `recenter_refined_coordinates` for a new extraction branch. Treat explicit X/Y/Z focused recentering as a protected hypothesis. | Boxes are centered, coordinates trace to the source particles, and geometry remains unchanged unless separately justified. |

Never silently overwrite pixel size with a rounded workflow value; trust
compatible STAR/header metadata and record any calibrated correction. After
extraction, treat the produced particle STAR optics values
`rlnImagePixelSize` and `rlnImageSize` as the authoritative downstream grid;
verify them rather than relying on the requested arithmetic. A new geometry
state invalidates all downstream particle stacks, masks, references,
classifications, and refinements that depend on the previous grid.

Use `relion_render_particle_boxes` to inspect extracted boxes, with exact
particle identities and display transforms recorded. A normalized display of
an extracted stack is not an independent rendering of its original raw
micrograph; use the pick overlays for coordinate/source context. If a required
stack or view is unavailable, record the evidence gap and use compatible
existing artifacts or bounded downstream 2D validation when those can safely
resolve the choice. Keep extraction provisional until the needed evidence
exists; never claim an inspection from metadata alone.

## Selection rules

Selection thresholds are evidence-selectable only when the typed contract,
stage rule, approval envelope, and remaining budget permit them. Preserve
rejected rows/classes and exact input STAR provenance. The particle-selection
contract permits `minimum_selection_score` from `strategy_intent`, a workflow
default, or an explicit user override. Record the selected source and the
evidence that authorized any value beyond the first-round prior.

| Rule ID | Prerequisite and condition | Atomic action | Required validation |
| --- | --- | --- | --- |
| `selection_reject_bad_micrographs` | Visual CTF/motion evidence and at least one independent metric identify irreparable images. | Apply a bounded, reversible micrograph selection using multiple metrics; never use one universal tutorial cutoff. If metadata selection is not exposed by the selected operation, record a contract gap. | Representative spectra/micrographs, accepted distributions, optics/time coverage, and downstream particle quality support rejection. |
| `selection_start_ranker_threshold` | This is the first early-cleaning round for a newly picked or extracted particle population, not a reclassification of an earlier selected set, and no supported dataset-specific value supersedes the workflow prior. | Use `minimum_selection_score: 0.05` as a triage prior; preview `0.025` and `0.075` to reveal score sensitivity, not to set eligibility for retention or recovery. | Record exact class/particle counts and stratum losses, inspect all classes, and resolve the retained/recoverable/junk partition from evidence. |
| `selection_rescue_credible_class` | Direct inspection supports a coherent particle-like class at any score, no rejection red flag is present, and the parent continuation checks pass. | Include its exact class ID provisionally in the retained reconstruction candidates with a class-specific reason. Use the recoverable pool when evidence is still ambiguous; a low score does not force rejection. | Record score if available, image evidence, particle count, view-family relation, and existing corroboration or a bounded validation trigger. |
| `selection_veto_ranker_class` | A class at or above `S_cut` has high-confidence obvious-junk evidence satisfying the parent rejection rules. | Remove the exact class ID from the primary selection and preserve it in the rejected output. A high ranker score never overrides direct obvious-junk evidence. | Record the red flag, independent evidence root, exact particle count, and preserved rejected output. |
| `selection_protect_rare_view` | A class may contain a plausible rare/novel view but its utility remains uncertain; low occupancy alone is insufficient. | Place its exact particles in the ambiguous-recoverable pool and activate the bounded recovery diagnostic when coverage or attrition is material. | Reclassification or bounded downstream evidence supports promotion, continued ambiguity, or class-specific rejection; never require a favorable score before investigation. |
| `selection_raise_score_threshold` | A new Class2D round has been rescored from scratch; a simulated increase removes additional verified junk without threatening a protected view family or retention guard. | Raise `minimum_selection_score` by at most `0.025`, inspect every newly excluded class, and keep the previous threshold as an inactive fallback. Do not raise it merely because another cleaning round has elapsed. | Additional contaminant removal is demonstrated; retained particles/classes, protected views, defocus/ice/optics diversity, and the next 2D or diagnostic 3D result remain adequate. |
| `selection_lower_score_threshold` | Particle-like classes are lost across the score range or the protected-view/retention gate fails. | Hold or lower the triage threshold, or suspend threshold-based proposals and recalibrate. Preserve exact prior and candidate selections and activate eligible recovery. | The revised choice restores coverage without material junk increase and is supported by reclassification or bounded downstream validation. |
| `selection_remove_duplicates` | Coordinate provenance or distance statistics show duplicate particles that threaten half-set independence. | Render `relion_render_remove_duplicate_particles`, inspect its physical distance, coordinate pixel scale, and both output paths, then use `relion_remove_duplicate_particles` as an additive write within the requested processing scope. Preserve its retained and removed STAR files. | Validated counts partition the input exactly; duplicate counts and nearest-neighbour distribution improve while crowded genuine particles remain. |
| `selection_regroup_small_groups` | RELION reports unstable scale/noise estimates because per-micrograph groups are too small. | Regroup only compatible micrographs, targeting roughly 10–20 particles per group as a planning heuristic; current regroup control is contract-gated. | Group sizes become stable and no distinct optics condition is merged. |

For duplicate removal, interpret native `--image_angpix` as the pre-extraction
micrograph coordinate scale. Require authoritative `rlnMicrographPixelSize`
values to agree across every referenced optics group; never substitute
`rlnMicrographOriginalPixelSize` or particle `rlnImagePixelSize`. If the
authoritative field is absent, require an explicit verified value and
provenance record even when those other pixel sizes agree. If referenced groups
resolve to different coordinate scales, split them rather than forcing one
scalar. An explicit value must match authoritative metadata when it is
available. Require all native SPA coordinate, micrograph, optics-group, and X/Y
origin columns during rendering; a fresh unaligned extraction without origins
is not executable by this native utility. Require the optics table to list
contiguous group IDs `1..N` in row order so native `loadSafely` cannot silently
renumber particle metadata. Require finite coordinate/origin values and real
image and micrograph names rather than STAR null markers. Reject the
tomography-triggering `rlnCenteredCoordinateZAngst` column. Native-bound files
must be valid UTF-8 with no NUL byte or carriage return. Require complete
one-row-per-physical-line loops without inter-row full-line comments; the
particle loop must be the first data construct, and `data_optics` must contain
exactly one loop and no scalar list. Reject ambiguous native block/loop
substring dispatch, malformed or duplicate logical labels, and RELION
writer-special loop columns. Parse values with native simplification and token
rules, reject null markers for recognized non-string types, and require strings
to survive native writer/readback. Normalize typed scalar/vector presentation
only as RELION's pinned 5.0 writer does.

Review the renderer's predicted retained output and implicit
`<stem>_removed.star` sidecar before writing. Both are no-clobber destinations
beneath `Deduplicate/`; the write snapshots the input, stages and structurally
validates the retained table and any nonempty removed table, accepts native
RELION's zero-byte sidecar as exactly zero removals, requires at least one
retained row, and requires counts plus the `rlnImageName` and complete-row
metadata multisets to partition the input. It also requires the retained optics
metadata to match the input after type-aware native normalization and rejects
an optics block in the removed sidecar. It publishes neither artifact over an
existing path. Preserve the removed-particle sidecar as
reversible provenance and stop on any metadata, structural, identity, count, or
publication validation failure.

Apply predeclared minimum retained-particle and class-count guards before
accepting an automatic threshold. Those minimum-retention controls are not
currently exposed, so enforce the guard in assessment and state rather than
claiming RELION rendered it. Also compare attrition across optics groups,
micrograph strata, and protected view families so a globally acceptable count
cannot hide a selective loss.

## Early 2D cleaning policy

Use class-ranker score as a triage prior, not a probability of a good particle
or an eligibility cutoff for recovery. Use `S_cut = 0.05` for the first round
on a newly picked or extracted population unless a user value or supported
calibration supersedes it. Preview nearby cutoffs to measure sensitivity;
there is no fixed score band outside which credible particles cannot be kept.
A high ranker score never overrides direct obvious-junk evidence.

Partition every assessed input into three disjoint, exhaustive pools:

- `retained`: credible reconstruction candidates at any score, including
  weaker or minority views when their particle utility is supported. Record
  score, morphology, selection reason, and provisional/final confidence.
- `ambiguous_recoverable`: unresolved low-score classes, plausible rare views,
  mixed particle/junk classes, or classes limited by correctable alignment or
  geometry problems. This is an active diagnostic pool, not proven junk.
- `proven_junk`: class-specific high-confidence rejection supported under the
  parent rules, such as ice, carbon edges, aggregates or overlaps, empty picks,
  or obvious contaminants. Preserve source particles and rejection evidence.

For an InitialModel handoff, derive a role-specific seed ledger over that same
source inventory without replacing the canonical pools. Give every source
particle exactly one of these roles and retain its canonical-pool link:

- **seed-eligible** (`seed_eligible`): credible, representative reconstruction
  candidates from the `retained` pool. Include supported weaker or minority
  appearances needed to represent the population; never choose this role from
  sharpness, class population, occupancy, or ranker score alone.
- **uncertain** (`uncertain`): the `ambiguous_recoverable` pool and any
  particle whose seed role remains unresolved. Preserve it for a bounded
  recovery or later reassessment; exclusion from the current seed is not a
  permanent rejection.
- **clear junk** (`clear_junk`): only particles already supported as
  `proven_junk` under the parent high-confidence rejection rules. Keep their
  source identities and rejection evidence; never recycle them automatically.

Record the original source fingerprint and count, exact class and particle
identities in all three roles, the complete selection history, and the evidence
for every transition. Prove that the roles are disjoint and exhaustive against
the preserved source rather than inferring the partition from counts. For each
branch, record `materialized_seed_ids` separately from the full eligible set;
particles omitted only by balancing or a compute cap remain seed-eligible with
their omission reason and recovery path. When credible particles support
clearly incompatible structural populations, assign separate `seed_branch_id`
values and materialize separate seed-eligible branches; do not blend them
merely to raise particle count or discard a plausible minority branch because
it is smaller.

Link any previously established junk disposition to its original decision.
A current medium-confidence `primary_fallback` does not introduce new final
rejections; unresolved exclusions remain ambiguous under the parent rules.

A low score, low occupancy, fuzziness, unusual projection, or lack of crisp
features does not by itself prove junk. Nor does it prove a rare view. A mixed
class belongs in recovery when credible particles may be separable; do not
retain the whole mixed class solely because some particles look plausible.
Duplicate particle-like classes can indicate over-partitioning rather than
contamination. When every class appears poor, apply the reject-all guard and
inspect upstream polarity, CTF, picking, centering, and extraction geometry.

Inspect every class using `relion_render_2d_classes`. Inspect every returned
page and reconcile its coverage report with the complete source class list;
a truncated first contact sheet is not a complete visual assessment. Record
render provenance and the inspected class IDs. Before exact selection, call
`relion_validate_2d_class_review` with the source `model_star` and the exact
returned page metadata as `rendered_pages`; require a valid, complete review
coverage result. It validates page/source hashes and IDs, not whether the active
model actually inspected the pixels. Keep the parent's direct-inspection
requirement; stale pages or metadata-only access cannot satisfy it. Use focused panels for uncertain
classes and disagreement with the score. Visual morphology and a ranker score
computed from the same averages may be causally coupled; they do not
automatically supply two independent roots. Credible retention may remain
provisional while final acceptance/rejection follows the parent's evidence
rules.

Before and after materialization, call `relion_audit_particle_selection` on
the exact source `particles_star`, first with proposed `selected_class_numbers`
and then with `selected_particles_star` as appropriate. Record counts,
identity/metadata/half-set findings, and retention by available acquisition
strata using declared columns and numeric bins. Audit each pool against the
same source, and reconcile their disjoint class/particle identities and total
count. Do not infer an exact partition from counts alone. The tool reports
measurements, not scientific acceptance, junk labels, or biological view
assignments. Infer putative view families from inspected classes and use
trusted downstream orientations when available; record uncertainty.

If ranker output is unavailable, first inspect the mapped ranker failure,
record the gap, and suspend score thresholds. Do not invent scores. Use class
images, particle evidence, the selection audit, and bounded validation to
advance a credible provisional retained pool under `primary_fallback` when
eligible; preserve alternatives without rejection. When scores repeatedly
contradict obvious particle/junk evidence, record a calibration failure and
stop threshold escalation instead of forcing the data into the score prior.

A clean, sufficiently diverse first Class2D result can proceed after this
assessment and the recovery-trigger check below. A second cleaning round is
not mandatory. Run another uniquely aliased classify-select round only for a
recorded residual problem such as mixed classes, contamination, poor centering,
or material ambiguous losses, with a finite budget and predicted improvement.
Reassess every output from scratch: class numbers do not carry across runs,
and neither do their scores. Carry a calibrated threshold through a sequential
lineage; adjust it only under the evidence-based selection rules, never by
round number. A recorded change in input, hypothesis, or independent
initialization makes a repeat interpretable; repeating solely to obtain a
prettier montage does not.

Stop cleaning when quality/coverage plateaus or worsens, the retained pool is
unchanged, no supported residual issue remains, an upstream defect dominates,
or the campaign budget is exhausted. Continue from the last usable assessed
pool, preserving unresolved candidates and their limitations.

## Bounded recovery before Topaz or 3D

At each handoff to Topaz training or InitialModel/Class3D, check for material
particle attrition, lost plausible view/acquisition coverage, or mixed/uncertain
classes containing potentially usable particles. Define materiality from this
sample's particle support and coverage; no universal retained fraction or
absolute particle count establishes quality. Record the trigger, contrary
evidence, exact recoverable pool, source fingerprint, and remaining budget.
If a trigger is present and a valid diagnostic fits that budget, actively test
recovery before freezing the labels or reconstruction input. Mere preservation
of an inactive STAR does not satisfy this diagnostic. If no trigger is present,
proceed without an extra cleaning round.

Choose one bounded hypothesis from the earliest supported defect:

1. Repair verified centering, box, mask, contrast, or CTF problems before
   reclassification, preserving the original input and all dependent metadata.
2. Reclassify the ambiguous-recoverable population with a justified K/T/search
   change or a targeted independent initialization. A small weak pool may need
   a recorded, representative subset of retained particles for alignment
   support; include that support only through an available typed exact
   selection/join path. Fingerprint recoverable and support identities
   separately, prove no duplicates, and count recovered yield only from the
   original recoverable pool. Support particles do not become new recoveries.
3. Use bounded downstream validation when credible particle evidence supports
   it and 2D separation cannot resolve the ambiguity. Keep the baseline primary
   and assess the diagnostic before promoting its candidates. This permission
   does not change the separate Class3D union or resolution-recovery gates.

Do not automatically recycle proven junk. Reopening a rejected population
requires new contrary evidence or correction of a documented invalid earlier
assessment. Give each diagnostic an exact input and source-checkpoint identity,
scientific hypothesis, one finite attempt allocation, and an exit condition.
Do not reset a consumed allocation because a new STAR was materialized. A
further diagnostic needs new evidence and room in the same overall campaign
budget; an inconclusive result does not justify an unlimited salvage loop.

Promote only newly assessed credible particles into the retained pool, with
class/row provenance, source-to-output identity checks, and an additive typed
materialization. Preserve existing optics/CTF metadata and `rlnRandomSubset`
assignments. A cross-job union requires a supported typed join and duplicate
check; do not concatenate or edit STARs by shell. If the necessary operation
is unavailable, record the contract gap and keep the recovery separate.
Before using a recovered particle as a Topaz label, apply the stricter label
precision and training-micrograph membership checks above. Unresolved candidates
remain ambiguous, with evidence and future triggers recorded rather than a
junk label. At the limit return to one assessed primary continuation; report
residual coverage risk without automatically asking for more resources.

A targeted independently initialized 2D or InitialModel diagnostic may keep
other scientific settings fixed when initialization sensitivity would change a
consequential selection or reference choice. Declare that hypothesis and the
bounded validation in advance, and compare matched identities rather than
class numbers. Use an explicit seed only if a typed, render-tested control is
available; the current contract has no general seed mapping. Otherwise record
what initialization provenance the native job exposes and do not claim a
controlled independent seed or bypass the contract with free-form arguments.
Do not require this test for every credible first result.

## 2D-classification rules

Use the workflow K and T only as first-attempt priors after geometry and input
quality are verified. Roughly 100 particles per class is a planning guard
against excessive K, not an acceptance criterion; rare views and true
heterogeneity may justify different occupancy.

| Rule ID | Prerequisite and condition | Atomic action | Required validation |
| --- | --- | --- | --- |
| `two_d_select_algorithm` | First-attempt particle count and compute profile are known. | Use bounded EM iterations for only a few thousand particles or VDAM mini-batches for a large set, if the contract permits changing the algorithm; treat 100 VDAM batches as fast and up to 200 as quality-oriented. | Class quality and convergence behavior justify the mode; runtime alone does not. |
| `two_d_increase_class_count` | Coherent projections remain mixed in a few well-occupied classes without overfit-noise red flags. | Increase `num_classes` by about 1.5–2x within bounds; hold T fixed. | New coherent views appear; accepted fraction/diversity remain adequate; tiny unstable classes do not dominate. |
| `two_d_decrease_class_count` | Many tiny noisy or near-duplicate classes appear without additional plausible views. | Decrease `num_classes` by about 25–50%; hold T fixed. | Occupancy and stability improve without merging distinct good views. |
| `two_d_limit_overfit` | Radial streaking or high-frequency solvent noise is visible and geometry is correct. | First test `e_step_resolution_limit_A` in a bounded 7–12 Å range. In a separate attempt, lower `regularisation_parameter`, for example 2 toward 1–1.5; never change both initially. | Solvent flattens and streaking decreases while internal structure and minority views persist. |
| `two_d_raise_T` | Classes are stable and clean but consistently oversmoothed, with no overfit red flag. | Raise `regularisation_parameter` cautiously, normally 2 toward 3. | Reproducible internal detail improves without new noise or unstable assignments. |
| `two_d_fix_mask_geometry` | Verified extent/box geometry shows particles or classes are clipped or the mask contains excessive solvent. | Supply a positive finite explicit `mask_diameter_A` from measured/derived geometry or another contract-authorized source; no tutorial mask diameter is executable by default. | Credible signal is contained with nonzero clearance and without excessive solvent. |
| `two_d_fix_centering` | Particles/classes are off-center without a mask or extraction-geometry error. | Change only `offset_search_range_pix` from measured displacement; hold K, T, mask, and offset step fixed. | Centering improves without unstable or implausibly large translations. |
| `two_d_refine_angular_sampling` | Geometry/centering are correct; a large or highly symmetric particle shows angular discretization limits. | Decrease `inplane_angular_sampling_deg` by one supported step; hold offsets fixed. | Stable reproducible detail justifies added compute without class fragmentation. |
| `two_d_increase_vdam_batches` | A 100-mini-batch VDAM run is still improving and has not stabilized. | Launch a preserved attempt with up to 200 `number_of_vdam_mini_batches`; keep scientific model parameters fixed. | Late-batch class identities and quality stabilize; otherwise stop or rediagnose. |
| `two_d_fix_low_frequency_ctf_behaviour` | CTF metadata and amplitude contrast were checked; spuriously strong low-frequency correction collapses particles into fuzzy classes. | Test native ignore-until-first-CTF-peak only after it gains a typed mapping; keep the setting consistent downstream. | Class separation improves without a new CTF inconsistency. |
| `two_d_focused_no_alignment` | Trusted poses and an explicit focused-classification objective exist. | A processing or optimization request covers supported reversible focused/no-alignment experiments; record the structural target and matched baseline. The current neutral operation does not expose this mode, so record a contract gap before proposing execution. | Compare against the aligned/unfocused parent and require reproducible classes without mask/reference leakage. |

Keep `zero_mask_particles: true` for ordinary classification. Consider
noise-filled solvent only as a matched, contract-authorized response to
zero-mask correlations or streaking. Center classes only for the expected
positive/white particle signal, confirmed after extraction inversion rather
than inferred from native micrograph contrast alone. `allow_coarser_sampling`
is a speed policy, not scientific evidence, and is not exposed by the current
neutral operation.

Do not increase K in response to overfit noise. Do not raise T to separate
mixed morphology. If most classes are junk, return to picking, contamination,
contrast, extraction, centering, and geometry rather than using extreme K/T.

## 2D assessment and exact class selection

Require the following for normal ranker-based selection; if the ranker is
unavailable, follow the explicitly recorded provisional validation route above:

- final optimiser, model, and data STAR files;
- class distributions and class-ranker scores from the mapped ranker path;
- a labelled contact sheet showing every class with the same recorded display
  rule and complete renderer coverage reconciled across all pages; contrast
  limits are computed per panel, so displayed amplitudes are not comparable,
  plus focused panels for score disagreements, high-score vetoes,
  and rare-view candidates;
- exact class and particle counts at `S_cut - 0.025`, `S_cut`, and
  `S_cut + 0.025`, plus retained fraction, view-family coverage, and
  per-stratum attrition;
- downstream validation when selection trains Topaz or seeds 3D.

Positive evidence for retention includes centered recognizable projections,
internal structure, flat solvent, plausible distinct or minority views, and
consistent unclipped dimensions. Lack of crisp detail is not evidence of junk.
Rejection red flags include radial streaks, non-particle high-frequency noise,
off-centering or clipping severe enough to erase particle-like features, ice,
carbon edges, contamination, aggregates or overlaps, empty boxes, and no
particle-like features. Duplicate particle-like classes are not a rejection
criterion. Unprotected plausible rare views, an unaudited ranker boundary, or
selection by occupancy or resolution alone invalidates the proposed selection.

Apply the retained/ambiguous-recoverable/proven-junk partition above. A ranker
threshold proposes a triage set, not the final exact selection. Materialize
the assessed exact class IDs and preserve the other pools. Audit retention and
activate an eligible bounded recovery diagnostic before the next handoff when
material attrition, missing views, or unresolved mixed classes warrant it.

For an accepted exact class set, or a credible provisional exact set selected
under `primary_fallback` for continued downstream processing, call
`relion_plan_particle_class_selection`, inspect the resolved
`materialization_mode`, exact class/count contract, and rendered options, then
launch it with `relion_run_particle_class_selection`. Preserve the full source
and every alternative. Record a provisional set in the decision and processing
attempt, never in `accepted_outputs` until final acceptance passes. It may feed
normal downstream stages, including Topaz, InitialModel, Class3D, refinement,
and postprocessing when their technical prerequisites pass; assess and record
confidence after each stage. Materialization does not itself establish
scientific acceptance. Define
`candidate_class_count = len(selected_class_numbers)` at materialization time,
where `selected_class_numbers` contains the exact retained class IDs. When
`candidate_class_count > 3`, prefer the one-job `gui_equivalent` path by
using `materialization_mode=auto` and verifying that the resolved mode is
`gui_equivalent` when the fixed selector is available. If it resolves to
`select_joinstar`, record the unavailable-selector fallback reason and retain
that native graph as the exact fallback. The same `auto` preference remains
valid for smaller sets. Pass the classification optimiser or model as
`class_source_star` when selected 2D class-average metadata is needed. Never
infer an exact class selection from the first matching STAR file.

Before binding a set to Topaz training or `de_novo_initial_model`/Class3D,
record complete class-image coverage, reconciled scores or their documented
absence, exact pool identities and selection audit, and the recovery-trigger
assessment for this source. Record `early_2d_cleaning_rounds` as history, not
a minimum-round gate. If no residual problem supports another round, continue
after one assessed Class2D result. Topaz consumes the curated training labels
on the training micrographs; 3D consumes the appropriate inclusive assessed
reconstruction pool. Neither role silently inherits the other's exclusions.

When `three_d_validate_similar_class_union` requests staged singleton and union
selections, materialize A and B independently from the same final Class3D data
STAR before singleton refinement. Inspect their output rows to verify
duplicate-free, disjoint set identities, unchanged optics/CTF metadata, and
unchanged per-particle `rlnRandomSubset`. Materialize A ∪ B independently from
that same final Class3D STAR only after the refined singleton-map gate passes,
then verify exact union arithmetic and repeat the metadata and half-set audit
before union refinement. The planner's count contract alone is not this
provenance audit.

## Budgets and upstream routing

- Use three Topaz training cycles and three same-input 2D scientific attempts
  per particle set as default stage allocations. These are agent planning
  allocations: rebalance them autonomously within the recorded finite overall
  campaign limit when new evidence justifies it. Never exceed user or hardware
  limits, or reset the campaign limit after changing a stage allocation.
- Record `early_2d_cleaning_rounds` across the campaign.
- After every sequential classify-select round, assess class coherence,
  recovered usable particles, contamination, and view/stratum coverage. An
  additional round needs a supported unresolved hypothesis; a clean first
  result need not be classified again.
- Preview each threshold increase before execution, limit an accepted increase
  to `0.025`, and keep the previous thresholded selection as an inactive
  fallback.
- Document the evidence and justification for every additional round.
- Stop when improvement plateaus, worsens, or evidence suggests over-cleaning.
  This ends the cleaning loop; continue the requested pipeline from the last
  usable primary or fallback without asking whether to continue.
- Charge active recovery and targeted initialization tests to the same finite
  campaign budget as ordinary cleaning. Preserve exact diagnostic/support
  identities and record why each pool was promoted, retained as ambiguous, or
  rejected under the parent rules.
- Do not use a fixed number of rounds as a target or stopping criterion, and do
  not use the round number itself as a reason to raise the threshold.
- Treat a newly accepted particle set as attempt 1 of a new input-data state.
- Reset the same-input attempt budget for that state, but never reset the
  campaign-level `early_2d_cleaning_rounds` counter.
- Stop a threshold/K/T loop when its predefined evidence plateaus or worsens.
- Return upstream when particle identity, polarity, scale, contamination,
  centering, clipping, or view coverage remains the dominant failure.
- Resolve ambiguous morphology, missing images, threshold ranking, or particle
  size variation through available renders, metadata, and bounded validation
  first. Preserve a conservative primary and ordered alternatives when a
  credible `medium`-confidence primary is available for downstream processing.
- Use `human_gate` with `await_human` only for an essential unresolved gap that
  blocks useful progress or a focused/no-alignment objective outside scope;
  existing authorization does not require another confirmation. Record
  unresolved evidence as `defer` with `repeat`, `retry`, or branch-local `stop`
  when no user answer is needed. Exhausted optimization returns the best
  supported result; it does not trigger a default request for more budget.
