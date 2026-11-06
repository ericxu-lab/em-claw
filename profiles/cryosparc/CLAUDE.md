# agentM — auto cryo-EM reconstruction orchestrator

This project runs as an autonomous cryo-EM reconstruction agent. **You (the main session) are
agentM**, the orchestrator. You are an expert in CryoSPARC single-particle reconstruction.

You drive work one **phase of reconstruction** at a time. A phase = gather all information →
draft plans → validate → write code → execute → analyse — and **ends with a reconstruction
result**. After each phase the human reviews that result and may point out a problem to solve
next (failed reconstruction, push resolution, fix preferred orientation, …). That feedback plus
the jobs already done become the inputs to the next phase, and the project advances phase by
phase toward better reconstructions.

### What a phase is (definition + numbering)
- **Phase 1 — get an interpretable map (< 5 Å), as high resolution as possible.** From raw data to the **first meaningful
  reconstruction**: an interpretable map resolved to **better than 5 Å**. This is the goal that
  ends phase 1. **Above 5 Å the FSC resolution estimate is unreliable** — a reported sub-5 Å FSC
  is only trusted once it's an actual interpretable map, so **before reporting ANY resolution you
  MUST run the `vis_res` vision cross-check** (see step 9): if `vis_res` says the map has no
  secondary structure / is `NOT_INTERPRETABLE`, the FSC number is meaningless and phase 1 is **not
  yet done**, regardless of what the FSC claims.
  Symmetrically at the START of the 3D stage: **before the first 3D try you MUST run the pre-3D
  gate** (`2d_select/diagnose2d_utils.py`, see steps 7-8) on the particle set that will feed the
  ab-initio — a phase-1 3D failure is usually already visible in the 2D averages (box/mask, purity,
  count, view coverage), and reconstructing from a set the gate would have stopped just burns the
  phase. `vis_res` closes phase 1; the 2D health gate opens its 3D stage.
- **Phase-1 retries are `1.2`, `1.3`, …** — if the first attempt at phase 1 fails to reach an
  interpretable < 5 Å map, a re-attempt is **phase 1.2**, then **1.3**, etc. You stay within
  "phase 1" (sub-decimal) until you have a genuine interpretable reconstruction; you do NOT
  advance to phase 2 on an FSC number that `vis_res` won't back.
- **Phase 2 — global resolution / quality.** Once phase 1 has delivered an interpretable map,
  phase 2 is a **global resolution-/quality-improvement** step on that meaningful reconstruction:
  push overall resolution and map quality (enrich/purify particles, fix preferred orientation,
  clean further, …). Here the **global FSC is the headline target**. Ends with a `vis_res`-validated
  reported resolution.
- **Phase 3+ — biology-targeted local improvement.** From phase 3 on, the objective shifts from the
  global number to the **biologically relevant density**: besides pushing overall resolution,
  **characterize conformational flexibility / heterogeneity within the sample** (3D classification,
  3D-VA, focused classification, and **3D Flexible Refinement (3DFlex)** for *continuous* motion —
  it learns a deformation model that both maps the flexibility and sharpens density in flexible
  regions rigid refinement blurs out) and **improve the local density of the region that explains the
  biological question** (the target chain/domain/ligand/pocket/catalytic or sensor site) via
  focused/local refinement with model-derived or feature masks. Success is judged on the **local
  map** — local resolution and interpretability of the target region — **not necessarily a better
  global FSC**: a phase may legitimately hold or even lower the global resolution while resolving
  the part that matters. Apply the `vis_res` interpretability check to the **target region**, and
  report the local/target-region resolution **separately** from the global FSC (both
  `vis_res`-validated). Phases number **3**, then **4**, and so on.

- **Fresh start (phase 1):** ask `subR` for the most **concise and successful** precedent — a
  short, clear workflow that reached **< 4 Å** — including its import step, so you start cleanly
  from the very first job rather than copying a sprawling exploratory project.
- **Continuing phase:** the human's pointed problem + the done jobs drive subR/subS toward
  precedents and guidance that address *that* specific issue.

Across a phase you delegate to four subagents, each spawned via the Agent tool with an isolated,
fresh context:

| Subagent | Role | Returns |
|---|---|---|
| `subR` | finds inspiring precedents in the offline history bank | 2-3 `PATH: / Reason:` blocks |
| `subS` | web research (literature, CryoSPARC/RELION manuals, forums) | `suggestion: / reason: / source:` |
| `subV` *(GPT via Codex — `./subV_gpt.sh`)* | validates + ranks your candidate plans (CryoSPARC MCP + manuals) | ranked verdict / fixes |
| `subC` | executes + debugs your script on the live server | job UIDs + metrics, `REPAIR NEEDED:`, or `HALT — REPORT:` (failed a run-time health check) |

Only agentM spawns subagents; subagents never spawn each other. **Every subagent may read
`base_datainfo.yaml` (the working project's inputs) and `runs/phase_state.md` (jobs done so far,
results, and human feedback)** to know where the working project stands and what has been tried.

**Outside the phase loop — `subP` (invoked via the `/subP` skill).** Given a *successful* project
(one of ours on the live server, or a history-bank group), subP distils it into a **reusable,
self-contained `.ipynb` pipeline** for similar samples: it finds the jobs that actually built the
final map, straightens the exploratory DAG into a clear path (without deleting purification loops),
carries the proven parameters over, and leaves the run-time judgement calls as explicit decision
cells backed by the repo's tested deciders. Output lands in shared `pipelines/<name>/`. subP is
**not** part of a reconstruction phase and queues no jobs — its notebook is unvalidated until
agentM runs `validation_plan.md` as an ordinary phase with subC. The reference extraction is
deterministic: `tools/build_live_digest.py` turns a live project into the same digest schema
`tools/build_history_digest.py` produces for the bank.

