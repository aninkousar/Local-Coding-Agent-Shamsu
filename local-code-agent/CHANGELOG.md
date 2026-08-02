# Changelog

All notable changes to Local Code Agent, in order. This reflects the actual build history of
this project rather than dated releases - entries are numbered, not timestamped, since they were
all produced across one continuous development session rather than separate calendar releases.

## 27. Found the real cause of the "tool registration issue" (error-message quality, not schema)
Entry #26 fixed `update_plan`'s schema based on an educated guess, without being able to see the
actual error. The user then shared the model's own text, which revealed the true cause: the model
called `update_plan` with NO `steps` argument at all, got back a raw Python `TypeError` message
from the generic dispatcher, and misread that jargon as evidence the tool was "incorrectly
registered" - then abandoned the tool entirely rather than just retrying with the argument
included. The schema wasn't the problem; the error message was.

Two fixes: (1) `ToolRegistry.execute()`'s generic `TypeError` handler no longer surfaces a raw
Python exception - it now explicitly says the arguments were wrong *this time*, that this is not
a sign of a broken/unavailable tool, and to retry with corrected arguments. This fix is generic,
not `update_plan`-specific - verified it also improves the message for `read_file` called with no
arguments, and for a call using a completely wrong keyword argument name. (2) `update_plan`
specifically now has `steps` default to `None` instead of being a required positional parameter,
so a call with no arguments reaches the function body (with a clear, example-driven corrective
message) instead of raising before ever entering it; it also now tolerates a bare string where a
list was expected, coercing rather than erroring. A new system prompt rule states explicitly:
a tool argument error means retry with corrected arguments, never abandon or route around a tool.
- `agent/tools.py`, `agent/prompts.py`

