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
agent/context_manager.py  Conversation Manager -> Context Selector -> Prompt Builder (see below)
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

## Bot bridge mode (remote control from Telegram)

A third way to run the agent: `agent/api_server.py`, a FastAPI adapter exposing a REST + WebSocket
API so a companion project, `telegram_agent_bot`, can control this agent remotely from Telegram -
send prompts, watch live progress, browse files, pause/resume/stop - instead of a terminal or a
local browser tab. Same underlying agent, same `config.yaml`, same tool pipeline; a third front end.

**This bridge deliberately reuses `telegram_agent_bot`'s own contract directly** rather than
duplicating it - it imports `bot/models/schemas.py` from that project (which must be on
`PYTHONPATH`, by default a sibling directory) and implements the exact same REST/WS shapes its
bundled `mock_agent/server.py` reference implementation does. Point the bot's `AGENT_API_BASE_URL`
/ `AGENT_WS_URL` at this bridge instead of `mock_agent` and the bot controls the real agent, with
no changes needed on the bot side.

**Run it:**
```bash
./launch-agent-api.sh /path/to/my-project                                # Mac/Linux
launch-agent-api.bat C:\path\to\my-project                               REM Windows
.\launch-agent-api.ps1 -ProjectPath C:\path\to\my-project                 # PowerShell
```
Each accepts a second argument/parameter for `telegram_agent_bot`'s location if it isn't a sibling
directory. Starts on `http://127.0.0.1:8000` - bound to localhost only; there's no auth on this
contract, so don't expose it beyond that without adding some.

### Two decisions made explicitly here, not guessed silently

**Single-project mode.** The real agent runs one session against one fixed codebase per process -
there's no "scaffold an unrelated new project" capability the way the bot's N-project contract
assumes. This bridge represents the one running codebase as a single fixed project, `id="local"`.
A first `POST /prompt` starts it; every later call (a new `POST /prompt`, or a follow-up via
`POST /project/local/prompt`) is just another turn against the *same* session, not a new isolated
codebase - matching how the agent already works in the CLI and GUI. True multi-project (one full
session per repo) would be a materially bigger feature - RAM and queuing assumptions change on
CPU-only hardware - and wasn't built.

**Auto-approve permissions, not full parity.** The default `permission_mode: "ask"` blocks the
agent loop on a human y/n for every file read/write/shell command - the bot's `WSEventType` has no
`PERMISSION_REQUEST` case and no way to answer one today. This bridge uses
`AutoApprovePermissionManager` (`agent/auto_permissions.py`): approved automatically within
`allowed_roots`/`hard_denylist`, with Telegram's own `ALLOWED_USER_IDS` whitelist as the real gate
on who can drive the agent at all, not a per-action prompt. Deliberately *not* "approve
everything" - a path outside `allowed_roots` or a command matching `hard_denylist` is refused just
as firmly as it always was; what's skipped is only the interactive step for what already passes
those checks. Every auto-approved (and denied) action is still logged and visible via
`GET /project/local/logs`, so skipping the prompt doesn't mean skipping visibility.

Full parity (a real `PERMISSION_REQUEST` round-trip over Telegram - a new WS event type, a new
`POST /project/{id}/permission` endpoint, a new bot-side inline keyboard) is a materially bigger
feature and wasn't built. Nothing about `AutoApprovePermissionManager`'s interface would need to
change to add it later - it's a drop-in alternative via the same `permission_manager_factory`
`gui/agent_setup.py`'s `build_agent_session()` already takes.

**pause/resume/stop**: no cooperative cancellation exists in the tool loop, and none was added to
it - `agent/api_server.py` instead wraps this session's own `ToolRegistry.execute`/`execute_batch`
at the instance level, checked before each tool call (not modifying `ToolRegistry` or
`GuiAgentLoop` themselves, both shared with the CLI/GUI entry points). `stop` is best-effort - an
in-flight shell command can't be un-run. `pause`/`resume` block via a polling flag on the
background worker thread, not true mid-tool suspension.

### Real bugs found and fixed during verification

This bridge, `agent/auto_permissions.py`, and `gui/agent_setup.py` (the `init_agent()` refactor
shared between the Flask GUI and this bridge) were largely already built when last picked up here
- but built code doesn't mean verified code, and treating it that way found two real issues:

**A genuine concurrency race, confirmed with a real test, not just inspected.** Neither prompt
endpoint checked whether a turn was already running before starting a new background thread -
`ContextManager` and `ToolRegistry` were never designed for concurrent access (this project's own
long-standing architecture: one CPU, one model, concurrent calls queue rather than parallelize).
Firing two prompts back-to-back against a stubbed-fast LLM call didn't visibly break anything -
the dangerous kind of race, silent rather than a guaranteed crash. Only became reproducible once
the stub's timing was corrected to resemble a real, several-second Ollama call; confirmed
concurrent threads really did run against the same session simultaneously. Fixed with a
`_turn_in_progress()` check (the real thread's `.is_alive()`, not just project status, which could
lag) - a second request while one is running now gets a clear `409 Conflict` instead of silently
racing.

**Two dropped imports in `gui/server.py`** left over from the `agent_setup.py` refactor -
`status()`'s type annotations referenced `OllamaClient`/`ToolRegistry` with no import for either.
Didn't crash (this file uses `from __future__ import annotations`, so annotations are never
evaluated at runtime), but confirmed rather than assumed - and fixed regardless, since dangling
references to undefined names in type hints are a real, if low-severity, code-quality issue -
before it was mistaken for a false alarm because nothing broke today.

### How this was actually tested, not just written

Every claim above was checked against the running server, not just read - including the concurrency
fix, verified by deliberately reconstructing the race window with a realistically-timed stub;
a full turn's real progress tracking, real file writes (verified on real disk, not just an HTTP
response), real permission-manager logging, and real diff previews from an actual `write_file`
call; path-traversal defense on `GET /project/{id}/files`; pause/resume/stop against real project
state, including stopping a turn genuinely in flight; real `WS /events` broadcasts received over an
actual WebSocket connection; and, most rigorously, `telegram_agent_bot`'s own `AgentAPIClient` run
directly against this bridge - so the bot's real Pydantic models, not ad-hoc test assertions,
validated every response shape.

The LLM call itself was stubbed for this (no Ollama available in the verification environment) -
everything else was the real, unmodified agent: real `ToolRegistry`, real `ContextManager`, real
`AutoApprovePermissionManager`, real file I/O against a real git-initialized test project.

## Context management architecture

Every turn's context is assembled from seven separate stores, not one growing pile of chat
history:

```
User
  │
  ▼
Conversation Manager (agent/context_manager.py)
  │
  ├── Recent Chat            - the raw last-few messages
  ├── Long-term Memory       - rolling summary of older turns, once compacted away
  ├── Project Knowledge      - related files, surfaced via the dependency graph, not semantic search
  ├── File Index             - a cheap, cached listing of what files exist and where
  ├── Task History           - the current update_plan state
  ├── Scratchpad             - durable notes the agent writes for itself, now cross-session
  └── Episodic Memory        - a brief log of what happened in past sessions on this project
          │
          ▼
  Context Selector   (select_context()) - decides what's relevant THIS turn
          │
          ▼
  Prompt Builder     (build_prompt())   - pure formatting, no relevance decisions here
          │
          ▼
        LLM
```

**Why split it up at all**: the old approach ("keep appending to one message list, summarize
when it gets too big") treats everything as equally important, when it isn't. File structure and
the current plan are cheap and always worth including. A summary of what happened 20 minutes ago
usually isn't worth much space. And which part of the actual codebase matters changes with every
single message - that's the one store (**Project Knowledge**) that genuinely needs a fresh
per-turn relevance decision rather than blanket inclusion, since a whole codebase can't fit in
context regardless of window size.

**What "Project Knowledge" being auto-selected means in practice**: `set_focus_file()` tracks a
small rolling window of files actually read/edited/written this session, and `_dependency_context()`
walks the dependency graph outward from them (what they import, what imports them), surfacing
related files' content automatically - see the section below for the full story, including a real
tradeoff worth knowing about and a real bug this caught before shipping.

**Task History and Scratchpad are new, purpose-built stores**, not just chat history that
happens to mention the plan: `update_plan`'s state is mirrored into Task History directly (so it
survives even after older chat gets summarized away), and a new `update_scratchpad` tool lets the
model save short, durable facts ("the DB env var is DATABASE_URL", "user wants tabs not spaces")
that persist across the whole session *verbatim* - unlike regular chat, which gets lossily
summarized once it's old enough.

