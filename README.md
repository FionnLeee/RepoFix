# RepoFix

English | [简体中文](README.zh-CN.md)

An agent workbench for repository repair, built on a pinned version of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent).

Stack: Next.js / TypeScript workbench, NestJS / Fastify control API, Prisma / PostgreSQL, RabbitMQ and a Python Worker. Supports small Python repositories at fixed commits, multi-file patches and independent verification.

The agent loop, model integration and trajectory format come from mini-swe-agent 2.4.6 (MIT), pinned as a submodule at `04d809ceab9df28f9adaed044884180159172930`; the upstream license is retained. RepoFix adds task management, a cross-language Worker protocol, Docker sandbox integration, independent verification and the web workbench outside the upstream code.

## What you can demonstrate

The control API persists a repair task and dispatches it through an Outbox and RabbitMQ. A Worker runs the agent in a sandbox at the fixed commit and exports a candidate patch. Leases, generations and checkpoints handle duplicate messages and interruptions. Independent verification determines whether the candidate is valid. Applying it to a user's checkout requires separate approval bound to the exact patch and target file fingerprints. Agent completion, verification and delivery are separate states.

The interview entry point is the deterministic, zero-model [`/showcase`](http://localhost:3100/showcase) demo: inspect a task's versions, events, candidate diff and verification evidence. The service must be running to demonstrate live behavior. The three main contributions are **reliable scheduling, independent verification and version-bound delivery**. The basic agent loop, model integration and trajectory format are upstream capabilities; final SWE-bench scoring belongs to the official harness.

| Workspace mode | Scope and context | Development permissions and checkpoints | Final verification |
| --- | --- | --- | --- |
| `snapshot` | Bounded text repositories; full/compact/managed context | Shell writes can require approval; checkpoints save text, conversation and budgets, excluding processes and environment | Replay the candidate in a new sandbox and run identical independent tests on base and candidate |
| `image` | Existing repository images at fixed commits; full context required | Shell action approval is disabled; checkpoints save repository Git deltas, conversation and budgets, excluding full container state | Official harness scores SWE-bench tasks; RepoFix first checks patch production and provenance binding |

**Frozen results and their scope.** The first local attempt on all 50 distinct Verified Mini issues produced **33/50 resolved** according to the official harness: 30 issues had a 60-call limit and 20 had a 100-call limit. Ten additional Reviewer-on pairs and ten retries of earlier failures are excluded from that first-attempt denominator. The best result across at most two attempts is 34/50, rather than a same-configuration pass@1 or a Verified 500 leaderboard result. In the adapted Aider Polyglot Python 34-task evaluation, public candidate tests passed on 33/34 and the platform's strict verification passed on 22/34; these use different criteria. Three custom cross-file tasks, repeated three times in each context mode, gave 6/9 for each of full/compact/managed. Managed context showed no success-count or token advantage on those short tasks. Failures and empty patches remain in the denominators; small Reviewer differences are not presented as a consistent improvement.

`scripts/interview_metrics.py` reads the frozen Mini attempt CSV, per-issue manifests and archived `result.json` files, checking run IDs, patch hashes, official verdicts and the first-attempt denominator. It also checks the 34 Aider runs and 27 context runs. Its derived JSON contains a schema version and input SHA-256 hashes; it neither runs models nor rewrites original evaluations. Pass `--mini-csv`, `--artifact-root`, `--aider-runs`, `--context-report` and `--output` explicitly. Raw CSV files and run artifacts remain in local `runtime/` and interview material folders, outside Git.

**Naming.** Source packages, the Compose project, RabbitMQ queues, database accounts and new sandbox labels use `RepoFix` / `repofix`. Older run records, evaluation artifacts, checkpoints and image hashes retain their original values. Existing database and retrieval data were retained through a one-time migration, and historical tasks remain queryable.

## Historical review and corrections (2026-09-19)

The current implementation differs from older experiment records in the following ways:

- SWE-bench exports use the exact run IDs from each batch. A historical nonempty patch cannot replace a failed attempt from the current batch. Unique `runtime/validation/subset-<uuid>/` directories hold manifests, predictions, gold data and official reports. Manifests retain retry chains, usage from every attempt and instance/run/patch/configuration digests; unknown call counts are null. Standalone exports require explicit `scripts/swebench.py export --run-id <id>` arguments, repeated for multiple instances.
- Official verification is separate from execution status. SUCCEEDED means execution ended; unjudged candidates are shown as awaiting official verification. Results are imported by instance, run ID and patch SHA-256. Wrong versions, nonterminal runs and conflicting results are rejected; repeated imports are idempotent. `scripts/swebench_subset.py --publish-batch <manifest.json>` retries imports without model calls.
- New image tasks do not derive allowed paths from gold or inject `FAIL_TO_PASS` into development commands. Snapshot adapters require explicit allowedPaths. **The historical 3/10 result used the old prompt configuration.** Later five-instance pairs are documented below. Screening gold patches establishes local evaluability, not whether other patches could succeed.
- Coder, Reviewer and image tasks share Redis concurrency quotas and request-time budget checks. Redis failure rejects new model requests; expired slots are reclaimed. Review counters and trajectories are persisted immediately. `agent.step` and `model.call` have separate timings.
- Invalid Reviewer output records a failure. Dropped findings and truncated evidence are marked incomplete. Development tests that alter the candidate invalidate review evidence. Large files prioritize code near diff hunks; deleted files support base-side locations, and new files appear in the diff list. The Reviewer has no tools; these mechanisms do not establish an improvement in repair quality.
- Strict approval conservatively exempts only a small set of clearly read-only direct commands. Scripts, compound shell commands and uncertain commands require approval. Auto is a heuristic for common commands, not a complete permission model. Applying a final patch to a user's checkout has the separate approval process documented below; sandbox editing is distinct from delivery approval.

Older descriptions of Redis fail-open behavior, automatic selection of historical nonempty patches and gold-derived prompts have been superseded by these behaviors.

## Repository contents and releases

Private repository: [FionnLeee/RepoFix](https://github.com/FionnLeee/RepoFix), default branch `main`. Clone with `git clone --recurse-submodules https://github.com/FionnLeee/RepoFix.git` to obtain the pinned upstream dependency.

Validated improvements are committed on topic branches and proposed to `main` through PRs. Merged `main` is the archived source baseline. Use `git log --oneline` to find historical versions, and use revert commits and PRs to preserve history when undoing changes. This version targets local demonstrations and research reproduction; it has no production SLA.

Git contains source, configuration templates and the English/Chinese READMEs. Learning notes, interview materials, design plans, retrospectives and validation records stay local. Actual `.env` files, database data and `runtime/` artifacts are outside the archive and must be configured or restored separately. The READMEs summarize frozen results and their criteria; per-issue evidence must be checked against retained local artifacts.

`.gitignore` excludes environment variants, credential directories, private keys, database snapshots, Office/PDF study files and evaluation output folders. Only sanitized `.env.example` templates may be committed. `.dockerignore` excludes corresponding private material from build contexts. `benchmarks/tasks.json` remains a reproducible custom task input; results, predictions, gold data and model trajectories stay in local `runtime/`. Ignore rules do not remove tracked files or historical copies; removing a file from tracking requires a separate check while retaining the local original.

## Local setup

Requires Git, Docker Desktop/Linux Docker, Node.js 22 and uv.

```bash
git submodule update --init --recursive
uv sync --frozen
uv run python scripts/configure.py
uv run python scripts/prepare_baselines.py
docker pull python:3.12-slim
docker compose up -d --build --scale worker=2
```

Web: <http://localhost:3100>. API: <http://localhost:3101/health>. The `.env` configuration file stays local.

The interview demo is at <http://localhost:3100/showcase>. Select one of three built-in cross-file tasks for deterministic execution without paid model calls. Open a run's focused evidence page to inspect fixed source, key events, the candidate diff, deterministic review findings and independent base/candidate tests. The full workbench also exposes tool trajectories. Verified demo runs are shown by default, with recent failures and cancellations available separately. Preset Coder/Reviewer behavior is distinguished from real-model evaluation. `GET /showcase` and `GET /showcase/runs/:id` return only local built-in demo summaries, excluding SWE-bench runs.

The full workbench history sidebar uses `GET /runs/summary` with stable pagination by creation time and run ID, status filters and direct Run ID lookup. `GET /runs` remains available to existing clients. Lists carry summaries; details are fetched when selected. Terminal task details stop continuous polling, while delivery status refreshes separately. For the same 40 local records, the original list returned 249.3 KB versus 8.65 KB across two summary pages, about 96.5% less response data. This is not a page-latency or production-throughput measurement.

When authorized to reuse TicketPilot's model configuration:

```bash
uv run python scripts/configure.py --ticketpilot-env /absolute/path/to/ticketpilot/.env
docker compose up -d --force-recreate api worker
```

The script maps only known OpenAI-compatible settings, retaining independent database passwords and Worker tokens. It does not print credentials.

## Verification

```bash
uv run --no-sync python scripts/doctor.py
uv run --no-sync python scripts/verify_interview.py unit
uv run --no-sync python scripts/verify_interview.py build
# Only against an isolated running test stack, without real models:
uv run --no-sync python scripts/verify_interview.py smoke
```

`unit` runs tests without Docker under both `free-quota` and `official-deepseek` host policies; tests prevent accidental provider calls. `build` builds the API/Web. Docker integration, fault injection and deterministic deployment smoke are separate tiers. `scripts/protocol_check.py` injects faults and requires an isolated stack with its Worker stopped and no in-flight tasks. Default smoke uses deterministic models; `--live` is a separately authorized real-model evaluation. Artifacts remain in local `runtime/`.

`scripts/diagnose_run.py <run-id> --output-dir <dir>` inspects failed-run checkpoints read-only and writes independent `diagnostic.json/patch` files. The patch is an **unsubmitted, unverified intermediate snapshot**, excluded from `result.patch` and official predictions. `scripts/interview_pack.py` builds a local offline HTML evidence pack from two explicit frozen runs. `scripts/public_test_entry.py` can inspect tracked files at a fixed base checkout for a bounded public development smoke entry point. It remains a read-only helper, not automatically integrated into image tasks; historical Mini development commands and scores are unchanged.

## Repository tasks and baselines

Choose a baseline task in the web UI, or supply a public GitHub URL, full 40-character commit, subdirectory, issue description, allowed files and independent unittest verification code. Custom repositories use real models; built-in baselines support both preset actions and real models.

Execution reads the fixed source snapshot, dispatches through RabbitMQ, runs the upstream agent in a Docker sandbox, checks modification scope, generates a multi-file Git patch and reapplies it to a fresh base copy. Identical-image tests run separately on base and candidate. Success requires assertion failures in the base without test-loading errors, the candidate passing the same unskipped tests, and a nonempty patch.

Snapshot input is limited to ordinary UTF-8 text: at most 100 KB per file, 200 files and 4 MB total. Binary files, symlinks, submodules and file-mode changes are unsupported. Execution uses a network-disabled Python standard-library environment without arbitrary dependency installation. Development commands and verification code run only inside sandboxes.

Register an exact local private-repository version without uploading its source to a new GitHub repository:

```bash
uv run python scripts/register_repository.py --repo /absolute/path/to/repo --id my-project --commit <40-character-commit>
```

The registrar reads only tracked files at the specified commit. It neither executes repository code nor includes uncommitted worktree changes. Set the UI source to `registered:my-project` and use the registered commit. Registration artifacts stay in `runtime/repositories/` and are mounted read-only into Workers. Register only source repositories suitable for model processing.

`prepare_baselines.py` builds reproducible Git snapshots for three custom tasks: order discounts and shipping, pagination boundaries and slicing, and configuration parsing and defaults. Each task requires two module changes and includes development tests plus five independent verification checks. Definitions and preset fixes are in `benchmarks/tasks.json`; the model sandbox receives only buggy source and development tests. These tasks support engineering regression and small comparisons, rather than SWE-bench or generalization claims.

```bash
# Check preset repairs across all baselines and both context modes; no model calls
uv run python scripts/evaluate_baselines.py
# Real-model pairs: one run per task/mode, retaining failures in the denominator
uv run python scripts/evaluate_baselines.py --live
# Optional repeated experiments consume additional model calls
uv run python scripts/evaluate_baselines.py --live --repeats 3
# Reverify archived source, tests, image ID and patch without model calls
docker compose exec -T worker python -m repofix.replay <run-id>
# Recover omitted usage into a new report without model calls or original-report changes
uv run python scripts/evaluate_baselines.py --recover-report runtime/validation/<report>.json
```

Run artifacts retain source snapshots, patches, test summaries, source/test/patch hashes, Git commits, container image IDs, model parameters and full trajectories. Summaries appear in the UI; complete local reports are in `runtime/validation/` and `runtime/artifacts/`, outside Git. Reverification requires these artifacts and corresponding images. Model outputs are not guaranteed to repeat exactly.

## M1 Checkpoint

Workers save checkpoints at safe boundaries: task start, a fully returned tool result and execution completion. Checkpoints contain workspace text files, including additions/deletions, active and full conversations, compression records, model/tool call counters, elapsed time and known costs. They bind the task, execution generation, original source digest, sandbox image ID and agent/model configuration digest. Registration requires Worker authentication, a valid lease and consecutive sequence numbers. Save events appear in the UI; `GET /runs/<run-id>/checkpoints` returns registered records.

```bash
# Restore the latest registered checkpoint in a disposable sandbox; check and destroy it
docker compose exec -T worker python -m repofix.checkpoint <run-id> --generation 1
# Use --checkpoint-id <id> to inspect a specific historical snapshot
```

Artifacts are in `runtime/artifacts/<run-id>/checkpoints/g<generation>/`. Python's `TracedAgent.restore_checkpoint(reference)` resumes from the latest safe boundary in a fresh sandbox within the same valid generation and configuration/image. Initialize the sandbox with checkpoint files, then call `enable_checkpoints` with the original run/source. Consumed budgets are retained and completed tool actions are not repeated. Recovery waiting time is excluded from consumed execution time. Unknown prices prevent claims of actual dollar-budget enforcement.

Terminal or expired snapshots, failed checks, configuration/generation mismatches, background processes and unconfirmed model/tool results cannot serve as continuation points. Snapshot checkpoints cover only the documented text workspace, excluding processes, environment variables and `/tmp`; image Git-delta recovery is described below. The CLI only verifies restoration. Lost-task requeueing, cross-generation recovery and approval recovery are described in M3. The UI has no direct restore-from-checkpoint button; coordination and approval decisions trigger recovery. Deterministic fault tests validate recovery behavior, rather than real-model effectiveness.

## M2 Context management: indexes, budgets, rules and memory

With `managed` context, the Worker rebuilds each model request rather than sending accumulated history directly. It includes four parts: verbatim system rules and original task; verbatim applicable root/directory `AGENTS.md` rules, rejecting startup above 16 KB; current state and evidence, including workspace summaries, modified paths, valid memories, retrieved code and a compressed structured summary; and recent conversation history. Full trajectories and actual requests are independently archived in `runtime/artifacts/<run-id>/context/call-N.json`.

Download the pinned local embedding model before starting; this does not use a paid API:

```bash
uv run python scripts/prepare_embeddings.py
docker compose up -d --build --scale worker=2
```

`qdrant/bge-small-en-v1.5-onnx-q` (revision `52398278…`) is verified against a manifest and mounted read-only into Workers. Qdrant starts with default Compose and listens only on localhost.

- **Versioned code indexing:** Source is split at AST function/class boundaries and line limits. Qdrant payloads bind project, commit, index ID, embedding version and file hash. The control API tracks pending and published builds per index head. Publication requires every declared point and a matching manifest; incomplete, stale or wrong-generation builds return 409. Retrieval queries only published indexes and accepts snippets only when their hashes match the current workspace. Modified/deleted snippets become invalid immediately; run-specific edits use an independent overlay index. Unavailable Qdrant/embeddings trigger literal current-file retrieval with `INDEX_FALLBACK`; ownership errors stop the task.
- **Budgets and compression:** UTF-8 bytes provide a conservative token upper-bound estimate. `CONTEXT_WINDOW_TOKENS` defaults to 16,384, reserving 1,600 for output, 512 for protocol and 1,024 for safety. Above 65% of input budget, or after a UI compression request, the Worker builds a deterministic structured summary of goals, constraints, recent decisions, modified files, the latest test excerpt, unresolved items and evidence references. It keeps recent complete action groups and makes no summarization model call. If still over budget, it drops evidence snippets, older history and memories in that order. The task, rules and latest action group are mandatory; excess becomes an error rather than silent truncation. Complete tool outputs are archived and readable through `repofix_read_log <id> <offset>`. `CONTEXT_ASSEMBLED` records estimates/sources; `CONTEXT_USAGE` records differences from actual provider input tokens.
- **Project rules:** Directory-scoped `AGENTS.md` files nest; child rules override parents only in their scope. Prompts explicitly place rules below platform policy and the user task.
- **Explicit memory:** Memories are saved only through the UI or `POST /projects/<id>/memories`, citing a source task/event. Optimistic versions support edit, disable/enable, delete and Markdown import/export. Memories bind the saved commit; commit changes require review and reconfirmation through a new task. Memory reads can be disabled per task. Every request refreshes valid memories from the control API, without cache use when it is unavailable. Memories and retrieval results are evidence, not authority.
- **Checkpoints:** Managed compression state and context configuration are bound to checkpoints and retained after recovery.

Managed tool outputs bind the post-action workspace hash. Later file changes replace stale outputs in requests and recent-test summaries with invalidation notices requiring rereads or reruns. Original content stays in trajectories; checkpoints retain version bindings. Older trajectories without hashes cannot be retrospectively checked. After this correction, 29 Linux tests and the latest eight context tests passed; deployment smoke confirmed two stale outputs were excluded from subsequent requests.

Functional checks on 2026-09-16 and 2026-09-17 included 28 Linux Worker-image `pytest` tests for published-index isolation, modified/deleted overlays, rule scopes, budgets, long-history compression, memory revocation, control-API failure, log reads and managed recovery. `scripts/context_check.py` used real Qdrant/local embeddings for cross-project isolation, memory disable/delete/version conflicts, commit-change review, manual compression persistence and incomplete/stale publication rejection. `scripts/context_smoke.py` completed a deterministic managed task and inspected its checkpoint restoration. These were behavior/fault checks; that stage had no real-model managed comparison and established no success-rate or token advantage. Later context experiments are recorded below. Byte estimates are conservative and actual token counts are usually lower.

## M3 Reliable scheduling and approvals

After claiming a task, the Worker obtains a recovery plan and chooses a fresh run, checkpoint recovery or an approval wait. Demo and real tasks share this protocol.

- **Lost-task requeueing:** Heartbeats renew leases. On expiry, the control API inspects the latest registered checkpoint and requeues if recoverable and below the three-recovery limit; otherwise the task remains interrupted. A superseded Worker receiving 409 stops its attempt without writing state.
- **Cross-generation recovery:** Reclaiming allocates a new generation. The Worker rebuilds from checkpoint files and verifies task, source, image, agent configuration and workspace digests before continuing. Consumed model/tool calls and time are inherited. Container disposal removes interrupted sandbox side effects; prior trajectories are archived as `runtime/artifacts/<run-id>/trajectory-before-recovery-g<N>.json`.
- **Version-bound action approval:** `auto` defaults to approval for writes outside allowed paths; `strict` requires approval for all file writes. The Worker saves a safe-boundary checkpoint, registers approval and enters a waiting state. Approval applies only to that action/workspace version; recovery rechecks both hashes. Rejection becomes an observation returned to the model. The UI exposes approve/reject actions.
- **Shared model concurrency:** Redis token slots limit all Workers' concurrent model calls through `MODEL_MAX_CONCURRENCY`, default 2. TTLs prevent permanent occupation after crashes. Redis failure records `QUOTA_UNAVAILABLE` and rejects new requests (fail-closed). Image-mode Coder and Reviewer share the same entry point, with cancellation and call/time/cost checks before requests.
- **Orphan sandbox cleanup:** Workers scan `repofix.managed=sandbox` containers, removing only those outside active runs and older than a 90-second grace period. An unreachable control API prevents deletion.
- **Broker reconnects:** Failed Outbox publication rebuilds the connection; unpublished rows remain for a later retry cycle.

Deterministic checks without generative model calls include `scripts/approval_smoke.py` for strict pause/approve/cross-generation recovery/verification, `scripts/recovery_check.py` for injected lease expiry, requeueing, registered recovery, stale-attempt rejection and orphan cleanup, and `scripts/quota_check.py` against real Redis for peak concurrency, waiting and slot recovery. The Linux Worker-image 48-test suite, 16 protocol checks, Ruff, builds and page checks passed at that stage. These are behavior/fault checks. Six delivery checks within the later 30-check protocol suite and end-to-end delivery smoke are described below.

```bash
# Strict approval, cross-generation recovery and independent verification; deterministic
uv run python scripts/approval_smoke.py
# Inject lease expiry, then check requeueing, recovery and orphan cleanup
uv run python scripts/recovery_check.py
# Shared concurrency quota against real Redis
docker compose exec -T worker sh -c 'python scripts/quota_check.py'
```

## Delivery to a target repository: final patch approval

The agent works in a private sandbox without access to host paths. Once independently verified or officially resolved, applying a patch to a user's checkout is a separate approved action executed by host-side `scripts/deliver.py`. The control API records and arbitrates it:

1. `prepare` verifies a Git worktree root containing the fixed base commit. Every affected file must match the base version: additions must not exist; deleted/modified files must match base blobs. `git hash-object --path` supports autocrlf checkouts, and a temporary index rehearses the patch. A successful check registers target path, HEAD, before/after blobs, target fingerprint and executor-token hash as `PENDING`, valid for 30 minutes. A target already containing the full candidate needs no delivery; mixed base/candidate/other content requires human resolution.
2. The delivery tab displays the target, whether HEAD equals the base, affected files, patch hash, target fingerprint and complete diff. Approval includes the displayed hash/fingerprint and rejects mismatches. A new snapshot for the same target invalidates old pending approvals.
3. `apply` can be claimed only by the registered token-bearing executor (`APPLYING`). It recalculates the fingerprint before writing. Changed targets or HEAD produce `INVALIDATED` without writes; failed `git apply --check` produces `NEEDS_ATTENTION`. Only matching candidate blobs after application produce `APPLIED`. Already-complete targets produce `APPLIED(already_applied)` without rewriting. Repeated runs and crash recovery avoid duplicate writes; unrelated uncommitted changes are preserved.

```bash
# After task verification, on your machine:
python scripts/deliver.py prepare --run <run-id> --target <your-repository-root>
# After approval in the UI:
python scripts/deliver.py apply --delivery <delivery-id>
# Deterministic delivery smoke: unrelated edits, invalidation and mixed states
python scripts/delivery_smoke.py
```

Delivery does not commit or change the user's index. Binary/special-file operations are unsupported; supported rename handling does not extend to arbitrary special files. Registration and application must run on the same machine using the same executor state file, `runtime/deliveries/<id>.json`. Delivery and task endpoints are local-only.

## M4 Independent review and workbench

The Reviewer receives the task, allowed paths, candidate diff, changed source and freshly rerun development-test output. It does not inherit the Coder's conversation, has no tools and cannot write files.

- **Structured findings:** `file / line / severity / finding / trigger / evidence / suggestion` must identify existing candidate files/lines. Unlocatable findings are dropped and counted. Unparseable output records a review failure rather than a clean review.
- **Bounded revision:** Review each candidate version once. Blocking findings return to the Coder for revision, defaulting to two rounds, configurable from 0–5. Candidate changes immediately make earlier reviews stale.
- **Finding dispositions:** The Coder can claim a fix or rebut with test/code evidence. Independent Reviewer rechecks save `fixed / rejected_with_evidence / unresolved / unverified`. Unchecked claims do not count as fixed. A rebuttal without edits can trigger another review; exhausted blocking findings remain recorded.
- **Shared budgets:** `reviewBudget=shared` shares the original call limit between Coder and Reviewer. `extra` retains the default five extra steps per revision round. The UI option is persisted and included in idempotent-create checks. Equal call/output/time ceilings do not imply equal input tokens or costs.
- **Verification decides:** Unresolved blockers or Reviewer failures do not halt independent verification. Review usage is included in the run's model totals and shared budget.
- **Workbench:** A read-only Monaco comparison shows the fixed base and candidate, with findings linking to named lines. Review history shows rounds and superseded reports.
- **Observability:** The control API generates a W3C `traceparent` per dispatch. The Worker continues it and exports model/tool/review/applicable-verification spans to Jaeger. API producer spans are not exported, so traces do not establish complete API/database/broker latency.

```bash
# Deterministic review-before-verification, one revision, stale review and clean rereview
uv run python scripts/review_smoke.py
# Control API → queue → Worker propagation and step spans
uv run python scripts/trace_check.py
# Existing fault injections with expected/observed behavior, about six minutes
python scripts/fault_matrix.py
```

SWE-bench integration is in `scripts/swebench.py`: instance-to-task mapping, official prediction exports, report imports and network/disk preflight. On 2026-09-19, cached local instance images, real-model patches and the official harness evaluated a **10-instance sample** across astropy/sympy/scikit-learn/matplotlib/pylint/xarray/pytest, after screening local evaluability with gold patches. **3/10 resolved**: `astropy__astropy-12907` (code hunk byte-identical to gold), `sympy__sympy-20590` and `scikit-learn__scikit-learn-13584`. Three other patches failed to resolve their issues, including one regression; four runs never submitted, with two step-limit and two repeated-format failures. `scripts/swebench_subset.py` reproduces the workflow; `preflight` reports other image/disk/dependency gaps. Later no-gold-prompt pairs are documented below. Historical and current model/prompt configurations cannot be pooled.

## Context comparisons and boundaries

### Image workspace recovery and artifacts

Image tasks first restore a disposable container's repository to the specified `base_commit`; additional image-preparation commits do not become the task base. Checkpoints bind the immutable image ID, base commit, task and agent configuration, saving binary Git deltas, conversations, call counts and elapsed time. Cross-generation recovery recreates a sandbox, replays deltas, verifies hashes and continues with inherited budgets. `python -m repofix.checkpoint <run-id>` also supports read-only image restoration checks.

Recovery covers tracked and nonignored repository files, excluding ignored caches, installed dependencies, processes, other container directories and `/tmp`. Full context remains mandatory and shell action approval remains disabled. Background processes or a delta beyond the checkpoint's 4 MiB encoded-snapshot limit reject saving rather than silently truncating it.

Candidate artifacts include every changed file in `file-changes.json`, content-addressed original base/candidate `blobs/`, the complete binary `candidate.patch`, and UTF-8 `source.json` / `candidate.json` previews. Additions, deletions, large/binary files and more than 60 changed files are supported. Remaining baseline content is identified by pinned image/commit. UI previews retain limits and count omissions separately; complete blobs are not discarded. The diff page provides base/candidate downloads per file. The API exposes only blobs listed in that run's manifest, checking size and SHA-256 on download.

Before requests, `MODEL_POLICY` validates routes through `services/agent-worker/repofix/model_policy.py`. `free-quota` accepts only the nine user-specified free model names. `official-deepseek` accepts only `deepseek-flash` at official `https://api.deepseek.com`, using official billing. The official route explicitly disables thinking mode; Coder and Reviewer do not automatically retry or switch models. Changing routes requires local `.env` settings for `MODEL_POLICY`, `MODEL_NAME`, `MODEL_BASE_URL` and `MODEL_API_KEY`, followed by a Worker rebuild. Configuration alone does not start evaluation.

The dated experiments below describe archived batches and end-of-batch service states; they do not establish the current local container or model configuration.

### 2026-09-22 frozen small sample

Five locally cached instances (scikit-learn-13584, pytest-7220, xarray-4248, pylint-6506 and matplotlib-18869) were paired with Reviewer off/on. Each arm allowed 60 total model steps, 1600 output tokens per call and 900 seconds. No gold paths or hidden-test hints were supplied, and gold screening was not used in this batch. Instances had previously participated in local development and were not an untouched random sample.

With `deepseek-v4.1-flash`, official results were **Reviewer off 1/5, on 1/5 resolved**, both passing only scikit-learn-13584. Both arms of pytest/xarray/pylint encountered repeated format errors before submission; matplotlib exhausted free quota in both arms. Excluding quota interruptions gives conditional 1/4 in each arm, but the original denominator remains five. The pair recorded 195 logical calls and 1,290,169 reported input/output tokens; failed requests had unknown usage. This frozen Worker's Coder transport attempted requests at most twice, so model steps do not equal HTTP requests or equal token spending.

Four real Reviewer calibration cases found both known defects and reported no blocking findings on two clean patches. This sample is too small to estimate general accuracy.

After the user selected `deepseek-v4-pro-0813`, two matplotlib arms were refrozen. At pause they had recorded 68 logical calls and 671,947 input/output tokens, without official verdicts. The user then requested waiting for a larger-quota model. Evaluation stopped with trajectories/checkpoints retained; **paused pro attempts are neither completed runs nor part of the resolved rate**. API token totals can differ from platform quota deductions.

`scripts/frozen_evaluation.py` handles freezing, exact run-ID exports and offline official judging; `--judge-only` submits no model tasks. Artifacts stay local. Further evaluation requires reconfirming the model and total token budget; this historical batch does not authorize trying other free models.

### 2026-09-23 official DeepSeek Flash paired reevaluation

After the user configured and authorized official `deepseek-flash`, the same five instances, pair order and shared limits were rerun: 60 combined Coder/Reviewer calls, 1600 output tokens per call and 900 seconds per arm, with `contextMode=full`, memory disabled, no gold/hidden-test hints and no gold-based screening. These cached convenience samples included earlier development instances and were neither random nor untouched.

The first frozen batch, `runtime/validation/frozen-20260923-official-flash-five-paired/`, scored **1/5 with Reviewer off and 1/5 on**, both passing only scikit-learn-13584. The on-arm revisions for xarray/pylint/matplotlib sent an internal `exit` message as an API chat role and were rejected by DeepSeek. All three failures remain in the denominator. The revision-message boundary was fixed while retaining internal submission markers in full trajectories; regression tests and all 113 Linux Worker tests passed at commit `344471a`.

A new Worker image refroze all five pairs after the fix, rather than retrying only failures. `runtime/validation/frozen-20260923-official-flash-fixed-five-paired/` again scored **off 1/5, on 1/5**, passing only scikit-learn-13584. Both pytest arms had indeterminate `no_tests_collected` failures; xarray-on exhausted 60 revision calls; pylint-off was refused submission because background processes violated checkpoint quiescence. Other nonpassing instances remain unresolved; execution completion is distinct from an official resolution.

The fixed on-arm batch recorded 11 finding-disposition items, including two xarray review rounds and one evidence-backed Coder rebuttal without explicit Reviewer confirmation. All were conservatively `unverified`, with one unresolved blocker. Other on-arms did not enter revision and their findings remained unverified. This five-instance sample showed no Reviewer resolved-rate improvement and cannot estimate its general effect.

Across both batches' 20 runs: **689 logical model calls, 686 usage-bearing replies and 5,579,209 reported input/output tokens** (2,974,437 initially; 2,604,772 after the fix). The three rejected initial requests returned no usage, making the total an observable lower bound. The user budget was about 20M; a stop-Worker threshold at 18M reported tokens was not reached. The Worker was stopped after evaluation, with no automatic expansion or continuation.

### 2026-09-23 additional interview evaluations

During the authorized 12:00–14:00 Beijing-time window, three SWE-bench Lite convenience instances were frozen, paired and officially judged: `pallets__flask-4045`, `pallets__flask-4992` and `psf__requests-1963`. Selection preceded model calls in `runtime/validation/interview-20260923-holdout-selection.json`; frozen artifacts are in `runtime/validation/frozen-20260923-interview-holdout-three-paired/`. Full context, memory off, no gold/test hints, Reviewer off/on and shared 60-call/900-second limits were retained. All six runs produced nonempty patches, but official results were **off 1/3, on 0/3 resolved**, with only Requests-off passing. Off recorded 58 calls/240,398 tokens; on recorded 83 calls/381,936 tokens. Four on-arm findings were `unverified`, with one unresolved blocker. This sample establishes no Reviewer advantage, and actual tokens differ.

Four further fixed Reviewer calibration cases found two known blocking defects and no blockers on two correct patches. This miniature behavior check cannot estimate population precision/recall. Three custom cross-file tasks ran full/compact/managed three times each, rotating order, with Reviewer and memory off. `scripts/evaluate_baselines.py --live --contexts full compact managed --repeats 3 --review-policy off` produced local `runtime/validation/interview-20260923-context/` reports. Every mode passed **6/9 independent verifications**, and all 27 usage records were complete. All nine failures missed the explicitly requested discount-range check. Reported input/output totals were **25,827 / 30,397 / 54,724**, with mean end-to-end durations **11.17 / 11.96 / 12.67 seconds**. Managed provided no success-count or token advantage on these tasks. Their explicit allowed paths distinguish them from SWE-bench evaluation.

Separate deterministic checks covered a 7/7 fault matrix, 6/6 delivery, 2/2 parallel Workers, 6/6 context smoke and 7/7 Worker-image context checks; Linux Worker pytest reported **114 passed**, and API/Web images built. These are system-behavior evidence, excluded from real-model resolution rates. The 33 new agent runs and four direct Reviewer calibrations reported **736,027 tokens**; together with the prior two batches, **6,315,236**. Three earlier rejected requests had unknown usage, so totals remain lower bounds.

### 2026-09-23 SWE-bench Verified Mini fixed ten issues

[Verified Mini](https://github.com/mariushobbhahn/SWEBench-verified-mini) is a third-party 50-issue subset of the human-curated SWE-bench Verified 500, containing Django and Sphinx only. [Inspect Evals](https://github.com/UKGovernmentBEIS/inspect_evals/blob/main/src/inspect_evals/swe_bench/swe_bench.py) also provides an entry point. It supports smaller local experiments but is not a separate official leaderboard track. Six issues were fixed first and expanded by predetermined positions to ten (five per repository) before official verdicts, without gold screening. Selection is in `runtime/swebench/verified-mini-ten-selection.json`. These ten were neither the full 50 nor a random sample.

Both Reviewer `off/auto` arms used official `deepseek-flash`, the same Worker image, full context, memory off, shared 60 calls/900 seconds and 1600 output tokens per call. Mini lacked newer harness metadata such as `eval_script`; `scripts/prepare_verified_mini.py` joins matching IDs from the [official Verified dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified), checking matching issue text, base commit, gold patch and test patch. SWE-bench 5.0.2 official judging occurs independently after model runs; judging metadata is excluded from agent prompts. Frozen run IDs, predictions and per-issue reports stay in `runtime/validation/frozen-20260923-verified-mini-*/`; the summary is local `runtime/validation/verified-mini-ten-summary.json`.

| Reviewer | Official resolved | Nonempty patch | Empty patch | Model calls | Reported tokens | Finding dispositions |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Off | **4/10** | 6/10 | 4/10 | 371 | 4,058,124 | Disabled |
| On | **5/10** | 6/10 | 4/10 | 380 | 4,095,065 | Five `unverified`, no unresolved blockers |

Usage was complete for all 20 runs; official infrastructure and indeterminate errors were zero. Only `django__django-12039` changed from off-failure to on-success; the other nine verdicts matched. Each arm had four empty patches, mainly from exhausted 60-call budgets; one on-arm also failed image checkpoint quiescence due to background processes. One extra resolution in ten nonrandom issues does not establish a general Reviewer benefit and cannot be pooled with earlier Lite samples as a leaderboard score. This batch reported **8,153,189 tokens**, bringing the total to **14,468,425**, below the authorized approximate 20M. At completion the Worker was stopped and API `LIVE_ENABLED=false`.

### 2026-09-24 full 50-issue Mini and call-limit checks

After the initial ten, Reviewer-off ran 20 more issues, resolving **16/20**. The first 30 distinct issues totaled **20/30** at a 60-call limit. The remaining **20 issues**, in original dataset order, ran at a 100-call limit and resolved **13/20**. All 50 distinct first attempts were complete: **33/50 resolved** under mixed call limits. This is an adapted third-party Verified Mini evaluation, not an official Verified 500 leaderboard result. Different call limits and Worker builds prevent treating the two groups as a same-configuration comparison.

Before retrying, all ten first-attempt failures among the initial 30 were fixed as a retry set and rerun sequentially at 100 calls. **1/10** became resolved, yielding a best-of-at-most-two-attempts **34/50**, rather than pass@1. The recovered issue used only 12 calls; five others exhausted 100 again. These data do not establish a benefit from a higher limit. Empty patches, unsuccessful patches and per-attempt usage are retained. The independent official harness determines Mini `resolved`; RepoFix handles orchestration, fixed-commit workspaces, agent tools and patch generation/export. Its own isolated base/candidate verifier applies to configured-test tasks, rather than being credited for official harness scoring.

Thirty additional runs reported **25,553,715 tokens**, with a then-cumulative **45,025,293**. Three earlier rejected requests returned no usage, so the total is a lower bound. Predictions, official manifests and artifacts stay in local `runtime/validation/` and `runtime/artifacts/`. `IMAGE_STEP_LIMIT` defaults to 60 and was explicitly 100 for this batch. Containers and Docker Desktop were stopped after the batch, then services were restored at the user's request. The recorded local `.env` had `LIVE_ENABLED=false`; future live calls require explicit enabling.

### 2026-09-24 other historical 60-call failures

After the user raised the cumulative task budget to 60M, the ten Mini-off retries above were excluded. Five other historical `LimitsExceeded` runs that reached 60/60 calls were fixed by original run ID: Lite pytest-off and xarray-on, plus Mini Django-on and two Sphinx-on issues from late September 23/early September 24. Tasks, Reviewer settings and official `deepseek-flash` stayed fixed; each rerun allowed 100 shared calls and received official judging. **5/5 valid reruns completed, 0/5 resolved**. Two produced unsuccessful patches and three still produced no final patch after 100 calls. This provides no evidence of a repair-rate improvement from the higher limit; changed Worker builds also prevent attributing differences solely to budget.

An initial pytest rerun hit a connection error after 51 usage-bearing replies and was restarted with a new request key. The infrastructure interruption is excluded from the five valid results, but its **511,927 known tokens** remain in total usage. Five valid reruns added **8,940,566 tokens**. The cumulative reported lower bound was **54,477,786 / 60,000,000**; reserving 1,200,000 for each of four unknown requests (this connection error plus three earlier rejections) gave a conservative budget estimate of **59,277,786**. This batch changes neither Mini's first-attempt 33/50 nor the ten-issue Reviewer pairs. Run IDs, predictions, official results and trajectories remain in `runtime/validation/historical-60-limit-to-100-*` and `runtime/artifacts/`. At that batch's end, Docker Desktop and services remained running with API `LIVE_ENABLED=false`; restarting or viewing services submits no paid tasks automatically.

`full` retains full conversation history. With sufficient older messages and history beyond 3,500 characters, `compact` replaces older history with an extract of at most about 1,200 characters, retaining system rules, original task and the four latest messages. Full original trajectories are archived independently and usage is aggregated from complete records; compression events appear in the UI.

This is lossy extractive compression v1, rather than semantic summarization or long-term memory. Character thresholds are not exact token budgets. Short tasks may not trigger compression, or may require more calls afterward. Success and token usage must be assessed together; small single-pair custom-task samples cannot establish statistical significance.

The 2026-09-16 initial preset checks passed 6/6 across three tasks and two modes. A single real-model pair using `deepseek-v4-flash-0731` scored full 1/3 and compact 2/3. Failures included two repeated-format errors and one missed discount-range check. Only one real run compressed history (5,001 → 3,985 characters). Usage omitted from the two failed reports was later recovered from full trajectories: full input/output totaled 7,163/1,770 tokens and compact 9,070/4,900, with complete records for all six runs. The sample showed no token savings, and the success difference cannot be attributed to compression. A separate custom-repository request changed two files and passed patch replay verification without model calls.

Successful and failed runs both aggregate trajectory usage, including format-error replies. `usage_status` distinguishes complete, partial and unavailable usage; partial values are known subtotals, not full-run consumption. Baseline reports identify completely covered runs. Format-retry prompts include a valid command-block example; three consecutive format errors still stop execution.

The application targets a single local user. User login, S3 artifact storage and a full SWE-bench Verified 500 evaluation are unfinished; third-party Mini's 50-issue first attempts are complete. Independent Reviewer and OTel traces exist, but inconsistent small-sample pairs do not establish a general Reviewer benefit. Trace export requires an OTLP endpoint. Lost tasks requeue from registered checkpoints at most three times; exhausted recovery or missing checkpoints leave them interrupted. Approval uses deterministic command parsing rather than a complete capability model; sandboxes, allowed-path checks and independent verification remain the practical boundaries. Redis shares concurrency quotas and is reachable only inside Compose. Task and approval interfaces lack a user-authentication system and are local-only. Fixed-test verification does not guarantee that arbitrary adversarial code cannot interfere with a test process. Historical ten-instance SWE-bench results were 3/10, with two other instances listed separately for environment limitations. Later Lite five-/three-instance and Mini ten-instance pairs, and mixed-limit Mini 50 results, do not estimate Verified 500 performance. `scripts/swebench.py preflight` reports image/disk/dependency gaps. Live calls use configured credentials; **HTTP 200 does not imply free usage**, and routing is explicit through `MODEL_POLICY`. The archived end-of-evaluation state had services running with API `LIVE_ENABLED=false`.

## Stop

```bash
docker compose down
```

Database data and artifacts are retained by default. Do not use `down -v` to delete them. The application binds local ports and has not implemented authentication for a public service.
