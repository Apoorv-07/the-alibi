# Interface

The UI answers one question without leaving the page: **"on what evidence did you decide this?"** Every choice
below serves that, including the ones that look like limitations.

There are now **two design systems in this repo, and that is deliberate**:

| | the calm layer | the fluid layer |
|---|---|---|
| pages | the six destinations (`/`, `/calendar`, `/tasks`, `/review`, `/evidence`, `/settings`) + `/onboard`, `/technical` | the 16 technical surfaces (`/claims`, `/queue`, `/feasibility`, `/technical/cockpit`, …) and `/settings` |
| files | `web/static/calm.css`, `web/static/calm.js` | `web/static/alibi.css`, `motion.js`, `atmosphere.js`, `scene.js`, `alibi.js` |
| motion | transitions on state changes, 120–200 ms, nothing looping | WebGL2 atmosphere, inertial scroll, cursor, a live field of facts |
| purpose | read a term, decide, act | inspect the machinery, prove a number |

A page loads one or the other, never both — a design system that has to win a cascade fight against another
design system is not a design system.

## Constraints the interface runs under

1. **No build step, no CDN, no framework — and a page that is *correct* without any of layer 2.**
   Layer 1 is the inline `<style>` block in `web/templates/base.html`: both systems' geometry and colour live
   there, so a page whose stylesheet 404s is still laid out, readable and navigable. Layer 2 adds what is
   worth caching. Nothing depends on layer 2 for the truth: numbers, receipts, filters and forms are all in
   the HTML, which `tests/test_renovation.py::test_a_calm_page_degrades_to_the_inline_layer` pins by looking
   for the inline rules (`html[data-calm]`, `min-height:100vh`, the 940px grid, the reduced-motion guard) and
   for the *absence* of the cinematic hooks.

   **Layer 1 comes first in the document, layer 2 after it.** The inline block used to be emitted *after* the
   `<link>`, at which point every responsive rule in `calm.css` was inert (equal specificity, later source
   wins) and an 834px tablet got `display:none` on its only navigation: a readable page you could not leave.
   A comment in `<head>` had been claiming "the file overrides them" the whole time. Do not restyle this file's
   order without running ``make browser-check``, which measures the bar at four viewports.
2. **It must render on an empty database.** A fresh install is a legitimate state, not an error page — and
   cold, `/` *redirects* to `/onboard`, because "what matters now" has no answer with nothing on file. Pinned
   by `tests/test_api.py::test_every_page_renders_on_an_empty_database` (24 walked pages) and
   `test_renovation.py::test_onboard_is_the_only_way_in_and_the_sample_is_one_click`. The walk list is derived
   from the router by `test_the_walk_list_matches_the_router`, so a route added without a smoke step fails.
3. **Numbers are rendered, never restated, never fetched.** Templates read `Presentation`, which reads
   `twin.db.stats()` and the solver's dict. `/eval` reads `EVAL_REPORT.json` from disk; if the file is missing
   the page says so rather than quoting last run's figures from a string.
4. **Anything the system could not verify is displayed as a question**, with the same visual weight as a fact.
   A greyed-out "unverified" note is a note nobody reads.
5. **Absence is shown as absence.** `0 of 0` is not a statistic, so a cold ledger reads
   *"Nothing has been read yet, so there is nothing to verify — ALIBI does not fill this in."* The task list,
   the calendar and the review queue each have an empty state that names what would fill it (`present.py`'s
   `_trust_words()` is the single source of that sentence, so no template can disagree with it).

## The six destinations

```
Home      /            what matters now: attention → this week → the plan → what changed → why to trust it
Calendar  /calendar    month grid + agenda, only dates a source states; empty days stay empty
Tasks     /tasks         every obligation, its effort (learned or labelled "assumed"), its receipts
Review    /review        the questions only a human can answer, one decision per card
Evidence  /evidence      claim → exact words → offsets → verification, searchable, per-subject trail
Settings  /settings      model routing, retention, the fluid-layer veto, and the door to Advanced
```