**On context window size**: currently set to 8192 (Ollama's un-trimmed default for this model),
with `history_soft_limit_tokens` at 6000 - both set explicitly, not just left at a default. Since
the new stores (file index, task history, scratchpad, dependency-graph-based project knowledge)
all add real, fixed overhead to every prompt now, on top of raw chat history, this pairing leaves
roughly 2192 tokens of shared headroom for all of that combined - workable, but tighter than it
looks, since that headroom now has five things competing for it instead of just a system prompt.
If a long session ever seems to lose track of something from earlier, `history_soft_limit_tokens`,
`file_index_max_entries`, and `dependency_context_max_files`/`dependency_context_max_chars_per_file`
in `config.yaml` are the levers to pull - either lower the soft limit so compaction kicks in
sooner, or trim how much the other stores contribute per turn. This context_window setting is
also already back to the model's
un-trimmed default, so it carries the full RAM cost the earlier trim was meant to avoid - worth
confirming your machine is genuinely comfortable there, not just that it starts.

## Fixed: orphaned dev server processes left running after the agent exits

A deeper look at Odysseus-Portable's actual source (beyond the README alone - its `src/runtime.js`
does dedicated PID tracking specifically to avoid leaving "zombie processes" behind when the
launcher exits) prompted checking whether this project had the same problem, rather than assuming
it didn't. It did: `start_dev_server`'s `_RunningProcess` tracking was purely an in-memory dict,
and `stop_process` only terminated a process if explicitly called. Nothing cleaned up a still-running
dev server if the agent itself exited first - normal exit, Ctrl+C, or an uncaught exception - which
meant a `npm run dev` (or similar) started by the model could be left running as a genuine orphan,
continuing to hold its port with nothing telling the user it was still alive. A real, common
annoyance for a coding agent that starts dev servers, confirmed rather than assumed.

Fixed with `atexit.register()` in `ToolRegistry.__init__`, calling a new
`_cleanup_processes_on_exit()` that terminates (escalating to kill after a 2-second timeout) any
process still running at shutdown. Best-effort throughout - never raises, since an error here
should never block the interpreter from actually exiting.

Tested at the level that actually matters - the real OS, not just internal state: started a
genuinely long-running process (`sleep 300`), confirmed via `os.kill(pid, 0)` that it was truly
alive at the OS level, ran the cleanup function directly (simulating an exit without an explicit
`stop_process` call), and confirmed via the same OS-level check that the process was genuinely
dead afterward - not just removed from a Python dict. Also confirmed: `atexit` registration
actually happens at construction time; a process that already exited on its own doesn't cause an
error during cleanup; three simultaneous long-running processes are all correctly terminated, none
left behind; and the normal, explicit `stop_process` path is completely unaffected by any of this.

## Proactive hardware-based context_window detection

Compared against a different kind of project entirely - Odysseus-Portable, a portable launcher for
a general-purpose chat web app (llama.cpp-backed, not a coding agent) - most of it wasn't a good
fit for this project's different audience and architecture (portable runtime bundling matters less
for developers who already have Python installed by definition; SQLite-backed memory would trade
away the deliberate human-readability of the current markdown-based scratchpad/plan files; parallel
inference slots hit the same CPU/RAM-multiplication reasoning already declined in the very first
architecture round of this whole project). But one idea connected directly to something already
built, and to real, prior evidence of an actual problem: Odysseus detects hardware upfront and
picks a context size proactively, rather than only recovering from an OOM after it happens.

This project already had the *reactive* half of that (automatic context_window fallback, retrying
at half-size after an actual 500 error - #41). What was missing was the proactive half - and there
was already direct evidence this specific gap had caused a real problem: an earlier session in this
project's own history hit genuine HTTP 500 errors from Ollama, traced to `context_window` being set
larger than the actual hardware could handle. A sensible default from the start could have avoided
that, rather than only recovering from it afterward.

Added dependency-free, cross-platform RAM detection (`/proc/meminfo` on Linux, `sysctl` on macOS,
`GlobalMemoryStatusEx` via `ctypes` on Windows - no new package like `psutil`, consistent with this
project's preference for staying self-contained) and a conservative RAM-to-context_window heuristic,
used **only** when `context_window` is genuinely absent from `config.yaml` - an explicit value
always wins, unconditionally, regardless of what hardware detection would suggest. This required
fixing a real ambiguity in the original property first: `raw.get("context_window", 20000)` couldn't
distinguish "explicitly set to 20000" from "not set at all, fell back to a hardcoded 20000" - both
produced the identical value through the identical code path, which made it impossible to know
when auto-detection should safely apply.

**A real bug hit and fixed during this exact implementation, not glossed over**: an early edit
left `@dataclass` orphaned above the new helper function instead of attached to the `Config` class
itself, breaking the entire module at import time. Caught immediately by actually trying to import
the module, not assumed away.

Tested rigorously: confirmed the single most important property directly - an explicit
`context_window: 20000` is respected even when a mocked hardware detection would suggest 2048, and
even when it would suggest 32768 for a smaller explicit value; confirmed a genuinely absent key
correctly engages auto-detection; confirmed detection failure falls back to a safe hardcoded value
without ever crashing startup; confirmed all six RAM tiers produce sensible, monotonically
increasing values. Real detection was verified against this actual machine, not just mocked -
`_detect_available_ram_gb()` returned 3.61GB, cross-checked directly against `free -m`'s own
"available" figure (3701 MB) and found accurate, not just non-crashing. Ran the full pipeline
through the real `Config.load()` on both a fresh config file with no `context_window` set (correctly
auto-detected and applied the right tier) and this project's actual `config.yaml` (completely
unchanged, the user's own prior explicit choice preserved exactly). Surfaced in both the CLI startup
banner and the GUI's session log via a new `context_window_source` property, so the decision is
visible, not silent.

## Persistent task state: mostly already built, one small nudge added

A "persistent task state" proposal (Goal/Completed/Current/Next/Known issues/Relevant files, kept
tiny) mapped almost entirely onto existing machinery: Completed/Current/Next is exactly what
`[x]`/`[~]`/`[ ]` plan steps already are, "Goal" is what Feature grouping's headers already do, and
persistence across process restarts (not just conversation reconstruction) was already built
several rounds back for exactly this reason.

"Relevant files" and "Known issues" weren't structurally missing - the scratchpad already exists
as a general-purpose, persisted list of durable facts, and could already hold either today. The
actual gap was narrower than it first looked: guidance, not infrastructure. Declined building new
dedicated fields (new `ContextManager` fields, new persistence files, new rendering) for something
the existing scratchpad already covers - more new surface area than the gap justified, same
reasoning that shaped Feature grouping as a light marker inside the existing plan format rather
than a new structure of its own.

Fixed with one line: the system prompt's scratchpad guidance already gave two examples (env var
names, stated preferences) - extended to explicitly include "which files matter for the current
goal" and "known issues you haven't fixed yet," folded into the same existing sentence rather than
a new paragraph. Still a 63% reduction from the original system prompt size.

A related, honestly-flagged but *not* built concern: Task History and Scratchpad currently have no
size enforcement, unlike File Index and Project Knowledge, which the Context Budget Manager
actively trims - nothing stops either from growing unbounded. Left unbuilt on the same grounds as
several other ideas in this series: no confirmed evidence this has actually happened, unlike the
summary-growth bug fixed earlier in this project with real evidence behind it.

## LSP declined for resource conflict; three cheap approximations built instead

A "give it an LSP" proposal (definition/references/type/diagnostics/rename/implementation, across
TypeScript/Python/Java/C/C++/Rust/Go) got a different kind of answer than most rounds: several of
the capabilities are genuinely missing (definition, references, rename, type inference), a
stronger case than most recent evaluations. But the literal proposal was declined for a direct
resource conflict, not a taste preference - a real LSP means spawning and maintaining long-running,
stateful language server processes (pyright, tsserver, rust-analyzer, gopls, clangd, jdtls) with
JSON-RPC protocol handling and continuous document sync, a fundamentally different category of
complexity from every existing checker in this project (all one-shot CLI calls: run, get output,
done). More importantly, several of these servers (tsserver, rust-analyzer especially) are known to
be genuinely memory-hungry on real projects, and this project's entire design has been shaped
around 8GB RAM with no GPU where the model itself already consumes most of that budget - directly
threatening the same constraint `context_window`'s automatic fallback logic (#41) exists to
protect, not a hypothetical concern.

Built instead: three cheap, deterministic, Python-only approximations of the highest-value pieces,
consistent with everything else in this project, with their precision limits stated honestly
rather than oversold.

**Real signatures with type hints** (approximates `type(symbol)`) - `list_symbols` and dependency-
context symbol maps used to replace every function's arguments with a bare `(...)` placeholder.
`extract_python_symbols()` now reconstructs the real signature via `ast.unparse()`, including
parameter/return type annotations and default values where the source actually has them - this is
extracting what's already written, not genuine type inference (which needs an actual type
checker). Tested against six real signature patterns at once - untyped args, typed args with
return types, defaults, keyword-only args, typed and untyped `*args`/`**kwargs` - all six
extracted exactly correctly, including the trickiest one (mixed positional/keyword-only/varargs
with partial typing).

**`find_definition(name)`** (approximates `definition(symbol)`) - a project-wide symbol table
(`build_symbol_table()` in `agent/indexer.py`), built once during `reindex_codebase` alongside the
dependency graph, mutated in place rather than reassigned (same fix as the dependency graph's
earlier stale-reference bug, applied proactively here rather than waiting to rediscover it).
Explicitly honest about its limits in both the tool description and its own docstring: this is a
name-based index, not real semantic resolution - it can't distinguish two unrelated classes
sharing a same-named method, and says so plainly when a name resolves to more than one location
rather than silently picking one. Tested against a deliberately constructed name collision (two
different files each defining a function called `login`) - correctly reports both locations with
an honest disclosure, rather than confidently returning just one; correctly refreshes after a file
is removed and the project is reindexed, with no stale entries left behind.

