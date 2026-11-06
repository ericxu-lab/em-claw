# Development and validation

This document defines durable repository checks and the invariants they protect.
It intentionally does not record test counts, pass dates, host-specific RELION
results, or other snapshots that become stale.

## Architecture and authorities

- [`server.py`](../src/relion_mcp/server.py) defines the public MCP tools,
  resources, signatures, annotations, and top-level result construction. Keep
  [`API.md`](API.md) synchronized with it.
- [`settings.py`](../src/relion_mcp/settings.py) defines runtime environment
  names, defaults, parsing, execution gates, and root/path confinement. Keep
  [`CONFIGURATION.md`](CONFIGURATION.md),
  [`.env.example`](../.env.example), and the
  [MCP configuration example](../examples/mcp-config.toml) synchronized with it.
- [`catalog.py`](../src/relion_mcp/catalog.py) loads the bundled contracts,
  resolves operation aliases, validates native option references, and renders
  logical values into RELION GUI-label assignments.
- [`workflow.py`](../src/relion_mcp/workflow.py) handles default/opt-in step
  inclusion, dependency planning, override validation, and planning/full-run
  status.
- [`runner.py`](../src/relion_mcp/runner.py) owns native execution, symmetry
  preflight, class-selection graphs, schedule launch, status, outputs, logs,
  abort, direct map rescaling, and enforcement of the Class3D acceptance gate
  at registration and launch.
- [`initial_model_acceptance.py`](../src/relion_mcp/initial_model_acceptance.py)
  defines the `relion.initial-model-acceptance/v1` schema, validates its
  assessment, geometry, evidence, source, and particle-lineage contract, and
  publishes and verifies content-addressed records under the RELION project.
- [`visual_evidence.py`](../src/relion_mcp/visual_evidence.py) owns the bounded,
  process-local cache that binds eligible rendered PNG bytes to report and
  canonical-project provenance, plus immutable no-replace publication of the
  exact inspected bytes for InitialModel acceptance.
- [`star.py`](../src/relion_mcp/star.py) and
  [`outputs.py`](../src/relion_mcp/outputs.py) parse RELION metadata and classify
  output paths.
- [`particle_audit.py`](../src/relion_mcp/particle_audit.py) measures exact
  selection attrition and plans/materializes disjoint stratified micrograph
  and optional curated-label populations without altering source metadata.
- [`visuals.py`](../src/relion_mcp/visuals.py) and
  [`challenge.py`](../src/relion_mcp/challenge.py) implement bounded rendering
  and direct-pixel verification. The pure numerical core for the bounded,
  orientation-matched map-projection search lives in
  [`projection_compare.py`](../src/relion_mcp/projection_compare.py), while the
  pure numerical core for the bounded, read-only C1 half-map scan lives in
  [`symmetry.py`](../src/relion_mcp/symmetry.py); `visuals.py` owns their confined
  MRC loading, validation, and report rendering. Renderer, comparison, evidence
  publication, and scan behavior is documented in
  [`VISUAL_TOOLS.md`](VISUAL_TOOLS.md).
- The four files under
  [`src/relion_mcp/configs/`](../src/relion_mcp/configs/) are the bundled
  machine-readable operation, manifest, mapping, and workflow contracts. The
  tutorial supplies stage order; mappings supply adapter-level execution
  support; the manifest supplies native RELION labels/options; neutral
  operations supply logical definitions and direct-operation metadata.

Do not edit contracts as a side effect of processing a dataset. Dataset values,
branches, and decisions belong in run state outside this package.

`relion_scan_symmetry` is a dedicated analysis tool, not a RELION pipeline job.
Do not add it to the neutral operation, pipeliner mapping, native manifest, or
pipeline-output contracts unless its execution model deliberately changes. Its
result is an in-memory image and metadata artifact, and it must remain usable
without registering or launching a job.

## Development environment

From the repository root:

```bash
uv sync --extra test
```

The `test` extra includes the unit-test and lint tools used below. RELION is not
required for the isolated test suite; tests that exercise process behavior use
temporary files and controlled subprocess doubles.

To inspect the MCP interactively:

```bash
uv run mcp dev src/relion_mcp/server.py
```

Interactive inspection is not a substitute for the image-content smoke test in
an actual image-capable client.

## Required local checks

Run all of these after changing source, contracts, tests, packaging, or public
documentation:

```bash
uv lock --check
uv run ruff check .
uv run python -m compileall -q src tests
uv run pytest
uv build
```

`uv build` validates both the source distribution and wheel, including bundled
YAML data and the `relion-mcp` entry point. Build outputs belong under `dist/`
and are ignored; do not commit them.

There is no configured static type checker. Do not report type checking as
complete unless one is deliberately added and run.

## Contract checks

