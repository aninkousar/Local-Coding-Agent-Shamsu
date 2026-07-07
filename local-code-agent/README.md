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

## Known limitations (be honest with yourself about these)

- No true multi-file transactional edits - each `edit_file`/`write_file` call is its own
  approval. For a change spanning many files, expect many prompts.
- The semantic index uses simple fixed-size chunking, not AST-aware parsing - it's good at
  "find code related to X," not perfect recall of exact structure.
- Tool-calling reliability on a 4B model is good but not perfect; if the agent seems to loop or
  stall, it may not have emitted a valid tool call - try rephrasing your request more concretely.
- Scanned/image-only PDFs aren't read as text - export the page as an image and use `read_image`
  instead, since the model can see images directly.