**`grep_codebase` documented as the references(symbol) approximation** - already existed, just
wasn't connected to this use case explicitly. Deliberately did NOT change its substring-matching
behavior to word-boundary-only, since that would be a real regression for its existing, reasonable
use (matching partial identifiers) - fixed with a clearer description and an honest caveat instead
of a behavior change.

## Error-context enrichment extended: run_command wiring + a real fallback for syntax errors

An "automatic error classification" proposal was evaluated and mostly declined: explicit category
labels ("[TYPE ERROR]", "[TEST FAILURE]") add little when the error text already conveys its own
category clearly - the same reasoning as declining symbol ranking a few rounds earlier. LSP-based
TypeScript type-checking was flagged as a substantial new tool integration (this project currently
only runs `node --check`, syntax only, for JS/TS), not a natural extension - a different, bigger
proposal than what got built. What *was* built: two concrete, well-scoped extensions of the
already-proven two-hop enrichment from the previous round.

**Wired into `run_command`**, not just `run_tests` and the write/edit check paths - if the model
runs a script directly and it crashes, that traceback now gets the same enrichment. Verified
against a real two-file crash (a script importing a helper module, dividing by zero): all three
traceback frames correctly enriched, including the correct dependency symbol map for the module
that actually raised the exception.

**A real fallback for syntax errors**, which the AST-based mechanism structurally cannot handle -
`find_containing_symbol` walks an AST, and a syntax error means the file doesn't parse, so there's
no AST to walk. Previously this meant syntax errors got *zero* automatic context, an inconsistency
with everything else now enriched. Added a plain line-range window (4 lines each side of the
reported line) as a fallback when AST extraction isn't possible.

**A real format-mismatch bug found and fixed while wiring this in**: the syntax-error message says
`"...at line 4: invalid syntax"` - the line number appears mid-sentence, not at the start of a
line the way pyflakes' bulleted output does. The existing regex didn't match this at all; added a
dedicated pattern for the `"at line N"` phrasing specifically.

**A second, more subtle bug found through direct testing, not assumed away**: the fallback
originally assumed "AST extraction failed" always meant "syntax error" - but it also correctly
triggered for a completely ordinary `if __name__ == "__main__":` block, which is valid
module-level code, not a syntax error, just outside what a function-boundary lookup can describe.
Fixed by actually trying to parse the file to distinguish "genuinely broken" from "legitimately
module-level" before choosing the message, rather than mislabeling both the same way. Verified
directly: a real syntax error now says "the file has a syntax error and can't be parsed"; a real
`if __name__` block now says "module-level code, not inside any function/class" - correctly
different messages for two different, easily-conflated situations.

Full regression battery re-run after both extensions: prior round's `run_tests` two-hop
enrichment, zero enrichment noise on a clean write or clean command, and the 3-location cap all
still hold correctly.

## Two-hop error-context enrichment: error → containing function → its dependencies

A "don't give the model the entire repo and entire error log, walk from error to relevant source
instead" proposal, scoped deliberately to two hops after weighing the risk directly: hop one (the
exact function containing the error) is high-confidence and safe, reusing `find_containing_symbol`
(new, AST-based, picks the innermost containing function/method/class - correctly returns a method
rather than its enclosing class when both technically contain the error line). Hop two - "what does
this function call" - was deliberately **not** built as a guessed specific call target, since
resolving that precisely is the same problem the call graph was declined for two rounds ago.
Instead it shows a compact **symbol map** (already built, already tested) of whatever the erroring
file directly depends on, letting the model make the final judgment itself rather than trusting an
automated guess about which call was actually involved.

Wired into both places Python errors actually surface: `run_tests` (only on a nonzero exit code,
so a clean pass never pays enrichment cost) and the pyflakes path of `_format_check_result`
(`write_file`/`edit_file`/`scaffold_files`).

**A real bug found and fixed mid-implementation, not glossed over**: the first version assumed
pyflakes-style output included the filename per line (`file.py:N: message`), matching raw pyflakes
CLI format. It doesn't - this project's own pyflakes wrapper reports `"line N: ..."` with no
filename at all, since there's only ever one file in that context. The regex silently matched
nothing against real output, caught by testing rather than assumed correct. Fixed by adding a
`known_file` parameter - `write_file`/`edit_file`/`scaffold_files` already know which file they
just checked, so that's passed directly instead of trying to re-derive it from text that doesn't
contain it; `run_tests`, where a traceback can span multiple files, keeps the original file:line
parsing since Python tracebacks (`File "auth.py", line 83`) do contain that structure correctly.

Tested against a real, deliberately constructed three-file scenario (a test importing `auth.py`
importing `session.py`) with a genuine failing assertion - not synthetic text: the auto-enrichment
correctly showed both `test_refresh_token`'s exact body and `refresh_token`'s exact body (the two
frames in the real traceback), each paired with a symbol map of what it depends on - `auth.py`'s
functions for the test, and `session.py`'s functions (including `get_session`) for `refresh_token`,
matching the exact "refreshToken → session.ts → getSession" scenario from the original proposal,
achieved safely rather than by guessing the call target. Also confirmed: graceful degradation with
no dependency graph built yet (hop one still valuable alone); zero enrichment noise on a clean
pass or a clean write; and a cap correctly enforced at 3 locations against a file with 10 real
errors, so this enriches rather than bloats.

## run_tests: dropped -v, measured empirically, not assumed

A "compiler as a second brain" proposal's core loop (write → check → error feedback → patch →
test) was already confirmed built in an earlier architecture round: `_format_check_result()` runs
automatically after every write/edit across roughly ten languages, feeding straight back into the
same conversation the model already sees. Test execution is deliberately *not* auto-triggered for
the same reason established then - cheap-and-always-valid gets automated, expensive-and-
context-dependent stays a judgment call. That finding didn't change here.

But the proposal's specific example - "send only the failures to the model, not all 27 results" -
led to actually checking `_tool_run_tests`'s pytest invocation rather than assuming it was fine,
and it wasn't: `-v` was set, meaning every *passing* test got its own line
(`test_login.py::test_valid_login PASSED [ 14%]`) mixed into the output alongside the failures
that actually needed attention.

**Verified with a real pytest run before touching anything**, not just reasoned about: a 7-test
suite (5 passing, 2 failing) with `-v --tb=short` produced 1714 characters; the identical suite
with just `--tb=short` produced 1175 - a 31% reduction, and every single piece of failure
information (test names, full tracebacks, assertion messages, the summary line) was byte-identical
between both. The only thing `-v` was adding was one PASSED line per passing test, which conveys
nothing beyond what the final "N passed" count already says - and this scales *worse* with more
tests, since each pass costs a full line at `-v`, versus one character (a dot) without it. Fixed
by dropping `-v` - one line changed, zero information lost, confirmed through the actual
`run_tests` tool call (not just raw pytest) that failure detail survives completely and the
PASSED noise is gone.

**Deliberately did not build the broader idea** - a universal, cross-runner filter that
post-processes any test framework's output down to just failures. That's the same category of
undertaking as the call graph and symbol ranking questions from recent rounds: real in principle,
but each test runner (cargo, go test, npm test, phpunit) has a different output format, and a
fragile per-framework parser is a worse trade than the one confirmed, measured, zero-risk fix that
was actually verified to work.

## get_symbol + symbol maps replacing truncated content in Project Knowledge

A "symbol map per file, fetch just one function on demand" proposal got checked against the code,
and unlike most recent rounds, it surfaced a real weakness in something *already built* - not just
a missing capability. `_dependency_context()` (the RAG-replacement mechanism) showed related files
as raw content truncated at a fixed character count (500 by default), which could cut a function
off mid-body - exactly the "arbitrary chunking" problem the proposal was critiquing, just not
recognized as a live issue until walking through this comparison. The call-graph-adjacent "Calls"
tracking idea from the proposal was set aside, same reasoning as the call graph declined the round
before - real but meaningfully harder and less precise than the two pieces that got built.

**Extracted a shared `extract_python_symbols()` into `agent/indexer.py`** - the same AST-walking
logic `list_symbols` already had, refactored out so both `list_symbols` and the new
dependency-context behavior share one implementation rather than two that could quietly drift
apart. Verified this refactor changed nothing observable: captured `list_symbols`'s exact output
on a real file *before* refactoring, then confirmed the refactored version produced byte-for-byte
identical output - a true regression-safe extraction, not just "still compiles."

**Added `get_symbol(path, name)`** - Python only, fetches the exact source of one named
function/method/class using AST's `end_lineno` (available since Python 3.8) for precise
boundaries, replacing the previous two-step workaround (`list_symbols` to find a starting line,
then `read_file` with a *guessed* end line, since nothing told the model where a function actually
ends). Includes decorators - a decorated function's true start is the decorator line, not the
`def` line, and this is computed correctly rather than assumed. Handles ambiguity (the same name
appearing more than once in a file) by returning the first match with a clear, explicit note about
where the others are, rather than silently guessing or refusing.