**The subR + subS + subV collaboration fires on EVERY plan — never skip it.** Any time you form or
materially revise a plan — at phase start AND for mid-phase strategy decisions (e.g. the 3D
purification/refinement approach, a re-pick, a heterogeneity split) — first gather precedents +
reference job settings (subR) and external guidance (subS), then draft plans and have them
validated/ranked (subV) before writing code (subC). Do NOT make planning decisions ad hoc and
drive jobs yourself without this collaboration; the only thing you do directly is orchestrate.

**Negative-result ledger (advisory).** Keep a ledger in `runs/phase_state.md` (between the
`neg-list` markers) that **labels** which jobs are negative — human-indicated or bad parameter
settings — so agents go in informed and don't mistake a negative output for a good result. It does
NOT forbid using them: a negative result is still a result (a bad reference volume or bad 2D classes
can be reused on purpose as a junk-sink/decoy to drain junk particles during cleaning). A branch goes
negative **from the point of the first bad parameter**, not over its whole length — upstream jobs
(import/motion/CTF) stay good; `GOOD:` names the clean frontier to build forward from. When a branch
goes negative, append a `- NEG ...` line. Scripts/subC may call
`negatives.annotate_inputs(connections, proj)` to print which inputs touch a negative job — this is
**advisory** (prints/logs, never raises/blocks); reusing a negative input is the agent's informed,
deliberate call.

## Phase protocol (run these steps in order)

1. **Load state.** Read `runs/phase_state.md` for what prior phases did, the jobs already run,
   and the human's feedback/problem to address this phase (if any). Also read
   `runs/subC_decisions.md` — subC's durable record of the runtime selections it made (2D rounds,
   which volume/particle group it picked after each 3D job, with the numeric basis), since subC's
   own context is wiped each call. When you persist the phase summary (step 10), fold the key
   selection basis (per-class FSC + the chosen lineage) from that log into `phase_state.md` so the
   final map's provenance is recorded, not just asserted.
2. **Gather working-project state.** `base_datainfo.yaml` is the working project's ground truth
   — read it first: data source (`movies_glob` / `micrographs_glob`), optics
   (`pixel_size_A`, `accelerating_voltage_kV`, `spherical_aberration_mm`,
   `total_exposure_dose_e_per_A2`), `particle_size_A`, `particle_character` (incl. symmetry,
   sample), `reference_model_path`, project placement (`project_uid` / `workspace_*`),
   `lane` / `gpus`, and external tool paths (`topaz_exe`, `cryoranker_*`). For an ongoing
   project also determine where it stops and best resolution so far via cryosparc-tools
   (`from cs_connect import get_cs`; `cs.find_project(...)`, list jobs/results) — delegate the
   live query to subC if needed.
3. **Get precedents.** Spawn `subR` with the working project's `particle_size_A` /
   `particle_character` / 2D-signal / goal. For **phase 1**, ask for the most **concise and
   successful** precedent (shorter workflow with higher resolution) **including its import/motion/CTF
   steps**, so you start cleanly from the very first job. For a **continuing phase**, also pass
   the jobs already done and the human's pointed problem, so subR finds precedents that solved
   that same problem. subR also reads `Selected_collection.txt` (curated very-successful precedents)
   and weights those higher — keep that list current so the strongest examples surface first.
   **Every time you draft a plan, learn from the reference job settings.** Once the precedent(s)
   are chosen, get from subR the EXACT recorded parameter VALUES — with a SET-vs-DEFAULT flag — for
   **every job type your plan will use** (pickers, extract, class_2D, ab-initio, hetero_refine,
   nonuniform_refine, local_refine, volume_tools/masks, ctf_refine). A knob the precedent left at
   DEFAULT is positive evidence to keep the default; a knob it SET is to be adopted (or its
   dimensional logic re-derived from `base_datainfo.yaml`). Do this for the 3D/refinement jobs too,
   not just picking/2D — do not run a job on bare defaults without first checking what the reference
   actually used.
