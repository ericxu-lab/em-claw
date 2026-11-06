# Refine3D and Postprocessing

Use this reference for Refine3D planning and acceptance, reference/mask use,
postprocessing, and the refinement validation required after CTF refinement or
Bayesian polishing. Apply the parent [orchestrator](../SKILL.md), and read its
routed symmetry/mask policy whenever symmetry or a mask enters the plan.

## Contents

- [Entry conditions](#entry-conditions)
- [Geometry, reference, and half-set invariants](#geometry-reference-and-half-set-invariants)
- [Refine3D parameter rules](#refine3d-parameter-rules)
- [Solvent-flattened FSC decision and validation](#solvent-flattened-fsc-decision-and-validation)
- [Refinement acceptance](#refinement-acceptance)
- [Postprocessing safeguards](#postprocessing-safeguards)
- [CTF-refinement and polishing handoff](#ctf-refinement-and-polishing-handoff)
- [Repeat and completion policy](#repeat-and-completion-policy)

## Entry conditions

Before rendering Refine3D, verify and record:

- one accepted homogeneous particle branch, one recorded activated fallback,
  or a credible assessed provisional branch continuing the requested workflow,
  including an eligible singleton/conditional-union diagnostic under
  `three_d_validate_similar_class_union`, with its half-set assignments;
- particle pixel size, even box, physical field width, Nyquist, CTF/optics
  provenance, polishing state, and centering history;
- reference source, hand, symmetry, pixel/box grid, origin/frame, filter,
  greyscale, and whether it is a joint map or corresponding gold-standard
  half-map;
- any optional mask's source map, grid, threshold/extension/edge, validation,
  and boundary clearance;
- the objective: exploratory consensus, validation of a material upstream
  change, or final high-resolution refinement; and
- approved scientific/resource hybrids, protected hypotheses, and remaining
  budget.

Unknown half-set provenance, mismatched geometry, an unsupported or improperly
framed symmetry experiment, or an unvalidated required mask blocks that branch. Auto-refinement is
convergence-driven; never invent an iteration-tuning loop.

When invoked by `three_d_validate_similar_class_union`, treat each singleton
and the exact union as a distinct upstream particle state and apply this entire
reference identically to every matched branch. The Class3D reference owns the
trigger, common-reference/mask controls, pairwise comparison, and aggregate
decision; this reference owns each branch's Refine3D and PostProcess validity.
For a first singleton or union refinement, its matched sibling branch is the
comparison parent; a pre-existing accepted map is not required.

## Geometry, reference, and half-set invariants

- Match particle and reference pixel size, box, frame, origin, hand, and
  symmetry convention exactly. Prefer an explicit provenance-recorded rescale
  over silent `resize_reference_if_needed`.
- Preserve original random half-set assignments. Never seed both halves from a
  joint high-resolution map in a way that leaks information.
- For a first refinement from a selected class, joined map, or external map,
  use a strong initial low-pass, commonly about 40–60 Å and often 50 Å as a
  planning prior.
- After CTF refinement or polishing, a higher-resolution start such as about
  8 Å is justifiable only with the corresponding half-map pair and intact
  half-set independence.
- Set `reference_map_absolute_greyscale` from provenance: an unchanged
  compatible RELION reconstruction may retain absolute scale; an external,
  filtered, transformed, or resampled map normally should not.
- Final high-resolution refinement starts from a native-sampling re-extraction
  with `rescale_particles: false`. Derive its even box from particle support,
  centering/CTF-delocalization allowance, background, edge clearance, and
  resources. Never carry a coarse Class3D rescaled size into the final branch.
- A downsampled post-Class3D refinement is an explicitly named exploratory
  branch only. Record a target-resolution/Nyquist margin before running it and
  return to a new native-sampling extraction before final Refine3D, CTF
  refinement, polishing, PostProcess, or LocalRes.

Starting-reference provenance governs causal interpretation, not whether
compatible final maps can be compared. Final maps from different strongly
low-passed starts may be aligned with `relion_compare_maps` and descriptively
compared; record the reference difference. Use the exact same starting
reference only for a matched claim that isolates particle-set or method effects.
Strong low-pass suppresses fine-detail imprint but does not prove equivalence
of coarse pose, envelope, hand, or convergence basin.

The current greyscale field accepts a workflow default or explicit user
override, not a provenance-derived/strategy source. Keep the supported value as
a candidate until an allowed source and render pass. Any geometry change also
invalidates the old reference grid and mask; explicitly regrid the reference
through a typed route and regenerate/revalidate the mask on the new grid.

The post-Class3D re-extraction and reference-regrid workflow steps are
deliberately unbound. Supply every name reported by
`missing_explicit_param_overrides` as a real `param_overrides` key and require
`execution_ready: true`. Read the native input pixel from the accepted
micrograph optics table, then verify the completed extraction's actual
`rlnImagePixelSize` and `rlnImageSize`. Read the selected Class3D map's input
pixel size from its MRC header. Use those measured values for
`output_pixel_size_A` and `new_box_size_pix`; do not copy
`tutorial_example_params`, an earlier coarse particle grid, or rounded planned
values.

Use `reported_or_target_resolution_A <= 1.2 * output_nyquist_A` as a
conservative trigger to review finer re-extraction; it means the target is
within 20% of Nyquist in Å units. This is a planning guard, not proof of usable
higher-frequency signal. Select the final margin from FSC falloff, CTF
delocalization, particle size, box clearance, and compute budget.

Current typed-route limitations are scientifically consequential:

- `particle_extraction.refined_particles` does not accept
  `polished_particles`; finer re-extraction of a polished branch must start
  from an auditable compatible pre-polish refined-particle parent and recreate
  polishing, or stop for contract work;
- `refine3d.reference_volume` does not accept a `half_maps` result, so a true
  corresponding-half-map restart is currently blocked; and
- `rescale_map` cannot bind half-map pairs or masks, so a geometry transition
  must not claim those artifacts were regridded through the present contract.

If no complete typed path preserves coordinates, half assignments, optics/CTF
metadata, and geometry, preserve the old branch and stop that route. Continue
other contract-valid work within scope; ask for contract development or missing
information only if the requested objective cannot otherwise progress.

## Refine3D parameter rules

These rules select scientific hypotheses; they do not bypass the parent
contract gate. Several expert native controls are mapped only as workflow
defaults or are absent from the neutral operation. If authority or mapping is
missing, record a `contract_gap` instead of an executable override.

| Rule ID | Prerequisite and condition | Atomic action | Required validation |
| --- | --- | --- | --- |
| `refine3d_restore_sampling_near_nyquist` | The accepted map approaches downsampled Nyquist and credible higher-frequency signal may remain. | Return to a typed compatible re-extraction parent and change the box/rescale/pixel geometry group for finer sampling; enlarge the even native box if needed for CTF delocalization. A polished-particle input is currently contract-blocked. | STAR/header geometry, unchanged half assignments, Nyquist margin, reference grid, mask clearance, storage/compute, and the recreated refinement/polishing state all validate. |
| `refine3d_start_from_lowpass_reference` | The start is a class, joined, external, or other non-corresponding-half-map reference. | Set a strong `initial_lowpass_A`, normally within a justified 40–60 Å range, while holding other scientific settings fixed. | Supported structure is recovered independently without reference-shaped detail. |
| `refine3d_restart_from_halfmaps` | A prior gold-standard refinement exists and particles changed through CTF refinement or polishing. | Bind one corresponding half-map so RELION uses both halves, and use a justified higher-resolution low-pass such as 8 Å only after `half_maps` gains a typed Refine3D input binding. The current contract blocks this rule. | Half-set independence remains intact; FSC and map behavior show no unsupported resolution jump. |
| `refine3d_fix_reference_greyscale` | Reference provenance conflicts with the configured greyscale flag. | Correct only `reference_map_absolute_greyscale`; do not combine with low-pass, geometry, or symmetry changes. | First-iteration scaling and likelihood behavior stabilize without density-amplitude artifacts. |
| `refine3d_fix_mask_geometry` | Verified particle extent and box show clipping or excessive solvent in the spherical particle mask. | Correct only `mask_diameter_A` as derived geometry, propagating its downstream state. | Signal is contained with clearance and refinement remains stable. |
| `refine3d_use_reference_mask` | A mature refinement has a validated global soft mask and solvent impairs orientation assignment. | Add the optional mask input on a preserved branch when its typed binding and technical prerequisites pass. Supported focused-mask experiments are covered by the processing request. | Boundary clearance, phase-randomized behavior, and comparison with the unmasked branch exclude mask-shaped density. |
| `refine3d_use_solvent_corrected_fsc` | A validated matching soft mask exists; measured masked volume occupies a relatively small fraction of a correctly sized box because of a small or elongated object or an authorized focus; and direct evidence shows substantial solvent/background. | Reuse or bind the typed `mask` input and set `solvent_correct_fsc: true` on a preserved branch when the `strategy_intent`, task scope, and render checks pass. | Convergence remains normal; corrected, masked, unmasked, and phase-randomized FSC, nearby-mask stability, matched map evidence, and final matched PostProcess support the change. |
| `refine3d_use_random_noise_solvent` | A large high-resolution complex shows Fourier correlations, edge, or corner artifacts under zero masking. | Compare `zero_mask_particles: false` with a preserved zero-mask branch if the operation contract authorizes the change. | Solvent flatness, corner behavior, FSC, half-map consistency, and map identity improve without new noise. |
| `refine3d_blush_candidate` | Standard regularization visibly overfits a low-SNR target and the environment can execute Blush. | Run one matched Blush branch only after Refine3D exposes and render-tests the typed native option; it is currently absent from the neutral operation. | Coherent density, solvent behavior, half-map consistency, and real heterogeneity improve together. |
| `refine3d_adjust_initial_sampling` | Particle scale/symmetry justify different starting angular coverage. | Use 7.5° initial/1.8° local-search onset as ordinary planning priors, or 3.7°/0.9° for justified high symmetry; change the sampling pair as one hypothesis if contract-authorized. | Reported angular accuracy, convergence, FSC, and reproducible detail justify the added compute. |
| `refine3d_fix_centering_search` | Geometry is correct but measured initial offsets exceed or are poorly sampled by the search. | Adjust `initial_offset_range_pix` from measured displacement; change `initial_offset_step_pix` separately only when discretization remains limiting. | Poses converge without implausible translations or edge clipping. |
| `refine3d_choose_speed_or_final_quality` | The objective is explicitly exploratory under compute pressure or final-resolution quality. | Exploratory runs may use `use_finer_angular_sampling_faster: true` and, only with ample box margin, `skip_padding: true`. Final runs disable faster progression and retain padding. Treat these as scientific-resource hybrids. | Matched comparisons show no lost resolution, interpolation bias, folded edge signal, or corner artifacts. |
| `refine3d_fix_low_frequency_ctf_behaviour` | Verified low-frequency CTF pathology required ignore-until-first-peak upstream. | Keep treatment consistent only after Refine3D gains a typed native mapping; currently record a contract gap. | Refinement remains coherent without low-frequency collapse and matches upstream CTF assumptions. |
| `refine3d_relax_pseudosymmetry` | Independently supported pseudosymmetry causes strict-symmetry pose ambiguity. | Within the processing task, test native symmetry relaxation as one group after typed mapping exists; preserve C1 and strict-symmetry parents. | Asymmetric signal improves without duplicated noise or unstable symmetry-related orientations. |
| `refine3d_rerun_material_state_change` | Accepted particles, reference, symmetry, geometry, CTF metadata, polished particles, or validated mask changed materially. | Launch one new uniquely aliased refinement for the new upstream state; do not change an unrelated parameter. | FSC, map evidence, compatible existing local-resolution evidence when available, matched-filter appearance, and the expected effect agree without new artifacts. |

## Solvent-flattened FSC decision and validation

Treat RELION's solvent-flattened FSC and solvent-corrected FSC as two names for
the Refine3D strategy represented by the neutral parameter
`solvent_correct_fsc`. Apply rule `refine3d_use_solvent_corrected_fsc`
as a reversible experiment when all technical prerequisites below hold. Final
`high` confidence is not needed merely to test this option:

- the operation contract authorizes `strategy_intent`, the processing task
  covers the option and compute, and the rule's predicted effect and stop
  condition are recorded before execution;
- particle extent, box field width, edge clearance, and masked-volume fraction
  are measured, showing that credible signal occupies a relatively small part
  of a correctly sized box and substantial solvent/background remains;
- the matching soft mask passes the global-mask acceptance checks, including
  full credible-density coverage, boundary clearance, softness,
  phase-randomized behavior, and stability across a reasonable nearby mask;
- a focused mask, if used, has a supported structural target, a mature
  consensus, trusted orientations, a specific local processing question, and its matched
  unmasked/aligned baseline; and
- a preserved matched baseline uses the same particles and random-half sets,
  reference, symmetry, geometry, scientific settings, and `mask` binding with
  `solvent_correct_fsc: false`. If the mask itself is new, first satisfy
  `refine3d_use_reference_mask` and preserve its unmasked baseline as well.

Substantial empty space caused by an oversized box is a geometry defect to fix,
not evidence for this rule. A mask's mere existence, focused processing by
itself, or membrane association by itself is also insufficient.

Execute only through the RELION MCP: bind the validated typed `mask` input and
set the typed override `solvent_correct_fsc: true`; do not use a shell command
or `other_args`. Render before the write and verify that native `fn_mask`
resolves to that exact mask and `do_solvent_fsc == Yes`. Record the effective
parameter, native assignments, mask hash/provenance, baseline branch, and a
unique alias.

After convergence, compare against the preserved matched baseline. Inspect the
trajectory and half-set provenance; corrected, masked, unmasked, and
phase-randomized FSC; solvent, edges, and corners; and matched-filter map
features in both half-maps. Accept autonomously only when the predicted map
benefit is reproducible, nearby valid masks do not materially change the FSC or
interpretation, phase-randomized correlation is controlled, and no mask-shaped
density or unsupported fine detail appears. A smaller nominal resolution alone
is insufficient; retain the baseline when no independent map-quality benefit
is demonstrated.

The FSC used inside Refine3D is convergence and strategy evidence, not the
final resolution measurement. After selecting the final branch, run matched
PostProcess on its corresponding unfiltered half-map pair and validated mask
with the calibrated pixel/MTF and common sharpening policy. Use the corrected
PostProcess FSC for final resolution assessment while inspecting all four FSC
curves.

When box suitability, mask coverage, membrane/micelle boundary, nearby-mask
stability, or matched evidence is missing or conflicting, first inspect the
available geometry, overlays, and matched-mask diagnostics. Keep
`solvent_correct_fsc: false` and the validated baseline while unresolved; a
missing optional optimization does not block routine processing. Use
`human_gate` with `await_human` only when an essential gap or a focused action
outside the existing scope prevents useful progress. A focused mask still
requires a supported target and matched baseline even when the Boolean
parameter is strategy-selectable.

Do not use a joint map where a half-map is required. Do not interpret faster
sampling, skipped padding, or lower runtime as evidence of scientific improvement.
Keep `do_ctf_correction: true` for particles with valid CTF metadata; resolve
bad or inconsistent CTF information upstream rather than disabling correction
to improve appearance.

Treat any non-C1 symmetry, including a tutorial D2 value, as a scientific
hypothesis. Use C1 as the reversible default. A processing or optimization
request covers a bounded matched candidate-symmetry experiment with assessable
evidence, a verified axis/convention, and preserved controls; final symmetry
acceptance requires independent evidence. A non-C1 candidate follows rather than
replaces the gold-standard C1 Refine3D baseline and must use the same particles
and preserved random-half assignments for comparison.

## Refinement acceptance

Do not run RELION LocalRes as part of every Refine3D/PostProcess branch.
Select the best/final deliverable map first, then opt in to
`local_resolution` once for that selected map. Run LocalRes before selection
only when the user explicitly requests it. Inspect compatible existing
local-resolution evidence when available; localized density inspection does
not trigger automatic LocalRes fan-out.

Require:

- convergence trajectory and final optimiser/model/data metadata;
- both final half-maps and preserved half-set provenance;
- corrected, masked, unmasked, and phase-randomized FSC;
- global-resolution evidence plus directional/anisotropy evidence, and
  compatible existing local-resolution evidence when available;
- map, solvent, edge, and corner inspection at several contours;
- sharpening/Guinier diagnostics; and
- a `relion_compare_maps` result and inspected aligned image against the
  previous accepted parent or, for the staged union rule, the declared matched
  sibling, using one declared transform across unsharpened and common-low-pass
  sensitivity views.

For a symmetry-constrained candidate, use C1 as the reference and additionally
inspect matched unsharpened, commonly low-passed, and PostProcess views for:

- disappearance of density reproducibly present in C1;
- smeared side chains or domains where local resolution supports the claim;
- split or distorted helices;
- weakened ligand or cofactor density;
- apparently fractional occupancy; and
- distinct conformations averaged into one.

Use the same effective filter, display transfer function, and contour or
enclosed-volume convention. Fit the candidate-to-reference transform once at a
declared physical low-pass and apply it unchanged to the unsharpened,
common-low-pass, and PostProcess comparisons. Record compatible hand/sampling,
transform direction, bounds/status, mask, and valid overlap. A failed,
boundary-limited, or insufficient-overlap fit yields `unassessable`, not
`distinct`. The optimized metric and every sensitivity view from that pair are
one `map_pixels` root. Verify local findings in both C1 half-maps and against
compatible existing local-resolution evidence when available. The central-slice
symmetry-comparison report is triage evidence and does not replace targeted
local inspection.

Finally accept a refinement only when it improves or remains stable across multiple
independent channels, matches the predicted effect of its material change, and
introduces no new red flag. A credible assessed `medium`-confidence primary
may continue normal downstream processing under `primary_fallback`, with its
baseline and alternatives preserved and confidence reassessed at each stage.
A smaller nominal FSC resolution, higher likelihood,
or sharper-looking display alone is insufficient.

Red flags include half-set leakage, abrupt unsupported resolution gain,
reference-shaped features, non-reproducible fine detail, anisotropic streaking,
disconnected solvent density, boundary/corner artifacts, mask dependence, and
loss of plausible minority signal.

## Postprocessing safeguards

Use the corresponding unfiltered half-map pair and a validated compatible soft
mask. Lock `pixel_size_A` to calibrated half-map metadata. Lock `mtf_file` and
`mtf_pixel_size_A` to the actual detector/acquisition provenance; never inherit
a tutorial detector MTF.

Every PostProcess and LocalRes workflow step requires an explicit
metadata-derived `pixel_size_A`; an omitted value makes the step not execution
ready. The workflow intentionally omits MTF settings. Apply MTF correction only
when it is intended and authoritative detector provenance is available, and
then supply `mtf_file` and `mtf_pixel_size_A` together for PostProcess (or the
supported MTF input for LocalRes). Omitting both means no tutorial MTF is
silently inherited.

Inspect corrected, masked, unmasked, and phase-randomized FSC, the Guinier
plot/fitted range, automatically estimated B-factor, compatible existing
local-resolution evidence when available, and the map under several matched
display filters.

- Use automatic B-factor estimation only when signal extends well beyond
  10 Å and a defensible approximately linear Guinier region exists.
- Choose `lowest_resolution_for_auto_bfit_A` from that reliable region, not
  because the tutorial uses 10 Å.
- Treat a user-supplied B-factor, skipped FSC weighting, or ad hoc high-
  resolution low-pass as a protected interpretation choice with a preserved
  automatic baseline. These native options are not exposed by the current
  neutral postprocessing operation.
- Do not represent manual sharpening by setting
  `estimate_bfactor_automatically: false` alone: the current neutral operation
  has no typed manual B-factor value, so that change cannot express or validate
  the requested sharpening hypothesis.
- Remember that a more negative B-factor can convert noise into convincing-
  looking detail. Require half-map support and compatible existing
  local-resolution evidence when available for interpreted features.

Do not accept postprocessing if nearby valid masks materially change the FSC or
interpretation, phase-randomized FSC remains elevated near the reported
resolution, the MTF/pixel provenance is wrong, or sharpening creates unsupported
features.

## CTF-refinement and polishing handoff

Treat CTF-refinement and polishing diagnostics as upstream proposals, not final
evidence. Preserve their input branch, optics groups, fitted parameter scope,
training subset, optimized-parameter file, and frame range.

After either operation changes particle metadata or images:

1. create a new input-data state;
2. run one compatible Refine3D branch with preserved half sets;
3. create/validate the appropriate mask if the map state changed;
4. run matched postprocessing; and
5. compare FSC, map evidence, compatible existing local-resolution evidence
   when available, trajectories or CTF fits, and visual features with the
   pre-change baseline, using the fixed-transform
   `relion_compare_maps` pairwise contract for quantitative map claims.

Do not accept polishing because training converged or trajectories look smooth.
Do not accept CTF refinement because fit likelihood improved. The larger
diagnosed limitation should be addressed first; iterate only when a new
refinement/postprocessing branch demonstrates complementary improvement.

## Repeat and completion policy

One accepted refinement per distinct upstream data state is the default.
Repeat only after a material state change or one supported parameter hypothesis;
never repeat a successful Refine3D job unchanged. End the affected refinement
loop when validation plateaus, worsens, or exposes a more upstream limitation;
continue the requested pipeline from the last usable primary or fallback.

Resolve half-set or visual-evidence gaps through available provenance checks
and renders before deciding whether human input is needed. Keep the accepted
baseline when a refinement comparison is inconclusive; a credible alternative
may continue normal downstream processing under `primary_fallback` at
`medium` confidence after each required stage assessment. Request `human_gate`
with `await_human` only for an essential unresolved gap or a symmetry, focused-mask, or manual sharpening/FSC action
outside the existing scope. Preserve every half-map pair, map, mask,
postprocessing STAR, FSC report, and inactive fallback.

Routine stage completion requires assessed Refine3D and postprocessing
outputs, a recorded provisional or accepted primary, and pixel/box/mask/half-set
provenance safe for any requested next operation. No additional operation is
required at the requested endpoint: return the best assessed provisional or
accepted outputs with their limitations. Missing final-acceptance corroboration
does not force a valid provisional workflow to pause or ask for acceptance.