**Switched `_dependency_context()`** to show a symbol map for Python related files instead of
truncated raw content - falls back to the previous truncated-content approach for non-Python files
or Python that fails to parse, so nothing regresses for cases the new path can't handle.

Tested thoroughly: `get_symbol` correctly includes a decorator in the extracted snippet; correctly
detects and explains an ambiguous name (a function and a same-named method in one file) while
still returning a usable result; correctly extracts a class, not just functions; gives a clear
message for a name that doesn't exist or a non-Python file, rather than crashing. The
dependency-context switch was verified to actually solve the problem it targets - a real file's
full structure (a class, three functions including an async one, a module-level variable) shown
in ~200 characters, where the previous truncated approach could have cut off before reaching a
function defined later in the file. Verified end-to-end through the real agent loop too: reading
one file correctly triggered another's symbol map to auto-surface on the very next turn.

## list_symbols: module-level variable extraction

A "code intelligence layer" proposal (Files, Classes, Functions, Variables, Imports, Function
calls, Types, API endpoints, Database tables, Tests, Configuration) got checked item by item
against the actual code, unlike the two immediately preceding architecture questions, which came
back essentially fully covered - this one surfaced several genuine gaps, not zero. Files, Classes,
Functions, Imports, and API endpoints were already built (File Index, `list_symbols`, Dependency
Graph, `list_api_routes`). Database tables was partial (the live database via `db_schema`, but no
tagging of ORM model classes in code as table definitions). Variables, Function calls (a call
graph), Types, and a dedicated Tests view were genuine gaps.

Deliberately NOT all built at once, and not all with the same confidence - flagged explicitly at
the time that a call graph is a fundamentally different, much harder problem than an import graph:
imports are exact (a statement says exactly what it imports), while resolving "does this call site
actually invoke that function" requires reasoning about dynamic dispatch and aliasing that a naive
version would get wrong regularly - the first genuinely imprecise thing this project would have
built, unlike everything else, which has been exact and deterministic. Only the cheap, exact,
low-risk piece was built: module-level variables.

`list_symbols`'s existing AST walk now also extracts module-level assignments - deliberately
iterating `tree.body` directly rather than `ast.walk()`, since the latter would recurse into every
function body and pull in local variables and loop counters as noise, not useful structure. Handles
simple assignments, annotated assignments, and tuple/list unpacking (each unpacked name captured
individually); deliberately excludes `Attribute`/`Subscript` targets (`obj.attr = x`, `d[key] = x`)
since those mutate an existing object rather than define a new one.

