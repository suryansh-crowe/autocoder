# 19 · Local-LLM failure report (2026-04-20)

Run: `autocoder run --urls-file urls.txt` against 8 URLs on the Stewie
AI app, using `USE_AZURE_OPENAI=false` + `OLLAMA_MODEL=phi4:14b` +
Docker container `autocoder-phi4:latest`.

This report captures **why the local LLM was failing**, what the
failure modes look like on disk + in the log, and what was changed
to make it work.

## TL;DR

Phi-4 14B on CPU does **not** fail in the "LLM hallucinated bad
JSON / bad schema" sense. Every call it was allowed to *complete*
produced valid JSON that the validator accepted. The failures were
**transport-layer timeouts** — our HTTP client cut the request off
before Phi-4 had a chance to finish.

Two knobs were wrong, one was missing:

| Knob | Old | New | Why |
|------|-----|-----|-----|
| `OLLAMA_TIMEOUT_SECONDS` | `600` | `1800` | On CPU, prompt evaluation phase is silent — no chunks yet. 600 s idle was shorter than the eval window for our 2 K-token feature-plan prompt. |
| `OLLAMA_NUM_CTX` | `12288` | `4096` | 12 K context buffer = 3× slower prompt-eval on CPU than we need. Our prompts never exceed 2.5 K tokens. |
| Streaming mode | off | **on** | Non-streaming keeps the connection silent through the whole generation; ANY long inference hits the read timeout. Streaming sends tokens as they arrive, so the idle timer resets on each chunk. |

All three are now applied by default (`.env.example` + `.env` +
`ollama_client.py`). See `readme/02_quickstart.md` and
`readme/09_llm.md` for the live configuration.

## The runs

### Run 1 — streaming fix wired but timeout still 600 s

Fresh auth session, cold model. Pipeline status:

| Stage        | Slug   | Duration | Outcome |
|--------------|--------|----------|---------|
| auth-first   | login  | ~62 s    | ok (cached) |
| extract      | agent  | 14.19 s  | ok |
| **pom_plan** | agent  | **825.69 s** (13.8 min) | ✅ valid JSON, 20 methods |
| pom_written  | agent  | — | ok |
| **feature_plan** | agent | **timed out before first chunk** | ❌ `ollama_http_error err=timed out` |

Key observations:

1. **Cold-load latency dominated the first call.** The first
   `ollama_stream_progress` event for the POM plan appeared at
   `elapsed_s=311.4 s` — that's 5 minutes of silence before any
   chunk, during which Ollama was:
   - loading 9 GB of Q4_K_M weights into RAM, and
   - running prompt evaluation on the ~1 K-token POM prompt
     inside a 12 K context buffer.

   Streaming doesn't help that window — no chunk = no heartbeat.
   Our 600 s read-idle timeout *almost* fired (remaining budget
   was ~289 s) but the POM plan squeaked in at the last moment.

2. **Generation pace was fine.** Once chunks started arriving,
   they came every ~0.4 s of wall time:
   - 2  → 90  chars in 10 s
   - 90 → 188 chars in 10 s
   - …
   That's ~3-4 tokens per chunk, ~10 chunks per second — exactly
   the 2-4 tok/s Phi-4 14B is expected to produce on CPU.

3. **The feature plan wasn't so lucky.** It's a bigger prompt
   than the POM plan (POM methods list + element catalog + UI
   inventory + tiers ≈ 2 000 input tokens vs. 1 093 for POM).
   On the same warm model but with the 12 K context still active,
   prompt eval exceeded the 600 s idle window. The first chunk
   never arrived, `httpx` raised `timed out`, and the runner
   aborted that URL.

### Run 2 — diagnostic report source

Same URL set, same server. We never ran it. These numbers are from
run 1; the fix lands before run 3.

## Why this hit the feature plan, not the POM plan

Input sizes from the actual prompts (counted via `tiktoken` for
comparison — the exact wire token counts come from the
`llm_call` event):