The unit suite exercises catalog rendering and workflow planning. These focused
commands are useful when changing YAML or adapter logic:

```bash
uv run python -c 'from relion_mcp.catalog import Catalog; c = Catalog(); issues = c.validate_catalog(); assert not issues, issues'
uv run python -c 'from relion_mcp.catalog import Catalog; from relion_mcp.workflow import validate_workflow; result = validate_workflow(Catalog(), "relion_5_spa_tutorial_v1"); assert result["valid_for_planning"], result["issues"]'
```

Do not assert that the bundled workflow is fully executable as a generic
contract invariant. A valid plan may intentionally contain a planning-only
operation, which makes `valid_for_full_execution=false`. Instead, verify each
support-state change explicitly and retain the write-side execution gate.

When a contract changes, check all of these relationships:

1. every workflow operation resolves directly, through an operation alias, or
   through the dedicated direct-operation path;
2. every mapped job type exists in the manifest;
3. every mapped input/parameter native key exists for that manifest job type;
4. workflow inputs, parameters, and expected results agree with the selected
   logical operation and mode;
5. `enabled_by_default`, `optional`, and `include_opt_in_steps` retain their
   distinct semantics;
6. planning-only support cannot reach generic registration;
7. dependency inference does not silently cross from one scientific branch to
   another;
8. the four YAML files remain package data in the built wheel.

## Documentation link check

The following dependency-free check verifies relative Markdown file links in
tracked Markdown documents. It ignores web URLs and in-page anchors.

```bash
uv run python - <<'PY'
from pathlib import Path
import re

root = Path.cwd()
missing = []
excluded = {".git", ".venv", ".pytest_cache", ".ruff_cache", "build", "dist"}
for document in root.rglob("*.md"):
    if excluded.intersection(document.relative_to(root).parts):
        continue
    for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
        target = target.strip().split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        if not (document.parent / target).resolve().exists():
            missing.append(f"{document}: {target}")
assert not missing, "\n".join(missing)
PY
```

Links to source files should be repository-relative. Do not copy the MCP TOML
example into another document; link to
[`examples/mcp-config.toml`](../examples/mcp-config.toml).

## Safety invariants

Changes must preserve all of the following:

- Writes require run mode, at least one approved root, and literal
  `confirm=true`.
- Every project path is absolute and confined to `RELION_PROJECT_ROOTS` after
  resolution; every artifact path is project-relative and remains contained
  after symlink resolution.
- Native processes receive argv lists. Never introduce `shell=True`,
  `os.system`, or an arbitrary shell-command argument.
- `schedule_mappable` is the only generic executable mapping support level.
  Direct utilities use dedicated, validated tools.
- Render calls remain project-read-only. Eligible InitialModel visuals may be
  registered only in the bounded process-local evidence cache; ordinary run
  calls register and launch under one public write, and add/start calls remain
  recovery interfaces.
- A returned `started=true` means asynchronous schedule launch, not job
  completion or scientific acceptance.
- A start failure after registration preserves and reports the registered jobs
  so recovery does not duplicate them.
- Every Class3D registration requires an explicit, valid
  `initial_model_acceptance` record bound to the rendered reference, final
  classification-particle STAR, and particle diameter. Supplying that argument
  for a non-Class3D operation remains an error.
- `relion_start_jobs` reparses each registered job and revalidates any Class3D
  job against a matching live record before creating the scheduler process.
  Missing, stale, changed, or mismatched records and artifacts fail closed.
- InitialModel acceptance records remain canonical-JSON, content-addressed,
  no-replace artifacts under `.relion-mcp/initial-model-acceptances/`. Their
  integrity checks bind submitted evidence and recheck MRC/PNG/particle-STAR
  structure, header geometry, and independent-root declarations; they must
  never be treated as semantic image interpretation or proof of scientific
  truth.
- InitialModel evidence publication accepts only the exact SHA-256 of an
  eligible PNG cached by the same MCP process for the same canonical project.
  Report types remain allowlisted per evidence kind, and publication remains an
  idempotent, immutable, no-replace write under
  `.relion-mcp/initial-model-evidence/`. A cache hit or successful write is not
  a scientific acceptance decision.
- A partial class-selection add reports preserved jobs and does not start an
  incomplete graph.
- Input maps are never overwritten in place. Distinct existing rescale outputs
  require `overwrite=true`.
- Abort remains a cooperative marker operation, not process killing.
- Symmetry uses the semantic point-group validator and the installed-build
  preflight before registration when compatibility is not statically known.
- Symmetry discovery requires independent C1 half-maps, a common explicit
  analysis low-pass, bounded common/high-order searches, and a capped
  shortlist. A scan result never registers a job, imposes symmetry, or marks a
  candidate accepted.
- Visual tools return a PNG image block first and provenance/metrics text
  second, repeating this pair for multipage 2D sheets, with no
  `structuredContent` that could shadow the image.
