# Langfuse integration: decisions (2026-10-13)

Branch `feat/langfuse` (from `main` at `6d6b08b`), step A of the plan the owner approved on 2026-10-13: the code, the offline tests and this file. **0 live commands**: no Gemini request, no request to Langfuse (the one network use of this step was `uv add`, which talks to PyPI to resolve the dependency; it is not a command of the live budget). The test split was not opened, `eval/results/phase8_test/` and `docs/PHASE8-PREREG.md` were not touched, and `.env` was not read.

Langfuse is the industry-standard layer for traces, cost and evaluation **next to** the existing tracing and dashboard, not instead of it: `logs/traces.jsonl` and the monitor stay exactly as they were (a test proves the file is byte-for-byte the same with and without Langfuse).

## L0. Sources and versions (no API guessed)

- **SDK: `langfuse` 4.17.0** (installed and checked: `Requires-Python >=3.10`; released 2026-10-05 according to PyPI, read in the survey turn), pinned exactly in `pyproject.toml` like `langgraph`. It brings `opentelemetry-api/sdk/exporter-otlp-proto-http` 1.45.1, `opentelemetry-semantic-conventions` 0.66b1, `opentelemetry-proto`, `googleapis-common-protos` 1.75.5, `backoff` 2.2.1, `wrapt` 2.5.0 (`uv.lock` has them all).
- **Documentation read** (the survey turn, before the design was approved): [instrumentation](https://langfuse.com/docs/observability/sdk/python/instrumentation), [setup](https://langfuse.com/docs/observability/sdk/python/setup), [masking](https://langfuse.com/docs/observability/features/masking), [token and cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking), [scores via SDK](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk), [datasets](https://langfuse.com/docs/evaluation/experiments/datasets), [experiments via SDK](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk), [langfuse on PyPI](https://pypi.org/project/langfuse/).
- **Source read in this turn** (the installed wheel, `.venv/lib/python3.12/site-packages/langfuse/`), for every call the code makes:

| Call | What the code relies on |
|---|---|
| `Langfuse(public_key, secret_key, base_url, mask, span_exporter)` | no key: the client disables itself (we never build it); `mask` is called as `mask(data=...)` on input, output and metadata of every observation and, if it raises, the SDK sends `"<fully masked due to failed mask function>"`; `base_url` wins over `LANGFUSE_BASE_URL`, then `host`/`LANGFUSE_HOST`; the client is a **singleton per public key** (tests reset `LangfuseResourceManager`) |
| `client.start_observation(trace_context=, name=, as_type=)` and `observation.start_observation(name=, as_type=)` | no `start_time` argument; does not make the observation current; `trace_context={"trace_id": ...}` makes a root |
| `observation.update(input, output, metadata, level, status_message, model, usage_details, cost_details)` | only input, output and metadata go through `mask`; `status_message` does not (so the sink masks it) |
| `observation.end(end_time=<ns>)` | the only way to give a real duration after the fact |
| `Langfuse.create_trace_id(seed=)` | static; `sha256(seed)[:16]` as 32 hex characters |
| `propagate_attributes(session_id, trace_name, metadata)` | only for observations created **inside** the `with` (read by the span processor when a span starts), so the sink wraps each creation |
| `client.create_dataset`, `create_dataset_item(id=, ...)` (upsert), `get_dataset(name).run_experiment(name, run_name, task, evaluators, metadata)` | evaluators are called `evaluator(input=, output=, expected_output=, metadata=)` and return an `Evaluation` (or a list); `None` means "not applicable" |
| `langfuse.Evaluation(name, value, data_type="BOOLEAN"/"NUMERIC")` | `ExperimentResult.dataset_run_url` is an attribute |
| `LANGFUSE_HOST` | still read by the SDK but the SDK now prefers `LANGFUSE_BASE_URL`; the code reads `LANGFUSE_HOST` as the owner asked and falls back to `LANGFUSE_BASE_URL` |

## The four defaults the owner approved ("duyệt, dùng cả 4 mặc định")

1. **Guardrail and offer have no observation of their own.** The guardrail is metadata of the `answer` observation (its `output` is the span summary: `prompt_version`, `guardrail {checked, violations, fallback_used}`); the offer is the `reply` span (`template`) plus a `retrieval` span of mode `count` (`offer_drops`, `blocking_constraint`). So the observations match the internal spans **one to one** (a test counts them). If a separate `guardrail` span under `answer` is wanted later, it is a small addition plus a changed count.
2. **The two screenshots are taken by the owner** (this session has no browser tool, and a headless browser cannot use the owner's Langfuse login): `docs/screenshots/langfuse/traces-list.png` and `trace-detail.png`. The "no email or phone leaked" check is done through Langfuse's public API instead (same data the UI shows). Not done in step A.
3. **The "≤ 20 commands" budget counts every shell command that touches a live service, Gemini or Langfuse.** Ledger below.
4. **`langfuse==4.17.0` is a normal dependency** (not an extra), pinned exactly.

## L1. Off unless both keys are set; opt-in per tracer

`app.observability.factory.connect()/build_sink()` read `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` (then `LANGFUSE_BASE_URL`) from the environment, then `.env`, like every other setting. Missing, empty or still the `your-...` placeholder of `.env.example`: no sink, **`langfuse` is not even imported** (a subprocess test checks `sys.modules`), so CI (no key) sends nothing and the system behaves as before. A client that cannot be built or an SDK that is missing is logged as a warning (class name only: the message could contain a key) and the application starts without Langfuse.

The sink is passed to a `Tracer` explicitly (`Tracer(sink=...)`), and the **only** place that does it is `app.chat_api.build_tracer()`, used by the live backend (a test scans the repository: no other module uses `build_sink`). The reason: `Tracer()` is built in many tests and the owner's `.env` will have keys; a Tracer that switched Langfuse on by itself would send junk traces on every `pytest` run. `.env.example` gained the (commented) `LANGFUSE_HOST` line and a sentence saying that unset keys mean off.

## L2. What a request becomes

- **Root observation `shopmate-turn`** per agent request, trace id `Langfuse.create_trace_id(seed=<internal trace_id>)`, so the dashboard and Langfuse can be cross-referenced. `session_id` is the agent's thread id (propagated to every observation), `trace_name` `shopmate-turn`, `metadata.turn`. At the end: `input` = the question, `output` = the reply (both already through `mask`), `metadata` = `trace_id`, `turn`, `intent`, `status`, `reply_source`, `reply_truncated`, `schema_version`, and `level` WARNING/ERROR with the error type and message when the trace says so. Trace-level input/output are deprecated in v4, so they sit on the root observation.
- **One child per internal span**, same name, same order: `router`, `extractor`, `answer` are `generation` when they have a model (a template answer has none, so it is a `span`); `retrieval` is `retriever`; `order_lookup` and `ticket_confirm` are `tool`; `reply` is `span`. Retrieval's model is the embedding model, so it is **not** a generation. A generation has `model`, `usage_details {input, output}`, `cost_details`, and `metadata` (`cache_hit`, `start_ms`, `duration_ms`, `cost_basis`). The span's `summary` is its `output`. **No prompt and no completion is sent**: the internal trace does not keep them either.
- **When it is built.** The SDK takes an observation's start from the clock and only `end(end_time=)` takes a time, and `annotate_span` still adds to a span after it ended (extractor, order_lookup, retrieval in `graph.py`). So an observation is opened when its step starts, kept open, filled from the finished line, and ended at the end of the request at `start + measured duration`. An observation therefore never ends before it starts and lasts its measured duration plus what the SDK needs to open it (tenths of a millisecond). The root ends at the measured duration of the request, never before a child.
- **Hooks in `app/tracing.py`**: four calls (`request_started`, `span_started`, `span_ended`, `request_ended`), each through `_call_sink`. `Tracer._write` now returns the line it wrote (or None) and the sink gets that same line. Spans are flat children of the root (no span nests inside another in the agent today).

## L3. One mask, two layers

Everything sent goes through `app.tracing.mask` (emails, API keys and tokens, Vietnamese phone numbers) via `app.observability.masking`:
1. the sink masks **every string it sends** (the line is already masked; this also covers error messages, which the line only `redact`s, and `status_message`, which the SDK does not mask);
2. `Langfuse(mask=sdk_mask(secrets))` masks input, output and metadata of every observation again inside the SDK (tested on an observation that did not come through the sink).

`mask` is idempotent. A 32-hex id is left alone: about one in a hundred ids has ten digits in a row that the phone pattern would turn into `[sđt]`, and an id must stay exact (the id test uses such an id). `session_id` and model names get `redact` only, like in `traces.jsonl`. Not covered, as in `traces.jsonl`: a name or an address typed in a question.

## L4. Cost

`cost_details {input, output, total}` = tokens × the theoretical paid-tier list price, **0.30 / 2.50 USD per million tokens** (`app/observability/pricing.py`, pinned by a test to `eval/results/phase8_dev/answer_v2.json`; the source is in that file). The free tier really charges 0; the number says what the same traffic would cost, and every generation says so in `metadata.cost_basis`. Tokens are the Gemini `usageMetadata` the tracer already has, never an estimate: no usage metadata, no usage and no cost. **A cache hit is free** (`cost_details` all 0, no `usage_details`, the recorded tokens are kept as `cached_tokens_in/out` in metadata), because it was not billed again and would otherwise count as usage twice.

## L5. Langfuse can never break a request

Every hook is wrapped in `_call_sink`: whatever it raises is logged as a warning on `shopmate.observability` (**the exception class only**: the message could contain what the customer typed) and dropped; the traces.jsonl line, the reply and the exceptions of the request are untouched (tests: a sink that explodes at every hook, a client that fails in each of `start_observation`, `update`, `end`, `create_trace_id`, a line that cannot be built). If one `update` fails the rest is still ended and the first failure is reported to the Tracer. Export is asynchronous (the SDK's `BatchSpanProcessor` thread, flushed at exit); the request never waits for the network.

## L6. Order and ticket turns are withheld

If the intent is `order_status` or `create_ticket`, or any span is `order_lookup` or `ticket_confirm` (a follow-up turn can reach the tool with no intent recorded), the root's `input` and `output` are replaced by `[withheld: <intent>, N ký tự]` and an error's message is dropped (only its type is sent). The spans, their times and their summaries (`verified`, `found`, `attempts`, `state`) are still sent: they hold no order content. Tested on the fake client and on the real SDK (nothing of the order, the address or the email is in what the SDK would send).

## L7. Dependency, CI and the Phase 8 guard

- `pyproject.toml` and `uv.lock` change; CI installs with `uv sync --frozen` and the tests never reach the network (fake client; the real-SDK tests use an in-memory span exporter and CI also sets `SHOPMATE_BLOCK_NETWORK=1`). Test keys are neutral strings (`test-public-...`, `test-secret-...`) so that gitleaks has nothing to report.
- **`eval/final_evaluation.py` refuses `--split test` unless HEAD is the freeze commit or changed nothing in `app/`, `eval/` (outside results and cache), `pyproject.toml`, `uv.lock`.** This branch changes all four. **Turn 2 of Phase 8 must therefore run from the freeze commit, not from this branch**, and `feat/langfuse` should be merged only after it.

## L8. Dev results as a dataset and one experiment run (`eval/push_langfuse_dev.py`)

- Default is a **dry run** (reads, checks, prints the numbers it would send; no client, no network). `--push` sends and needs the keys.
- Dataset `shopmate-dev`, one item per dev question (`shopmate-dev-gs-xxx`, upsert): input = the masked question; expected output = the gold intent and the gold SKUs or clause. Then **one `run_experiment`** whose task only **replays** the answers recorded in `answer_v2.json` (0 Gemini calls), with three evaluators: `answer_correct` (BOOLEAN, the Phase 6 score of the item), `router` (BOOLEAN, first turn routed to the gold intent) and `top5_first_hit_rr` (NUMERIC, only the items with a gold SKU or clause). Order and ticket items are withheld (question, reply, gold) like in L6.
- **`top5_first_hit_rr` is not the pre-registered metric, and its name says so.** It was first called `mrr` and was renamed on the owner's request (2026-10-13) so that nobody compares it with the Phase 8 numbers: it is the reciprocal rank of the first gold SKU or clause in what the *agent* retrieved (its top five) in the committed dev run; the pre-registered `product_mrr` / `policy_mrr` of Phase 8 are measured on the full pipeline by `eval/final_evaluation.py` and are different numbers. The comment of the score says so too.
- Dry run on the committed dev results: 71 items (19 withheld), `answer_correct` 70/71 = 0.9859 (equal to the file's own count), `router` 1.0, `top5_first_hit_rr` 0.9242 over 33 items. The run name is `answer_v2@<first 12 characters of deterministic_sha256>`.

## L9. The test split stays shut

The script reads golden items through `load_split("dev")` only (a test checks the single call and its literal argument), has no option that names another split (`--results`, `--push` only), accepts only a file **directly inside `eval/results/phase8_dev/`** (so `phase8_test`, `phase6`, ... are refused by the directory rule; the script never names `phase8_test`), and refuses a result whose provenance is not `golden_items_read: "dev only"`, whose `dev_split_sha256` is not the current one, whose `deterministic_sha256` does not match its numbers, or whose item ids differ from the dev split. The existing guards (`test_golden_access`, `test_final_evaluation`) pass unchanged.

## Verification (step A)

- New tests: **97** (`tests/test_observability_{pricing,masking,sink,tracer_hook,factory,real_sdk,wiring}.py`, `tests/test_push_langfuse_dev.py`, plus the fake in `tests/fake_langfuse.py`). Written test first, and each file was run and failed for the missing module or argument before its code existed, **except `test_observability_real_sdk.py`**: it was written after the code, as the check that the fake client behaves like the real SDK (it passed at once apart from the assertion of mine described below).
- `tests/test_observability_real_sdk.py` runs the same flow on the **real SDK** against an in-memory exporter, so the fake cannot drift from 4.17.0.
- Mutation check of the tests that matter: switching off the withholding, the sink's mask, the SDK mask, the warning level, the free cache hit, or the catching of hook errors, each made at least one test fail (all six caught); the sources were restored afterwards.
- One test written by me (`test_the_measured_durations_...`) was wrong, not the code: it demanded the observed duration within 0.2 ms of the measured one, but an observation also contains the time the SDK takes to open it (0.2 to 0.6 ms). The code choice (read the clock after the SDK opened the observation) is the one that can never produce an end before a start, so the test now says "not shorter than measured, longer by at most 10 ms" (10 consecutive runs and 5 runs of the whole new group are green).
- Full suite: see the report (`uv run pytest -q`).

## Live ledger (budget 20, counting Gemini and Langfuse commands)

| # | Command | Service | When |
|---|---|---|---|
| | none in step A | | |

**Used: 0 of 20.**

## Step B (not run): what it will do, once the owner says the Phase 8 test split is done

The owner fills `.env` (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`) and starts Qdrant and Saleor. Then, about 10 commands, each written in the ledger above before it runs:
1. `uv run python -m eval.push_langfuse_dev` (dry run, no network) and `... --push` (Langfuse: dataset, 71 items, one run, 3 scores per item);
2. the chat API in live mode (`python -m app.chat_api --live`, Gemini and Langfuse), then four conversations through `POST /api/chat`: a product search with a filter, a policy question, an order lookup with an email and a phone number in the message (checks the mask and the withholding), and an empty search followed by accepting the offer (about 9 to 11 Gemini requests);
3. read the traces back through Langfuse's public API (the script reads the keys itself and prints none): one trace per request, one observation per internal span, tokens and cost on the three generations, and no email, phone number or key anywhere;
4. the owner takes the two screenshots (default 2).
Stops after that. Step C (README "Observability" section, architecture diagram, final `uv run pytest -q`) follows.

## Limits, said plainly

- The observations are flat children of the root; a span nested inside another would be shown beside it.
- Spans of the retry loop inside a Gemini call are not separate observations (the internal trace has one span per call).
- The last batch of traces is lost if the process is killed with `kill -9`; a normal exit flushes.
- Names, phone numbers in a format the pattern does not know, and addresses typed in a *product* question are not masked (same as `traces.jsonl`).
- Langfuse prices are the list prices of one day (2026-10-09 per the pricing page the report cites); a price change needs `pricing.py` and `answer_v2.json` updated together (the test fails until they are).
- Not done yet: README, diagram, screenshots, anything live.

## Undo

`git checkout -- app/tracing.py app/chat_api.py .env.example pyproject.toml uv.lock`, delete `app/observability/`, `eval/push_langfuse_dev.py`, `docs/DECISIONS-langfuse.md` and the new tests, then `uv sync`. Nothing else depends on it; `traces.jsonl` and the dashboard never did.