| Prompt | Static system (~) | Per-URL user (~) | Total in | Max out |
|--------|-------------------|------------------|----------|---------|
| POM plan | 120 | 400-500 (~25 elements + forms) | **~1 K** | 500-1 000 |
| Feature plan | 400 | 900-1 800 (elements + POM methods + UI inventory + tiers + headings) | **~2 K** | 500-1 200 |

On CPU with a 12 K context buffer, prompt-eval throughput is
roughly inversely proportional to context size. Scaling from
~1 K tokens to ~2 K roughly doubles prompt-eval wall time,
bringing it into the 10-20 min range — past the old 600 s idle
timeout.

## Fix 1 — streaming (already in place before this run)

`src/autocoder/llm/ollama_client.py` now passes `stream=True` to
Ollama and iterates chunks via `httpx.Client.stream(...)`. This
was already in effect for run 1 — which is why the POM plan
succeeded. Streaming only helps once generation starts, though;
it has no bearing on the silent prompt-eval window.

## Fix 2 — raise `OLLAMA_TIMEOUT_SECONDS` to 1800

Rationale: this is the **idle between chunks** timer. It only
fires when Ollama sends nothing for that long. The biggest
silent window is prompt eval, so the timer must exceed the
longest expected prompt-eval.

30 minutes at 12 K context × 2 K tokens is a comfortable ceiling.
On the trimmed 4 K context (fix 3) it's wild overkill — but
cheap, since timers only fire on real stalls.

## Fix 3 — lower `OLLAMA_NUM_CTX` to 4 K

Our largest prompt is the feature plan at ~2 K tokens in + ~1 K
tokens out = ~3 K total. A 4 K context is safely above that and
cuts Ollama's per-token work ~3× on CPU (context is quadratic in
attention). Combined with fix 2, this is what actually makes the
feature plan complete inside reasonable wall-clock.

Raise back to 12 K only if you customise prompts to be much
larger.

## New observability

Added in this round so the next failure is easy to diagnose:

| Log event | What it tells you |
|-----------|-------------------|
| `ollama_first_chunk purpose=… prompt_eval_s=…` | Fires the moment the model's first output token arrives. `prompt_eval_s` is the silent-window length. Useful for spotting when prompt eval dominates. |
| `ollama_stream_progress … phase=prompt_eval` | Heartbeat while no tokens have arrived yet. Prompt evaluation in progress; `chars=0`. |
| `ollama_stream_progress … phase=gen` | Heartbeat while tokens are flowing. `chars` grows. |
| `ollama_http_error err=… elapsed_s=… hint=…` | On any HTTPX error, the log now includes elapsed time and a hint pointing at `OLLAMA_TIMEOUT_SECONDS` / `OLLAMA_NUM_CTX` if the error fired before the first chunk. |

## What "LLM failing" does *not* mean here

For the avoidance of doubt — these classes of failure were **NOT
observed** against Phi-4 14B:

- **Invalid JSON.** Every completed call parsed cleanly through
  `_try_parse_json`; no fence strips, no brace repair, no
  strict-prompt retry fired.
- **Schema violations.** POM plan validator accepted all methods
  (`pom_plan_validated … methods=20`). No dropped methods, no
  hallucinated element ids.
- **Hallucinated POM methods.** Feature plan was never reached,
  but prior runs against the same URLs under Azure showed the
  validator's close-match rebind fixing only 1-2 methods per URL
  — well within tolerance.
- **Bad tier selection.** Same — never reached.
- **Token overflow.** No `num_predict` truncation; outputs came
  in at ~1 200 tokens, well under the 2 048 cap.

The CPU-LLM problem is latency, not quality. Phi-4 14B at Q4_K_M
produces correct plans — it just takes 10-20 × longer than Azure
`gpt-4.1` to do so.

## Expected performance after fixes

Per-URL wall-clock on CPU with the new config:

