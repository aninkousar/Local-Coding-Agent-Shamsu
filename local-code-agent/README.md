# Local Code Agent

A fully offline, permission-gated coding agent shaped like Claude Code, built on a model
small enough to run on an 8GB-RAM machine with no GPU.

## Read this first: what to actually expect

This uses a **4-billion-parameter local model** (Qwen3.5-4B). That is roughly two orders of
magnitude smaller than the models behind Claude Code. Concretely, that means:

- **It's genuinely useful for**: reading a codebase and explaining it, small-to-medium focused
  edits, scaffolding a new file/module from a clear spec, writing boilerplate, answering "what
  does this code do", reading a requirements doc or a mockup image and turning it into a first
  draft.
- **It will struggle with**: large multi-file refactors, subtle bugs, ambiguous requirements it
  has to infer, anything requiring holding a lot of context in its "head" at once.
- **Work in small steps.** Give it one file or one feature at a time. Review every diff. Treat it
  like a fast, tireless, occasionally-wrong junior developer who never gets tired of your
  corrections - not a drop-in replacement for a senior engineer.

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
5. **Runs 100% locally.** After the one-time model download, there is no network call anywhere in
   this codebase except to `localhost:11434` (your own Ollama server). No API keys, no rate
   limits, no per-message cost, no usage cap - your only limits are your own hardware.

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
agent/ollama_client.py    the ONLY network code in this repo - talks to localhost:11434
    │
    ▼
Ollama (local server)
    │
    ▼
qwen3.5:4b (chat+vision, ~2.5GB RAM at Q4) + nomic-embed-text (~50MB, for codebase search)
```

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

## Built for writing code efficiently

A few tools and behaviors exist specifically to reduce wasted round-trips and catch mistakes
before you have to:

- **Auto syntax-check after every write_file/edit_file/scaffold_files.** Python files are
  checked with the stdlib `ast` parser, JSON with `json.loads`, and JavaScript with `node --check`
  if Node is installed. If something's broken, the model sees "⚠ Syntax check FAILED: ..." in
  the *same turn* and is instructed to fix it immediately - not wait for you to notice and report
  it next message. Other file types aren't checked this way (nothing is claimed about them).
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
(e.g. Flask+Jinja2) by default, rather than framework/bundler-heavy stacks - a 4B model is far
more reliable on the simpler stack unless you specifically ask for something else.

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
# 2. Pull the two local models (one-time, ~3GB total download)
ollama pull qwen3.5:4b
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
  but confirm it actually helped rather than assuming - check `ollama show qwen3.5:4b` or the
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
- Tool-calling reliability on a 4B model is good but not perfect; if the agent seems to loop or
  stall, it may not have emitted a valid tool call - try rephrasing your request more concretely.
- Scanned/image-only PDFs aren't read as text - export the page as an image and use `read_image`
  instead, since the model can see images directly.
- **GUI-specific**: the chat panel renders bold, inline code, and fenced code blocks, but not
  full markdown (no lists/headers/tables yet); the Flask dev server it runs on is fine for this
  single-user local use case but isn't hardened for exposure beyond `127.0.0.1`, so don't
  port-forward it.