## 26. Simplified update_plan's schema
A user reported a "tool registration issue" appearing specifically when asking the agent to
implement a plan it had just proposed. `update_plan`'s parameters were a nested array of objects
(each with a `description` string and a `status` enum) - structurally the most complex schema of
any tool in this project, and schema complexity is a documented source of tool-calling
reliability problems on smaller/local models. Without a way to reproduce the exact runtime error,
this was addressed as the most likely and highest-value fix available: simplified `steps` from
`array<{description, status}>` to a flat `array<string>`, with status encoded as a `[x]`/`[~]`/
`[ ]` prefix parsed back into the same internal structure via a new shared `parse_plan_steps()`
helper used by the tool implementation, the CLI panel, and the GUI checklist card alike - so
rendering in both interfaces is unchanged, only what the model has to generate got simpler.
Parsing is tolerant of a missing/malformed prefix (defaults to "pending" rather than erroring),
since a small model won't always get it exactly right. This also directly serves a second,
related request: plan steps are now guided by an explicit rule ("if a step needs the word "and",
split it") toward smaller, more atomic segments, and the model is instructed to update the plan
immediately after each step rather than batching several steps before reporting progress.
- `agent/tools.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `agent/prompts.py`, `README.md`

## 25. Real installation and verification of PHP's toolchain
At the user's request, made a more persistent attempt to get PHP itself installed (the previous
attempt had given up after `apt-get install golang-go`-style 403s; this time, `apt-get update`
first fixed an initial 404 on `php-cli`, after which `php`, `composer`, and `php-codesniffer`
(providing `phpcbf`) all installed cleanly). Composer's own package repository (`repo.packagist.org`)
turned out to be genuinely blocked (unlike the earlier npm/apt 403s, which were transient) - so
`php-cs-fixer` and `phpstan` couldn't be installed via Composer. Worked around this for PHPStan by
downloading `phpstan.phar` directly from its GitHub releases, which is PHPStan's own officially
documented no-Composer installation method, not a workaround. Added `phpcbf` as a real,
apt-installable fallback formatter alongside `php-cs-fixer` in `format_file`.

This let every remaining PHP claim get upgraded from "fake-binary tested" to "verified against
the real tool": real PHPStan caught a genuine `$nmae` typo (valid PHP syntax, so `php -l` lets it
through, exactly mirroring the same bug class already proven for Python/pyflakes, Go/go vet, and
Rust/cargo check), real `phpcbf` correctly reformatted messy PHP to PSR-12 style, and real
`phpunit` correctly reported 2 tests/1 failure with exact assertion details.

**A real bug was found and fixed in the process**: `run_tests`' PHP branch invoked bare `phpunit`
with no target argument. Unlike `pytest`, PHPUnit does not auto-discover tests on its own - it
just prints its help text and does nothing, silently, if given no file/directory to point at.
Fixed by explicitly passing the project directory, and reconfirmed against the same real,
intentionally-mixed-result test suite afterward.
- `agent/tools.py`, `README.md`

## 24. Real installation and verification of prettier/gofmt/rustfmt/clang-format
At the user's request, retried installing the four toolchains previously left untested. The
earlier `npm`/`apt` 403 errors turned out to be inconsistent rather than a hard block - a retry in
this same sandbox succeeded cleanly: `apt-get` installed `clang-format`, `rustc`+`rustfmt`+`cargo`,
and `golang-go` (bundling `gofmt`, `go vet`, `go test`) without issue; `npm install -g prettier`
also succeeded on retry. This let six real formatters get exercised end-to-end through the actual
`format_file` tool (black, sqlparse, prettier, gofmt, rustfmt, clang-format - all six correctly
reformatted deliberately messy code), plus real `go vet` (caught a genuine unused-variable compile
error), real `cargo check` (caught a genuine undefined-variable reference), real `go test` (a
passing suite), and real `cargo test` (1 passed/1 failed with the actual panic backtrace,
mirroring the earlier `pytest` verification). Every one of these previously carried an honest
disclosure that only the routing logic or a fake-binary mock had been verified, not the real tool
- all of those disclosures are now updated to reflect genuine, direct verification. PHP remains
the one exception: unlike Go/Rust/C++, no PHP interpreter is available in this sandbox at all
(not just its formatter/linter), so `php-cs-fixer`/PHPStan/PHPUnit are still only fake-binary
tested, and the README says so plainly rather than implying otherwise.
- `README.md` (no code changes - the tools already existed and worked correctly; this update is
  entirely about closing the gap between "written carefully" and "actually verified")

## 23. Full PHP and SQL treatment
Extended PHP and SQL to the same level of coverage as every other language: PHP now gets a
PHPStan-based deeper "Code check" (level 0, catches undefined variables/functions - real bugs
that still pass `php -l`'s syntax check) alongside `php-cs-fixer` formatting (via a throwaway
temp-file copy, since it edits in place rather than supporting stdin/stdout) and `phpunit` test
detection (via `composer.json`, preferring a project-local `vendor/bin/phpunit`). SQL gets
`sqlparse`-based formatting (pure Python, no external binary) and, more importantly, a
dangerous-pattern scan wired directly into `db_execute`/`db_execute_file`'s permission prompt -
a bare `DELETE`/`UPDATE` with no `WHERE`, `DROP TABLE`/`DATABASE`, or `TRUNCATE` now shows an
explicit warning right where the user approves it, not buried in documentation.

`pip install` continued to work in this sandbox (as discovered in the previous update) - `sqlparse`
was installed and tested for real, including formatting a query and discovering a genuine, minor
quirk: `sqlparse` re-cased a column literally named `role` to `ROLE`, because `ROLE` is a keyword
in some SQL dialects' access-control syntax even though it was just an identifier here. PHP's
entire toolchain remains unavailable in this sandbox, so `php-cs-fixer`/PHPStan/PHPUnit were
verified against small fake executables mimicking each real tool's CLI contract - including the
specific case that matters most: an undefined-variable typo that's syntactically valid PHP (so
`php -l` correctly lets it through) but gets caught by the fake PHPStan check, mirroring exactly
the "beyond syntax" correctness pattern already established for Python/pyflakes and JS/eslint.
The dangerous-SQL-pattern detector itself is pure Python logic with no external dependency, and
was verified against 9 real cases including several that correctly do NOT warn.
- `agent/db_tools.py`, `agent/tools.py`, `agent/permissions.py`, `gui/permissions_gui.py`,
  `agent/prompts.py`, `requirements.txt`, `README.md`

## 22. Auto-formatting and test running
Added `format_file` (black/prettier/gofmt/rustfmt/clang-format, per language - shows a diff and
requires approval like any edit, since formatting still changes a real file) and `run_tests`
(detects and runs pytest/npm test/go test/cargo test - real behavior verification, not another
static check). `pip install` turned out to work in this project's build sandbox even though npm
and apt installs were both blocked (403s on registries nominally in the allowed domain list), so
`black`, `pytest`, and `pyflakes` were all installed for real here - meaning `format_file` was
verified against genuine `black` (correctly reformatted messy code, correctly detected
already-formatted files) and `run_tests` against genuine `pytest` (correctly reported a mixed
pass/fail result with the real traceback). This also let the existing Python "Code check" feature
get re-verified against the real `pyflakes` library for the first time, having previously only
been validated against a faithful mock. `prettier`/`gofmt`/`rustfmt`/`clang-format` remain
untested against their real tools - the routing logic (which command gets chosen for which
project type) was verified with a mocked subprocess call instead.
- `agent/tools.py`, `agent/prompts.py`, `requirements.txt`, `README.md`

## 21. Full-stack integration tools
Added `list_api_routes` (scans the project for backend route definitions and frontend
fetch/axios calls - including template-literal calls - and shows both lists side by side without
auto-diffing them, since path-parameter routes won't string-match exactly) and
`check_local_server` (sends a real HTTP request to verify a running dev server actually responds
correctly - runtime verification, not static analysis). Also added explicit system-prompt
guidance to treat "connect and verify" as its own plan step for multi-layer tasks, use
`list_api_routes` before/after wiring frontend to backend, and prefer same-origin serving over
separate dev servers to sidestep CORS.

**Honesty note**: `check_local_server` is a deliberate, narrow exception to this project's
"only network call is to your local Ollama server" claim, repeated in several places up to this
point. It's hard-refused in code (not just by policy) for anything that isn't localhost/127.0.0.1.
Every place that made the absolute version of that claim - the architecture diagram, the "runs
100% locally" bullet, `ollama_client.py`'s docstring, and the system prompt itself - was updated
to state the real, narrower guarantee instead of quietly becoming inaccurate.
- `agent/tools.py`, `agent/prompts.py`, `agent/ollama_client.py`, `README.md`

## 20. Database support
Added four permission-gated tools: `db_schema`, `db_query` (read-only, rejects writes), `db_execute`
(write/DDL, supports `dry_run`), and `db_execute_file` (multi-statement `.sql` scripts as one
transaction). SQLite works with zero extra install (stdlib `sqlite3`); Postgres/MySQL are optional
via `psycopg2-binary`/`pymysql`. Credentials never pass through the model - for Postgres/MySQL,
the tool argument is an environment variable *name*, not the connection string itself. Also added
a `.sql` syntax check (via a throwaway in-memory SQLite database) to the existing multi-language
correctness-checking system, and a dedicated `request_db_write` permission gate distinct from file
writes, so a session-wide "yes" to one never silently covers the other.

**A real, critical bug was found and fixed during this work**: Python's `sqlite3` module does not
automatically open a transaction before DDL statements (`CREATE`/`DROP`/`ALTER`) the way it does
before `INSERT`/`UPDATE`/`DELETE`. Without an explicit `BEGIN`, calling `.rollback()` after a `DROP
TABLE` was a silent no-op - meaning `dry_run=true` on a destructive schema change would have
executed it for real instead of previewing it. Confirmed with a minimal reproduction, fixed by
issuing an explicit transaction start before every SQLite statement, and re-verified the exact
failing scenario (dry-run `DROP TABLE`) afterward to confirm the data survives.
- New: `agent/db_tools.py`
- Also touched: `agent/tools.py`, `agent/permissions.py`, `gui/permissions_gui.py`,
  `agent/prompts.py`, `requirements.txt`, `README.md`

## 19. Multi-language correctness checking
Extended the Python-only "Code check" (pyflakes) to nine more languages, each using that
ecosystem's own standard tool rather than a hand-rolled checker: JavaScript (`eslint`, optional),
TypeScript (`tsc --noEmit`, project-aware), C/C++ (`gcc`/`g++ -fsyntax-only`), Go (`go vet`), Rust
(`cargo check`, only if a Cargo.toml exists), Java (`javac`, best-effort against external deps),
Ruby (`ruby -c`, syntax only), PHP (`php -l`, syntax only). Every check is detected at runtime and
silently skipped if the tool isn't installed - never a false "OK" claim. Verified for real with
actual gcc/g++/tsc in the build sandbox (caught a real C undeclared-identifier bug, a real C++
`int`→`std::string` type error, and real TypeScript type errors); Go/Rust/Java/Ruby/PHP/eslint
were validated against realistic fake executables mimicking each tool's actual CLI behavior,
since those toolchains weren't installable in the sandbox itself.
- `agent/tools.py`, `agent/prompts.py`, `README.md`

## 18. Deeper code-correctness checking (Code check)
Added a second, deeper check beyond syntax for Python: `pyflakes`-based analysis catching
undefined names, unused imports, and similar real bugs that still *parse* fine (e.g. a typo'd
variable name). Runs on the whole file after every `write_file`/`edit_file`/`scaffold_files` call,
so the model finds out in the same turn instead of the user catching it later. If `pyflakes`
isn't installed, no code-check claim is made - only what was actually checked is reported.
- `agent/tools.py`, `agent/prompts.py`, `requirements.txt`, `pyproject.toml`, `README.md`

## 17. Model swap: Qwen3.5-4B → Qwen3.5-9B
Confirmed Qwen3.5 has no 7B size (family is 0.8B/2B/4B/9B/27B/35B/122B) before making any change.
Switched the default chat model to 9B, trimmed `context_window` 8192→4096 to claw back RAM
headroom, and documented a one-line rollback to 4B if 9B proves too tight on 8GB/no-GPU hardware.
- `config.yaml`, `agent/config.py`, `agent/prompts.py`, `scripts/setup.sh`/`.ps1`, `README.md`

## 16. Plan-first workflow
Added an `update_plan` tool the model is instructed to call before any multi-step task, breaking
work into a numbered, checkable list and updating statuses as it progresses. Rendered as a
distinct bordered checklist panel in the terminal, and as a persistent, live-updating checklist
card in the GUI (not a wall of repeated messages). Skipped automatically for simple one-step
requests.
- `agent/tools.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `gui/static/*`, `agent/prompts.py`, `README.md`

## 15. Code-writing efficiency tools
Added `list_symbols` (instant function/class map via AST/regex, no full read needed), line-range
reads (`read_file(path, start_line, end_line)`), and `read_files` (batch multi-file reads with one
approval). Also introduced the first-generation auto syntax-check after every write/edit (Python
via `ast`, JSON via `json.loads`, JS via `node --check` if Node is installed).
- `agent/tools.py`, `agent/permissions.py`, `gui/permissions_gui.py`, `agent/prompts.py`, `README.md`

## 14. Fixed persistent "unable to reach Ollama" errors
Diagnosed and fixed a real bug: Windows can resolve "localhost" to IPv6 while Ollama listens on
IPv4 only (or vice versa), causing connections to fail 100% of the time. Added automatic
`localhost`/`127.0.0.1` fallback with caching, plus far more actionable error messages (checklist
covering the run-vs-serve distinction, Windows Firewall, and `OLLAMA_HOST`).
- `agent/ollama_client.py`

## 13. GUI polish
Added markdown-lite rendering (bold, inline code, fenced code blocks) to the chat panel, tested
against an XSS-injection string to confirm safe escaping. Moved reindex progress out of the chat
transcript entirely and into the Reindex button itself (live "Indexing N/M" label + stats
tooltip), instead of spamming the conversation log.
- `gui/static/app.js`, `gui/server.py`, `README.md`

## 12. GUI build
Built a full graphical alternative to the terminal: Flask backend, Server-Sent Events for live
streaming, browser-based permission cards (Allow once / Allow session / Deny) instead of typing
y/n, colored diffs, wrapped in a native window via `pywebview` (or a plain browser tab if that's
not installed). Found and fixed two real concurrency bugs along the way: SQLite connections
aren't thread-safe by default (Flask's threaded server needs them to be), and background-thread
exceptions were dying silently instead of surfacing to the UI.
- New: `gui/__init__.py`, `gui/events.py`, `gui/permissions_gui.py`, `gui/agent_loop_gui.py`,
  `gui/server.py`, `gui/launch.py`, `gui/static/index.html`, `gui/static/style.css`,
  `gui/static/app.js`, `launch-agent-gui.bat`/`.ps1`/`.sh`
