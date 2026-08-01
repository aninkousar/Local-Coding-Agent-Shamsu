# Local Code Agent

A fully offline, permission-gated coding agent shaped like Claude Code, running on a
9-billion-parameter local model - a step up in capability from the original 4B design, at the
cost of a genuinely tight fit on an 8GB-RAM, no-GPU machine. Read the RAM callout below before
you commit to this size.

See [CHANGELOG.md](CHANGELOG.md) for the full build history.

## Read this first: what to actually expect

This uses **Qwen3.5-9B**, still roughly an order of magnitude smaller than the models behind
Claude Code. Concretely, that means:

- **It's genuinely useful for**: everything the 4B version was good at, plus noticeably more
  reliable multi-file work and better judgment on ambiguous requirements - reading a codebase and
  explaining it, focused edits, scaffolding a new module from a spec, writing boilerplate, reading
  a requirements doc or mockup image and turning it into a first draft.
- **It will still struggle with**: large-scale refactors, subtle bugs, and anything that needs a
  lot of context held in its "head" at once - a 9B model is meaningfully better than 4B, not a
  different league.
- **Work in small steps.** Give it one file or one feature at a time. Review every diff. Treat it
  like a fast, tireless junior developer who never gets tired of your corrections - not a
  drop-in replacement for a senior engineer.

**RAM callout - read this if you're on 8GB with no GPU**: Qwen3.5-9B needs roughly 5-7GB just for
the model weights at Q4 quantization, against a commonly recommended minimum of ~12GB total
system RAM. On an 8GB machine this leaves very little headroom for the OS and everything else -
expect it to be noticeably slower than 4B was, and there's a real chance of swapping or
instability, especially with other apps open. `context_window` has already been trimmed to 4096
(from 8192) in `config.yaml` to claw back some of that headroom, but if it's still too rough,
dropping back to 4B is a one-line change:
```yaml
# in config.yaml
chat_model: "qwen3.5:4b"
```
(pull it first if you don't already have it: `ollama pull qwen3.5:4b`). No other files need to
change - everything else reads the model name from this one config value.

Anthropic doesn't make this model or endorse this specific configuration - this is a scaffold
built from your requirements, using open-weight models you download and run yourself.

## What it does

1. **Reads files, documents, and images** and builds from them. `.txt/.md/.json/.yaml` are read
   directly; `.pdf`/`.docx` need one extra `pip install`; images (screenshots, mockups, photos of
   a whiteboard) are passed straight to the model, which is natively multimodal.
2. **Understands existing codebases** via a local semantic index (chunks + embeddings, stored in
   a SQLite file in `.local_agent/` inside your project) so it can find relevant code without
   needing the whole repo in context.
3. **Reads, edits, and explains code.** Edits are shown as unified diffs before anything touches
   disk, and the agent explains what it changed and why after every edit.
4. **Asks permission for everything.** Every file read, file write, and shell command is a
   separate approval prompt. Nothing happens silently. See "Permission model" below.
5. **Runs 100% locally by default.** After the one-time model download, this codebase makes no
   network call except to `localhost:11434` (your own Ollama server) - with two narrow, explicit
   exceptions you control: `check_local_server` (refuses anything that isn't localhost/127.0.0.1)
   and connecting to a remote Postgres/MySQL database if you choose to configure one. No API
   keys, no rate limits, no per-message cost, no usage cap - your only limits are your own
   hardware.

## Architecture

```
your terminal
    │
    ▼
agent/main.py            REPL, startup checks
agent/tool_loop.py        the agent loop (model ↔ tools, native tool-calling via Ollama)
agent/permissions.py      every filesystem/shell action is gated here
agent/tools.py            read_file, edit_file, write_file, run_command, search_codebase, ...
agent/indexer.py          chunk + embed + store codebase in local SQLite (no external vector DB)
agent/memory.py           rolling summarization so long sessions don't blow the context window
agent/doc_reader.py       pdf/docx/text extraction
agent/ollama_client.py    talks to localhost:11434 (your Ollama server)
agent/db_tools.py         sqlite/postgres/mysql - postgres/mysql involve their own network calls
                          to wherever you point them, same as any database client would
    │
    ▼
Ollama (local server)
    │
    ▼
qwen3.5:9b (chat+vision, ~5-7GB RAM at Q4) + nomic-embed-text (~50MB, for codebase search)
```

Two tools are deliberate, scoped exceptions to "local only": `check_local_server` refuses any URL
that isn't localhost/127.0.0.1 (it exists to verify your own dev server actually responds, not to
make general web requests), and connecting to a remote Postgres/MySQL database is obviously a
network call to wherever you configured it - same as any database client. Neither of these talks
to the open internet on your behalf; both require your explicit per-call approval like everything
else in this project.

Why Ollama instead of raw llama.cpp: it packages the GGUF weights, quantization, and the vision
projector together, runs as a plain background service, and needs zero manual compilation on
Windows/Mac/Linux. If you outgrow it, everything here talks to a normal `/api/chat` HTTP
endpoint, so you can point `config.yaml`'s `ollama.host` at any Ollama-compatible server instead.

## Launching from a dedicated terminal window

If activating the venv every time is annoying (or you keep landing in the wrong shell type),
use the launcher for your OS instead of the manual steps above. Each one opens its **own new
terminal window**, activates the environment, moves into your project folder, and starts
`local-agent` - so your original terminal is untouched if anything goes wrong.

**Windows (cmd or double-click):**
```
launch-agent.bat
launch-agent.bat C:\path\to\my-project     REM skip the folder prompt
```

**Windows (PowerShell):**
```powershell
.\launch-agent.ps1
.\launch-agent.ps1 -ProjectPath C:\path\to\my-project
```

**Mac/Linux:**
```bash
./launch-agent.sh
./launch-agent.sh /path/to/my-project
```

All three will tell you clearly if setup hasn't been run yet, instead of failing silently.

## GUI (an app window, like a local Copilot)

There's also a graphical version, for anyone who'd rather not live in a terminal: a chat panel,
clickable Allow/Deny/Session buttons for every permission prompt instead of typing y/n, and
colored diffs shown inline. It's a small local web app (Flask backend + plain HTML/CSS/JS
frontend, no framework, no CDN calls) wrapped in a native window - same underlying agent, same
config.yaml, same permission model, just a different front end.

