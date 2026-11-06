# Map Interpretation

Use this reference for absolute hand, biological map interpretation,
model-based evidence, minority-state interpretation, and deposition/publication
claims. Apply the shared visual, decision, approval, and provenance policies in
the parent [orchestrator](../SKILL.md). This reference contains no adaptive
RELION parameter rules.

## Contents

- [Scope and evidence labels](#scope-and-evidence-labels)
- [Evidence hierarchy](#evidence-hierarchy)
- [Handedness](#handedness)
- [Broad and specific interpretations](#broad-and-specific-interpretations)
- [Model-based evidence](#model-based-evidence)
- [Competing interpretations and minority states](#competing-interpretations-and-minority-states)
- [Human gates and completion](#human-gates-and-completion)

## Scope and evidence labels

Separate processing facts from biological interpretations. Label every claim
as one of:

- `observed`: directly visible in image content at adequate local resolution;
- `supported_interpretation`: visible evidence plus independent composition,
  sequence, biochemical, or model support;
- `tentative_interpretation`: plausible but missing decisive evidence; or
- `unsupported`: contradicted or below the available evidence limit.

Never claim a feature was inspected from a filename, metric, map header, or
textual renderer metadata. If direct image content is unavailable, report the
missing evidence and defer any morphology-dependent conclusion. First try
available renders and compatible existing evidence; processing that does not
depend on that conclusion may continue.

Record the map, half-maps, mask, pixel size, hand, symmetry, filtering,
contouring policy, available local-resolution context or its absence, and image
SHA-256 behind every visual claim. Compare maps only on compatible grids/frames
and at matched filtering.

Localized density inspection from the map, half-maps, matched filtering, and
compatible existing evidence does not authorize an intermediate LocalRes job.
Missing optional LocalRes evidence does not block processing or require a user
question. Defer claims that need it and schedule one opt-in LocalRes run only
after selecting the best/final deliverable map. Run it earlier only when the
user explicitly requests that characterization.

## Evidence hierarchy

Rank evidence in this order:

1. directly visible map features at adequate local resolution;
2. verified sample composition and sequence;
3. independent atomic model or prediction fit;
4. automated model building with map-model and stereochemical validation;
5. homologous structures or expected domain architecture; and
6. speculative shape resemblance.

Lower-ranked evidence cannot override contradictory higher-ranked evidence.
Global nominal resolution does not establish the local resolution of a claimed
feature. A fit score is not independent when the same model/reference biased
the reconstruction.

## Handedness

Standard untilted single-particle images do not determine absolute hand. Record
`hand: indeterminate` unless independent chiral evidence exists.

Useful independent evidence includes:

- directly visible right-handed alpha-helical pitch at adequate local
  resolution;
- a trusted atomic model, homolog, or high-confidence prediction fitted and
  validated against both hands;
- automated model building performed for both hands with stereochemistry and
  map-model validation;
- stage-tilt, tomography, known chiral labels, or another independent
  experimental constraint.

Compare both hands on the same grid, filter, mask policy, and validation
metrics. Prefer one hand with `high` confidence only when independent evidence
strongly favors it and the alternative shows clear stereochemical or map-fit
contradictions.

Never decide hand from low-resolution overall shape, FSC, class occupancy,
symmetry score, refinement likelihood, or the tutorial. Hand inversion is a
protected action. Continue processing in the recorded arbitrary hand and
defer absolute-hand claims. Use `human_gate` with `await_human` only if an
explicitly requested protected action requires a missing authorization or
essential unresolved choice; do not interrupt ordinary processing to settle hand.

## Broad and specific interpretations

Broad structural descriptions may be autonomous when direct visual evidence
and composition agree, for example:

- a membrane-associated region or micelle-like envelope;
- oligomeric architecture already supported by the symmetry analysis;
- large domains or domain rearrangements;
- obvious nucleic-acid-like density; or
- alpha-helical bundles where local resolution supports helices.

Require adequate local resolution plus independent sequence/model evidence
before assigning:

- a specific protein, domain, subunit, ligand, cofactor, or nucleic-acid
  sequence;
- a conformational state or functional state;
- an active site, side-chain identity, register, or post-translational
  modification; or
- a mechanism, interaction pathway, or causal biological narrative.

Record uncertainty, alternative explanations, and features that should be
visible if the interpretation is correct. Do not convert an expected sample
component into observed density without visual support.

## Model-based evidence

Before treating a model as biological evidence, record:

- model source, sequence correspondence, predicted confidence, and whether it
  influenced picking, reconstruction, classification, or symmetry;
- map hand, local resolution, filtering, sharpening, and mask;
- fitting/building method and restraints;
- map-model FSC or equivalent fit evidence, geometry/stereochemistry,
  clashes, sequence register, and local agreement; and
- the same checks for plausible competing hand/domain/state assignments.

An attractive fit is not enough if geometry is poor, local density is absent,
the map is too low resolution, or the model is not independent. ModelAngelo or
another automated builder must be workflow-validated as executable before use,
and its completion does not prove the assignment.

Do not edit an atomic model unless the user has explicitly authorized model
work and the operation lies within the approval boundary.

## Competing interpretations and minority states

When multiple interpretations are plausible:

1. record the shared observed facts separately;
2. list evidence for and against each interpretation;
3. rank one primary only when the evidence supports a defensible ordering;
4. preserve ordered alternatives with explicit activation or reassessment
   triggers; and
5. keep source maps and particle sets for plausible minority states.

Do not combine classes merely because their global resolutions are similar or
discard a coherent low-occupancy class because it is small. If multiple states
are the requested scientific objective, record a bounded plan and budget using
that existing authorization; do not silently advance every branch.

When identity depends on missing sample knowledge or a minority state may be
important, keep the interpretation tentative and preserve its maps and
particles. Continue compatible processing and collect available diagnostic
evidence. Use `human_gate` with `await_human` only when a requested consequential
interpretation or protected action requires an essential unresolved choice;
unknown biological identity alone does not block data processing.

## Human gates and completion

Reuse existing authorization for explicitly requested model work and named
interpretation tasks; do not repeat an approval question. Authorization never
replaces scientific evidence. Complete ordinary processing with tentative
interpretations and preserved minority states. Missing interpretation evidence
is a `defer` decision, not a request for a user to approve a guess. Ask a focused
question only when the specifically requested outcome requires an essential
unresolved choice or action outside that scope, including:

- choosing hand without decisive independent chiral evidence;
- assigning an unresolved specific component, ligand, register, or mechanism;
- discarding a plausible biological minority state;
- editing an atomic model;
- resolving evidence needed for an established interpretation; or
- preparing deposition, publication, or other irreversible biological claims.

Human agreement cannot make an unsupported interpretation established. Keep
such a claim tentative until its evidence requirements pass.

An actual `human_gate` must use `next_action: await_human` and include exact
maps/renders/models, the preferred interpretation and confidence, evidence for and against each option, missing high-confidence
criteria, and a reversible default.

Complete interpretation only when every reported claim has an evidence label,
source artifacts, available local-resolution context or its absence,
uncertainty, and alternatives where needed. Report `hand: indeterminate` when
appropriate. Never claim biological
correctness because a pipeline completed, a class was large, a model fit, or a
single numerical metric improved.