- Visual paths and report sources obey the same project confinement as other
  project tools.
- Generic map comparison fits one bounded local candidate-to-reference
  transform at a declared physical low-pass, freezes it across comparison
  cutoffs, reports bound/overlap status, and never writes an aligned MRC.
- Map-projection comparison uses explicitly selected 2D classes, matching
  physical sampling and field of view, one common physical low-pass, and a
  bounded direction/rotation/shift search. Scores are diagnostic only: tests
  and documentation must not introduce an automatic match or seed threshold,
  and central-slab views must never be labelled as synthetic projections.

A quick source scan for the two prohibited shell patterns should produce no
matches:

```bash
rg -n 'shell\s*=\s*True|os\.system\s*\(' src tests
```

Because `rg` returns status 1 for no matches, treat empty output as success.
The unit suite remains the executable specification for argv construction and
path rejection.

## Public API changes

Public compatibility includes tool/resource names, argument names and defaults,
workflow step IDs, operation aliases, environment variables, return-field
semantics, and image-content ordering.

For an intentional API change:

1. preserve the existing spelling or behavior when reasonable;
2. add or update a behavior test;
3. update [`API.md`](API.md) from the live `server.py` signature and result;
4. update configuration or visual documentation only if that responsibility
   changed;
5. call out compatibility handling in the change summary.

Workflow-tool `alias=None` currently derives the native alias from `step_id`.
Keep that behavior aligned across render, combined run, and add-only workflow
tools.

The optional `initial_model_acceptance` parameter must remain aligned across
`relion_run_job`, `relion_run_workflow_step`, `relion_add_job`, and
`relion_add_workflow_step`. Tests for this surface must cover fail-closed
Class3D registration without a record, reference/particle/diameter and hash
mismatches, non-Class3D rejection of the argument, successful registration,
and start-only revalidation before the scheduler subprocess.

Symmetry-scan tests should use deterministic synthetic C1 half-map pairs and
cover common orders, a higher prime order, harmonic/subgroup ambiguity,
half-map disagreement, Cn/Dn discrimination, optional-mask/grid validation,
input preservation, resource bounds, and image-first MCP transport. Do not add
or report a scan-derived asymmetry-fraction threshold; keep candidate support
limited to reproducible half-map, axis, and complete-group evidence. Keep the
scan report-only and human-gated, and cover C1-referenced matched-comparison
guidance. Do not
replace matched branch confirmation with a test that simply expects the top
synthetic score to be biologically accepted.

Map-comparison tests should use asymmetric synthetic density with known local
rigid and translation perturbations. Assert improved post-alignment metrics,
fixed-transform reuse across cutoffs, boundary-limited failure semantics,
source-byte preservation, one canonical voxel-pair root per map pair,
origin/start-only root deduplication, axis-mapping sensitivity, centered-grid
affine direction, requested-cutoff
views, and image-first MCP transport. Include unrelated-map and deterministic
spatial-jackknife cases so a chance optimizer gain cannot receive `aligned`.
Do not encode a Pearson cutoff as a `same` or `distinct` decision.

Projection-comparison tests should use deterministic asymmetric synthetic
density and known projections. Cover physical-grid and low-pass validation,
explicit class selection, bounded direction/in-plane/shift search, contrast
handling, source preservation, provenance hashes, image-first MCP transport,
and the report's non-automatic acceptance semantics. Do not make an NCC or
runner-up-margin threshold equivalent to a scientific match. Policy coverage
must continue to require at least three explicitly selected credible, distinct
2D appearances and to reject central-slab substitution.

Visual-evidence publication tests should cover report-kind allowlists,
same-project binding, exact PNG hash/size checks, expiry and cache-miss failure,
bounded eviction, run-mode and confirmation gates, path containment,
symlink/non-regular destination rejection, immutable no-replace publication,
idempotent replay of identical bytes, and conflict failure. Tests must publish
only through the public tool and must not treat materialization as semantic
pixel validation.

## Testing real RELION installations

Default checks must not write to an existing scientific project. Real RELION
integration requires installed executables, site configuration, and an
explicitly disposable project beneath an approved root. Keep such runs
separate from unit tests and record the RELION version, exact command, project
scope, and cleanup outcome in the task report rather than as a permanent claim
in repository documentation.

When external software is unavailable, report the integration as not run and
name the missing executable or resource. Never weaken an isolated test to hide
an unavailable external integration.

## Review checklist

Before handing off a change:

- inspect `git status` and separate unrelated user work;
- review the complete diff for accidental contract or public-interface changes;
- run the required local checks and focused contract checks;
- verify relative documentation links;
- confirm that no cache, build, log, or scientific output file is tracked;
- report commands and outcomes from the current run without copying them into a
  permanent “latest results” document.