**Run it:**
```bash
pip install -e .              # already includes Flask
pip install pywebview         # optional - gives a real app window; skipped, it opens in your browser instead
local-agent-gui
```

Or use the launcher for your OS, same pattern as the terminal one:
```
launch-agent-gui.bat C:\path\to\my-project      REM Windows
.\launch-agent-gui.ps1 -ProjectPath C:\path\to\my-project
./launch-agent-gui.sh /path/to/my-project        # Mac/Linux
```

**How permission prompts work here**: instead of a terminal question, you'll see a bordered card
inline in the chat with the file/command/diff shown and three buttons - Allow once, Allow for
this session, Deny. Nothing happens until you click one, exactly like the terminal version's
`y`/`session`/`n`.

**Note on `pywebview`**: without it, `local-agent-gui` still works perfectly - it just opens in
your default browser as a tab instead of its own window. It's a one-line install if you want the
dedicated-window feel; there's no functional difference either way, both talk to the same local
server on `127.0.0.1` and nothing leaves your machine.

## Plan-first workflow for multi-step tasks

For anything more than a single small action, the agent is instructed to call `update_plan`
*before* touching any files - breaking the request into a short numbered list of concrete
segments - then work through them one at a time, updating the plan's statuses as it goes.

- **In the terminal**, this shows as a distinct bordered "Plan" panel with a checklist
  (`✔`/`▶`/`○`), separate from the normal dim tool-call lines.
- **In the GUI**, it's a persistent checklist card that updates *in place* as steps complete,
  rather than a wall of repeated messages.

