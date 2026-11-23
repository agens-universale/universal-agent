# Harbor ATIF export for UAG

**Status** Draft contract based on the November 20 scoring call and the ATIF 1.7
reference artifacts. Acceptance with the scoring team's importer is pending.

## Request and acceptance evidence

| Meeting time | Request | Acceptance evidence |
| --- | --- | --- |
| 24:17–25:10 | Inspect timestamps, prompt and completion usage, and the number of model calls in a UAG trial. | Each accounted model response carries its UTC timestamp, model name, call count, and available provider usage. |
| 27:23–30:39 | Keep the telemetry needed for scoring in the standard Harbor artifact. The team also discussed exporting native thread logs for debugging. | The trial contains `agent/trajectory.json` and the Harbor reader can import it. A tool invocation and its output can be joined by ID. |
| 31:45–32:04 | Check the final shape against a reference supplied by the scoring team. | Review the draft against the reference and import a UAG artifact with the scoring team's configured reader. |
| 23:35–23:52 | Run UAG as a Harbor External Agent with single-model and Auto profiles. | Live single-model and Auto trials verify production of the enriched artifact and usage attribution across a reroute. |

Before this change, the adapter reconstructed steps from live and compacted
message history. It represented function calls as text and assigned observations
without call IDs. The Harbor result carried run-wide usage, and ATIF carried
the step count.

## Export contract

- Use ATIF-v1.7 and write `trajectory.json` in the agent log directory. Harbor
  publishes it as `agent/trajectory.json`. Use the runner-provided session
  identifier when supplied, otherwise use the root agent state's run identifier.
- Emit ordered system and user messages and one `source: "agent"` step for each
  model response recorded by the transport, with sequential step IDs. Each model
  step carries `llm_call_count: 1`, the start of its request attempt in UTC ISO
  8601, and the reported model name, with the requested ID as a fallback.
  `extra.usage_category` identifies the accounting category, such as `main`,
  `finish_gate`, or `summary`. Local subagent steps appear in the same file with
  `extra.agent_scope: "subagent"` and `extra.subagent_id`.
- Enrich the existing step for a canonical root response with assistant text,
  reasoning, and tool calls. Auxiliary and local subagent steps carry telemetry.
  Responses with missing usage still get a step.
- For the main Chat Completions response, write provider `reasoning_content` or
  `reasoning` to the ATIF reasoning field. `strip_think` controls the agent's
  history projection. A response with only provider reasoning has no answer
  text in ATIF.
- Retain both canonical responses when Auto reroutes a turn. Mark the first
  response as discarded in `Step.extra` before awaiting the replacement request.
  Proposed tool calls on a discarded step are retained with an empty observation
  set.
- Represent calls as `tool_calls` on the originating response, with the function
  name, canonical tool call ID, and JSON object arguments. If a provider emits
  malformed arguments for an identifiable call, preserve that text in the call's
  extension field so the agent's validation result remains inspectable. If a
  response repeats a call ID, assign distinct IDs to subsequent occurrences
  before routing, execution, and export. Tool results use the repaired IDs.
  The Responses API continuation replays the call and result pairs from the
  available conversation history after a repair.
- Attach each committed model-facing tool result to its originating step using
  `observation.results[].source_call_id`. Keep the output as delivered to the model
  history, including error, timeout, and truncation details. A result retained
  only in history that is about to be compacted is captured before it is dropped.
  Export `extra.is_error` when the result reports a Boolean status. In a partial
  trajectory, calls awaiting a result remain open.
- Populate `metrics.prompt_tokens`, `completion_tokens` and `cached_tokens` from
  that response's usage when present. A cache hit is already included in prompt
  tokens. Export a reported `reasoning_tokens` counter in `metrics.extra` and
  preserve its reported value. The provider measures a model request as a whole.
  A tokenizer can separately estimate the size of a tool's output.
- Sum the reported step usage in `final_metrics` and include `total_steps`. Preserve
  the all-run usage accounting in the Harbor result. In a completed trial with
  fully reported usage, the step sums can be reconciled with the trial account.
  Total reasoning tokens are present in `final_metrics.extra` when every tracked
  model step reports that counter. Root `extra.trial_usage` mirrors the token totals
  in the Harbor result. The totals, `metadata.llm_usage_by_model`, and Harbor's
  `model_usage` field when available use one snapshot of the trial account.
  Root `model_calls` and `atif_model_calls` inside `extra`
  compare the accounted calls and the calls represented by published steps.
  `extra.call_statistics.model_calls_without_usage` counts responses for which
  the account received neither a prompt token count nor a completion token count.