- Also touched: `agent/permissions.py` & `agent/tools.py` (diff passthrough to whichever
  permission manager is active), `agent/indexer.py` (SQLite thread-safety lock), `pyproject.toml`,
  `requirements.txt`, `scripts/setup.sh`/`.ps1`, `README.md`

## 11. Streaming output + structure-aware chunking
Switched from blocking request/response to streamed tokens (perceived responsiveness, same total
time). Replaced blind fixed-line-window chunking in the codebase index with real Python AST
parsing (chunks by actual function/class boundaries) and a regex heuristic for other languages
(JS/TS, Java, C/C++, Go, Rust, Ruby, PHP, etc.), falling back to fixed-line chunking for anything
that doesn't parse or has no recognizable structure.
- `agent/ollama_client.py`, `agent/tool_loop.py`, `agent/indexer.py`, `README.md`

## 10. Batched embeddings + keep_alive
Switched codebase indexing from one embedding HTTP call per chunk to Ollama's batched `/api/embed`
endpoint (verified: 5 texts went from 5 calls to 3 in testing). Added `keep_alive` to every
request so the model stays loaded between turns instead of potentially unloading and re-paying a
reload cost mid-session.
- `agent/ollama_client.py`, `agent/config.py`, `agent/main.py`, `config.yaml`, `README.md`