This exists because a small model drifts more easily on multi-step work than a large one - an
explicit, visible plan gives it (and you) something concrete to check progress against, and lets
you catch a wrong approach after step one instead of after step five. It's skipped automatically
for simple one-step requests, so you won't see a plan for "what does this function do."

**On the wire, a plan is just a flat list of strings** - `["[x] Create the model", "[~] Build the
route", "[ ] Add tests"]` - deliberately not a nested list of objects with separate
description/status fields. Tool-calling reliability on smaller local models is known to degrade
with schema complexity, and a flat string array is about as simple a shape as JSON schema
supports; the `[x]`/`[~]`/`[ ]` prefix is parsed back out into a normal `{description, status}`
structure before anything renders it, so the CLI panel and GUI checklist card look identical
either way. If the model forgets the prefix entirely, that step is just treated as pending rather
than causing an error.

## Connecting frontend, backend, and database

Building each layer separately is the easy part - the tools above already covered that. What
usually breaks a full-stack app is the *wiring* between layers, so two tools exist specifically
for that:

- **`list_api_routes`** - scans the project for backend route definitions (Flask/Express-style)
  and frontend `fetch`/`axios` calls (including template-literal calls like
  `` fetch(`/api/users/${id}`) `` - it captures the static prefix even when the full path can't be
  string-matched), and shows both lists side by side. It does **not** auto-diff or judge matches -
  a route with a path parameter won't string-match a call with a real ID in it even when they're
  the same endpoint - it just puts both lists in front of you so a mismatch is easy to *notice*.
- **`check_local_server`** - sends a real HTTP request to your own running dev server and reports
  the actual status code and response body. This is runtime verification, not static analysis:
  the only way to actually know the frontend and backend are connected is to make them talk to
  each other and see what happens. **Refuses any URL that isn't localhost/127.0.0.1** - it exists
  to verify your own dev server, not to make general web requests, and this is enforced in code,
  not just policy.

The system prompt also nudges the model to make "connect and verify" an explicit last step in its
plan for any multi-layer task, rather than declaring victory once each layer individually looks
right - and to prefer same-origin serving (one server handling both the API and the frontend)
over separate dev servers on different ports, since that sidesteps CORS entirely, which is one
more thing a small model can get subtly wrong.

**Note on the "fully local" claim**: `check_local_server` and connecting to a remote
Postgres/MySQL database (if you choose to configure one) are the two exceptions to "the only
network call is to your local Ollama server" - both require your explicit approval per call, and
`check_local_server` is hard-refused for anything beyond localhost/127.0.0.1 in code, not just by
convention.

## Database support

Four tools, all permission-gated: `db_schema` (list tables/columns), `db_query` (read-only -
rejects anything that isn't SELECT/EXPLAIN/PRAGMA/SHOW), `db_execute` (write/DDL - INSERT,
UPDATE, CREATE, ALTER, DROP), and `db_execute_file` (run a multi-statement `.sql` migration as
one all-or-nothing transaction).

- **SQLite works out of the box** - no extra install, it's Python's stdlib `sqlite3` module. This
  is the default for a reason: no server, no credentials, just a file, which fits a fully local
  offline agent. Postgres and MySQL are supported too, via `pip install psycopg2-binary` /
  `pip install pymysql` respectively.
- **`dry_run=true` runs inside a transaction and rolls back** - useful for previewing exactly what
  a migration or a `DELETE`/`DROP` would do before committing to it for real. This was tested
  specifically against DDL statements (`CREATE`/`DROP`/`ALTER`), because Python's `sqlite3` module
  has a real gotcha here: it does NOT automatically start a transaction before DDL the way it does
  before `INSERT`/`UPDATE`/`DELETE`, so a naive rollback-after-DDL is a silent no-op that would
  have actually executed the destructive statement for real. This is now fixed with an explicit
  transaction start - it isn't a hypothetical concern, it's a bug this project's own testing
  caught and corrected before shipping.