- Capture the trace independently of compaction and record the summary inserted
  into the model's context. Publish the available trace on
  ordinary completion, error, or cooperative cancellation. Freeze the snapshot
  before writing it and publish the completed JSON atomically. New files follow
  the runner's umask. Replacing a trajectory preserves its existing file mode.

## Implementation

Bind a trace observer to the Harbor adapter's agent execution. Capture responses
where the transport records usage for Chat Completions and Responses API calls.
Update the matching step when the root loop converts that payload into canonical
messages. Use payload identity so distinct attempts can share a response ID.
Route model-facing tool output through the loop's message commit boundary and
track calls by ID. The adapter owns ATIF projection and file writing.

Model recordings are scoped to a trial, and commits of messages and tool results
are scoped to its root loop. Thread persistence and all-run accounting continue
through their regular paths. Finalization uses the adapter's cleanup path.

## Verification

1. Drive the real adapter with scripted model responses and fake task I/O.
   Check one response with multiple calls, reversed result order, actual usage
   values, timestamp and model identity, repeated message commits, missing
   usage, Auto discard, auxiliary and subagent responses, and cancellation.
   Check that a retried response retains the start time of its own attempt.
2. Validate a live artifact using `harbor.utils.trajectory_validator`. Check
   that every observation resolves to a call on its own step. Recompute usage
   from the published steps and reconcile it against the Harbor trial result
   for a single-model smoke with the finish gate enabled.
3. Run a local Harbor trial with Docker, the repository's `GcCliExternal` adapter,
   a synthetic two-file task and a real DeepSeek completion through Sber
   OpenRouter. Read authentication via the existing environment reference.
   Verify reward 1, the two actual tool outputs under their matching calls,
   and UTC timestamps, model names, and provider usage in the resulting artifact.
4. On receipt of the reference, compare its shape and values with the exported
   fixture and run the scoring team's importer on a UAG artifact.

## November 20 verification on `feat/atif`

The local branch starts at `master` commit `3b031a8fb`. The relevant suite
passed 694 tests, including Harbor integration, Auto routing, usage capture,
compaction, cooperative cancellation, observation ID matching, and the
Temporal port contract. Ruff, the configured complexity check, and the
Sonar-like check passed for the changed Python source.

The final single-model Harbor 0.22.0 trial `task__TYQMFzY` used local Docker and
`openrouter/deepseek/deepseek-v4.1-flash-1017` through the Sber proxy. The task
verifier returned reward 1. The Harbor validator accepted the trajectory.
The artifact has six timestamped steps, four model responses with usage, and
five tool calls with five observations linked on the originating step.
The two expected file contents were verified against the observations for the
two separate read calls. Summing the step metrics produced 53,376 prompt, 544
completion, and 39,424 cached tokens, exactly matching the Harbor trial result.

The Auto integration test ran the actual provider-adapter boundary with
scripted responses. It verified separate usage on each model step and routed
the output to the executed call when the discarded and executed responses
proposed calls with the same ID.

## Terminal-Bench 2.1 verification with the finish gate enabled

The local Harbor dataset provided `log-summary-date-ranges`. Its prompt and
verifier were used unchanged. The trial copy restricted container network
access during the agent phase. The reference solution was excluded from the
trial copy. The external adapter called DeepSeek V4.1 Flash through Sber
OpenRouter with the finish gate enabled.

The final trial `log-summary-date-ranges__jQPnH4Y` returned reward 1 and passed
Harbor's trajectory validator. The exporter produced 14 UTC-timestamped
steps. All 12 main-model steps matched their provider response IDs and
usage counters. All 15 tool definitions and argument objects matched the
provider responses, and every result linked to its own call. The 14 results
reused in later main-model prompts matched those prompts' tool messages
exactly. The remaining result was the final `turn_finish` observation.

Harbor counted 13 model responses. The thirteenth was the finish-gate
request, with 22,946 prompt and 140 completion tokens. Adding it to the ATIF
totals gives exactly the Harbor trial totals. This trial predates per-response
export for auxiliary model calls.

An integration regression test reproduced a mixed model response with an
integer tool ID and a valid call. The exporter now applies the same
identity check as the loop and retains the model usage and the valid
observation. The updated focused suite passed 717 tests.

## Verification after review fixes

The focused suite passed 768 tests, with one capability test skipped on Harbor
0.22.0. The new cases cover cancellation before worker startup and during timing
setup, repeated call IDs in Chat Completions and Responses, and preservation of
file permissions during creation and replacement.

