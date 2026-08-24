# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script Python tool that scrapes a novel from ranobes.net and saves it as an `.epub`, optionally including reader comments. Everything lives in `main.py` — there is no package structure, no tests, and no build/lint tooling. The script is organized into functions/classes (not one flat top-to-bottom block), but it's still a single file by design.

## Commands

Setup:
```
pip install -r requirements.txt
python -m playwright install chromium   # one-time; downloads the browser main.py drives
```

Run:
```
python main.py
python main.py --help                                    # list every CLI flag
python main.py --url <novel-url> --start 1 --end 20       # skip the matching prompts
python main.py --url <novel-url> --start 1 --end 20 --comments
```
Any flag left unset falls back to an `input()` prompt (novel URL, then start/end chapter, then a comments y/N prompt) — the script is still fully interactive by default, matching its original behavior. At the end, after all chapters are fetched, it opens a native tkinter "Save As" dialog pre-filled with a sensible filename and directory to write the `.epub`.

There is no test suite, linter, or CI config in this repo.

## Architecture

`main.py` is built around a handful of pieces that all funnel through one fetch path, rather than being four independent bolted-on features:

1. **`BrowserSession`** (replaces the old bare module-level `playwright`/`browser`/`page` globals). Owns one persistent Playwright/Chromium session and exposes `fetch_soup(url)`. ranobes.net fronts every page with a Cloudflare Turnstile bot check that a plain HTTP client (`urllib`/`requests`) cannot pass — do not reintroduce raw HTTP calls for fetching site pages. `switch_to_headed(url)` / `switch_to_headless()` swap the entire browser between headless and a visible window while carrying `storage_state()` (cookies) across the swap — used for the anti-flood hand-off below. `safe_page_content()` retries `page.content()` a few times if it's called mid-navigation (e.g. right after a Turnstile challenge auto-redirects on success), which otherwise raises instead of returning content.

2. **`PageCache`**. Caches fetched pages on disk under `.page_cache/` (default; `--cache-dir` to relocate, `--no-cache` to disable), keyed by a SHA-256 hash of the URL. **Only ever written to from `fetch_with_retry`, never from `BrowserSession.fetch_soup` directly** — a page is cached *after* `is_anti_flood_blocked()` has confirmed it's real content, so a CAPTCHA/challenge page can never get cached and served back later as if it were the real page. `set()` self-heals if the cache directory goes missing mid-run (e.g. deleted externally) by recreating it and retrying once, rather than silently going dark for the rest of the process — this was a real bug once (an unrelated `.page_cache` deleted while a run was live silently disabled caching for the remainder of that run).

3. **`fetch_with_retry(session, url, args, *, label, progress_bar=None)`**. The single fetch path used everywhere — novel page, each chapter-index page, each chapter page, and comment-pagination follow-ups. On each call: checks the cache first; on a miss, fetches via `BrowserSession.fetch_soup`; classifies any exception into a human-readable message (`classify_exception` — no internet, DNS failure, connection refused/timed out, or a generic fallback) and retries up to `--retries` times with `--retry-delay` between attempts; if the fetched page is itself a Turnstile/CAPTCHA challenge (`is_anti_flood_blocked`), hands off to `handle_anti_flood` (opens a *visible* browser window reusing the same session's cookies — solving it in an unrelated browser wouldn't clear the Playwright session's block — then resumes) without burning a retry attempt. **Once `--retries` is exhausted, it never auto-skips or auto-aborts** — `prompt_human_intervention` stops and asks a human to retry/skip/abort. `safe_input()` degrades `input()` to `None` instead of crashing if stdin is ever unavailable (e.g. non-interactive invocation), treated as an abort.

4. **Comment extraction** (`extract_comments`, `_extract_comment_list`, `_sanitize_node`, `render_comments_html`, `build_comments_section`; opt-in via `--comments`, off by default). Recursively parses `#dle-comments-list > ol.comments-tree-list` (nested `<li class="comments-tree-item">` / `<ol class="comments-tree-list">` for replies, arbitrary depth) into `CommentNode`s — author, ISO timestamp, reply-to, up/downvotes, and a sanitized body (safe subset only: text, `<br>`, and quote blocks; links/images/other tags are dropped since comment bodies are untrusted user content). Rendered with Kindle-friendly inline styles and a depth-capped indent (`MAX_COMMENT_INDENT_DEPTH`), appended after the chapter text in the same `EpubHtml` item. **Extraction must run on `site_soup` before the span-strip/decompose that follows it** in the main chapter loop — the reply-to marker and online-status spans it depends on live inside `#dle-comments-list`, which that decompose step removes (the decompose itself is unconditional, even when `--comments` is off, so the unscoped `find_all("p")` used for chapter text can never accidentally pick up stray content from inside the comments tree).

5. **CLI / interactive prompts** (`parse_args`, `resolve_interactive`, `resolve_chapter_range`). `argparse`-based, `-h`/`--help` documents every flag. Anything left unset on the command line falls back to the original `input()` prompt — see Commands above.

6. **Assembly + save** (`build_output_filename`, `finalize_and_save`, `offer_partial_save`). Filename is derived from the novel title and the actual chapter numbers present in `chapter_epub` (not `--start`/`--end`, since a partial/aborted run may not match), sanitized for filesystem-invalid characters. The save dialog opens in `--output-dir` (defaults to the project directory) pre-filled with that name — `write_epub` is called on the dialog's full path directly rather than `os.chdir`-ing into the target directory (the old code did this, which had the side effect of permanently changing the process's working directory). If the run stops early — a human chooses "abort" after exhausted retries, gives up on an anti-flood block, or Ctrl+C — `offer_partial_save` is always given a chance to build the `.epub` from whatever chapters were already fetched rather than discarding that work; it only skips the prompt if nothing was fetched yet.

7. **`main()`** ties the above together and wraps the whole run in `try/except RunAborted/except KeyboardInterrupt/finally: session.close()` so the Chromium subprocess is always cleaned up, including on an aborted/interrupted run.

**Retry/failure behavior (changed from the original script):** every fetch goes through `fetch_with_retry`, which caps retries at `--retries` (default 5) with `--retry-delay` (default 15s) between attempts, then always escalates to a human decision (retry/skip/abort) rather than looping forever or silently giving up. `--request-delay` adds an optional pacing delay between successive fetches to proactively avoid triggering anti-flood in the first place.

**Anti-flood / CAPTCHA handling:** `is_anti_flood_blocked()` detects both the JS Turnstile challenge and further-escalated interactive CAPTCHAs by checking for known challenge-page selectors (`.cf-turnstile`, a `challenges.cloudflare.com` iframe, generic captcha selectors) and title/body text markers (`CAPTCHA_MARKERS`). When triggered, `handle_anti_flood` pops a real visible browser window (same session, same cookies) for a human to solve, then resumes automatically. If chapters start failing repeatedly in a burst even after this, that's still a sign to slow down (`--request-delay`, lower `--retries`) rather than retry harder.

**Known gotcha for future edits:** `PageCache` writes are gated on `is_anti_flood_blocked()` returning `False` — never move a cache write earlier in the fetch path (e.g. into `BrowserSession.fetch_soup`) without preserving that gate, or a challenge page can get cached and served back as if it were real content on a later run.

**Unused imports:** `shutil`, `zipfile`, and `pdfkit` are imported in `main.py` and listed in `requirements.txt` but not currently called anywhere in the script (the README mentions optional PDF conversion, but that path isn't implemented).