- **Credentials never pass through the model.** For Postgres/MySQL, `db_path` is the *name* of an
  environment variable holding the real connection string, not the string itself - set it before
  starting the agent (`export DATABASE_URL=postgresql://...`) and the model only ever sees the
  variable name, never anything that would end up sitting in conversation history.
- **`.sql` files get a syntax check too** (via a throwaway in-memory SQLite database - pure
  stdlib, a real parser, not a hand-rolled one), on the same write_file/edit_file/scaffold_files
  cycle as every other language. Honest limitation: this only fully validates a *self-contained*
  script - a statement that alters a table not created earlier in the same file will report a
  false "no such table" error, since the check has no way to see your real target database's
  existing schema.

## Auto-formatting and running tests

Two more tools, each deferring to the real standard tool for its language rather than anything
hand-rolled:

- **`format_file`** - `black` (Python), `sqlparse` (SQL, pure Python - no external binary),
  `prettier` (JS/TS/CSS/HTML/JSON/YAML/Markdown, if installed), `gofmt` (Go), `rustfmt` (Rust),
  `clang-format` (C/C++), and for PHP either `php-cs-fixer` (if you have it via Composer) or
  `phpcbf` from PHP_CodeSniffer as a fallback (installable directly via `apt`/your OS package
  manager, no Composer/Packagist needed) - both format a throwaway temp copy and read the result
  back, since neither supports clean stdin/stdout. Shows a diff and requires approval like any
  other edit - formatting still changes a real file, so it goes through the same gate as
  `write_file`/`edit_file`, not a silent auto-fix. If a file's already well-formatted, it says so
  and changes nothing. If no formatter is available for that file type (or the tool isn't
  installed), it says that plainly rather than pretending to have done something.