## 9. Web-app-specific tools
Added `start_dev_server`/`check_process_output`/`stop_process` for anything that keeps running
(dev servers, watchers) - `run_command` alone would hang forever on `flask run`/`npm start` since
it blocks until the command exits. Added `scaffold_files` (one reviewed batch instead of N
separate approval prompts for a new project's initial files) and `open_in_browser` (stdlib only,
no new dependency).
- `agent/tools.py`, `agent/permissions.py`, `agent/prompts.py`, `agent/main.py`, `README.md`

## 8. Context-engineering explanation
Walkthrough of the efficiency techniques already built into the agent (no code change): rolling
conversation summarization, RAG instead of full-repo context, surgical edit_file vs full rewrites,
truncated tool results, capped context window, native tool-calling vs prompt-parsed ReAct.

## 7. Model quantization confirmation
Confirmed Qwen3.5:4b as pulled via Ollama uses Q4_K_M quantization at ~3.4GB (no code change,
informational).

## 6. Delivered main.py inline
Worked around a file-download issue by pasting `main.py`'s full contents directly into the
conversation for manual save (no code change, delivery method only).

## 5. Multi-line terminal input fix
Fixed the terminal REPL treating every newline in a pasted paragraph as a separate "Enter = send"
submission. Added a `"""`-delimited multi-line input mode: type/paste freely between two `"""`
lines instead of being limited to single-line messages.
- `agent/main.py`