The live Auto trial `regex-log__EHFZXMU` returned reward 1. Its ATIF contained
16 main-model responses and 13 linked tool results. All model names and token
counts matched the provider responses, including three discarded responses.
The timestamp of each model step preceded its logged request. Harbor accounted
for one additional finish-gate call.

The fixed-model trial `log-summary-date-ranges__NRPY4Be` returned reward 1
using DeepSeek with raw agent logs disabled. Its ATIF contained six model
responses and six linked tool results. Both trajectories passed the Harbor
validator and had file mode 0644 under umask 022.

## Verification with reference fields

The focused suite passed 769 tests with one Harbor capability test skipped.
The live DeepSeek trial `log-summary-date-ranges__HMQR7RM` returned reward 1.
The validator accepted six model steps with 918 reported reasoning tokens and
nine tool results with a status flag. The trial summary matches Harbor's seven
accounted calls. The additional response was a finish-gate call. The ATIF and
Harbor result use the same session identifier.

## Verification on the `develop` merge candidate

The candidate was tested on `develop` commit `0e323bb66`. The focused suite
passed 781 tests. One capability test was skipped on Harbor 0.22.0 because
that release does not expose `harbor.agents.capabilities`. The cancellation
regression test exercises a response arriving after finalization starts.
Usage field tests cover fully absent counters and one-sided usage.

All four Harbor trials used Docker and the Sber OpenRouter proxy. The trial
copies kept the original prompts and verifiers, excluded reference solutions,
and blocked container egress during the agent phase. The smoke profiles used
an 18-step task budget. SWE-bench used an amd64 override for its main container.

| Benchmark | Task and profile | Reward | ATIF / accounted model calls | Linked tool results |
| --- | --- | ---: | ---: | ---: |
| Terminal-Bench 2.1 | `regex-log`, DeepSeek + Gemini Auto | 0 | 19 / 20 | 18 |
| SWE-bench Verified | `pytest-dev__pytest-6202`, DeepSeek | 0 | 18 / 19 | 30 |
| SkillsBench | `threejs-structure-parser`, DeepSeek | 0 | 15 / 16 | 17 |
| Terminal-Bench 2.1 | `log-summary-date-ranges`, DeepSeek, raw agent logs disabled | 1 | 8 / 9 | 11 |

Harbor's trajectory validator accepted all four artifacts. The first three
also matched raw provider response IDs, chosen model names, and prompt,
completion, cache and reasoning counts. Model step timestamps preceded
the corresponding logged requests. Tool result flags matched
the reported status, and the discarded Auto response had no executed
observation. Each artifact's step sums matched its `final_metrics`. Its
`extra.trial_usage` and accounted call count matched the Harbor result.
The fourth trial emitted the enriched trajectory with no raw agent log.

## Verification after MR 201 review

The follow-up regression suite passed 891 tests with one Harbor 0.22.0
capability skip. Harbor 0.24.0 and 0.25.0 passed 43 adapter tests each. Coverage
includes an auxiliary model's usage, a response arriving after the snapshot,
the summary sent to the model after compaction, and the regular runtime's
handling of valid and repeated tool call IDs in JSON and SSE.

On Harbor 0.25.0, the live `log-summary-date-ranges__jDvSbTK` trial received
reward 1 and passed the trajectory validator with raw agent logging disabled.
Its seven ATIF model steps accounted for 144,972 prompt and 4,529 completion
tokens. The eight accounted calls in `extra.trial_usage` and standard
`result.json` `model_usage` agreed on 158,776 prompt, 4,633 completion,
and 109,312 cached tokens.

## Verification of per-response telemetry

The focused suite passed 1,880 tests with two skips. Harbor 0.24.0 and 0.25.0
each passed 47 adapter tests. The scripted adapter tests exercise the finish
gate, the summary request and its later inclusion in the root context, and a
local subagent's response. A regression test checks distinct timestamps when
two attempts reuse a provider response ID.

The live Harbor 0.25.0 trial `log-summary-date-ranges__zLnn8gi` used DeepSeek
through the Sber OpenRouter proxy with raw agent logging disabled. It returned
reward 1 and passed the trajectory validator. The nine model steps include
eight root responses and one `finish_gate` response. The step totals agree
with `extra.trial_usage` and `result.json` `model_usage` on 191,072 prompt,
4,154 completion, and 148,992 cached tokens. All ten tool observations link
to their respective calls.

## Importer decisions

- The target Harbor and importer versions, required timestamp convention, and
  the handling of discarded Auto responses.
- Whether a result should additionally include output before model-facing
  truncation or a reference to a separately retained raw stream.
- Whether the scoring reader expects a tokenizer-derived tool-output size or
  further telemetry such as costs or native diagnostic logs.