- **`run_tests`** - detects `pytest`-style Python tests, `package.json` (`npm test`), `go.mod`
  (`go test ./...`), `Cargo.toml` (`cargo test`), or `composer.json` (`phpunit`, preferring a
  project-local `vendor/bin/phpunit` if present, pointed at the project directory since - unlike
  `pytest` - PHPUnit doesn't auto-discover tests with no target argument), and runs whichever
  applies. This is real behavior verification, not another static check - a change can pass every
  syntax/lint/type check built into this project and still be wrong in ways only running the
  actual tests would catch.
- **PHP gets the full treatment**: `php -l` (syntax) plus `PHPStan` at level 0 (undefined
  variables/functions and similar real bugs that still parse fine).
- **SQL gets a dangerous-pattern warning, not just a syntax check.** Before `db_execute` or
  `db_execute_file` actually runs anything, the SQL is scanned for `DELETE`/`UPDATE` with no
  `WHERE` clause, `DROP TABLE`/`DROP DATABASE`, and `TRUNCATE` - and if found, the exact warning
  ("DELETE with no WHERE clause - affects EVERY row") appears right in the approval prompt, not
  buried in documentation. This is a heuristic (a `WHERE` clause hidden in unusual formatting
  could slip past it), so its absence isn't a safety guarantee - `dry_run` still matters
  regardless of whether a warning shows up. Verified against 9 real cases including several that
  should NOT warn (a properly-scoped `DELETE ... WHERE`, a plain `CREATE TABLE`).

**Everything above is now verified against the real tools, including PHP's full toolchain** - a
prior version of this project could only get PHP-related checks working against fake executables,
since no PHP interpreter was installable in the build sandbox at the time; a later, more
persistent attempt got PHP, Composer, `php-codesniffer` (for `phpcbf`), and `phpunit` installed
via `apt` (retrying after a refreshed package index fixed an initial 404), and `phpstan.phar`
downloaded directly from its GitHub releases (this is PHPStan's own officially documented
no-Composer installation method, not a workaround - see
[phpstan.org's getting-started guide](https://phpstan.org/user-guide/getting-started)). Real
PHPStan caught a genuine `$nmae` typo that's valid PHP syntax (so `php -l` correctly lets it
through), real `phpcbf` correctly reformatted messy PHP to PSR-12 style, and real `phpunit`
correctly reported 2 tests/1 failure with the exact assertion details for an intentionally broken
test. **A real bug was found and fixed in the process**: `run_tests`' PHP command was invoking
bare `phpunit` with no target, which - unlike `pytest` - doesn't auto-discover tests and just
prints its help text instead of running anything silently. Fixed by passing the project directory
explicitly, and reconfirmed against the same real test suite afterward.

Every other language mentioned above (`black`, `sqlparse`, `prettier`, `gofmt`, `rustfmt`,
`clang-format`, `go vet`, `cargo check`, `pytest`, `go test`, `cargo test`) was likewise installed
and exercised for real in this project's build sandbox, not merely mocked - each one took
genuinely messy or genuinely buggy code and produced the correct, real-tool output.

**Real, minor quirk found during testing, worth knowing about**: `sqlparse`'s `keyword_case`
option can re-case an identifier that happens to match a keyword in some SQL dialect - e.g. a
column literally named `role` got uppercased to `ROLE` in testing, because `ROLE` is a keyword in
some databases' access-control syntax (`CREATE ROLE`), even though here it was just a column
name. Harmless in practice for case-insensitive identifiers (the vast majority), but worth a
glance at the diff before approving a SQL formatting change.

## Built for writing code efficiently

A few tools and behaviors exist specifically to reduce wasted round-trips and catch mistakes
before you have to:

- **Auto syntax-check after every write_file/edit_file/scaffold_files.** If something's broken,
  the model sees "⚠ Syntax check FAILED: ..." in the *same turn* and is instructed to fix it
  immediately - not wait for you to notice and report it next message.
- **Deeper correctness checking, per language, using each ecosystem's own standard tool - not a
  hand-rolled checker that would risk false positives:**

  | Language | Tool used | What it catches |
  |---|---|---|
  | Python | `pyflakes` | undefined names, unused imports/variables, redefinition |
  | JavaScript | `node --check` (syntax) + `eslint` (if installed) | syntax, plus undefined/unused variables |
  | TypeScript | `tsc --noEmit` (only if `tsconfig.json` exists) | real type errors across the project |
  | C | `gcc -fsyntax-only` | undeclared identifiers, type errors - not just parsing |
  | C++ | `g++ -fsyntax-only` | same, C++-aware (e.g. catches assigning an `int` to a `std::string`) |
  | Go | `go vet` | Go treats unused imports as a compile error, so this catches that plus more |
  | Rust | `cargo check` (only if `Cargo.toml` exists) | full type-checking without producing a binary |
  | Java | `javac` (compiles to a throwaway temp dir) | real compile errors - can false-positive on unresolved external/third-party dependencies |
  | Ruby | `ruby -c` | syntax only - no deeper static analyzer is bundled by default |
  | PHP | `php -l` (syntax) + `PHPStan` level 0 (if installed) | syntax, plus undefined variables/functions |

  **Every single one of these is entirely optional and detected at runtime** - if a given tool
  isn't installed on your machine, that check is silently skipped and nothing is claimed about
  it (no false "OK"). None of this needs internet access beyond whatever one-time install you
  choose to do yourself; `pyflakes` is the only one that's a Python package (already in
  `requirements.txt`) - everything else is a normal system tool (compiler, runtime, etc.) you
  either already have or can install independently of this project.
- **`list_symbols`** - a near-instant function/class map of a file (via the same AST/regex logic
  the codebase index uses), so the model can survey an unfamiliar file's structure before
  deciding whether it's worth reading in full.
- **`read_file` line ranges** - pass `start_line`/`end_line` to read just the relevant section of
  a large file instead of the whole thing.
- **`read_files`** (plural) - read several related files in one batch/one approval, instead of
  separate `read_file` calls and separate prompts for each.

## Built for web app work specifically

A few tools exist mainly because "build me a web app" has needs plain coding doesn't:

- **`start_dev_server` / `check_process_output` / `stop_process`** - `run_command` blocks until
  a command finishes, which means it would hang forever on `flask run` or `npm start`. These
  three give the agent a way to launch something that keeps running, check its logs without
  blocking, and stop it - all still permission-gated like everything else.
- **`scaffold_files`** - creates a whole new project's initial file set (e.g. `index.html`,
  `style.css`, `app.py`) as one reviewed batch instead of N separate approval prompts.