Tested against real code covering every case at once: simple and annotated module-level
assignments correctly captured; tuple unpacking (`x, y = 1, 2`) correctly captured both names
individually; a class attribute correctly excluded (not module-level); local variables inside two
different functions correctly excluded; a subscript mutation and an attribute mutation both
correctly excluded. Also confirmed a file with no module-level variables at all omits the section
entirely rather than showing it empty, and confirmed the existing File Memory cache (mtime-based,
from #43) still works correctly with the new section included - a cache hit still avoids
re-parsing.

## Making the LLM stateless - already true, plus one real remaining gap closed

A "make the LLM stateless" proposal got a different kind of answer than most in this series: the
model has been stateless since day one. Ollama's `/api/chat` is fundamentally request/response -
no server-side session, `keep_alive` only keeps the model *weights* loaded for speed, not any
conversational state. Every single call sends the complete message list from scratch, reconstructed
fresh by `build_prompt()`. Everything built throughout this project - `compact()`, the seven-store
context architecture, session logs, `recall_history` - exists precisely because this was already
true; the model was never going to be trusted as the source of truth, because it structurally
can't be one.

But checking the deeper claim - does everything persistent actually *survive a restart* - found
one real, confirmed gap: the scratchpad and episode log both persist to disk correctly, but the
live task plan didn't. A process restart (crash, update, closing the terminal) mid-task would lose
the detailed, in-progress plan, leaving only a one-line episode summary from the *previous*
session's clean shutdown, if there was one.

**Flagged explicitly before building**: this overlaps with #37 (persisted plan file), which was
explicitly rolled back. The two are different in a specific way worth being precise about - #37
tied plan persistence to a hard reset/wipe mechanism for within-session context pressure; this is
purely continuous persistence with **no reset behavior attached at all**, aimed at surviving an
actual process restart, not a mid-session event. Built only after this distinction was made
explicit and confirmed.

`update_task_history()` now persists the plan to `.local_agent/PLAN.md` on every update, and a new
`load_persisted_plan()` restores it once at startup - same pattern as scratchpad/episode-log
persistence. One implementation detail mattered: the persisted format is deliberately **not**
`format_plan_text()`'s output (which bakes in step numbers and indentation for display) - a raw,
round-trip-safe serialization was used instead, since feeding numbered/indented text back through
the parser would have corrupted descriptions with leftover step numbers.

Tested precisely: a flat plan round-trips through a simulated restart with byte-identical
structure; a feature-grouped plan round-trips correctly with zero step-number corruption in
descriptions (the exact bug a naive approach would hit); no persisted file yet is a graceful
no-op; a plan intentionally cleared to empty correctly stays empty on the next load rather than
resurrecting as a phantom; no persist path configured at all doesn't crash anything. Verified
fully end-to-end too: a real `update_plan` tool call through the actual agent loop, a simulated
crash, a fresh session loading the plan at startup, and confirmed it correctly appears in the new
session's very first prompt.

## Prompt Compiler: condensed context-block labels, ~137→~16 tokens per turn

A "Prompt Compiler" proposal - compile structured state tersely instead of assembling it with
prose - turned out to have real, measured teeth once checked against the actual code. `build_prompt()`
was wrapping every context block in a full explanatory sentence, every single turn: *"Code
structurally related to the file(s) you've been working with (via imports, auto-surfaced from the
dependency graph, not a semantic search) - still use search_codebase for anything by meaning/topic,
or read_file for anything this didn't surface:"* before the actual related-code content, repeated on
every turn that had any related files. Every one of these six explanations was already redundant
with something the persistent system prompt already says once - *"Your context already includes,
automatically: a file index, your current plan, your scratchpad notes..."* This is exactly the
same mistake already caught and fixed once before (#33, condensing the system prompt itself) -
just not yet applied to this part of the pipeline, and arguably a *bigger* miss, since the system
prompt is fixed overhead paid once per prompt assembly while these labels were being explained in
full on every turn that included them.

Condensed to short, structured labels only (`FILES:`, `RELATED:`, `PLAN:`, `NOTES:`,
`PAST SESSIONS:`, `SUMMARY:`) - safe specifically because the fuller explanation of what each
store means and how to use it already lives once in the persistent system prompt, immediately
above these blocks in every single prompt. Nothing about *what* information is included changed,
only how tersely it's labeled. Measured precisely: 551→65 characters of pure label overhead when
all six blocks are present in one turn - an 88% reduction, ~137→~16 tokens, saved on every turn
that has this content, not a one-time cost.

Tested directly: confirmed every actual piece of content (the plan text, scratchpad notes, file
list, related code, episode log, summary) survives completely intact with only the wrapper labels
shortened - nothing was cut, only re-labeled; confirmed the Context Budget Manager's truncation
logic still correctly trims oversized blocks with the new labels in place.

## search_codebase: filtering weak matches, deterministically not learned

A question about "Learning Retrieval Quality" got evaluated against the same fundamentals as
Context Prediction Network earlier in this series: there's no real feedback signal anywhere in
this system (no click-through data, no rating, nothing beyond noisy inference), and the scale -
one user, one project - is far too small to learn anything statistically reliable from. Declined
for the same reasons.

But it pointed at something real: `search_codebase` (the one remaining embedding-based semantic
tool, since the automatic per-turn mechanism was replaced by the dependency graph) returned its
`top_k` results with no filtering at all - if nothing in the codebase was actually relevant to a
query, it still confidently handed back "the top 6" regardless of how weak even the best match
was. **Correction worth being upfront about**: the evaluation's original claim that scores weren't
shown was wrong - they always were. The actual, narrower gap was filtering, not visibility.

Fixed deterministically: results below `search_codebase_min_score` (default 0.3) are now excluded
entirely, and if *nothing* clears the bar, that's stated plainly - including the actual best
score, so the model can judge for itself - along with a concrete suggestion (`grep_codebase` for
an exact identifier) rather than silently handing back irrelevant results dressed up as "the top
matches." **Also fixed while touching this tool**: its own description still referenced "auto-
retrieved snippets" from the semantic-RAG mechanism that was replaced several updates ago - a
stale leftover from that migration, found and corrected here.

Tested precisely: mixed strong/weak results correctly keep only the strong ones, with a clear
count of how many were omitted and why; a query where nothing meets the threshold gets a clear,
actionable message instead of confusing near-empty output; results that are all strong show no
filtering noise at all; the pre-existing "no results whatsoever" case (a fresh, unindexed project)
is completely unaffected; and the configured default applies correctly when not explicitly set.

## Differential Memory: read_file shows changes, not the whole file again

A question about "Differential Memory" got a stronger case than most of the recent architecture
questions in this series, for a specific reason: it directly extends a pattern already observed
causing real problems earlier in this project (a PRD document getting re-read repeatedly, fixed
at the time with pagination and read deduplication), and it reuses infrastructure that already
exists and is already proven - `agent/diffs.py`'s `make_unified_diff()`, the same function that
powers the write/edit approval flow - rather than requiring anything new and risky.

**The gap this closes**: deduplication already handled *identical* repeated reads well, but did
nothing for the more common real case - read a file, edit part of it, read it again later (to
double-check, or because context got compacted and the model wants to refresh). Previously this
showed the full file content again, even if only a few lines changed.

**What changed**: `read_file`, on a full-file read (not a `start_line`/`end_line` slice), now
checks whether this exact file was already read in full earlier this session. If it was and
nothing changed, a short confirmation replaces the full content. If it was and the file *has*
changed, a unified diff against what was last shown replaces the full content - more compact and
more informative, since the change is usually what's actually relevant. A new `full=true`
parameter is the escape hatch when complete current content is genuinely needed regardless.
First-time reads of any file are completely unaffected - full content, exactly as always. Uses
the same bounded LRU/LFU hybrid cache built for `_list_symbols_cache`'s fix, for the same
no-unbounded-growth reason.

Tested against a real file, all four core paths: first read (unchanged, full content as always);
second read with no changes (short confirmation, not the full file); a real edit followed by a
read (a genuine unified diff, confirmed the new content actually appears in it); and `full=true`
forcing complete content regardless. Also confirmed partial reads (`start_line`/`end_line`) are
completely isolated from this mechanism in both directions - they never trigger it, and they
never corrupt the cache for a later full read. Verified end-to-end through the real agent loop,
not just direct tool calls: read a file, edited it via `write_file`, read it again - confirmed the
model's second `read_file` result was a diff showing exactly the new function that got added, not
the whole file repeated.

## Fixed: _list_symbols_cache's unbounded growth, with a real LRU/LFU hybrid

A follow-up to evaluating whether Linux-style LRU/LFU page replacement would help: most of where
it could apply either already existed (`focus_files_max` is already simple LRU, just too small a
window for a hybrid to meaningfully pay off) or was a poor fit (`compact()` preserves importance
through summarization, not cache eviction). But `_list_symbols_cache` - added when File Memory
caching was built - turned out to be a genuinely unbounded plain dict, growing for an entire
session with no cap at all, the same category of bug as an earlier unbounded-summary-growth fix.

Added `_BoundedHybridCache`, modeled directly on how Linux's actual page reclaim works: entries
start on an "inactive" list; a second reference promotes them to "active", which is protected from
eviction unless it grows past its own share of total capacity (in which case the oldest active
entries get demoted back to inactive, not dropped). Eviction always comes from inactive first,
oldest entry first. The point, same as real LRU/LFU hybrids: a file referenced only once, long
ago, gets evicted before anything else - but a file referenced repeatedly throughout a long
session stays cached even if it hasn't been touched recently, which plain LRU would evict just as
readily as a one-off reference.

Tested precisely: confirmed the core hybrid property directly - an entry referenced twice survives
an eviction that a single-referenced, older entry does not, something plain LRU would get wrong
(it would evict the twice-referenced entry, since recency alone doesn't distinguish it); confirmed
the active list itself stays capped, correctly demoting overflow rather than growing unbounded;
confirmed total cache size never exceeds its configured max across 50 sustained insertions,
regardless of access pattern - the original bug, genuinely fixed, not just capped in theory.
Verified through the real `list_symbols` tool too, not just the cache class in isolation: cache
hits still correctly avoid re-parsing, mtime-based invalidation still correctly triggers a
re-parse when a file actually changes, and reindex still correctly clears the whole cache - all
identical to before, on top of the new, genuine size bound.

## Optional Feature grouping in plans (a lighter Hierarchical Planning)

A proposal for a strict, mandatory 6-level hierarchy (Project → Feature → Module → File →
Function → Line) got evaluated the same way as every other architecture question here: four of
the six levels already had a corresponding mechanism (Project=File Index, Module/File=Dependency
Graph+`list_symbols`, Function=`list_symbols`'s line numbers, Line=`read_file`'s line ranges), and
"never retrieve the whole project" was already the design philosophy, not a gap - nothing in this
system has ever dumped full project content into context. The one genuine gap was **Feature**: an
explicit layer between "the whole goal" and "each atomic step," which `update_plan`'s flat step
list didn't have.

The literal proposal - a *mandatory*, *strict* 6-level hierarchy every task must formally pass
through - was declined. This project already has direct evidence of what happens when a 9B model
gets handed too much imposed structure: the system prompt once grew complex enough that the model
started literally citing its own rule numbers back to the user (fixed in #33 by condensing it).
Forcing a one-line fix through six formal levels risks the same failure mode for no proportional
benefit.

**What got built instead**: optional Feature grouping, added to `update_plan` with **zero schema
changes** - a line starting with `## Feature Name` in the same flat string array groups the steps
that follow until the next `##` line. A simple plan with no `##` lines looks and behaves exactly
as it did before this existed; a genuinely multi-part task can organize itself, entirely at the
model's discretion. `parse_plan_steps()` gained a `"feature"` key (defaulting to `None`), and a
new shared `format_plan_text()` function keeps the tool's own result text, what the model sees in
context, and the CLI panel all rendering identically. The GUI's plan card got the same grouping
with a small header and indentation.

Tested precisely: confirmed a flat plan (no `##` lines) produces text byte-for-byte identical to
before this was added - true backward compatibility, not just "doesn't crash"; confirmed a
grouped plan parses each step with the correct feature tag and renders with correct *global* step
numbering across features (not restarting at 1 per group); and confirmed all three rendering
surfaces (tool result, model-visible context, CLI panel) produce consistent output for the same
grouped plan, end-to-end through the real `ToolRegistry`. One sentence added to the system prompt,
folded into the existing planning paragraph - still a 64% reduction from the original prompt size.

## Real token-cost observability (Token Cost Optimizer, evaluated honestly)

A question about whether a "Token Cost Optimizer" would help got an unusually direct answer for
this series: mapped against everything already built (system prompt condensing, the Context
Budget Manager, deduplication, Adaptive Compression, the RAG-to-dependency-graph replacement,
`recall_history`), the *optimization* work was already substantially done. What was missing was
different in kind - not another optimization mechanism, but **measurement**. Every budget decision
in this project relies on `chars // 4`, a rough estimate that's never been checked against reality,
and there was no way to see what a turn's context actually cost without manually instrumenting the
code - which is exactly what this whole build has done repeatedly to test things.

Fixed for free: **Ollama's own API response already includes real token counts**
(`prompt_eval_count`, `eval_count`) on every call - this project just wasn't reading them.
`OllamaClient` now captures both as `last_prompt_tokens`/`last_completion_tokens` after every
`chat()`/`chat_stream()` call, and `chat_stream()`'s `done` event carries them too. `SessionLogger`
gained a `token_usage()` method, called after every model call in both loops, so the session log
now shows the *actual* cost of each call, not an estimate.

Tested end-to-end with real simulated Ollama responses (both the streaming and non-streaming
paths correctly extract the fields), and through the full agent loop: a turn involving one tool
call showed real prompt cost growing from 1200 to 1450 tokens between the first model call and
the second - the actual, measured effect of that tool result being added to context, visible in
the session log for the first time rather than only inferable from code.

**Scope, stated plainly**: this is observability, not a new optimization. The real counts aren't
(yet) fed back into recalibrating the char-based budget heuristics used elsewhere - they're
logged for review. Using them to make the existing estimates more accurate would be a reasonable
next step if the discrepancy turns out to matter in practice, but wasn't built here.

## Adaptive Compression - fixing a known gap, and smoothing a sharp cliff

Two pieces, following directly from evaluating whether "Adaptive Compression" would help (the
same evaluate-before-building pattern as every other architecture question in this project).

**Part 1 closes a real, previously-known gap.** While building the hard-reset mechanism (#37,
later rolled back at the user's request), a real finding surfaced: `compact()` concatenates each
new summary onto the old one with no cap, so Long-term Memory could grow unboundedly across a
long session with many compactions. #37's fix was a full context wipe - too blunt, and correctly
rejected. This is the surgical version: `summary_max_chars` (default 1500) - once the summary
crosses it after a compaction, `_recompress_summary()` condenses it into itself via one more
(infrequent) model call, keeping Long-term Memory bounded without touching anything else. Tested
directly: repeated compactions across 5 rounds correctly kept the summary bounded the whole time
(re-compressing every round it crossed the cap), and confirmed no extra model call happens at all
when the summary stays comfortably under the limit.

**Part 2 smooths what used to be a single sharp cliff.** Previously, a message either had full
detail (within `compact()`'s `keep_recent`) or none (once compacted away) - nothing in between.
`_apply_graduated_compression()` adds a middle tier specifically for tool-role messages (usually
the largest individual messages - a big file read, a large search result): full detail for the
most recent `full_detail_recent_count` (default 4), a moderate cap for the next
`light_compression_recent_count` (default 10, capped at `light_compression_max_chars`), and a
more aggressive cap beyond that (`heavy_compression_max_chars`) for anything older still in the
active window but not yet compacted away. Pure deterministic truncation, zero extra model calls -
same architectural pattern as the existing read-deduplication (transforms what's assembled for
this turn, never mutates `self.messages` itself). Tested precisely: verified all three tiers
produce exactly the expected lengths with controlled test data, confirmed user/assistant messages
are never touched (only tool results), and confirmed it end-to-end through the real agent loop -
the oldest of several tool results correctly shrank tier by tier as newer ones arrived.

## Context as virtual memory - and the one real gap that closed

A question came up about whether an explicit "Context Virtual Memory" layer would help, framed
around the OS analogy: bounded physical RAM, a larger backing store, paging content in and out
based on what's actually in use. Worth stating plainly: **most of that already existed here**,
just under different names, for the same underlying reason - a bounded context budget forces the
same tradeoffs bounded RAM does.

| Virtual memory concept | What's already here |
|---|---|
| Physical RAM | `context_window` |
| Page-out (evict, compress) | `compact()` - old messages summarized and evicted from active memory |
| The swap disk (nothing truly lost) | The session log - every message and tool result, in full |
| Page-in (bring back what's needed) | `_dependency_context()`, `load_episodic_memory()` |
| Page table (what exists, without loading it) | File Index + Dependency Graph |
| LRU eviction | `focus_files_max` - the rolling anchor-file window |

**The one genuine gap**: page-out had no page-back-in for chat history specifically. Once
`compact()` summarizes old messages away, that detail was recoverable only if the model happened
to think to read the session log file directly - nothing told it to, or helped it search
efficiently rather than reading the whole thing blind.

**Closed with a new `recall_history` tool**: searches the current session's log for a
keyword/phrase and returns matching entries (full blank-line-delimited blocks, not bare grep
lines, so each result carries its own natural context) rather than just the single matching
sentence. Never prompts for permission - it's the agent's own already-exchanged conversation
record, not new file content - so it's in the always-parallel-safe tool set alongside
`search_codebase`/`grep_codebase`.

Tested against a real session log, not just designed and assumed: a bcrypt-vs-md5 password
decision correctly returned both the user's message and the agent's reply together as one
coherent match; a genuinely unmentioned topic correctly returned "no mention found"; a
never-yet-created log correctly failed gracefully rather than erroring; and a case with more
matches than the display cap (5) correctly showed the most recent ones with a clear note that
more exist. One short sentence was added to the system prompt (folded into an existing closing
sentence, not a new paragraph) - still a 66% reduction from the original prompt size.

## Project Knowledge: replaced semantic RAG with dependency-graph traversal

At the user's explicit request, the automatic "Project Knowledge" retrieval (what used to be
called auto-RAG throughout this README) no longer uses embeddings or semantic search at all. It's
now purely dependency-graph-based: `ContextManager.set_focus_file()` tracks a small rolling window
of files actually read/edited/written this session (the "focus files"), and
`_dependency_context()` walks the dependency graph outward from them - what they import, what
imports them - surfacing each related file's actual content automatically, with zero embedding
model calls.

**The tradeoff, stated plainly rather than glossed over**, since it's real: semantic search could
surface relevant code for a brand-new, open-ended question before any file had been touched -
"how do I calculate the total price" matching a function by *meaning* alone, with no file named.
A dependency graph has nothing to offer in that situation - it needs an anchor, a file already in
view, before it can traverse anywhere. This is a deliberate limitation of the replacement, not an
oversight, and `_dependency_context()` returns `None` honestly rather than guessing when no anchor
exists yet. The system prompt says so directly, pointing at `search_codebase` (the explicit,
on-demand semantic tool - unaffected by this change, still available whenever the model wants a
real search) for anything by topic rather than file.

**A real bug caught by testing before it shipped**: the first version had the relation labels
backwards - a file that `routes.py` imports was labeled "imports routes.py" instead of "imported
by routes.py" (the two directions swapped). Traced to confusing `dependency_forward[focus]` (files
*focus* imports, so each one *is imported by* focus) with `dependency_reverse[focus]` (files that
import *focus*, so each one *imports* focus) - fixed, and re-verified both directions produce the
correct label afterward.

Tested at every level: core dependency-graph traversal against real files with real import
relationships (correct in both directions after the fix); the "no anchor yet" case correctly
returns `None`; the rolling focus-file window caps correctly and re-touching an older file moves
it to front without duplicating; a long related file truncates correctly with a clear note; and a
full loop-level test - read one file in turn 1, confirmed its dependency automatically appeared in
turn 2's context with the correct relation label, with no explicit tool call needed for it to
show up.

`CodebaseIndex`/embeddings remain fully in place and unaffected for `search_codebase` (the
explicit, on-demand tool) - only the *automatic* per-turn injection changed. The now-unused
`index`/`auto_rag_top_k`/`auto_rag_min_score` fields and config were removed from
`ContextManager`/`config.yaml` entirely, replaced by `project_root`, `dependency_forward`,
`focus_files_max`, `dependency_context_max_files`, and `dependency_context_max_chars_per_file`.

## A second architecture proposal, evaluated the same way as the first

Another diagram was proposed (Intent Detection → Task Planner → Working Memory → Context Budget
Manager → {Episodic DB, Semantic DB, File Memory} → Dependency Graph + AST → Progressive Context
Retrieval → Multi-Resolution Context → Prompt Compiler → Qwen3.5-9B → Reflection & Memory Writer).
Most of it maps onto what already existed under different names (Task Planner=`update_plan`,
Episodic DB=episodic memory, Semantic DB=Project Knowledge, File Memory=File Index, Dependency
Graph + AST=already built, Prompt Compiler=`build_prompt()`). Two pieces were argued against for
the same reason as the first proposal's Intent Classifier/Reflection step: a separate **Intent
Detection** model call and a separate **Reflection** LLM self-critique call both cost a real extra
inference round-trip on CPU-only hardware, for something already handled - the system prompt
handles "should I plan or just answer" inline for free, and this project's real deterministic
tools (pyflakes, `run_tests`) are more reliable verification than the same small model critiquing
its own output, at zero extra latency.

Four pieces were genuinely new and got built:

**Context Budget Manager** - task history, scratchpad, and episodic memory are small and
high-priority, so they're always included in full. File Index and Project Knowledge are the
flexible stores: rather than each having its own fixed cap regardless of what else is using
space (the previous design), they now split whatever's left of one shared budget
(`context_overhead_budget_tokens`, default 2000), shrinking proportionally if the high-priority
stores are unexpectedly large this turn. Tested directly: confirmed File Index gets meaningfully
less room (503→329 chars in one test) when task history/scratchpad grow larger.

**Confidence-adaptive retrieval breadth and multi-resolution context - superseded, not current.**
Both of these were built on top of the semantic-RAG `_auto_rag()` method described just above -
similarity-score-based chunk-count tiering, and a "(depended on by: ...)" annotation bolted onto
RAG-retrieved chunks. That method was removed entirely and replaced by `_dependency_context()` (see
the dedicated section further down) - there are no similarity scores left to tier by, and the
"what depends on this" idea is now the *core* mechanism itself (the relation label on every
surfaced file) rather than an add-on annotation. Left here as an honest record of what was built
and superseded, not deleted outright, matching how this changelog handles anything rolled back or
replaced elsewhere.

**File Memory caching** - `list_symbols` results are now cached by `(path, mtime)`, so calling it
twice on an unchanged file skips re-parsing entirely. Verified by instrumenting `ast.parse` itself
to count real calls: a cache hit produces zero re-parses; modifying the file correctly invalidates
the cache and re-parses, picking up the new content.

**A real bug found and fixed while wiring this together**: `_tool_reindex_codebase` was
*reassigning* the dependency-graph dictionaries on every reindex rather than mutating them in
place. Since `ContextManager` holds a reference to these same dict objects (for multi-resolution
context), reassignment would have made it permanently see a stale, empty dependency graph after
the very first reindex in any real session - a genuine, silent bug that testing caught before it
ever shipped. Fixed to clear-and-update in place instead, and verified the exact before/after
behavior directly: constructed a `ContextManager` before any reindex, ran a real reindex
afterward, and confirmed it automatically saw the update with no separate refresh call needed.

## Parallel tool execution (real, not the same kind as Claude's subagents)

Worth being precise about what this is and isn't, since it was explicitly explored and mostly
ruled out first: this project cannot parallelize *model calls* the way Claude's subagent pattern
does - that works because Claude runs on datacenter GPUs, where a second subtask really does get
its own separate compute. This agent has exactly one CPU (or one GPU) and one loaded model
instance as its entire compute budget; running two "subtasks" against it either queues them
(same total time, plus overhead) or time-slices them (same total time, worse overhead) - there's
no way to get real model-level parallelism out of one single-threaded inference engine.

What *is* real: if the model returns several tool calls in one response - reading three files at
once, say - the actual work behind each one (disk I/O in `read_file`/`read_document`, numpy
cosine-similarity math in `search_codebase`) releases Python's GIL while it runs, so threads
genuinely overlap even on a single CPU. `ToolRegistry.execute_batch()` runs a batch concurrently
via a small thread pool exactly when it's safe to, and falls back to the identical one-at-a-time
behavior otherwise.

**"Safe" is deliberately conservative, given what could go wrong**: write/execute-type tools
(`write_file`, `edit_file`, `run_command`, `db_execute`, etc.) always force the whole batch
sequential - they have side effects, and each needs its own uninterrupted permission prompt.
Read-only tools gated by `request_read()` (`read_file`, `read_document`, `list_symbols`,
`list_directory`, `db_schema`, `db_query`) only parallelize once the session has *already*
granted read trust (answered "session" to an earlier prompt) - before that, running them
concurrently risks two threads trying to show an interactive permission prompt at the same time,
which could genuinely garble a terminal. `search_codebase`/`grep_codebase` never prompt at all,
so they're always safe regardless of trust state. A single tool call, or any batch containing
even one unsafe tool, always falls back to exactly today's sequential behavior.

This needed no system prompt changes at all - the model already could, and might, return
multiple tool calls in one turn; this only changes how quickly this project executes what it
already asked for, invisibly to the model.

Tested at every level: the safety-gating logic across five cases (single call, always-safe tools,
read-gated tools with and without trust, a mixed batch with a write tool); a genuine measured
wall-clock speedup (5 calls at ~0.3s each: 1.50s sequential vs. 0.30s parallel, a real 5x, not
just a theoretical one); results preserved in call order despite finishing in a different order;
one call's exception correctly isolated without affecting the others or crashing the batch;
real disk I/O (not mocked) reading actual files concurrently; and full loop-level tests through
both the CLI and GUI confirming a genuinely mixed batch (read + write) still falls back to the
exact sequential path with no behavior change at all.

## Solving memory pressure WITHOUT lowering context_window

Two things address this together - one changes how Ollama itself runs (the real fix), the other
is an automatic safety net inside this project (graceful degradation if the real fix isn't in
place, or isn't enough).

**The direct fix: Ollama's own KV cache quantization.** `context_window` sets the *ceiling*
Ollama allocates for - the actual RAM cost of that ceiling depends on how many bytes each token
of context costs, and that's controllable independently of the ceiling itself. By default each
token costs full precision (f16). Two environment variables change this, set on the machine
running the **Ollama server** (not this project) before it starts:

```bash
# Linux/macOS, if you launch Ollama yourself:
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve
```

On Windows, or if Ollama runs as a background service/tray app rather than a terminal you control
directly: set these as **system environment variables** (Settings → System → About → Advanced
system settings → Environment Variables), then fully restart the Ollama service/tray app so it
picks them up on its next launch.

`OLLAMA_KV_CACHE_TYPE=q8_0` roughly **halves** the memory cost of the same `context_window` -
`q4_0` cuts it further still, at a bit more quality cost. **`OLLAMA_FLASH_ATTENTION=1` is required
for the quantized cache type to actually take effect** - without it, Ollama silently keeps using
full precision regardless of `OLLAMA_KV_CACHE_TYPE`. This is the correct lever for "keep
`context_window` at 20000 but make it cost less RAM" - it changes the cost-per-token, not the
ceiling, which is exactly the constraint asked for.

**The safety net: automatic one-level context fallback, built into this project.** Added to
`agent/ollama_client.py`: if a chat request fails with a 5xx error and no content has been
streamed back yet (so nothing's lost or duplicated by retrying), the client automatically retries
that *one* request with half the context window, purely as a per-request parameter - your
configured `context_window` in `config.yaml` is never touched. A visible message explains what
happened and points at the KV-cache-quantization fix above. This is deliberately conservative:
exactly one fallback attempt (no spiraling through ever-smaller windows), skipped entirely if any
content already streamed before the failure (retrying then could produce duplicated or garbled
output), and skipped for genuine connectivity failures (a smaller context window doesn't help if
Ollama isn't reachable at all - that's a different problem with a different fix).

Tested directly against the client, not just described: confirmed a 500 on the first attempt
followed by a successful retry at half the context (verified the exact `num_ctx` values sent in
each request, and that `context_window` itself is unchanged afterward); confirmed a genuine
connection failure does NOT trigger a wasted fallback attempt; confirmed a 500 arriving *after*
some content had already streamed does NOT retry (raises normally, since a silent retry there
could duplicate output); confirmed a 500 that persists even at the smaller fallback context stops
after exactly one retry rather than recursing indefinitely.

## Fixed: a 500 error was showing the wrong troubleshooting advice

A user's screenshot showed *"Could not reach a local Ollama server"* alongside a *"500 Server
Error"* detail - those two things don't actually agree with each other, and that inconsistency was
the tell. A 500 status means Ollama **did** respond - it's running and reachable - the request
itself is what failed on Ollama's side. That's a fundamentally different problem from a genuine
connection failure (Ollama not running, a firewall blocking the port), and needs different
troubleshooting advice, but `agent/ollama_client.py` was routing both through the same generic
"could not reach" checklist, because `requests.HTTPError` (raised by `raise_for_status()` for any
non-2xx response) was being caught by the same handler as a real connection failure.

Fixed by adding `_request_error()`, which checks whether the exception actually has a response
object. If it does, it's not unreachability - for a 5xx status specifically, the new message says
so plainly and points at the most likely real cause on this project's hardware profile: running
out of memory while processing a large context (pointing directly at `context_window` in
`config.yaml`), since that's a much more likely explanation for an intermittent 500 than a
firewall issue would be, especially following the recent bump to a much larger context window.
It also surfaces Ollama's own error response body when available, and points at checking Ollama's
own logs, since the HTTP client genuinely cannot see *why* Ollama's own process failed, only that
it did. Tested against both a real simulated 500 (confirmed the new, more useful message appears)
and a genuine connection failure (confirmed the original checklist message is unchanged for that
case) - the fix is additive, not a regression risk for the case that was already handled correctly.

Also investigated, but did not find a bug in: the "Agent is working" bar appearing to stay shown
in the same screenshot. Checked every code path in both `gui/agent_loop_gui.py` and
`gui/static/app.js` that should clear it - every error path correctly fires a `turn_complete`
event, and the frontend correctly calls `endTurn()` unconditionally on receiving it. The most
likely explanation is a screenshot catching the brief, asynchronous gap between an error
rendering and its `turn_complete` event being processed, not a persistent stuck state - but this
is flagged as unconfirmed, not fixed, since it couldn't be reproduced directly.

## Dependency graph, file summaries, and episodic memory

Three additions built together, evaluated against a proposed architecture diagram - most of that
diagram turned out to already exist under different names, but these three were genuine gaps.

**Dependency graph and file summaries share one enhancement, not two new tools.** Rather than
adding `get_dependencies` and `get_file_summary` as separate tools, both were folded into the
*existing* `list_symbols` - deliberately, since this project already learned the hard way that
tool count itself hurts reliability on a small model, so enhancing what exists beats adding more
surface area for the same value. `list_symbols` now shows, before the usual function/class list:
a one-line summary (the file's docstring, or its leading comment block for non-Python files), and
its dependencies - what it imports, and critically, what imports *it* (what would be affected by
changing this file). Both come from pure static analysis (`ast` for Python, regex for JS/TS,
matching the honest limitations of this project's other JS heuristics) - no LLM calls, rebuilt
automatically on every reindex. Tested against real files with real import relationships: verified
forward and reverse dependencies resolve correctly for both Python and JS, and verified the
combined output (summary + dependencies + symbols) renders correctly in one call.

**Episodic memory is a genuinely new, seventh store** - and the one place this project's
persistence story previously stopped short. Everything else resets when the process closes; this
is the first thing that survives across separate sessions on the same project. Two parts:

- The **Scratchpad** (`update_scratchpad`) is now saved to `.local_agent/SCRATCHPAD.md` on every
  update and reloaded automatically at the start of the next session - a fact learned last week
  ("use bcrypt for passwords") is still known today, not just for the rest of that one session.
- A rolling **episode log** (`.local_agent/EPISODES.md`) gets one line appended at the end of
  every session, generated from the final plan state with no LLM call needed (e.g. "Plan: 2/3
  steps done - was working on: Add tests"), and the last few entries are shown at the start of
  future sessions for brief continuity.

Tested as an actual two-session simulation, not just each piece in isolation: session 1 learned
two facts and worked partway through a 3-step plan, then ended; a brand-new `ContextManager`
instance (simulating a fresh process) correctly loaded both the scratchpad facts and the episode
summary - confirmed not just in memory, but that they actually reach the assembled prompt sent to
the model; session 2 then continued the work and ended, correctly *appending* a second episode
rather than overwriting the first; and a separate test confirmed the episode log caps at the most
recent entries even with many accumulated, rather than growing unbounded.

**Worth being upfront about**, matching this project's practice of separating what's confirmed
from what's designed: episodic memory is directly adjacent to a hard-reset mechanism built and
then rolled back a few updates ago. That rollback was about handling context pressure *within* one
session; this is about continuity *across* separate sessions - a different concern, requested
explicitly with the tradeoff already known. If it turns out not to be wanted in practice either,
the persisted files can simply be deleted or `scratchpad_persist_path`/`episode_log_path` can be
unset in `config.yaml` without touching any code.

## Session logs

Every session (CLI or GUI) now writes a plain-text log to `.local_agent/sessions/session_<timestamp>.log`
in your project - every user message, every tool call and its result, every final response, with
timestamps. This exists because everything else in this project (conversation, plan, scratchpad)
lives only in memory and is gone the instant the process closes - the session log is the one
durable record of what actually happened, readable after the fact without needing to keep the
terminal or GUI window open or scroll back through it.

It flushes after every single write rather than buffering, specifically so an abrupt exit (Ctrl+C,
a crash, force-closing the GUI window) doesn't lose anything - tested directly: killed a logger
object mid-session with no clean shutdown at all, and confirmed everything written up to that
point was still fully readable on disk afterward. The CLI prints the log's exact path in its
startup banner; the GUI exposes it via `/api/status` as `session_log_path`. A session's log is
independent of the SQLite codebase index and gitignored the same way (`.local_agent/`), so it
never ends up committed to your project by accident.

## Reducing redundant replies, prioritizing delivery

Added a tight, targeted addition to the system prompt (mindful of the lesson from the "system
prompt size" section below - this was folded into an existing sentence, not appended as a new
paragraph): the model is told explicitly not to restate a plan or intention it already stated in
an earlier turn, and to prioritize actually producing the deliverable over describing it. This
targets a pattern visible in an earlier bug report's screenshot, where the agent said close
variations of "I'll create a comprehensive PostgreSQL schema..." across three or four separate
turns without the schema ever actually getting built. Still only a ~7% increase in prompt size
(from ~944 to ~1017 tokens) - the 69% reduction from the original ~3,300-token prompt still holds.

## GUI: input is disabled while the agent is working

The Send button being disabled wasn't actually enough - the text input itself was still typeable
while a turn was in progress, meaning a message typed and sent then would just silently do
nothing rather than clearly refusing. Fixed properly: the input field itself now gets the native
HTML `disabled` attribute (not just a JS-level check) while a turn is running, so it's genuinely
un-typeable, not just unresponsive - and a visible "Agent is working - input is paused until it
finishes" bar with a pulsing-dots animation appears so it's clear why, rather than the input just
going quietly greyed out with no explanation. The CLI didn't need an equivalent fix: its REPL loop
is fully synchronous, so there's structurally no way to type a new message while a turn is running
- the terminal isn't even prompting for input until `run_turn()` returns.

## Context architecture: deduplicating superseded reads

Beyond raising `context_window` (see below), a real new optimization was added using the existing
Context Selector: if the same read-type tool (`read_file`, `read_document`, `read_files`,
`list_symbols`, `list_directory`, `db_schema`, `search_codebase`, `grep_codebase`) gets called
with the same target more than once in a conversation - a known real pattern, seen in earlier bug
reports where the agent re-read the same file multiple times - only the LAST call's full content
is sent to the model; earlier, superseded calls collapse to a short placeholder. Write/execute
tools (`write_file`, `edit_file`, `run_command`, etc.) are deliberately never touched by this -
running the same command twice is meaningful history, not redundant, and collapsing it could hide
something worth seeing.

This only affects what's *sent to the model this turn* - the true underlying conversation history
(used for compaction and the permanent record) is completely untouched, so nothing is actually
forgotten, only de-prioritized for a given turn's prompt. Tested against the exact repeated-read
pattern from an earlier bug report (the same file read three times): confirmed only the last read
keeps its full content (saving ~2,255 tokens in that specific realistic test), confirmed different
files remain fully independent of each other (no cross-file false positives), and confirmed
write/edit/run_command are never collapsed under any circumstance.

## Context window: 20000 (increased at the user's request)

Bumped from 8192 - a much bigger jump than the earlier 6144→8192 change, and worth being direct
about again: KV cache memory scales with `context_window`, so this meaningfully increases the 9B
model's RAM footprint on top of a fit that was already tight on 8GB/no-GPU hardware. This is
included because it was explicitly requested with a specific number, not because it's
risk-free - confirm it's actually comfortable on your machine, not just that it starts, and drop
it (or fall back to `qwen3.5:4b`) if you see swapping, stalling, or instability.

`history_soft_limit_tokens` was rebalanced to 14000 accordingly (up from 6000), leaving ~6000
tokens of headroom for the system prompt, file index, task history, scratchpad, auto-retrieved
project knowledge, AND the model's own response generation (`num_ctx` covers the whole exchange,
prompt and response together, not just the prompt). This is a considerably better ratio than the
old 8192/6000 pairing left, both because the window itself is bigger and because the system
prompt is now ~900 tokens instead of ~3,300 (see below) - and the deduplication optimization above
means the 14000-token raw-chat budget goes further in practice than the raw number suggests, since
repeated identical reads no longer eat into it at full cost.

## read_document couldn't read past its own truncation point

A user's screenshot showed the agent calling `read_document` on the same file twice with
identical arguments, getting identical truncated content back both times, and correctly - if
unhelpfully - diagnosing its own problem out loud ("the read_document tool is returning the same
content each time"). It was right, and it had no way to fix it: `read_document` capped every call
to the first 20,000 characters with no way to ever see anything past that point, no matter how
many times or how it was called.

Added an `offset` parameter, the same fix pattern as `read_file`'s `start_line`/`end_line`: each
call still returns at most 20,000 characters, but the result now says exactly what offset to pass
next if the document continues ("call read_document again with offset=20000 to continue
reading"), and using that offset actually returns the next chunk rather than the same one. Tested
against a real 57KB text file: confirmed calling with identical arguments still (correctly)
returns identical content - that's not the bug, a deterministic function returning the same
output for the same input is correct - but following the suggested offset returns genuinely
different, advancing content; confirmed the final chunk doesn't suggest a nonexistent next offset;
confirmed requesting an offset past the end says so clearly instead of returning nothing
confusing; and confirmed a short document that fits in one call gets clean output with zero
pagination clutter added.

The same screenshot also showed the agent apparently freezing mid-response after several of these
repeated calls. The pagination fix directly addresses part of what could cause that - two
duplicate 20,000-character tool results were bloating the conversation for no benefit - but
being honest about the limits of what could be confirmed here: a 9B model generating a long
response on CPU with no GPU can also just genuinely take a while, which can look identical to
"frozen" if you're not sure how long to expect. If a response still never completes after this
fix, that's worth its own report.

## System prompt size matters more than it looks

A user reported the agent repeatedly narrating intentions ("I'll create...", "Let me start
by...") across many turns without ever actually building anything, needing to be prodded
("Start", "Build it", "Ok build it") over and over - and, tellingly, at one point the agent
literally said "as mentioned in rule 23," citing its own system prompt's rule numbering back in
conversation. That was the real clue: the system prompt had grown to **32 numbered rules,
~13,200 characters (~3,300 tokens)** after a long series of incremental additions, each one
reasonable on its own but never weighed against the cumulative cost. For a 9B model, that's over
40% of the entire 8192-token context window consumed before a single message of actual
conversation - a real, measured cause of degraded tool-calling reliability, not a guess.

Rewritten from the ground up: same behavioral coverage (every safeguard, every tool's guidance
survived - verified with a systematic check of the old prompt's tool names against the new one,
which caught and restored two that had gone missing in the first pass), but condensed into
grouped prose instead of a numbered list, and stripped of the verbose "why" explanations that
are useful for a human reading this README but pure overhead for a model processing them on
every single turn. Result: **~900 tokens, a 73% reduction.** Also moved away from explicit rule
numbering specifically because of the "rule 23" incident - a numbered list invites a model to
treat the numbers as citable content, which is exactly the failure observed.

Measured, not assumed: the full assembled context (system prompt + a populated file index, task
plan, and scratchpad) now costs about 1,000 tokens total, versus the system prompt *alone*
costing 3,300 before this fix - leaving roughly 7,000 tokens of real headroom for actual
conversation instead of the window being nearly exhausted before it starts.

**The lesson for anyone extending this further**: every rule added to `agent/prompts.py` has a
real, compounding cost on a model this size. Before adding another one, consider whether it can
fold into existing guidance instead of becoming a new paragraph.

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
- Tool-calling reliability at this size is good but not perfect. Two known failure modes and what
  guards against each: (1) the model calls a tool with missing/wrong arguments - it gets back a
  clear corrective message telling it to retry, not a raw Python error (see rule 8b in the system
  prompt); (2) the model narrates "I will use X to..." in plain text without actually including
  the tool call - a real, observed failure, not a hypothetical - which gets detected and given one
  bounded corrective nudge before the turn gives up gracefully rather than looping forever. Neither
  guard is a substitute for the model reliably calling tools in the first place; if you notice the
  agent stalling or narrating actions that don't happen, rephrasing your request more concretely
  can help, and it's worth trying again since these two specific patterns are now caught.
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