Exactly six, pinned by `test_renovation.py::test_the_navigation_is_six_destinations_and_nothing_else`: the
count is the product's shape, and a seventh rail item is how a command centre slides back into a dashboard of
panels. The eleven technical pages did not disappear — they live under `/technical`, grouped, reachable in one
click from the rail and from Settings.

**The budget** the renovation was specified against — 80 % obligations and evidence, 15 % what needs
attention, 5 % system status — is enforced by structure, not by CSS: the attention list is the first section
and is *absent* when empty (`{% if h.attention %}`), and the system status is one sentence with a link,
`status_line()`, which translates `degraded`/`unavailable` into what still works ("No language model is
running, so the deterministic reader did the extraction").

## The language contract

`alibi/present.py` is the **only** module allowed to turn internal state into sentences. Templates call
`Presentation` and render what it returns; they do not re-derive meaning, format dates, or map a `kind` to a
word. `tests/test_renovation.py` holds that line by testing the phrasing functions directly against a real
ledger:

```
title_for("os-ia_2")          → "IA 2"            (short codes upper, real words title, only before a digit;
                                                   a 3+-token title is prose and keeps its own case)
when_phrase(d, today)         → "Was due Fri 28 Aug · 17 days ago" / "No date on file"
value_phrase({"date": …})     → "Thu 30 Oct 2026 (date only, no time)"
option_label(raw_json_label)  → phrased, never `{"date": "2026-10-30", "time": null}`
_plan_words(feas, summary)    → {"state": "infeasible", "headline": …, "fixes": [{"label", "cost", …}]}
_trust_words()                → one sentence, cold-aware, shared by home + cockpit + onboarding
```

Two rules learned the hard way, both now commented in the code:

* **A dict key is part of the template contract.** `Presentation.attention()` returns `cards`, not `items`:
  Jinja's `rv.items` resolves to the *method* before the key, so `{% for it in rv.items %}` dies with
  "builtin_function_or_method is not iterable" on the page that carries the product's whole promise.
* **A card must not pick a date.** When two sources disagree, the card exposes `due=None`, `due_any` (for
  ordering) and `needs_choice`, and reads "Two dates on file — you choose". A tool that silently chooses
  loses the right to be called your twin.

Progressive disclosure is four layers, never more than one open: **headline → what ALIBI thinks → why (a
`<details>` "Why does ALIBI say this?" with sources, claim count, the raw ISO date) → the receipts** (one link
to `/evidence?subject=…`, and one to the JSON at `/api/graph?task=…`). The raw rows are always one click
away; they are never the front page.

## Progressive enhancement, honestly bounded

`calm.js` is ~150 lines and does four things: an Ask suggestion fills the question box; a deep link into a
`<details>` opens it; a chosen review option keeps the form's own submit (it never invents a second path to
the same write); and one POST → one sentence → reload. Every one of them has a no-JS equivalent already in the
HTML, and a form marked `data-silent` (the file upload, whose answer is a whole ingest report) keeps the
browser's behaviour and its own results panel. `alibi.js`/`calm.js` together contain **zero** `innerHTML`,
`insertAdjacentHTML`, `eval` or `document.write` — a query string must never reach a markup parser.

If `calm.js` throws, the page is unaffected: each handler is wrapped and everything is delegated on
`document`. `window.ALIBI` (the RAF engine) is never created on a calm page, and
`verify-fluid` asserts that.

### The reader's veto

`Settings → Reading preferences` writes `localStorage["alibi.fluid.off"] = "1"`, and `motion.js`,
`atmosphere.js` and `scene.js` each bail out at line 1 when they see it — before they allocate a canvas or
subscribe a frame. `prefers-reduced-motion: reduce` does the same without being asked. The button labels
itself from the stored value rather than guessing, and says so plainly if storage is blocked. The calm pages do
not even *create* the canvas, orb or cursor nodes: absent is a stronger promise than hidden.

## Verifying an interface change