- **`open_in_browser`** - opens a local HTML file or a running dev server's URL in your default
  browser so you can see the actual rendered result immediately (stdlib only, no new dependency).

The system prompt also nudges the model toward plain HTML/CSS/JS or server-rendered templates
(e.g. Flask+Jinja2) by default, rather than framework/bundler-heavy stacks - a model this size is
far more reliable on the simpler stack unless you specifically ask for something else.

## Setup

**Requirements:** Python 3.10+, ~6GB free disk space for models, internet access *only* for this
one-time step.

### Mac / Linux
```bash
git clone <this folder, or just keep it where it is>
cd local-code-agent
chmod +x scripts/setup.sh
./scripts/setup.sh
```

### Windows (PowerShell)
```powershell
cd local-code-agent
.\scripts\setup.ps1
```

### Manual setup (any OS)
```bash
# 1. Install Ollama: https://ollama.com/download
# 2. Pull the two local models (one-time, ~6-7GB total download)
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
# 3. Python deps
pip install -r requirements.txt
# 4. For PDF/Word document reading, optionally:
pip install pypdf python-docx
```

## Usage

Install it once as a command, then run it from inside any project you want help with:

```bash
cd local-code-agent
pip install -e .          # installs the `local-agent` command
cd /path/to/your/project
local-agent
```

The agent always treats its current working directory as the project root - and, per
`config.yaml`'s `allowed_roots`, the only place it's allowed to touch.

Once running:
```
you> reindex
you> explain how the auth flow in this app works
you> here's a screenshot of the UI I want, build it: ./mockup.png
you> refactor the retry logic in utils/http.py to use exponential backoff
```

Type `exit` to quit. Type `reindex` any time you've changed a lot of files outside the agent.

## Permission model

`config.yaml` → `permissions.mode` defaults to `ask`: **every** read, write, and command prompts
you individually, every time, with these choices:

- `y` - allow just this once
- `n` - deny
- `always` - allow this exact file/command for the rest of this session
- `session` - allow this entire category (all reads, or all writes) for the rest of this session

Nothing is remembered between runs - each new session starts back at "ask every time." The agent
also physically cannot touch anything outside `permissions.allowed_roots` (defaults to the
project directory you launched it from), no matter what it's asked to do, and a small hard
denylist blocks catastrophic shell commands (`rm -rf /`, fork bombs, `mkfs`, raw `dd`) outright,
with no override.

## Tuning for your hardware

`config.yaml`:
- `ollama.context_window`: lower (e.g. 4096) if you're tight on RAM or responses feel slow;
  raise if you have more than 8GB and want the agent to see more code at once.
- `ollama.enable_thinking`: `true` turns on the model's step-by-step reasoning mode - better
  answers on harder tasks, noticeably slower on CPU. Off by default for responsiveness.
- `agent.history_soft_limit_tokens`: how much raw conversation to keep before the agent
  summarizes older turns automatically. This is what keeps sessions "unlimited" without the
  context window growing forever on limited RAM.

## Further performance tuning

Beyond what's built in above, these are server-level settings you can layer on top - some are
genuinely worth trying, all are optional:

- **`OLLAMA_KEEP_ALIVE`**: `config.yaml`'s `ollama.keep_alive: "30m"` already tells Ollama to
  keep the model loaded between turns, so it shouldn't reload mid-session. If you still notice a
  reload pause after a long gap (e.g. you stepped away while reviewing a diff), you can also set
  `OLLAMA_KEEP_ALIVE=-1` when starting the Ollama server so it never unloads at all - at the cost
  of it sitting in RAM permanently.
