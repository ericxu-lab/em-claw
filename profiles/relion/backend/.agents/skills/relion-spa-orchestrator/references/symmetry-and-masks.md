# Symmetry and Masks

Use this reference for point-group hypotheses, symmetry-axis alignment,
symmetry imposition or relaxation, global solvent masks, and focused masks.
Apply the shared policy and contract gate in the parent
[orchestrator](../SKILL.md). This reference defines safeguards and acceptance
tests, not a generic parameter-optimization matrix.

## Contents

- [Scope and protected boundary](#scope-and-protected-boundary)
- [Symmetry evidence](#symmetry-evidence)
- [Exploratory symmetry scan](#exploratory-symmetry-scan)
- [Symmetry testing and acceptance](#symmetry-testing-and-acceptance)
- [Pseudosymmetry and relaxation](#pseudosymmetry-and-relaxation)
- [Global mask evidence](#global-mask-evidence)
- [Global mask acceptance and bounded repeats](#global-mask-acceptance-and-bounded-repeats)
- [Focused masks](#focused-masks)
- [State and completion](#state-and-completion)

## Scope and protected boundary

Treat symmetry as a structural hypothesis, never a tutorial constant. Begin
classification and initial inspection in C1 unless independently supported
symmetry has already been established within the requested task. Preserve the
C1 map, particle branch, and unmasked baseline.

Do not run LocalRes on every symmetry candidate. Use compatible existing
local-resolution evidence when available for candidate comparison. Select the
final C1 or symmetry branch before scheduling LocalRes, then opt in once for
that selected map. Run it earlier only when the user explicitly requests it.

Record the following as scientific hypotheses with preserved matched controls:

- imposing or changing point-group symmetry;
- choosing among alternative axis/convention settings, including icosahedral
  conventions;
- relaxing symmetry for pseudo-symmetric particles;
- introducing a focused mask for classification or refinement; and
- coupling a focused mask to no-alignment, local-search, subtraction, or
  explicit recentering behavior.

A processing or optimization request covers reversible, evidence-supported
symmetry comparisons, focused/no-alignment technical exploration of an already
supported structural target, global masks, and their matched controls. Record
the hypothesis and budget; do not require the user to name each experiment or
confirm it separately. An assessable candidate need not have final `high`
confidence before it can be tested. Do not invent a new biological target from
ambiguous density. Keep C1 or global processing primary when the experiment is
ineligible or inconclusive, and preserve every candidate. Resource availability,
a tutorial value, or one favorable score never provides scientific authority for a symmetry conclusion.

## Symmetry evidence

Collect at least:

- C1 map inspection for repeated subunits, consistent candidate axes,
  asymmetric features, ligands, and broken or incomplete repeats;
- an independently reproducible `relion_scan_symmetry` candidate-axis report
  with exact half-map and scan provenance;
- matched C1 and candidate-symmetry maps/half-maps at the same grid, frame,
  contouring policy, and filter;
- masked, unmasked, and phase-randomized FSC plus compatible existing
  local-resolution evidence when available;
- known composition, oligomeric state, stoichiometry, homologs, biochemical
  evidence, or suitably filtered atomic-model fits; and
- stability across an independent refinement, seed, particle subset, or other
  reasonable perturbation.

Quantitative alignment alone is insufficient. A map must already be on the
expected grid and its symmetry axis must follow RELION's convention before a
candidate operation is interpreted. Do not infer symmetry from particle count,
the tutorial dataset, global shape alone, or a single correlation value.

## Exploratory symmetry scan

Use `relion_scan_symmetry` only on two independently reconstructed C1
half-maps with preserved random-half assignments. Verify identical shape,
positive voxel size, origin, start indices, MRC axis mapping, and a common
molecular center.
Choose `analysis_lowpass_A` from resolution supported by both halves, not from
the nominal resolution of a sharpened combined map. If a mask is used, require
the same physical grid, record its provenance, and repeat without it or with a
reasonable nearby mask when mask-imprinted periodicity is plausible.

Keep the discovery bounded:

1. Stage 1 scans common rotational orders through `common_max_order` (default
   8).
2. The high-order sentinel checks for reproducible periodic evidence above the
   common range through `max_order` (default 32).
3. At most two supported sentinel anchors open neighboring Stage 2 orders
   bounded by `stage2_window` (1–8, default 2): the strongest evidence peak and,
   when present on the same per-half axis, one composite order supported by
   harmonic divisor closure.
4. Retain the ranked `max_candidates` baseline (default 5) in the exploratory
   shortlist. Explicit focus groups whose raw reproducibility gates pass remain
   visible—even when a later high-order control marks them confounded—and may
   expand the detailed list up to the eight-group focus bound.

Always read `scan_summary`, `result_completeness`, and `hypothesis_results`
before describing any group.
Absence from the capped candidates array is not evidence that a group failed the scan heuristics.
A requested hypothesis should
be named in `focus_groups` so that its per-group outcome and detailed evidence
remain visible independently of the presentation rank. Describe a group only
as passing or failing the scan heuristics under the reported center, effective
low-pass, mask, and search configuration; never shorten that statement to a
biological claim that the group is supported or unsupported.
For C4 and higher cyclic or dihedral hypotheses, also require the reported
high-order small-angle control to be assessable and free of both a monotonic
ramp and a uniformly high plateau. If too few C4-and-higher orders are
identifiable, if support rises monotonically as rotations shrink, or if it stays
nearly constant at high support, report the raw reproducibility result as
confounded/inconclusive—not as finite-order support or failure. For a dihedral
hypothesis, separately require its perpendicular C2 operations to meet the
reported half-map and cross-half thresholds.

The sentinel is not a symmetry candidate. It prevents the common scan from
silently missing a prime or otherwise high order while avoiding an open-ended
`C9`, `C10`, `C11`, ... / `D9`, `D10`, `D11`, ... refinement sweep. Interpret
harmonic relationships explicitly: for example, a true C12 architecture can
also support C2, C3, C4, and C6 evidence. Require compatible evidence in both
half-maps and treat inconsistent axes/orders, a flat ranking, or several
harmonically related leaders as ambiguous.

Compare independently found `D2`, `T`, `O`, and `I` orientations as complete
conjugated operator sets, not as one arbitrarily labelled axis. Polyhedral
groups contain several symmetry-equivalent characteristic axes, and `D2` has
an unordered orthogonal triad rather than a unique principal axis. Require a
tight operator-set match and reciprocal complete-group support in both
half-maps. Never average two different equivalent axes into a fictitious
consensus direction. Treat a reported `D2` axis frame as unordered and keep
abstract `I` separate from the unresolved RELION `I1`–`I4` convention choice.

The scan is read-only and does not require write approval, but its shortlist is
not scientific acceptance. The processing request supplies experiment
authority. The scan does not write an aligned reference,
apply a RELION convention, impose symmetry, or demonstrate that asymmetric
features are preserved. It identifies hypotheses from reproducible per-half
axis and complete-operator support. If composition, axis/convention, mask
dependence, or half-map agreement is unresolved, record the uncertainty and
inspect available diagnostics without making a symmetry claim. Continue C1
processing while unresolved; use `human_gate` with `await_human` only if the
requested objective requires that choice and no safe useful diagnostic remains.
Keep C1 as the processing baseline until later confirmation; that default is
not a biological rejection of a shortlisted group.
No symmetry-scan outcome accepts or rejects biological symmetry. Before making
either conclusion, require the matched C1 and candidate-symmetry refinement and
PostProcess evidence described below.

## Symmetry testing and acceptance

Use this practical sequence and test only a small candidate set covered by the
processing or optimization task and its finite budget:

1. Build the ab-initio InitialModel in C1 and preserve its provenance.
2. Run a gold-standard C1 Refine3D baseline with independent half-maps.
3. Scan those C1 half-maps to identify candidate group/axis hypotheses; preserve
   the scan artifact and every shortlisted convention.
4. Verify the exact candidate-axis transform and RELION convention, then run a
   candidate-symmetry Refine3D branch with the same particles and unchanged
   random-half assignments.
5. Apply matched PostProcess masks, filtering, sharpening policy, and FSC
   procedures to the C1 and candidate branches.
6. Compare the already aligned candidate branch against C1 using
   `relion_compare_symmetry` as a global triage view plus matched local map
   inspection, and validate masked, unmasked, and phase-randomized FSC,
   directional resolution, compatible existing local-resolution evidence when
   available, and an independent refinement, seed, particle subset, or held-out
   comparison.

During the matched comparison, explicitly inspect whether imposing symmetry
causes:

- disappearance of density reproducibly present in both C1 half-maps;
- smeared side chains or domains where local resolution supports that claim;
- split or distorted helices;
- weakened ligand or cofactor density;
- apparently fractional occupancy, treated qualitatively unless separately
  calibrated; or
- distinct conformations being averaged into one.

Compare unsharpened and commonly low-passed maps first, followed by matched
PostProcess views. Hold the physical grid/frame, effective resolution, display
transfer function, and contour or enclosed-volume convention fixed. Verify any
local loss in both C1 half-maps and against compatible existing local-resolution
evidence when available. The current `relion_compare_symmetry` central-slice
report is an overview, not a sufficient side-chain, off-center ligand, domain,
occupancy, or conformation test.

Finally accept a candidate symmetry with `high` confidence only when all of
these hold:

- one specific group and axis are strongly favored by quantitative evidence;
- the C1 map directly shows the corresponding repeated architecture;
- symmetrization improves repeat consistency without creating duplicated
  noise, disconnected density, or false high-resolution features;
- composition and stoichiometry do not contradict the group;
- none of the matched-comparison symmetry-damage findings above is reproducible;
- asymmetric ligands/subunits or partial occupancy are not being erased; and
- an independent branch supports the same result.

The scan shortlist alone neither accepts nor rejects a candidate. A nominal FSC
or resolution gain cannot override reproducible local density damage in the
matched comparison.

Treat a Class3D-only symmetry result as provisional when it lacks corresponding
half-map evidence. Route a matched Refine3D comparison through the parent
orchestrator within the processing or optimization task before final
high-confidence symmetry acceptance.

When C1 and one candidate remain plausible but rankable, use
`primary_fallback`: advance the better-supported branch and preserve the other
with a concrete refinement/validation activation trigger. Default to C1 when
evidence for imposition is not decisive.

When groups/conventions fit similarly, composition is uncertain, asymmetric
components may exist, images are unavailable, or candidates cannot be ranked,
continue the supported C1 branch and collect available diagnostic evidence.
Use `human_gate` with `await_human` only when the unresolved choice is essential
to the requested objective and no safe useful route remains. Never average over
these uncertainties by running all branches downstream without a stated
objective and budget.

## Pseudosymmetry and relaxation

Symmetry relaxation is not a routine response to a poor strict-symmetry map.
Require independent evidence for pseudo-symmetry, a reference aligned to the
appropriate convention, trusted particle poses, and a specific asymmetric
question. Preserve C1 and strict-symmetry parents and predefine evidence that
would distinguish improved asymmetric signal from symmetry-related pose
instability.

The current Class3D and Refine3D neutral operations do not expose native
symmetry relaxation. Record a `contract_gap`; never emulate it through generic
arguments. If the user authorizes separate contract development, complete and
render-test that work before resuming the scientific branch.

## Global mask evidence

Generate a small bounded set around a defensible baseline rather than accepting
one arbitrary threshold. For every candidate record:

- input map and pixel size;
- `lowpass_filter_A`, `initial_binarisation_threshold`,
  `extend_binary_map_pixels`, and `soft_edge_width_pixels`;
- map/mask overlays on orthogonal slices at multiple contours;
- mask volume, connected-component count, enclosed solvent fraction, and
  minimum clearance from credible density to the hard/soft boundary;
- whether flexible, weak, membrane-associated, or partially occupied density
  is contained;
- masked/unmasked/phase-randomized FSC and nearby-mask stability; and
- matched map appearance and interpretation after postprocessing.

A low-pass around 15–20 Å is a planning prior for initial mask generation, not
a universal value. Increase its Å value when high-frequency mask detail tracks
noise. Interpret threshold, extension, and edge width jointly through their
resulting physical boundary; do not optimize a scalar FSC value.

For a membrane protein, include the whole protein plus coherent, reproducible
lipid or detergent/micelle density that is supported in both half-maps at a
compatible filter, then add a soft margin. Do not make a very tight protein-
only mask, cut stable surrounding density, or expand the mask around the entire
diffuse micelle. Preserve nearby mask variants when the stable boundary is
uncertain and compare matched FSC and map evidence through a bounded mask
validation. If the boundary remains consequentially ambiguous, preserve the
conservative validated mask and defer the affected interpretation; ask only
when that interpretation is essential and diagnostics cannot resolve it.
Membrane association alone never authorizes solvent-corrected FSC.

## Global mask acceptance and bounded repeats

Accept a global solvent mask only when:

- it contains all credible molecular density with nonzero safety margin;
- it excludes most solvent and unrelated disconnected noise;
- its edge is soft and does not conform to high-resolution noise;
- nearby reasonable mask settings yield similar FSC and map interpretation;
  and
- phase-randomized FSC is approximately zero at the reported resolution.

Within a bounded mask sweep covered by the processing request:

- lower the binarization threshold or increase extension when credible density
  is excluded;
- raise the threshold only when disconnected solvent noise is included and
  weak real density remains safe;
- widen the soft edge when the boundary is too sharp or phase-randomized FSC
  shows mask correlation; and
- use a smoother pre-threshold low-pass when the mask follows noisy detail.

These are acceptance safeguards, not autonomous rule IDs. Preserve every mask
and postprocessing result, change one boundary hypothesis at a time, and stop
when nearby masks are stable. Diagnose uncertain multi-component occupancy,
flexible/disordered regions, membrane or micelle boundaries, or unstable FSC
through overlays, half-map evidence, and bounded nearby-mask comparisons.
Retain the last validated global mask when possible. Ask for a biological
boundary choice only when it is essential and no safe useful diagnostic or
fallback remains; never accept an unstable mask merely to continue.

## Focused masks

Require a mature consensus map, trusted orientation provenance, a supported
structural target and testable local processing question, and an unmasked
baseline. Technical exploration need not claim the target's biological identity.
A focused mask must have a soft boundary, exclude unrelated signal without cutting the target, and remain
stable under a modest threshold/extension/edge perturbation.

Before use, record:

- the target region and why global classification/refinement cannot answer it;
- map and coordinate frame, pixel/box geometry, and alignment to the parent;
- mask overlay, clearance, connectedness, and volume;
- whether alignment will remain global, become local, or be disabled;
- the matched unmasked/aligned baseline and existing processing authority; and
- rejection criteria for mask-shaped classes, boundary leakage, reference
  imprinting, orientation drift, or lost global consistency.

Class3D exposes an optional typed `reference_mask` input mapped to native
`fn_mask`. A processing or optimization request covers a bounded focused
classification on a supported structural target. Validate the mask binding and
grid, verify the native render, and preserve a matched unmasked/aligned
baseline. That authority also covers no-alignment validation when trusted
orientations and a supported technical hypothesis exist; no separate
confirmation is needed. Set `alignment: false` and verify native
`dont_skip_align == No` before execution. The availability of typed fields does not authorize a new
biological target.

## State and completion

Record every symmetry candidate, convention, alignment transform, C1 parent,
source half-map pair and random-half provenance, analysis low-pass, optional
scan mask, common/high-order bounds, sentinel result, Stage 2 window, half-map
agreement, matched filtering, evidence artifact, confidence, and fallback
trigger. Record every mask's generating parameters, map/grid provenance,
report SHA-256, FSC artifact, acceptance status, and downstream consumers.

For routine continuation, record an assessed primary with its provisional or
accepted symmetry status (including C1), a technically validated compatible
mask, and a contract-valid next step. Final symmetry claims still require the
full acceptance evidence above. A provisional status does not require a pause.
Preserve unresolved candidates and focused masks; never overwrite or discard them.
