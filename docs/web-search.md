# Web research

The browser path uses ordinary signed-in Safari and a named native helper.
Discovery/capture are deterministic; an LLM does not click through every page.

1. Search Google and discover up to 20 distinct candidate URLs.
2. Visit them sequentially in a task-owned window. Extract the currently loaded
   DOM using pinned Mozilla Readability and Bloom's serializer.
3. Capture native image strips, normally 512 pixels wide by up to 2048 pixels
   tall. Native API image detail is `high`; the strip is not the whole page.
4. Give Luna the question, recent public summaries and every captured candidate's
   complete document/native images. Select 3–7 sources and useful visuals.
5. Apply working-evidence budgets and large-result curation before delivering
   oversized selected evidence to Astra/Sol. Keep exact excerpts and source paths.

Each page has **one model-facing document**. Real tables appear inline with
headers, rows and span annotations. Layout tables contribute text once. Useful
anchors use deduplicated references; raw table/link/heading/visual inventories
stay in the saved capture metadata. This rule also applies to Luna, even though
Luna bypasses generic tool-result summarization.

`web_open` captures a specified URL and supports scrolling/wider renders.
`web_fetch` handles ordinary text/data directly and sends HTML through Safari.
`read_file`/`view_image` can return supported images as native API content.
Full source text, images, access failures and provenance remain under
`artifacts/runtime/web/`; SQLite records source lineage. Old captures remain
readable without rewriting their original text or historical prompts.

## Limits

Captures cover loaded content and declared screenshot ranges. They do not claim
unopened accordions, child frames or unloaded content. PDFs are not extracted.
Login/access challenges can fail capture. Search-engine markup can change.
Cross-query freshness-aware capture reuse is not implemented; a fresh open
represents a fresh page version. Reviewing 20 pages can be slow and token-heavy
even after structural duplication is removed.

## Verification

Unit tests exercise model-facing projections and native image transport using
fake providers. The maintained evaluator also has two **zero-inference** modes:

```sh
# A fresh directory is required. This needs retained searches in that workspace.
.venv/bin/python scripts/evaluate_web_representation.py --root "$PWD" --directory artifacts/checks/replay-001 --replay

# Actual Safari/native-helper capture of locally served regression pages.
.venv/bin/python scripts/evaluate_web_representation.py --root "$PWD" --directory artifacts/checks/browser-001 --browser
```

Run the browser check in a GUI login context with the helper and capture grants
already configured. It tests 483 comments inside nested layout tables, genuine
data tables with spans, and content/control links. Captures/reports stay in the
explicit directory; the owned browser window and local HTTP server are closed.

`scripts/evaluate_web_search.py` is a separate opt-in **live model** evaluation.
It can purchase inference and access websites even though Discord is disabled.
Local tokenizer replay measures request construction; it does not establish a
new provider bill, latency or answer-quality result.