- **`OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`**: roughly halves the memory used by
  the conversation's context cache as sessions get longer. Set both as environment variables
  before starting `ollama serve` (on Windows: System Settings → Environment Variables, then
  restart Ollama). The benefit is most documented on GPU; on CPU-only it's still worth trying,
  but confirm it actually helped rather than assuming - check `ollama show qwen3.5:9b` or the
  server log after setting it.
- **Context caching happens automatically**: Ollama/llama.cpp reuse the already-processed part of
  a conversation (like the system prompt) instead of reprocessing it every turn, as long as
  earlier messages aren't changed - which is why `memory.py` only ever appends or (rarely)
  compacts, never edits history in place.
- **Free up real RAM**: on an 8GB machine, closing your browser/IDE while running the agent
  matters more than any config flag - once you're paging to disk, everything slows down by an
  order of magnitude.
- **Use an SSD, not an HDD**, for wherever `.venv`/Ollama's model store lives - model loading is
  disk-bound the moment RAM is tight.

Two further upgrades now built in:
- **Streaming output**: the agent prints tokens as they generate instead of waiting for the
  full reply - doesn't reduce total time, but the wait feels much shorter.
- **Structure-aware code chunking**: `.py` files are chunked by actual function/class
  boundaries (via Python's `ast` module) instead of blind line windows. Many other languages
  (JS/TS, Java, C/C++, C#, Go, Rust, Ruby, PHP, Swift, Kotlin, Scala) get a lighter-weight
  regex heuristic that looks for lines starting a function/class/method - not a real parser,
  so unusual formatting or patterns like arrow-function assignments can still slip through
  and get folded into the previous chunk, but it's meaningfully better than fixed-line windows
  for typical code. Anything unrecognized (plain text, JSON/YAML/config, or a parse failure)
  falls straight back to the original fixed-line chunker, so nothing breaks.

## Known limitations (be honest with yourself about these)

- No true multi-file transactional edits - each `edit_file`/`write_file` call is its own
  approval. For a change spanning many files, expect many prompts.
- The semantic index chunks Python by real AST structure and other languages by a regex
  heuristic (not a real parser) - good at "find code related to X," but not immune to
  misfiring on unusual code formatting in non-Python files.
- Tool-calling reliability at this size is good but not perfect; if the agent seems to loop or
  stall, it may not have emitted a valid tool call - try rephrasing your request more concretely.
- Every deeper "Code check" depends on a tool being installed on your machine (see the table
  above) - if none of them are, you only get the syntax-level check, or nothing at all for
  languages with no checker wired up (Swift, Kotlin, Scala, C#, and anything else not listed).
  Java's check can false-positive on external/third-party dependencies it can't resolve outside
  the project. None of these are hand-rolled - they all defer to each ecosystem's own standard
  tool, so false positives should be rare, but they're not impossible.
- Scanned/image-only PDFs aren't read as text - export the page as an image and use `read_image`
  instead, since the model can see images directly.
- **GUI-specific**: the chat panel renders bold, inline code, and fenced code blocks, but not
  full markdown (no lists/headers/tables yet); the Flask dev server it runs on is fine for this
  single-user local use case but isn't hardened for exposure beyond `127.0.0.1`, so don't
  port-forward it.
- **Database-specific**: `db_execute_file` splits multi-statement scripts on `;` naively - a
  semicolon inside a string literal or a stored procedure body would misparse. Postgres/MySQL
  support needs their driver installed and couldn't be tested against a real server in this
  project's own development sandbox (no such server was available there) - the SQLite path was
  tested thoroughly and directly caught one real transaction-handling bug before shipping;
  treat the Postgres/MySQL paths as less battle-tested until you've exercised them yourself.