```bash
make test                       # 195 tests, incl. tests/test_renovation.py (37, the language contract)
./.venv/bin/python scripts/smoke.py     # 24/24 pages render cold + seeded, no template flags
make browser-check                      # 111 checks in Chromium: both design systems
node .tools/e2e-review.mjs                # and the one irreversible act, driven through the real Review form
node .tools/verify-fluid.mjs --shots    # …and .tools/shot-calm-*.png per viewport
```

`verify-fluid` is the one that catches what unit tests cannot: that the WebGL field painted real pixels, that
the calm pages carry no canvas/cursor/inertia at four viewports, that the mobile bar is one row with ≥44px
targets, that the tap targets and the reading measure stay bounded, and that both sync round trips report in
one sentence. It renders `/?demo=1` on the running server, so start one first (`make dev`).

## Running the browser harness on a bare container

`make browser-check` (i.e. `.tools/verify-fluid.mjs`) drives real Chromium, so it needs two things a Python-only image does not have.

```bash
make browser          # .tools/npm install playwright, then download chromium + the headless shell
make browser-check    # runs the harness with LD_LIBRARY_PATH=$HOME/.local/lib
```

If Chromium exits with `error while loading shared libraries`, the container lacks the usual GL/GTK
libraries (`libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 libxkbcommon0
libxdamage1 libasound2t64 libcups2t64 libgbm1`). With root, `apt-get install -y` those names. Without root —
the exact situation this repo was developed in — resolve the `.deb` URLs from the release index and extract
them into a directory you export:

```bash
python3 - > /tmp/urls.txt <<'PY'
import urllib.request, lzma, re
data = lzma.decompress(urllib.request.urlopen(
    "http://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.xz").read()).decode("utf8", "replace")
want = {"libnspr4","libnss3","libatk1.0-0t64","libatk-bridge2.0-0t64","libatspi2.0-0t64","libxkbcommon0",
        "libxdamage1","libasound2t64","libcups2t64","libgbm1","libdrm2","libxcomposite1","libxcursor1",
        "libxfixes3","libxrandr2","libxi6","libexpat1","libgraphite2-3","libharfbuzz0b","libthai0",
        "libfribidi0","libdatrie1","libpixman-1-0","libdbus-1-3","libudev1","libpango-1.0-0","libcairo2"}
for block in data.split("\n\n"):
    pk = re.search(r"^Package: (\S+)$", block, re.M)
    fn = re.search(r"^Filename: (\S+)$", block, re.M)
    if pk and fn and pk.group(1) in want:
        print("http://deb.debian.org/debian/" + fn.group(1))
PY
mkdir -p /tmp/debs ~/.local/lib
cd /tmp/debs && xargs -n1 -P8 curl -sO < /tmp/urls.txt
for f in *.deb; do ar x "$f" data.tar.xz && tar -xJf data.tar.xz ./usr/lib/x86_64-linux-gnu/ 2>/dev/null; done
cp -a usr/lib/x86_64-linux-gnu/*.so* ~/.local/lib/
export LD_LIBRARY_PATH=$HOME/.local/lib
# proof it worked, before blaming the product:
ldd ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell \
  | grep -c "not found"          # must print 0
```

Two harness rules this file has already earned, both recorded in the code:

* **Release a WebGL page before opening more of them.** Section 1 holds the cockpit — two live contexts and an
  atmosphere loop that never idles. Loading 17 more pages behind it made the *next* cockpit navigation stall
  past its 30 s `page.goto` timeout, which reads exactly like a page bug and is not one. A suite that is green
  on a fast machine and unrunnable on a slow one is not green.
* **A screenshot is diagnostic, never a check.** `page.screenshot()` on a live-GL page can hang in software
  rasterisation, so every capture carries its own timeout and a `.catch()`. The checks themselves read the GL
  layer through `window.__alibiAtmos.sample()` and the DOM, neither of which can hang.
* **Judge a smoothed value at convergence, not after a guessed number of frames.** `--px`/`--py` are springs;
  sampling them two frames after the last pointer event measures the chase. The pointer check now drives the
  pointer to a fixed point and polls until the published value settles there.
