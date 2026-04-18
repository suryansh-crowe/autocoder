# 13 · Token budget and why it stays low

The system targets the lowest token cost per generation that still
produces correct output on Phi-4 14B. Two LLM calls per URL, both
under ~600 tokens of input.

## Per-URL cost (typical)

| Stage          | Lives in                            | Tokens (in / out)        |
|----------------|-------------------------------------|--------------------------|
| 1. Intake      | `autocoder/intake/`                 | 0 / 0 (browser only)     |
| 2. Auth-first  | `autocoder/extract/auth_probe.py`   | 0 / 0                    |
| 3. Extract     | `autocoder/extract/inspector.py`    | 0 / 0                    |
| 4. POM plan    | `autocoder/llm/`                    | ~400 / ~120              |
| 5. POM render  | `autocoder/generate/pom.py`         | 0 / 0                    |
| 6. Feature plan| `autocoder/llm/`                    | ~350 / ~180              |
| 7. Render F+S  | `autocoder/generate/{feature,steps}`| 0 / 0                    |
| 8. Persist     | `autocoder/registry/`               | 0 / 0                    |
| **Per URL**    |                                     | **~750 in / ~300 out**   |

Optional heal stage (one LLM call per stub or per failure):

| Mode                       | Tokens (in / out)    | When                          |
|----------------------------|----------------------|-------------------------------|
| `heal` (stub fill)         | ~250 / ~30           | One per `NotImplementedError` |
| `heal --from-pytest` (failure) | ~400 / ~60–150  | One per failing test          |

Plan + heal caches make a rerun on an unchanged page (or unchanged
failure) cost **0 tokens**.

## What keeps the input small

1. **Compact element catalog.** The inspector caps elements per page
   (default 60), keeps only interactive roles, and emits four fields
   per element (`id`, `kind`, `name?`, `role?`). No DOM dump, no
   accessibility tree.
2. **JSON envelopes, not prose.** User prompts are JSON objects with
   short keys. The compact JSON format strips all whitespace.
3. **No examples.** System prompts state schema + rules, no
   demonstration scenarios. The constraint set carries the model.
4. **No history.** Each call is independent. We never pass prior
   conversations.
5. **No source code.** The model never sees `base_page.py`, the
   templates, or the fixtures — those are deterministic.

## What keeps the output small

1. **JSON action plan, not Python.** A POM plan is `~120` tokens; a
   POM file is ~600. We pay for the plan once and template the file
   forever.
2. **Bounded methods.** System prompt caps POM methods at 20 and
   scenarios at 6. The validator drops anything beyond.
3. **Format mode.** Ollama `format="json"` returns no markdown
   fences, no preamble, no postscript. We never spend output tokens
   on framing characters.

## What rerun avoids

A rerun on an unchanged page:

- re-classifies the URL (browser, 0 tokens),
- re-extracts the page (browser, 0 tokens),
- finds `fingerprint == last_fingerprint`,
- skips stages 4-7 entirely.

A rerun on a page with one new element:

- re-classifies + re-extracts as above,
- finds the fingerprint changed,
- recomputes the POM plan (one cache miss, ~120 tokens out),
- recomputes the feature plan only if you also asked for a new tier
  set (the tier-set is part of the cache key).

## When you would spend more

| Cause                                           | Extra cost                          |
|-------------------------------------------------|-------------------------------------|
| Raise `MAX_ELEMENTS_PER_PAGE`                   | Linear in element count.            |
| Add a tier (`--tier regression`)                | One feature plan call per URL.      |
| `--force` on an unchanged page                  | Two plan calls per URL.             |
| Dense pages with many forms                     | Linear in field count.              |

## Hardware reality check

`info/02_local_llm_recommendation.md` (legacy, still accurate) measured
~2-4 tok/s on a CPU-only Phi-4 14B at Q4_K_M. With ~300 output tokens
per URL, that is ~75-150 seconds of CPU per URL. Caching brings that
to **zero** on reruns of unchanged pages, which is the common case in
day-to-day work.