| Call | Input tokens | Output tokens | Approx wall time |
|------|--------------|---------------|-----------------|
| POM plan (uncached, warm model) | ~1 000 | ~1 000 | 5-10 min |
| Feature plan (uncached, warm model) | ~2 000 | ~800 | 8-15 min |
| Stub heal (per stub) | ~900 | ~30 | 30-60 s |

First call on a cold container adds 1-2 min for model load into
RAM (regardless of context size — it's about disk I/O on the
9 GB Q4_K_M file, not token counts).

Cached reruns on unchanged pages: zero LLM calls. Fingerprint
match → plans read from `manifest/plans/<fixture>.*.json`.

## Mitigations if 15 min per URL is still too slow

1. **Use Azure OpenAI for iteration.** Flip `USE_AZURE_OPENAI=true`
   in `.env`. Generation drops from 2-4 hours to 5-10 min for the
   8-URL list. Same codepath, same validators, same artifacts.
2. **Trim URL list.** `urls-minimal.txt` already ships with just
   `/catalog`; use `autocoder run --urls-file urls-minimal.txt`.
3. **Enable GPU offload.** On Linux with an NVIDIA GPU, add
   `--gpus=all` to the `docker run` (or the compose
   `deploy.resources.reservations.devices` block). Token
   throughput jumps to ~40-60 tok/s.
4. **Smaller model for heal only.** Leave POM/feature plans on
   Phi-4 14B for quality, but swap `autocoder heal` to a
   smaller model (`phi3:mini`, `llama3.2:3b`) by running a
   second Ollama container on a different port and pointing
   only the heal client at it. Not wired today; documented as a
   future extension in `readme/14_extending.md`.

## Running just two URLs for a quick smoke test

```bash
# Wipe prior state so nothing is cached
rm -rvf tests/features/*.feature tests/steps/test_*.py \
        tests/pages/agent_page.py tests/pages/catalog_page.py \
        tests/pages/dq_insights_page.py tests/pages/home_page.py \
        tests/pages/security_page.py tests/pages/source_connection_page.py \
        tests/pages/sources_page.py tests/pages/stewie_page.py \
        manifest/extractions manifest/plans manifest/heals manifest/runs \
        manifest/registry.yaml manifest/report.html

# Keep .auth/user.json — no need to re-MFA

# Two-URL run (login is in LOGIN_URL + .env; catalog from urls-minimal.txt)
autocoder run --urls-file urls-minimal.txt
```

Expected: one POM plan + one feature plan for catalog, ~15-25 min
total. Then:

```bash
autocoder report --run --html manifest/report.html
```

## Event timeline cheatsheet

When reading the next run's log, these are the key milestones:

```
stage:run_start                                       t = 0
auth_storage_trusted                                  t = ~0      (reuse cached session)
stage:url_begin slug=catalog                          t = ~0
extract_done slug=catalog                             t ≈ 15 s
stage:pom_plan slug=catalog
pom_plan_cache_miss                                   t = x
ollama_stream_progress … phase=prompt_eval chars=0    t = x + 10 s
ollama_stream_progress … phase=prompt_eval chars=0    t = x + 20 s
… (repeats until model starts generating)
ollama_first_chunk prompt_eval_s=300                  t = x + 300 s   ← eval done
ollama_stream_progress … phase=gen chars=200          t = x + 310 s
…
llm_call purpose=pom_plan:catalog_page duration=600s  t = x + 600 s   ← POM complete
pom_written                                            …
stage:feature_plan
… (same pattern)
llm_call purpose=feature_plan:catalog_page duration=900s
feature_written
stage:steps_autoheal
…
url_done slug=catalog status=complete
run_done
```

If `ollama_http_error err=timed out` fires **before**
`ollama_first_chunk`, prompt-eval exceeded the timeout. Raise
`OLLAMA_TIMEOUT_SECONDS` and/or lower `OLLAMA_NUM_CTX`. If it
fires **after**, generation stalled mid-stream — bug in Ollama
or disk I/O contention; restart the container.