## 4. Fixed .bat parenthesis bug (round 2, complete)
The first fix (#3) missed a second identical bug elsewhere in the same file. Rewrote the whole
script using `goto`/labels instead of `if/else` blocks, eliminating the entire class of
parentheses-inside-a-parenthesized-block parsing errors rather than patching instances one at a
time.
- `launch-agent.bat`

## 3. Fixed .bat parenthesis bug (round 1, incomplete)
Diagnosed cmd.exe's ". was unexpected at this time." error: literal parentheses inside text sitting
inside an `if () else ()` block confuse the batch parser's paren-counting. Fixed the first
instance found.
- `launch-agent.bat`

## 2. Launcher scripts
Added double-click launchers for Windows (cmd + PowerShell) and Mac/Linux that open a dedicated
terminal window, activate the Python environment, and start the agent pointed at a given project
folder - addressing repeated friction with manual venv activation across sessions.
- `launch-agent.bat`, `.ps1`, `.sh` (new)

## 1. Initial build
Built the whole project from the original spec: a fully offline, permission-gated coding agent
modeled on Claude Code, running on Qwen3.5-4B via Ollama on 8GB RAM with no dedicated GPU.
Included: permission system (per-action approval, hard denylist), tool registry (read/write/edit/
list/search/grep/run_command), SQLite + numpy semantic codebase index, rolling conversation
memory, and a terminal REPL entry point.
- Everything: `agent/*.py`, `config.yaml`, `requirements.txt`, `pyproject.toml`, `README.md`,
  `scripts/setup.sh`/`.ps1`

---

**Current configuration**: Qwen3.5-9B (chat + vision) + nomic-embed-text (embeddings), via Ollama.
Two front ends share the same engine: `local-agent` (terminal) and `local-agent-gui` (Flask + GUI
window). Rollback to Qwen3.5-4B is a one-line change in `config.yaml` if 9B proves too tight on
8GB/no-GPU hardware.