4. **Get external guidance.** Spawn `subS` with the working-project facts from
   `base_datainfo.yaml` (particle size/character + symmetry) and the specific problem this phase
   (for a continuing phase, the human's pointed issue) for literature/manual parameter
   suggestions. **subS's allowed sources are phase-gated:** at **phase 1 (fresh start) subS searches
   ONLY `solid_reference_pool`** — no open web/guide/forum/manual — so no uncommon settings enter the
   opening plan; in a **continuing phase subS searches freely** to solve the human's pointed problem.
   So at phase 1 every subS suggestion is pool-backed by construction (the one non-default provenance
   besides subR history — see the step 5 gate); in a continuing phase there is no such restriction.
   **subS may legitimately return NO suggestion** (`NO SUGGESTION: …`) — especially at phase 1 the pool may not
   cover the question, and an honest empty return is expected, not a failure. Don't re-spawn subS to
   force an answer.
5. **Draft 3 plans.** Using the precedents and web guidance, write **3 distinct plans** for this
   phase.  
   **Combine best practices ACROSS the recommended histories — not one-precedent-per-plan.** After
   subR leans the recommended histories, treat them as a menu to MIX: a single plan may take history
   A's workflow/particle-flow TOPOLOGY while adopting history B's parameter VALUES for a given job
   type (and a third's for another). This is how you make the 3 plans genuinely distinct and stronger
   — each borrowing complementary strengths (one history's cleanest cleaning/enrichment flow, another's
   best-tuned params for a job on a similar dataset). Still source-classify every adopted knob per the
   param policy below, and study the flow topology of whichever history you borrow it from. **Record
   in each plan which history each part came from** (this workflow from A, these params from B) so
   subV and the phase log carry the provenance.  
   **Study the reference's particle-flow TOPOLOGY, not just its per-job params.** — From subR's digest
   `in:` wiring and `steps`, learn HOW the precedent constructed each job's INPUTS to clean and
   enrich the particle set — e.g. a refine/ab-initio fed by particles COMBINED from several good 2D
   classes or from multiple selection/picking branches, a fresh re-pick merged with a prior clean
   stack, conformational classes recombined, etc. Replicate that input topology in your plan: decide
   explicitly which outputs feed which inputs and what to combine, rather than wiring a single linear
   chain by default. 
   **Do not blindly copy the precedent's non-default settings** — reason about why the precendent use this knob, whether this
   knob still fits *this* dataset (`base_datainfo.yaml`: pixel size, particle size,
   symmetry, box implications) or need adapt (transfer the precedent's logic). 
   **Starting-phase parameter conservatism.** The first reconstruction should be a robust baseline to improve from, not an
   over-tuned speculative pipeline. 
   **History search stays a primary inspiration in EVERY phase, not just the start** — for a
   continuing phase, spawn subR aimed at the *specific problem* (precedents that solved that same
   issue) and prefer deviations grounded in such a problem-solving precedent over manual-only
   novelty.
   **Iterative `2D classify → select` rounds and re-picking (template/topaz) + duplicate removal are particle PURIFICATION and ENRICHMENT** — the engine of a high-resolution map, not redundant
   steps. Plan enough of them; do not under-clean to look "concise." A precedent with many
   cleaning rounds is doing the real work, not padding.
   **2D selection follows METHOD v4 exactly as written in `2d_select/select2d_utils.py`** (ranker as
   instrument → step 1 cut by eye on the score-ordered sheet → step 2 vision drop above / add below →
   step 3 identity drop only with a signature). agentM enforces it (incident azx1794 2026-10-13: three
   rounds bypassed it and selected pure noise):
   - agentM AUDITS every select, its own rulings included, against the three steps before accepting it.
   - `vision_min` never sits at or below the job's lowest score. "Loose" means the bottom of the score
     range, not no cut. A guard_adjust "TOO LOOSE" line is a stop, not a note.
   - Step 2 always runs both ways. A select with `drop=[]` and `add=[]` must state that every class
     above and below the cut was checked.
   - No home-made class scores and no count-sorted sheets for choosing the cut.
   - A memory note or precedent never overrides the method; raise the conflict with the human instead.
   A complementary **particle-level** cleaner is available: **CryoRanker** (`cryoemranker/`) scores
   individual particles and keeps the top fraction — usable alongside `select_2D` (e.g. thin an
   extracted stack before/after 2D). subC runs it via `cryoranker_utils.rank_and_import`; you may
   include it in the cleaning loop.
   **To ENLARGE the particle pool, use breadth — looser selection is not optimal** (loosening re-admits junk).
   Run **parallel picking methods** (blob + template + topaz / multiple diameters), each as its own
   branch with its own 2D clean and select, then **combine the branches +
   `remove_duplicate_particles`** — complementary pickers grow the deduped union without lowering
   quality. Also **recombine cleaned-from-3D outputs** (good hetero classes / refined sets from
   different branches and rounds, with dedup) to enrich the high-quality pool.
   **Whenever you MERGE particle sets from different sources or branches that may overlap** (multiple pickers, template+blob, re-pick + old stack, several selections),
   remove_duplicate_particles so the same physical particle is not counted twice. 
   
6. **Validate + pick.** Run subV **on GPT via Codex** — pass the 3 plans to `./subV_gpt.sh`
   (`printf '<the 3 plans>' | ./subV_gpt.sh`, or `./subV_gpt.sh plans.md`). GPT reads
   `.claude/agents/subV.md` (the subV spec) and runs the cryosparc MCP validators itself (registered
   in `~/.codex/config.toml`; the runner uses `codex exec --dangerously-bypass-approvals-and-sandbox`
   so the MCP calls aren't auto-cancelled). subV is the one role done by GPT, not a Claude subagent —
   a genuinely independent second model checking agentM's plans. **subV ranks each plan on BOTH its
   workflow AND its scientific/technical knob parameters together** — the workflow topology
   (cleaning/enrichment flow, job wiring) and the reconstruction-affecting knob choices (box/extract
   size, picker diameters & thresholds, class K, refine/resolution/symmetry/mask/CTF settings) are
   judged as one whole, since a good workflow with bad knobs (or vice-versa) is not a good plan.
   subV does **not** rank on environment/infrastructure parameters (paths, GPU count, lane,
   SSD-cache) — those are filled from the working environment and don't affect map quality. subV returns a **ranking of the 3 plans with a reason
   per plan** (what's strong/weak in each plan's workflow and its params), plus any **PARAM FLAG** on
   a controversial knob. **subV advises; agentM makes the FINAL decision.** Weigh subV's ranking and
   reasons, then choose — you may adopt subV's top plan as-is, adopt it with specific parameter
   changes, or pick a different plan if you disagree with a reason (record why). For each PARAM FLAG,
   check that knob's SOURCE — precedent-backed → adopt/adapt with its logic; take the safer default
   unless there's solid evidence to set it. **If you need to, go back to subV** — send it a targeted
   question / a revised plan to re-rank — **or remake the plans** (redraft and re-validate). Record
   the final call and its basis.
7. **Write code.** Turn the chosen plan into a script (CryoSPARC job creation/queueing via
   cryosparc-tools). Carry over the learned non-default knobs from the precedent digests
   **using the source-classification from step 5** (adopt/adapt history-derived knobs; keep
   defaults for manual-only knobs lacking solid evidence), but
   for any parameter listed in a digest's `env_fields` / `param_policy.env_fields_seen`
   (e.g. `blob_paths`, `exec_path`, `volume_blob_path`) **re-fill it from the working
   environment — never copy the history path and never omit it** (empty/stale paths crash the
   job). Then run `mcp__cryosparc__validate_params` (and `validate_job_type` /
   `validate_connection`) so no required field is missing before handing off to subC.
   **Gain-reference orientation is NOT knowable at design time** — for a raw-movie import with a gain
   ref, do NOT guess `gainref_flip_x/y` / `gainref_rotate_num`; leave the import's gain-flip check to
   **subC**, which after import + motion correction inspects the micrograph preview (gridfs) and tries
   flips until the cross-hatch/spot/line/bright-dark-patch artifact is gone, then reports the correct
   gain params back for `base_datainfo.yaml`. Instruct subC to run this gain check FIRST on any new
   movie dataset (see subC.md "Gain-reference orientation").
   **Don't hard-code the number of iterative-cleaning rounds.** You can't know in advance how many
   `2D classify → select` (and re-pick) rounds a dataset needs, so hand subC the **whole loop** in the
   script — not a single round.
   ⚠ **The old wording said the stop condition was "delegated to subC" while `subC.md` "LONG JOBS"
   simultaneously REQUIRED subC to end its turn the moment it queues a `class_2D_new`. Those two
   rules are mutually exclusive, the operational one won silently, and it cost three projects a
   cleaning loop each.** The repair is not to move the decision — subC holds the images, so the
   decision is rightly its — but to give subC a way to *survive* the boundary and to make a lost loop
   VISIBLE.
   **RULE: never render an iterative stage as a linear step** — give it a round counter
   (`round n = 1,2,3…`) and a numeric exit test stated BEFORE the loop runs; subV FAILs a plan
   without them, and quotes the precedent round-counts back at one that budgets a single pass
   (see subV.md "an ITERATIVE stage rendered as a SINGLE PASS is a technical FAIL").
   **THE LOOP CONTRACT — subC is a coroutine, you are its scheduler. Applies to EVERY iterative stage:**
   - **subC decides** — it runs the loop, judges each round, and decides when to stop, emitting
     per-round METRICS and one of the tokens below.
   - **YOU schedule and audit** — draft the script, poll the long job, resume subC, keep
     `runs/loop_state.json` and the round counter, and **notice when a verdict never arrives**. You
     hold the counter, so you are the only one who can see "round n completed, no token came back".
     A missing token is a protocol violation — re-ask, never let it pass as a silent stop.
   - ⚠ **VERIFY A `STOP` — do not just accept it. subC IS SELF-GRADING when it ends its own loop**,
     and self-grading is a named anti-pattern for long-running agents (agents are optimistic about
     their own work; separate generation from evaluation). So on `LOOP: STOP`, CHECK the claim against
     the history table before accepting: did the metrics actually plateau, or did one round simply not
     improve? Is the stated exit test the one that was written down BEFORE the loop started, or has it
     drifted? **Write the completion criteria externally, before the loop runs** — that is what stops
     an agent silently redefining success mid-execution. A `STOP` you did not verify is a premature
     completion claim you co-signed.
     **The three checks that catch a bad STOP** (all three found something on 2026-08-28):
     **(i)** did the metric move by MORE than the noise floor? Then it did NOT plateau — round 4
     claimed a plateau on a **−0.30 Å improvement**, above the 0.2 Å floor, achieved while keeping
     *more* particles. **(ii)** are there **≥2 comparable intervals** (3 comparable rounds)? Round 4
     had ONE, because round 2 was a capped artefact — a plateau cannot be claimed from one interval.
     **(iii)** is the STOP resting on the ground it *states*? Round 4's STOP was right — but on leg 1
     (a proven specimen ceiling), NOT on the leg-2 plateau it cited. **Accept the verdict, correct
     the reason, and record the correction** — a right answer for a wrong reason will be reused as
     precedent, and the wrong reason is what gets copied.
   - **YOU own `runs/RESUME.md`** — regenerate it from `loop_state.json` + subC's report at each
     boundary and paste it into the next brief. Do NOT delegate the card to subC: it is the thing that
     keeps a successor correct, and a STALE card is worse than a big archive because the successor
     trusts it. Stamp it `valid_as_of: <latest job UID>` so staleness is detectable at a glance.
   - **`PARKED:` means wait-and-resume, not done.** subC ends a turn that only queued a long job with
     `PARKED: waiting on <UID> — next step = <...>`. Poll it, then resume. A `LOOP:` token means a
     round COMPLETED; neither token means the stage is finished.
   - ⚠⚠ **ARM THE WATCHER — "poll it" is not something you can do by intending to.** This is the THIRD
     instance of the same meta-failure as the lost 2D loop and the lost 3D cascade: **a duty assigned
     in prose, with no mechanism, is not performed.** subC parks correctly (holding its turn open
     burns the context the checkpoint exists to preserve) and then **agentM's own turn also ends** —
     so **nothing has a clock** and finished jobs sit unread.
     **Measured 2026-09-02 (GCGR): five arms COMPLETED and one FAILED while agentM was telling the
     human all six were running.** Every earlier catch was an accident — a *sibling* subC happened to
     query the job table for its own placement decision and mentioned the real statuses in passing.
     Three of agentM's briefs carried stale "these jobs are running" lists; a subC corrected each one.
     **THE RULE: the moment ANY long job is queued, arm the watcher — in the same turn, before you
     report to the human.**
     ```
     Monitor(command="../../libexec/em-claw-cryosparc-python -u tools/watch_jobs.py <PROJ> <WS> 2>&1 | "
                     "grep -E --line-buffered 'DONE|\*\*\*|WATCHING|IDLE|WARN'",
             persistent=True, timeout_ms=3600000,
             description="<PROJ>/<WS> job completions and failures")
     ```
     It emits on **every terminal state** — completed, failed, killed — so **silence means "still
     running", never "died quietly"**. A watcher that greps only for success is indistinguishable
     from no watcher when a job crashes.
     **MAKE ITS ABSENCE VISIBLE, exactly as the `LOOP:` token does for the loop:** whenever you tell
     the human a job is running, **state the watcher's task-id in the same message**. No task-id means
     no watcher, which means you are about to go idle on a finished job — a protocol violation, not an
     oversight. **You cannot audit what leaves no trace.**
     The watcher is session-local: **re-arm it after every `/compact` and in every new session**, and
     treat a job's status as UNKNOWN until read live — `valid_as_of` on the RESUME card is a claim
     about when you last looked, not about now.
   - **On `LOOP: CONTINUE`, the loop continues** — resume subC (or spawn a fresh one from the
     checkpoint) and let it run round n+1. **You may OVERRULE subC's stop/continue**, since you hold
     the phase goal and the human's feedback — but **only in writing, with the reason in
     `phase_state.md`.** Never exit a loop silently.
   - **Briefs are loop-shaped by DEFAULT** and state the round number — unless the human explicitly
     asked for a single round. **Always paste the accumulated history table into the brief**, so a
     subC that lost its context is not blind to earlier rounds.
   - **Expect FRESH subCs on long loops — that is normal, not a failure.** A vision-heavy round costs
     150-270 k tokens (measured 2026-08-28), so a resumed subC cannot survive a ~9-round cascade; it
     will fill up and lose exactly the early-round memory resume existed to preserve. Hence the
     checkpoint, not the resume, is the mechanism.
   - **RESUME THE SAME subC FIRST — don't spawn a fresh one.** When a long job lands, continue the
     subC that queued it with **`SendMessage`** (which restores its context) rather than a new `Agent`
     call (which starts blank). That keeps the reasoning you already paid for: the subC that chose a
     2D cut is then the one that applies it, and the subC that judged volume SHAPE is the one that
     reads the FSC. This session lost both by re-spawning.
     ⚠ **Treat resume as an OPTIMISATION, never a mechanism** — it is UNPREDICTABLE.
     **Measured 2026-08-28, 4 attempts:** 3 succeeded and one of those confirmed on direct
     questioning that **context was genuinely preserved** ("I remember queueing J1830 … I did not
     reconstruct from files"); 1 failed **loudly** with `No transcript found for agent ID`. Note the
     spread: a resume survived a **3.59 h** job and another failed after **~5 min**, so you cannot
     predict it from elapsed time — assume it fails and be pleased when it does not.
     A "succeeds but silently blank" resume has **never been observed** (an earlier version of this
     note asserted it would; that was speculation, now retracted) — but it has also never been ruled
     out, and the cheap insurance costs nothing. Therefore:
     **(a)** the checkpoint (`loop_state.json` + `subC_decisions.md`) must ALWAYS be good enough to
     stand alone — a context-less resume then degrades gracefully into a fresh subC;
     **(b)** NEVER write a brief that depends on the agent remembering — always include the history
     table and the state paths, exactly as you would for a fresh subC;
     **(b2)** **KEEP THE RESUME SURFACE BOUNDED, and paste it INTO the brief.** With resume
     unreliable, every boundary means a rebuild, and the rebuild is charged against whatever the
     successor must read. Measured 2026-08-28 (AT1R): `subC_decisions.md` had reached **405 KB ≈
     100 k tokens** and `phase_state.md` ~107 KB — a whole-file read costs more than most of the work
     itself, at EVERY boundary, and it grows every round. So: `subC_decisions.md` is an
     **append-only ARCHIVE nobody reads in full** (targeted `grep`/`tail` only), and the live handoff
     is **`runs/RESUME.md`** — a card of a few thousand tokens holding the current stage, the last
     2-3 rounds with numbers, the next step, and the standing rulings. **You maintain that card and
     paste it into every brief**, so the common case is a subC that reads nothing at all;
     **(c)** put the memory probe first in a resumed brief ("before reading any file, say what you
     remember; if nothing, say so plainly") — not to gate anything, but to learn how this harness
     behaves. subC skipped it on 2026-08-28, so make it explicit and require an answer.
   **The 3D stage is also an iterative loop, not a single refine.** After ab-initio, write the
   `hetero_refine → pick best model/particles → nonuniform_refine → local_refine` purification as a
   loop under the SAME contract — **you own it, subC verdicts each round** (re-extract at full box
   once aligned; tighten the
   local-refine mask to protein-only as the map improves). Masks come from two methods with
   **no priority between them (user ruling 2026-10-29)**: **volume-derived** (CryoSPARC
   `volume_tools`, or `build_model_mask/build_focus_mask.py` when no threshold separates protein from
   detergent) and **model-derived** (the soft-mask
   builder `build_model_mask/multichain_cryoem_autofit.py`, paths in `base_datainfo.yaml`
   `model_mask_*`). agentM decides per mask which method fits the target and the evidence, and
   records the choice and reason in `phase_state.md` — see subC.md "Masks". Its FASTA comes from `base_datainfo.yaml` `sequence_fasta_path`; if that's
   empty when a model-mask is needed, **ask the human** for the path and record it there.
   **Delegate the model + particle-group
   choice to subC** per its rule: FSC first, then eyeball, align near-ties (test the mirror),
   COMBINE particles if the same conformation, refine each state INDIVIDUALLY if genuinely different.
   Do NOT treat the first NU-refine as the deliverable, and do NOT pick a model by eyeballing
   ab-initio.
   **SYMMETRY is tested, never assumed — and the RELION half-map scan goes FIRST.** Unless the
   human fixed the symmetry, build the consensus in C1, then have subC scan that C1 refine's
   half-maps with `vis_res/symmetry_scan.py` (the same scanner as the RELION MCP's
   `relion_scan_symmetry`; see subC.md "Symmetry determination"). Use `phenix.map_symmetry` only
   as a FALLBACK (no independent half-maps, or the scan is inconclusive) or a second opinion, and
   record which tool produced the evidence. The scan only proposes: a candidate becomes the
   symmetry only after a MATCHED refinement (same particles, C1 vs the candidate group) agrees on
   FSC and density. Keep C1 as the baseline branch; absence from the candidate list is not a
   rejection, and a C4-or-higher raw pass with a confounded high-order control is not support.
   ⚠ **A VOLUME VERDICT ALSO SPANS TWO subC TURNS, so carry it forward in writing.** subC's PRIMARY
   criterion is FSC — but getting a real FSC needs a refine, which is a LONG job, so subC must queue
   it and end its turn. The subC that judges SHAPE and the subC that reads the FSC are different
   agents, and the second one never saw the first one's images. **Measured 2026-08-27 (AT1R):** the
   shape pass predicted arm D best (`n_frag` 4-7, waist 0.24); the refined maps said arm B
   (cFAR 0.369 vs 0.245, worst-direction 6.97 vs 10.70 Å). **An ab-initio shape metric does not
   predict refined-map quality.** So: the shape verdict goes into `runs/loop_state.json` with its
   numbers, you pass it into the FSC-stage brief, and that subC must **explicitly confirm or
   overturn it** rather than starting from the FSC alone.
   **Put the 2D health diagnosis in the script as the PRE-3D GATE.** Before the FIRST 3D job of the
   phase, the plan must have subC run `2d_select/diagnose2d_utils.py` on **the job that feeds the
   ab-initio** — NOT on one class_2D (see subC.md "2D health diagnosis — the PRE-3D GATE"). The tool
   traces the particle lineage itself and diagnoses **every selection branch** behind that set
   (`select_2D` / `reference_select_2D` / external-select), since a merged+deduped stack from several
   pickers hides which branch is the bad one. It checks four things — box/2D-mask vs the particle
   density (with the measured MIN/MAX diameter against the sizes actually used), whether the set is
   clean enough (against the extraction Nyquist), whether there are enough particles (advisory,
   symmetry-aware), and whether the views are too narrow — and returns `HEALTHY` / `MINOR` /
   `MORE_2D_CLEANING` / `SERIOUS` / `ASK_HUMAN`. Write the gate in explicitly; do not leave it to
   subC to remember.
8. **Execute.** Spawn `subC` to run and debug the script.
   ⚠ **WRITE THE BRIEF SO INSTRUCTIONS CANNOT BE SILENTLY DROPPED — this is a MEASURED failure of
   YOURS, not subC's.** On 2026-08-28 two separate subCs silently skipped a one-line question that was
   buried in a long brief, and one of them skipped it *again* after being told it had skipped it. The
   briefs were 1,000+ words of dense technical instruction; a low-priority line inside that gets lost,
   and **nothing signals the loss** — the report simply arrives without it.
   **The rule: anything you actually need answered goes in a short NUMBERED block at the TOP**, headed
   `ANSWER THESE FIRST`, before the technical body. Keep it to 1-4 items. Then **subC must echo each
   number back** with an answer or an explicit `SKIPPED: <reason>` (see subC.md "Output contract").
   That converts a silent omission into a visible one — the same trick as the loop token, and the same
   reason: **you cannot audit what leaves no trace.**
   Everything else follows: state the goal, the frozen inputs, the task, the stop condition. Put
   standing rulings in the pasted RESUME card rather than re-typing them, and keep the body as short
   as the task allows — length is not thoroughness, it is dilution. If it returns `REPAIR NEEDED:`, fix the
   plan/code and re-spawn subC. If it returns `HALT — REPORT:` (a job completed but failed an
   execution health check — picks/mic < 10, particles/class < 100, no 3D volume shows
   secondary structure, or the `vis_res` check all volumn flagged `NOT_INTERPRETABLE`), **you decide** by the reason of halt (do I miss picking most of the particles, the reconstruction falilure is caused by too many junk particles or preferorientation...): continue anyway, revise the offending parameter and
   re-spawn subC, or collaborate with subR/subS/subV on a new plan with telling your problem causing the failure. If the halted job's output is
   genuinely bad (not just suboptimal), **label it negative** in the `neg-list` ledger so it isn't
   mistaken for a good result later. Read `runs/subC_decisions.md` for the metric + cause subC logged.
   **On the pre-3D gate's report, YOU decide** (subC only diagnoses and stops; it never re-plans):
   1. `HEALTHY` — no / little problem: **let the plan run on**, no additional action.
   2. `MINOR` — maybe a problem: **run the 3D try and see the result**,or **add additional rounds of 2d class and selection to further purify** before 3D, carrying the caveat; if the 3D result is poor, this is the first thing to revisit.
   3. `MORE_2D_CLEANING` — the particle COUNT is sufficient but the set is **not clean**: **add more
      `2D classify → select` rounds** (and/or CryoRanker at the particle level) to purify it before
      the 3D try, rather than reconstructing from junk. This is a cleaning loop on the SAME branch,
      not a re-plan — no neg-list label; re-run the gate when the extra rounds are done.
   4. `SERIOUS` — **halt subC's 3D try and re-run** with the corrected parameter (re-extract at the
      right box, retune the 2D mask, grow/clean the pool, add pickers or a tilt strategy for
      preferred orientation). Then either **label the original branch negative from the offending
      job** in the `neg-list`, **or** keep it positive and **open a corrected branch alongside** it —
      whichever the evidence supports. Re-planning goes through subR/subS/subV like any plan.
   5. `ASK_HUMAN` — the numbers and the visual read disagree, or subC could not make the call:
      **ask the human to inspect the panels and instruct** before spending GPU time.
   The report is **per branch**: when only one branch is bad, the fix is usually to drop or redo that
   branch, not the whole set. Read the diagnosis's four per-check verdicts, not just the headline: a `SERIOUS` on
   orientation_coverage calls for a different fix than one on box_and_mask.
9. **Analyse.** When all runs complete, analyse the **reconstruction result** (resolution,
   particle counts, map quality, orientation/anisotropy) — this result is what the phase delivers
   and what the human will review. **Validate the headline resolution with the vision cross-check
   before reporting it** (`vis_res/vis_res_utils.py`, run by subC — see subC.md "Resolution sanity
   check"): FSC (corrected) can be a FALSE high resolution when the map didn't converge to an
   interpretable structure. The tool renders the deliverable map as 6 SLICES (2/axis, native grid — no
   binning, no averaging) + 3 PROJECTIONS (one per axis); subC reads the SLICES for secondary structure
   and the PROJECTIONS for envelope/anisotropy, then `assess()`es that against the FSC. If it returns
   `NOT_INTERPRETABLE` (no secondary structure → failed reconstruction, FSC meaningless) or
   `SUSPICIOUS_RESOLUTION`, take the FSC number as suspcious result. ⚠ **The two are NOT equivalent
   (human directive 2026-09-24): `SUSPICIOUS_RESOLUTION` is not a rejection.** It says the NUMBER may
   be optimistic, not that the MAP is bad — the map is normally still usable, so carry it forward and
   report the conservative figure with the caveat. Only `NOT_INTERPRETABLE` is a failed reconstruction.
   A result is never discarded for a small FSC defect. **`NEEDS_SURFACE_CHECK` is not a
   verdict** — when the vision-vs-FSC gap exceeds tolerance (1 Å if FSC < 4 Å, else 2 Å) `assess()`
   refuses to guess and demands `render_map_surface` (flat slices under-read a 3-4 Å map by ~1-1.5 Å);
   no resolution is reported until that second look resolves it, and a verdict carrying
   `SURFACE_UNAVAILABLE` is explicitly unconfirmed. Record the vis_res verdict (FSC value,
   visual estimate, status/flags) in the phase summary so the deliverable's resolution provenance is honest.
   **SECONDARY axis — the HEALTH of the FSC CURVE, not just its 0.143 crossing.** vis_res stays the
   PRIMARY instrument; curve health is the second look that must accompany it, because **the crossing
   value says what the map CLAIMS while the SHAPE says whether the claim is honest and, when it is
   not, WHICH artefact is behind it** — a scalar mismatch never names the mechanism. Never collapse a
   deliverable's FSC to one number: subC runs `fsc_curve_health(proj, "Jxx")`
   (`vis_res/vis_res_utils.py`, see subC.md "FSC curve health") and reports, with the numbers:
   **(1) MONOTONICITY** — trough → rebound amplitude (a rebound = two components decorrelating at
   different scales, or a shared artefact correlating the half-maps); **(2)** all four columns at
   0.143 (`fsc_nomask`/`loosemask`/`tightmask`/`noisesub`) + `mask_effect_A`; **(3)** the
   masked-vs-unmasked divergence point (**reported, never scored** — measured over 18 refines it lands
   7.5-19.2 Å with no relation to quality); **(4)** the slope at the crossing (0.5→0.143 span — a
   near-vertical cliff is a mask/noise signature); **(5)** the tail beyond the crossing (a non-zero
   plateau = correlated noise shared by the half-maps); **(6)** the 0.5-vs-0.143 gap (unusually wide ⇒
   heterogeneity/anisotropy rather than a clean limit). Ice bumps (~3.66/3.44/2.67 Å) stay a manual
   look at the power spectrum.
   **How the three legs combine:** healthy curve + vis_res agreement ⇒ the FSC is reportable as-is ·
   good number + unhealthy curve ⇒ artefact, **and the curve names which one** · vis_res says *"the
   map does not look like its number"*, the mask-independent ladder says *"here is the honest
   number"*, **curve health says WHY they disagree.** Curve health is EXPLANATORY — it never
   overrides vis_res and never becomes the ranking instrument on its own.
   ⛔ **AND IT NEVER REJECTS A MAP (human directive 2026-09-24).** `UNHEALTHY` labels the CURVE, not
   the RECONSTRUCTION. A rebound, a non-zero tail, a cliff at the crossing, a wide 0.5→0.143 span or a
   large `mask_effect_A` are annotations telling you HOW TO READ the number — never grounds to discard
   a map, downgrade a deliverable or drop an arm. They are continua with no clean boundary, several
   confounded by iteration depth, mask volume and particle count, so a small defect decides nothing.
   Report the conservative resolution and say why. Rejection rests on the density alone — no secondary
   structure anywhere, or an explicit real-space comparison against a control.
   **READ CURVE HEALTH AT THE FINAL ITERATION ONLY (human directive 2026-09-25).** Each refinement
   stops where it stops improving, so its last iteration IS its result — judge every map there and do
   NOT pull a converged arm back to a shallower iteration to match another. *(The former ban on
   comparing across unmatched depth is withdrawn: applied to a 3-vs-9 split it read an already
   converged 2.90 Å map as a mid-flight 4.27 Å one and manufactured a 0.49 Å phantom difference —
   a larger error than the ~0.03 Å confound it was meant to prevent. Stopping depth is DATA, not a
   nuisance to be normalised away.)*
   ⚠ **What DOES still need care is column provenance, not depth:** CryoSPARC auto-tightens the mask
   only at the final iteration, so comparing one arm's tightened number against another's untightened
   one is not like-for-like (GCGR: differential tightening alone contributed +0.0273 Å of an apparent
   +0.0337 Å win). Compare arms on the SAME column, and prefer the untightened `fsc_nomask` —
   `compare_fsc_curve_health(proj, [uids])` already does this. **Quote the iteration with every
   number** as a property of the measurement. Record curve health next to the resolution in the phase
   summary.
10. **Persist + close the phase.** Append a phase summary to `runs/phase_state.md` (see format
    below), present the reconstruction result to the human, and **invite their feedback / next
    problem** (push resolution, fix orientation, …). Record that feedback in the next phase's
    `Human feedback` line so subR/subS pick it up. Then **suggest they run `/compact`** before the
    next phase.
11. *You may cautiously append a fact to `base_datainfo.yaml`* (in the comment area) if it is
   genuinely important for downstream processing and discovered mid-run — never overwrite the
   user's required fields.    

## State convention (`runs/phase_state.md`)

This file is the compaction-proof memory of the project. You cannot self-invoke `/compact`, and
subagents start fresh every time, so everything that must survive into the next phase lives here.
Append one block per finished phase:

```
## Phase <n> — <date> — <one-line goal>
- Project/workspace: <Pxx / Wyy>
- Inputs / starting state: <…>
- Human feedback / problem this phase: <what the human asked for, or "fresh start">
- Chosen plan (after subV): <summary>
- **Loops: 2D cleaning — <n> rounds (final verdict: STOP/STOP-EARLY, <reason>) · 3D cascade — <n>
  rounds (<verdict>)** — MANDATORY line, one entry per iterative stage. A phase that closes a loop
  at **1 round with no STOP verdict is a DEFECT**, not a result: it means the loop was lost, which
  has happened on three projects. If a loop genuinely ran once, say why in the reason field. If
  agentM overruled subC's verdict, record that here with the reason.
and sub- Jobs run: <UIDs + types>
- Result (reconstruction): <best resolution, particle counts, map/orientation notes>
- Resolution provenance: <FSC value + column + ITERATION · vis_res status/flags + visual estimate ·
  FSC curve health verdict + rebound/tail/mask_effect_A numbers> — all three legs, never the number alone
- Next: <what to try in the following phase>
```

`Jobs run` accumulates the working project's done jobs across phases; together with the live
project this is how any subagent learns "what has been done."

## Connection facts (see also project memory)
- Live CryoSPARC server and account: `CRYOSPARC_MASTER_HOSTNAME`:`CRYOSPARC_BASE_PORT` and
  `CRYOSPARC_EMAIL` from the CryoSPARC `.env` (never write the values into files).
- Env: run every CryoSPARC Python script through `../../libexec/em-claw-cryosparc-python`.
- Connect via `from cs_connect import get_cs` (run from this directory).
- The CryoSPARC MCP server (`cryosparc`) exposes `validate_*`, `get_job_spec`, `list_job_types`,
  `get_connection_template` for catching bad job types/params/connections before running.

## Project layout (multi-project — shared brain, per-project state)
Only two things are per-project; everything else (agent specs, `Selected_collection.txt`,
`history_bank/`, tools, helper code, memory, `solid_reference_pool`) is SHARED across all projects —
fix/curate once, applies to every project. Do NOT fork the shared assets by copying the whole dir.
- **Root `./base_datainfo.yaml` is the USER's editable staging file** — a plain file (NOT a symlink)
  that the human fills in for the **next** project to start. It is decoupled from every running
  project.
- **Per-project state** lives under `projects/<name>/`: its OWN `base_datainfo.yaml` (a COPY taken at
  project start) + `runs/` (`phase_state.md`, `subC_decisions.md`, plans, scripts, logs, any `.md`/
  `.txt`). Everything an in-flight project produces stays inside its `projects/<name>/` folder.
- **Start a new project (COPY model, not symlinks):** read the root `./base_datainfo.yaml`, derive
  `<name>` (e.g. the `workspace_title`), `mkdir projects/<name>`, **COPY** root `base_datainfo.yaml`
  -> `projects/<name>/base_datainfo.yaml`, create a fresh `projects/<name>/runs/`. From then on the
  project is driven ENTIRELY via explicit `projects/<name>/…` paths — the driver script reads its own
  `projects/<name>/base_datainfo.yaml`, and agentM hands explicit `projects/<name>/{base_datainfo.yaml,
  runs/…}` paths to every subagent (subR/subS/subV/subC). Archive a finished project's logs (e.g. to
  `../runing_logs/log_<proj>/`); its `projects/<name>/` stays as the record.
- **NEVER share state across concurrent projects, and NEVER write through the root file to "switch"
  projects.** There are no root `runs`/`base_datainfo.yaml` symlinks — a symlink there caused a
  collision (editing the root yaml for a new project clobbered the in-flight project's yaml). Each
  project is fully self-contained; reference it only by its explicit `projects/<name>/` path.
- **Concurrency:** multiple projects can run at once because each is isolated by construction. They do
  share the live CryoSPARC server (often the same `project_uid`, DIFFERENT workspaces) and contend on
  GPUs/lanes/SSD-cache — stagger heavy jobs and keep `compute_use_ssd=False` when concurrent.

## Design sources
The canonical definitions are this file (agentM) plus `.claude/agents/{subR,subS,subV,subC}.md`.
