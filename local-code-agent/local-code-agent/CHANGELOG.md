# Changelog

All notable changes to Local Code Agent, in order. This reflects the actual build history of
this project rather than dated releases - entries are numbered, not timestamped, since they were
all produced across one continuous development session rather than separate calendar releases.

## 62. Fixed: orphaned dev server processes left running after the agent exits
Continuing the Odysseus-Portable comparison deeper into its actual source (not just the README)
surfaced that `src/runtime.js` does dedicated PID tracking specifically to prevent "zombie
processes" when the launcher exits - prompting a direct check of whether this project had the
same problem, rather than assuming it didn't. It did: `_RunningProcess` tracking was purely an
in-memory dict, `stop_process` only terminated a process on explicit request, and nothing cleaned
up a still-running dev server if the agent itself exited first. A `npm run dev` (or similar)
started via `start_dev_server` could be left running as a genuine orphan, holding its port, with
no indication to the user it was still alive.

Fixed with `atexit.register()` in `ToolRegistry.__init__`, calling new
`_cleanup_processes_on_exit()` - terminates any still-running process, escalating to kill after a
2-second timeout, best-effort throughout so an error here can never block actual shutdown.

Tested at the level that actually matters: real OS-level verification via `os.kill(pid, 0)`, not
just internal state. Started a genuinely long-running process, confirmed it was truly alive at the
OS level, ran cleanup directly, confirmed via the same OS-level check that it was genuinely dead
afterward. Also confirmed: `atexit` registration happens at construction time; an already-exited
process doesn't error during cleanup; three simultaneous processes are all correctly terminated
with none left behind; the normal explicit `stop_process` path is completely unaffected.
- `agent/tools.py`, `README.md`

## 61. Proactive hardware-based context_window detection
Compared against Odysseus-Portable (a portable launcher for a general-purpose llama.cpp-backed
chat app, not a coding agent) - most of it wasn't a fit for this project's different audience
(portable runtime bundling, SQLite-backed memory trading away deliberate file readability,
parallel inference slots hitting the same reasoning already declined in the first architecture
round of this whole project). One idea connected to real, prior evidence: this project already
had the *reactive* half of hardware-aware context sizing (#41's automatic fallback after an actual
OOM); Odysseus does the *proactive* half - detect hardware upfront, pick a sensible default before
ever starting. There was already direct evidence this specific gap mattered: an earlier session in
this project's own history hit real HTTP 500 errors traced to `context_window` exceeding actual
hardware capacity.

Added dependency-free, cross-platform RAM detection (`/proc/meminfo` on Linux, `sysctl` on macOS,
`GlobalMemoryStatusEx` via `ctypes` on Windows) and a conservative RAM-to-context_window heuristic
in `agent/config.py`, engaged only when `context_window` is genuinely absent from `config.yaml` -
an explicit value always wins unconditionally. Required fixing a real ambiguity first: the
original property couldn't distinguish "explicitly set to 20000" from "unset, fell back to a
hardcoded 20000" - both went through the identical code path, making it impossible to know when
auto-detection should safely engage.

Real bug hit and fixed during implementation: an early edit left `@dataclass` orphaned above the
new helper function instead of attached to the `Config` class, breaking the module at import time
- caught immediately by actually importing it, not assumed correct.

Tested rigorously: explicit `context_window` respected even against a mocked hardware suggestion
of both a much smaller and much larger value; genuinely absent key correctly engages detection;
detection failure falls back safely without crashing; all six RAM tiers monotonically sensible.
Real detection (not mocked) verified against this actual machine - 3.61GB returned, cross-checked
directly against `free -m`'s own "available" figure (3701 MB) and found accurate. Full pipeline
tested through real `Config.load()` on both a fresh config (correct auto-detection) and this
project's actual config.yaml (completely unchanged, prior explicit choice preserved exactly).
Surfaced via new `context_window_source` property in both the CLI banner and GUI session log.
- `agent/config.py`, `agent/main.py`, `gui/server.py`, `config.yaml`, `README.md`

## 60. Persistent task state: mostly already built, one small scratchpad nudge added
A "persistent task state" proposal (Goal/Completed/Current/Next/Known issues/Relevant files, kept
tiny) mapped almost entirely onto existing machinery: Completed/Current/Next is `[x]`/`[~]`/`[ ]`
plan steps, "Goal" is what Feature grouping's headers already provide, and persistence across
process restarts was already built several rounds back for this exact reason.

"Relevant files" and "Known issues" weren't structurally missing - the scratchpad already exists
as a general-purpose, persisted fact list capable of holding either today. Declined new dedicated
fields for something the existing scratchpad already covers, same reasoning that shaped Feature
grouping as a marker inside the existing plan format rather than new structure.

Fixed with one line in `agent/prompts.py`: extended the scratchpad guidance's existing examples
(env var names, stated preferences) to explicitly include "which files matter for the current
goal" and "known issues you haven't fixed yet," folded into the same sentence. Still a 63%
reduction from the original system prompt size.

Honestly flagged but not built: Task History/Scratchpad have no size enforcement, unlike File
Index/Project Knowledge which the Context Budget Manager actively trims. Left unbuilt without
confirmed evidence of a real problem, same grounds as several other ideas set aside in this series.
- `agent/prompts.py`, `README.md`

## 59. LSP declined for resource conflict; three cheap approximations built instead
A "give it an LSP" proposal surfaced genuinely missing capabilities (definition, references,
rename, type inference) - a stronger case than most recent rounds - but was declined for a direct
resource conflict: real LSP servers (pyright, tsserver, rust-analyzer, gopls, clangd, jdtls) are
long-running, stateful, memory-hungry processes requiring JSON-RPC protocol handling, a
fundamentally different category from every existing checker in this project (one-shot CLI calls).
This project's entire design has been shaped around 8GB RAM with no GPU where the model already
consumes most of the budget - running multiple additional memory-hungry servers directly threatens
the same constraint `context_window`'s fallback logic (#41) exists to protect.

Built three cheap, Python-only, deterministic approximations instead, precision limits stated
honestly rather than oversold:

**Real signatures with type hints**: `extract_python_symbols()` now reconstructs actual function
signatures via `ast.unparse()` (including type annotations and defaults) instead of a bare `(...)`
placeholder - extracting existing hints, not performing real type inference. Tested against 6 real
signature patterns simultaneously (untyped, typed+return, defaults, keyword-only, typed/untyped
varargs) - all correct, including the trickiest mixed case.

**`find_definition(name)`**: new `build_symbol_table()` in `agent/indexer.py`, built once during
`reindex_codebase` alongside the dependency graph, mutated in place (proactively applying the same
fix as the dependency graph's earlier stale-reference bug, rather than waiting to rediscover it
here). Honestly documented as name-based, not semantic - explicitly discloses multiple matches
rather than silently picking one. Tested against a deliberate name collision (two files each
defining `login`) - correctly reports both with disclosure; correctly refreshes with no stale
entries after a file removal + reindex.

**`grep_codebase` connected to the references(symbol) use case** via its description, not a
behavior change - word-boundary-only matching would have regressed its existing substring-search
utility, so the fix was documentation plus an honest imprecision caveat instead.
- `agent/indexer.py`, `agent/tools.py`, `README.md`

## 58. Error-context enrichment extended: run_command wiring + a syntax-error fallback
An "automatic error classification" proposal was mostly declined: explicit category labels add
little when error text already conveys its category clearly (same reasoning as declining symbol
ranking earlier), and LSP-based TypeScript type-checking was flagged as a substantial new tool
integration - this project currently only runs `node --check` (syntax only) for JS/TS - not a
natural extension. Built instead: two well-scoped extensions of the proven two-hop mechanism.

Wired the same enrichment into `run_command` (previously only `run_tests` and write/edit checks) -
verified against a real two-file crash (script → helper module, division by zero): all three
traceback frames correctly enriched with the right dependency symbol map.

Added a plain line-range fallback (4 lines each side) for when `find_containing_symbol` can't work
at all - which happens exactly for syntax errors, since unparseable code has no AST to walk.
Previously syntax errors got zero automatic context, unlike everything else now enriched.

Two real bugs found and fixed while wiring this in, not glossed over: (1) the syntax-error message
embeds "at line N" mid-sentence, not at line-start like pyflakes' bullets - the existing regex
didn't match it at all, fixed with a dedicated pattern. (2) The fallback initially assumed "AST
extraction failed" always meant "syntax error" - but it also correctly triggered for an ordinary
`if __name__ == "__main__":` block, which is valid code, not a syntax error. Fixed by actually
parsing the file to distinguish "genuinely broken" from "legitimately module-level" before
choosing the message. Verified directly: a real syntax error and a real `if __name__` block now
produce correctly different, accurate messages instead of the same misleading one.

Full regression battery re-confirmed after both extensions: prior round's `run_tests` enrichment,
zero noise on clean writes/commands, and the 3-location cap all still hold.
- `agent/tools.py`, `README.md`

## 57. Two-hop error-context enrichment: error → containing function → its dependencies
A "walk from error to relevant source, don't dump the whole repo/error log" proposal, explicitly
scoped to two hops after weighing the risk: hop one (exact function containing the error) via new
`find_containing_symbol()` in `agent/indexer.py` - AST-based, picks the innermost containing
function/method/class, correctly returning a method rather than its enclosing class when both
technically contain the line. Hop two deliberately does NOT guess a specific call target - that's
the same precision problem the call graph was declined for two rounds ago - and instead shows a
compact symbol map (already built) of what the erroring file directly depends on, letting the
model make the final judgment itself.

Added `ToolRegistry._enrich_with_error_context()`, wired into `run_tests` (only on nonzero exit,
so a clean pass pays nothing) and the pyflakes path of `_format_check_result` via
`write_file`/`edit_file`/`scaffold_files`.

Real bug found and fixed mid-implementation: the first version assumed pyflakes output included
`file.py:N:` per line (raw pyflakes CLI format) - it doesn't, this project's own wrapper reports
just `"line N: ..."` since there's only ever one file in context, so the regex silently matched
nothing against real output. Caught by testing, not assumed correct. Fixed with a `known_file`
parameter - write/edit/scaffold call sites already know which file they checked, so that's passed
directly rather than re-derived from text that doesn't contain it; `run_tests` keeps the original
file:line parsing since Python tracebacks do contain full path+line structure correctly.

Tested against a real, deliberately constructed three-file scenario (test → auth.py → session.py)
with a genuine failing assertion: enrichment correctly showed both stack frames' exact function
bodies, each paired with the right dependency's symbol map - `session.py`'s functions (including
`get_session`) alongside `refresh_token`, matching the exact scenario from the original proposal
without guessing the call target. Confirmed graceful degradation with no dependency graph yet;
zero enrichment noise on a clean pass/write; cap correctly enforced at 3 locations against a file
with 10 real errors.
- `agent/indexer.py`, `agent/tools.py`, `README.md`

## 56. run_tests: dropped -v after measuring the actual difference, not assuming
A "compiler as a second brain" proposal's core loop (write→check→feedback→patch→test) was already
confirmed built in an earlier round - `_format_check_result()` runs automatically after every
write/edit, test execution stays a deliberate judgment call for the model, same reasoning as
before, unchanged here. But the proposal's specific example ("send only failures, not all 27
results") led to actually checking `_tool_run_tests`'s pytest invocation instead of assuming it
was fine - `-v` was set, adding a PASSED line per passing test alongside the failures that
actually mattered.

Verified with a real pytest run before making any change: a 7-test suite (5 passing, 2 failing)
produced 1714 characters with `-v --tb=short`, and 1175 with just `--tb=short` - a 31% reduction,
with every piece of failure information (names, tracebacks, assertion messages, summary line)
byte-identical between both. `-v`'s only contribution was one PASSED line per pass, conveying
nothing the final "N passed" count doesn't already say - and this scales worse with larger test
suites, since each pass costs a full line at `-v` versus a single character without it. Fixed by
dropping `-v` from the pytest command in `_tool_run_tests`. Confirmed through the actual tool call
(not just raw pytest) that failure detail survives completely and the PASSED noise is gone.

Deliberately declined the broader version - a universal, cross-runner failure-only filter for
cargo/go test/npm test/phpunit too. Same category of undertaking as the call graph and symbol
ranking questions from recent rounds: each runner has a different output format, and a fragile
per-framework parser is a worse trade than the one fix that was actually measured and confirmed.
- `agent/tools.py`, `README.md`

## 55. get_symbol + symbol maps replacing truncated content in dependency-context
A "symbol map per file, fetch one function on demand" proposal surfaced a real weakness in
something already built, not just a missing capability: `_dependency_context()` showed related
files as raw content truncated at a fixed character count, which could cut a function off
mid-body - exactly the arbitrary-chunking problem being critiqued, unrecognized as a live issue
until this comparison. The proposal's "Calls" tracking idea was set aside for the same reason the
call graph was declined the round before - real but meaningfully less precise than what follows.

Extracted `extract_python_symbols()` into `agent/indexer.py` from `list_symbols`'s existing
AST-walking logic, so both callers share one implementation instead of two that could drift apart.
Verified as a true regression-safe refactor: captured `list_symbols`'s exact output on a real file
before refactoring, confirmed byte-for-byte identical output afterward.

Added `get_symbol(path, name)` - Python only, uses AST's `end_lineno` (3.8+) for exact function/
method/class boundaries, replacing the previous two-step workaround (`list_symbols` for a start
line, then `read_file` with a guessed end line). Includes decorators - correctly computes a
decorated function's true start as the decorator line, not the `def` line. Handles the same name
appearing more than once in a file by returning the first match with an explicit note about where
the others are, rather than guessing silently or refusing.

Switched `_dependency_context()` to show a symbol map for Python related files instead of
truncated raw content, falling back to the previous truncated approach for non-Python or
unparseable files.

Tested thoroughly: `get_symbol` correctly includes a decorator; correctly detects and explains an
ambiguous name while still returning a usable result; correctly extracts classes, not just
functions; clear messages for a missing name or non-Python file. The dependency-context switch
verified to solve the actual problem: a full file structure (class, three functions including an
async one, a module-level variable) shown in ~200 characters, where truncation could previously
have cut off before reaching a later-defined function. Verified end-to-end through the real agent
loop: reading one file correctly triggered another's symbol map on the very next turn.
- `agent/indexer.py`, `agent/tools.py`, `agent/context_manager.py`, `README.md`

## 54. list_symbols: module-level variable extraction
A "code intelligence layer" proposal (Files/Classes/Functions/Variables/Imports/Function
calls/Types/API endpoints/Database tables/Tests/Configuration) got checked item by item, unlike
the two immediately preceding rounds which came back essentially fully covered - this one
genuinely wasn't. Five items already existed (File Index, `list_symbols`, Dependency Graph,
`list_api_routes`, partial `db_schema` coverage of live databases). Variables, a call graph,
Types, and a dedicated Tests view were real gaps, not equally worth building: explicitly flagged
that a call graph would be the first genuinely imprecise thing in this project - imports are
exact, but resolving actual call targets requires reasoning about dynamic dispatch a naive
version would get wrong regularly. Only the cheap, exact piece got built.

`list_symbols`'s AST walk now also extracts module-level variable/constant assignments, iterating
`tree.body` directly (not `ast.walk()`, which would pull in every local variable and loop counter
as noise). Added `_assignment_target_names()` handling simple names and tuple/list unpacking
recursively; deliberately excludes `Attribute`/`Subscript` targets since those mutate an existing
object rather than define a new one.

Tested against real code covering every case simultaneously: simple/annotated module-level
assignments captured correctly; tuple unpacking captured both names individually; a class
attribute, two different functions' local variables, a subscript mutation, and an attribute
mutation all correctly excluded. Confirmed a file with no module-level variables omits the section
entirely rather than showing it empty; confirmed the existing File Memory cache (#43) still works
correctly with the new section included.
- `agent/tools.py`, `README.md`

## 53. Continuous plan persistence - closing the one real gap in "the LLM is stateless"
A "make the LLM stateless" proposal got answered directly: the model has been stateless since
day one (Ollama's `/api/chat` is request/response with no server-side session), and everything
built throughout this project exists because of that fact, not despite it. But checking whether
everything persistent actually survives a *restart* (not just a within-session compaction) found
one real gap: scratchpad and episode log both correctly persist to disk, but the live task plan
didn't - a process restart mid-task would lose it, leaving only a stale one-line episode summary
if the previous session had shut down cleanly.

Explicitly flagged before building: this overlaps with #37 (persisted plan file), which was
rolled back. Distinguished clearly and confirmed before proceeding - #37 tied persistence to a
hard reset/wipe mechanism for within-session context pressure; this is purely continuous
persistence with zero reset behavior, aimed at surviving an actual process restart instead.

`update_task_history()` now persists to `.local_agent/PLAN.md` on every call; new
`load_persisted_plan()` restores it once at startup, following the exact scratchpad/episode-log
pattern. Deliberately did NOT reuse `format_plan_text()`'s output for the persisted format - that
bakes in step numbers and indentation for display, which would corrupt descriptions with leftover
numbers on the next parse. A raw, round-trip-safe serialization was written instead.

Tested precisely: a flat plan round-trips through a simulated restart with byte-identical
structure; a feature-grouped plan round-trips with zero step-number corruption (the specific bug
a naive approach would hit); no file yet is a graceful no-op; an intentionally-cleared plan stays
empty on reload rather than resurrecting as a phantom; no persist path configured doesn't crash.
Verified fully end-to-end: a real `update_plan` tool call through the actual loop, a simulated
crash, a fresh session loading it at startup, confirmed correctly present in that session's very
first prompt (using the condensed `PLAN:` label from #52).
- `agent/context_manager.py`, `agent/main.py`, `gui/server.py`, `agent/config.py`, `config.yaml`,
  `README.md`

## 52. Prompt Compiler: condensed context-block labels (~137→~16 tokens/turn)
A "Prompt Compiler" proposal - compile terse structured state instead of assembling prose - had
real measured impact once checked against `build_prompt()`. Every context block was wrapped in a
full explanatory sentence, every single turn (e.g. the Project Knowledge block's ~40-word
explanation of what it is and how to use it), and every one of these was already redundant with
something the persistent system prompt already explains once - the same mistake already fixed for
the system prompt itself in #33, just not yet applied here. Arguably a bigger miss than that one:
the system prompt's cost is fixed per assembly, but these labels were paying their explanation
cost in full on every turn that included the block, not once.

Condensed to short labels only (`FILES:`, `RELATED:`, `PLAN:`, `NOTES:`, `PAST SESSIONS:`,
`SUMMARY:`), safe because the fuller explanation of each store already sits once in the
persistent system prompt, immediately above these blocks every single time. Measured precisely:
551→65 characters of pure label overhead with all six blocks present - an 88% reduction,
~137→~16 tokens, saved on every turn carrying this content rather than paid once.

Tested directly: confirmed every actual piece of content (plan text, scratchpad notes, file list,
related code, episode log, summary) survives completely intact with only the wrapper labels
shortened - nothing cut, only re-labeled; confirmed the Context Budget Manager's truncation logic
still correctly trims oversized blocks with the new labels.
- `agent/context_manager.py`, `README.md`

## 51. search_codebase: deterministic weak-match filtering, not learned retrieval
"Learning Retrieval Quality" was evaluated against the same fundamentals as Context Prediction
Network: no real feedback signal exists anywhere in this system, and the scale (one user, one
project) is far too small to learn anything reliable from - declined for the same reasons. But it
pointed at something real: `search_codebase` returned raw `top_k` results with zero filtering,
confidently handing back "the top 6" even when nothing in the codebase was actually relevant.

Correction made openly during this work: the evaluation's initial claim that scores weren't shown
was wrong - `search_codebase` always displayed them. The real, narrower gap was filtering, not
visibility. Added `search_codebase_min_score` (default 0.3, threaded through `index_cfg` like
other index settings rather than a new constructor parameter) - results below it are excluded
entirely, and if nothing clears the bar, that's stated plainly with the actual best score shown
and a concrete alternative suggested (`grep_codebase`), instead of silently handing back
irrelevant results as if they were meaningful. Also fixed a stale tool description found while
touching this code - it still referenced "auto-retrieved snippets" from the semantic-RAG
mechanism replaced by the dependency graph several updates ago, missed during that migration's
own documentation sweep at the time.

Tested precisely: mixed strong/weak results keep only the strong ones with a clear omitted-count;
a query where nothing meets the threshold gets an actionable message, not confusing near-empty
output; all-strong results show no filtering noise; the pre-existing empty-results case (fresh,
unindexed project) is unaffected; the configured default applies correctly when unset.
- `agent/tools.py`, `agent/main.py`, `gui/server.py`, `agent/config.py`, `config.yaml`, `README.md`

## 50. Differential Memory: read_file shows changes instead of the whole file again
Given a stronger case than most recent architecture questions in this series: extends a pattern
already observed causing real problems in this project (repeated full-file reads, partially fixed
earlier via pagination and read deduplication for the *identical*-content case), and reuses
already-proven infrastructure - `agent/diffs.py`'s `make_unified_diff()`, the same function
powering the write/edit approval flow - rather than adding anything structurally new.

The gap: deduplication only handled re-reading a file that hadn't changed. The more common real
case - read, edit, read again to verify or after context got compacted - still showed the full
file content a second time, even for a one-line change. `_tool_read_file`, on a full-file read
(never on a `start_line`/`end_line` slice), now checks a new bounded cache (same
`_BoundedHybridCache` design as #49's fix, same reasoning) for what was last shown for that exact
path. Unchanged → a short confirmation instead of the full content. Changed → a unified diff
against what was last shown. New `full=true` parameter is the escape hatch. First-time reads of
any file are entirely unaffected.

Tested against a real file across all four paths: first read (unchanged behavior, confirmed);
second unchanged read (short confirmation, not full content); an actual edit followed by a read
(genuine diff, confirmed the new content appears in it); `full=true` forcing complete content
regardless. Confirmed partial reads are fully isolated from this mechanism in both directions -
never trigger it, never corrupt the cache for a later full read. Verified end-to-end through the
real agent loop: read a file, edited it via `write_file`, read it again - confirmed the model's
second `read_file` result was a diff showing exactly the new function added, not the whole file
repeated. No system prompt changes - the tool's own schema description already explains this
wherever the model would actually need to know it, so there's no reason to pay a permanent token
cost for it on every turn.
- `agent/tools.py`, `README.md`

## 49. Fixed _list_symbols_cache's unbounded growth with a real LRU/LFU hybrid
Follow-up to evaluating Linux-style LRU/LFU page replacement for context management: most
potential applications were already covered (`focus_files_max` is already simple LRU) or a poor
fit (`compact()` handles importance via summarization, not cache eviction), but
`_list_symbols_cache` turned out to be a genuinely unbounded plain dict - the same category of bug
as an earlier unbounded-summary-growth fix, just in process memory rather than context tokens.

Added `_BoundedHybridCache`, modeled directly on Linux's real page reclaim design: entries start
on an "inactive" list, get promoted to "active" on a second reference (protected from eviction
unless active itself overflows its share of capacity, in which case the oldest active entries are
demoted back to inactive rather than dropped), and eviction always targets the oldest inactive
entry first. This is what makes it a genuine hybrid rather than plain LRU: a file referenced twice
survives eviction that a more-recently-touched-but-only-once-referenced file does not.

Tested precisely: confirmed the core hybrid property directly (a twice-referenced entry survives
where plain LRU would have evicted it, since recency alone doesn't distinguish it); confirmed the
active list stays capped with correct demotion rather than unbounded growth; confirmed total cache
size never exceeds its configured max across 50 sustained insertions regardless of access pattern.
Also verified through the real `list_symbols` tool, not just the cache class in isolation: cache
hits still avoid re-parsing, mtime invalidation still correctly triggers re-parsing on a real file
change, and reindex still clears the whole cache - all unchanged from before, now with a genuine
size bound the old plain dict never had.
- `agent/tools.py`, `README.md`

## 48. Optional Feature grouping in plans (lighter Hierarchical Planning)
A proposed strict 6-level hierarchy (Project→Feature→Module→File→Function→Line) was evaluated
against what already existed: four levels already had a mechanism (File Index, Dependency
Graph+`list_symbols`, `list_symbols`'s line numbers, `read_file`'s line ranges), and "never
retrieve the whole project" was already this system's design philosophy throughout, not a gap.
The one genuine gap was Feature - nothing between "the whole goal" and each atomic step in
`update_plan`'s flat list. The literal *mandatory, strict* hierarchy was declined - this project
has direct prior evidence of what excess imposed structure does to a 9B model (#33's system
prompt condensing, prompted by the model literally citing its own rule numbers back to the user).

Built instead: optional Feature grouping with zero schema changes. `parse_plan_steps()` now
recognizes a `"## Feature Name"` line within the same flat string array as a group header
(everything after it, until the next `##` line, gets tagged `"feature"` in its dict) - a plan with
no `##` lines is entirely unaffected. Added a shared `format_plan_text()` so the tool's own result
text, what the model sees in context (`ContextManager._format_task_history()`), and the CLI panel
(`_render_plan()`) all render identically; updated the GUI's plan card (`app.js`,
`.plan-feature-header`/`.plan-step-grouped` CSS) to match.

Tested precisely: confirmed a flat plan produces text byte-for-byte identical to before this
existed (true backward compatibility); confirmed a grouped plan parses correct feature tags per
step and renders correct *global* step numbering across groups (not restarting at 1 per feature);
confirmed all three rendering surfaces stay consistent for the same grouped plan, end-to-end
through the real `ToolRegistry`. One sentence added to the system prompt, folded into the existing
planning paragraph - still a 64% reduction from the original prompt size.
- `agent/tools.py`, `agent/context_manager.py`, `agent/tool_loop.py`, `gui/static/app.js`,
  `gui/static/style.css`, `agent/prompts.py`, `README.md`

## 47. Real token-cost observability, evaluated honestly against a "Token Cost Optimizer" question
This one broke the pattern of the last several architecture questions: mapped against everything
already built (system prompt condensing #33, Context Budget Manager #43, deduplication #36,
Adaptive Compression #46, the RAG replacement #44, recall_history #45), the *optimization* work
was already substantially done - there wasn't a missing mechanism the way there was for virtual
memory or adaptive compression. What was actually missing was measurement: every budget decision
in this project relies on a `chars // 4` estimate never checked against reality, with no way to
see a turn's actual cost short of manually instrumenting code - which is how this exact data got
checked throughout this whole build.

Fixed for free: Ollama's own `/api/chat` response already includes real token counts
(`prompt_eval_count`, `eval_count`) on every call. Added `last_prompt_tokens`/
`last_completion_tokens` to `OllamaClient`, captured in both `chat()` (from the JSON response) and
`chat_stream()` (from the final streamed chunk where `done: true`) - `chat_stream()`'s `done`
event now also carries both fields directly. Added `SessionLogger.token_usage()`, called after
every model call in both loops, so the session log shows real per-call cost.

Tested against real simulated Ollama responses for both the streaming and non-streaming paths
(confirmed correct field extraction in each), and through the full agent loop: a turn with one
tool call showed real prompt cost growing 1200→1450 tokens between the two model calls within
it - the measured effect of a tool result actually being added to context, now visible in the
session log rather than only inferable from reading code.

Scope stated plainly in the README: this is observability, not a new optimization mechanism. The
real counts aren't fed back into recalibrating the char-based heuristics used elsewhere in this
project - they're logged for review. That would be a reasonable next step if the estimate turns
out to be meaningfully off in practice, but wasn't built here.
- `agent/ollama_client.py`, `agent/session_log.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`,
  `README.md`

## 46. Adaptive Compression - bounded summary re-compression + graduated tool-result tiers
Two pieces, both from evaluating whether "Adaptive Compression" would help against what already
existed (deduplication is a binary compression decision; the Context Budget Manager is adaptive
*space allocation*, not compression). Both fill genuine gaps rather than duplicating existing work.

**Bounded summary re-compression** directly fixes a known, previously undone gap: `compact()`
concatenates each new summary onto the old one with no cap (found while building #37, which was
rolled back because its fix - wiping the whole conversation - was too blunt and rejected). Added
`summary_max_chars` (default 1500) and `_recompress_summary()`: once the summary crosses this
after a compaction, it gets condensed into itself via one more infrequent model call - the
surgical version of the same fix, touching only the summary, not the whole conversation. Tested
directly: 5 rounds of repeated compaction correctly kept the summary bounded every round (each
crossing triggered re-compression back down), and confirmed zero extra model calls happen when
the summary stays comfortably under the cap.

**Graduated tool-result compression** smooths what was a single cliff (full detail up to
`keep_recent`, then compact()'s full summarization, nothing in between) into three tiers, scoped
specifically to tool-role messages (the usual size culprit): full detail for the most recent
`full_detail_recent_count` (4), a moderate cap for the next `light_compression_recent_count` (10,
capped at `light_compression_max_chars`), a more aggressive cap beyond that
(`heavy_compression_max_chars`) for anything still active but not yet compacted. Pure
deterministic truncation, zero extra model calls, same "transform at selection time, never mutate
self.messages" pattern as the existing read-deduplication. Tested precisely: all three tiers
produce exactly the expected lengths against controlled data; confirmed user/assistant messages
are never touched (tool-role only); confirmed end-to-end through the real agent loop - the oldest
of several tool results correctly shrank tier by tier as newer ones accumulated.

Added `summary_max_chars`/`full_detail_recent_count`/`light_compression_recent_count`/
`light_compression_max_chars`/`heavy_compression_max_chars` to `ContextManager`, `config.py`, and
`config.yaml`, wired into both entry points. No system prompt changes needed - both mechanisms are
fully transparent to the model, same as deduplication.
- `agent/context_manager.py`, `agent/config.py`, `config.yaml`, `agent/main.py`, `gui/server.py`,
  `README.md`

## 45. recall_history - closing the one real gap in context-as-virtual-memory
A question about whether an explicit "Context Virtual Memory" layer would help led to mapping the
OS virtual-memory analogy onto what already existed: `context_window`=physical RAM, `compact()`=
page-out, the session log=the swap disk (nothing truly lost), `_dependency_context()`/
`load_episodic_memory()`=page-in, File Index+Dependency Graph=page table, `focus_files_max`=LRU
eviction. Most of the concept was already built under different names. The one genuine gap:
page-out had no page-back-in for chat history specifically - once `compact()` summarizes old
messages away, that detail was only recoverable if the model happened to think to read the
session log file directly, with no guidance or efficient way to search it.

Added `recall_history(query)`: searches the current session's log for a keyword/phrase, returning
full blank-line-delimited blocks as matches (not bare single-line grep hits, so each result
carries its own natural surrounding context - who said what, in what exchange). Wired
`session_log_path` onto `ToolRegistry` as a post-construction attribute (set by `main.py`/
`gui/server.py` right after both `ToolRegistry` and `SessionLogger` exist, since the logger is
built second) rather than a constructor parameter, avoiding disruption to existing call sites.
Never prompts for permission - it's the agent's own already-exchanged conversation record, not
new file content - so it joins `search_codebase`/`grep_codebase` in the always-parallel-safe set.

Tested against a real session log: a bcrypt-vs-md5 decision correctly returned both the user's
message and the agent's reply together as one match; a genuinely unmentioned topic correctly
returned "no mention found"; a session with no log set at all correctly failed gracefully; and 8
matches against a 5-result display cap correctly showed the most recent 5 with a clear note that
more exist. One sentence added to the system prompt, folded into an existing closing sentence
rather than a new paragraph - still a 66% reduction from the original prompt size.
- `agent/tools.py`, `agent/main.py`, `gui/server.py`, `agent/prompts.py`, `README.md`

## 44. Replaced semantic RAG with dependency-graph-based Project Knowledge
At the user's explicit request: the automatic per-turn "Project Knowledge" retrieval no longer
uses embeddings/semantic search at all. Flagged the real tradeoff before building, not after:
semantic search could match relevant code by *meaning* for a brand-new question before any file
had been touched; a dependency graph needs an anchor - a file already in view - before it has
anything to traverse. Built anyway, as explicitly requested, with the limitation stated plainly
in both the README and the system prompt rather than glossed over.

Removed `_auto_rag()`, the `index`/`auto_rag_top_k`/`auto_rag_min_score` fields, and the
now-unused `CodebaseIndex` import from `agent/context_manager.py` entirely. Added
`set_focus_file()` (tracks a rolling window of files actually read/edited/written this session -
the new anchor) and `_dependency_context()` (walks `dependency_forward`/`dependency_reverse`
outward from those anchors, surfacing related files' content with a bounded preview length, pure
static analysis, zero embedding calls). Wired `set_focus_file()` into both loops' shared
`_record_tool_result()` helper, triggered by `read_file`/`edit_file`/`write_file`/
`read_document`/`list_symbols`/`read_files`. Added `project_root`, `dependency_forward`,
`focus_files_max`, `dependency_context_max_files`, `dependency_context_max_chars_per_file` to
`ContextManager`/`config.py`/`config.yaml`, removing the obsolete semantic-RAG settings.

**A real bug caught by testing before it shipped**: the first version had the relation labels
exactly backwards - a file `routes.py` imports was labeled "imports routes.py" instead of
"imported by routes.py". Traced to swapping which dict means which direction
(`dependency_forward[focus]` = files focus imports, so each one *is imported by* focus;
`dependency_reverse[focus]` = files that import focus, so each one *imports* focus) - fixed, and
re-verified both directions produce the correct label afterward.

Tested at every level: core traversal against real files with a real dependency graph (both
directions correct after the fix); the "no anchor yet" case correctly returns `None`; the rolling
focus-file window caps correctly and re-touching an older file moves it to front without
duplicating; a long related file truncates correctly; and a full loop-level test - read one file
in turn 1, confirmed its dependency automatically appeared in turn 2's context with the correct
label, no explicit tool call needed. `search_codebase` (the explicit, on-demand semantic tool)
is completely unaffected - only the automatic per-turn injection was replaced.

Updated the system prompt, and did a full sweep of the README for stale descriptions of the old
mechanism - two sections from #43 (confidence-adaptive retrieval breadth, multi-resolution
context) described logic built directly on top of `_auto_rag()`, which no longer exists; marked
both clearly as superseded rather than left describing dead code as if still current, matching
how this changelog handles anything rolled back or replaced elsewhere in this project.
- `agent/context_manager.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `agent/main.py`,
  `gui/server.py`, `agent/config.py`, `config.yaml`, `agent/prompts.py`, `README.md`

## 43. Context Budget Manager + confidence-adaptive retrieval + multi-resolution context + File Memory caching
A second architecture proposal was evaluated the same way as the first one (#42's predecessor
discussion): most of it (Intent Detection → Task Planner → {Episodic DB, Semantic DB, File
Memory} → Dependency Graph + AST → Prompt Compiler) mapped onto existing pieces under different
names. Intent Detection and a separate Reflection step were argued against again, same reasoning
as before - real extra inference round-trips on CPU-only hardware for something already handled
inline or by deterministic tools. Four genuinely new pieces got built.

**Context Budget Manager**: added `context_overhead_budget_tokens` (default 2000) - task
history/scratchpad/episodic memory are always included in full (small, high-priority), while File
Index and Project Knowledge now dynamically split whatever budget remains, implemented directly in
`build_prompt()`, replacing independent fixed caps that didn't account for what else was using
space. Tested directly: confirmed File Index shrinks proportionally (503→329 chars in one test)
when task history/scratchpad grow larger.

**Confidence-adaptive retrieval breadth**: rewrote `_auto_rag()` to search wider than
`auto_rag_top_k` first, then use the top result's own similarity score to decide how much to
actually keep - confident (≥0.6) shows fewer/tighter chunks, uncertain (<min_score+0.1) casts a
wider net. A cheap version of "progressive retrieval" needing zero extra model calls. Tested all
three tiers with precise controlled mock scores (2/5/3 chunks respectively) after an initial test
attempt using constructed embedding vectors ran into real-vector coincidences (orthogonal/negative
similarity edge cases) that made direct score control unreliable - switched to mocking
`index.search()`'s return value directly for precision.

**Multi-resolution context**: `_auto_rag()` now checks `dependency_reverse` for each retrieved
chunk's file and appends a one-line "(depended on by: ...)" note if other files depend on it -
connects three previously-separate features (auto-RAG, the dependency graph, file summaries) that
didn't talk to each other before. Verified in isolation with controlled test data; a full
realistic end-to-end attempt hit a toy fake-embedder artifact (a test file's own `import Product`
line accidentally matched the keyword being tested for) - confirmed this was a test-construction
flaw, not a logic bug, by re-verifying the isolated case still passes.

**File Memory caching**: `list_symbols` results are now cached by `(path, mtime)` in
`ToolRegistry._list_symbols_cache`, added to the constructor and cleared on reindex (dependencies
can change even when a file's own content doesn't). Verified by instrumenting `ast.parse` itself
to count real calls: a cache hit on an unchanged file produces zero re-parses; modifying the file
correctly invalidates the cache and re-parses.

**A real bug found and fixed while wiring dependency_reverse into ContextManager**:
`_tool_reindex_codebase` was reassigning `self._dep_forward`/`self._dep_reverse` to new dict
objects on every reindex, rather than mutating the existing ones. Since `ContextManager` holds a
reference to these same dicts for multi-resolution context, reassignment would have left it
permanently pointing at a stale, empty dependency graph after the first reindex in any real
session. Fixed to `.clear()` + `.update()` in place; verified directly by constructing a
`ContextManager` before any reindex, running a real reindex afterward, and confirming the
reference automatically reflected the update with no separate refresh call anywhere.

Full integration tested through the actual loop with a real `CodebaseIndex` and real dependency
graph: confirmed the assembled prompt correctly includes task history, scratchpad, and retrieved
project knowledge together; confirmed `list_symbols` shows both cached-and-correct dependency info
and file summary in one call; confirmed parallel tool execution (from #42) still works correctly
alongside all of this.
- `agent/context_manager.py`, `agent/tools.py`, `agent/main.py`, `gui/server.py`, `agent/config.py`,
  `config.yaml`, `README.md`

## 42. Parallel tool execution (real speedup, distinct from Claude-style subagent parallelism)
Requested after a prior discussion in this project's history concluded that Claude-style subagent
parallelism doesn't apply here - this agent has exactly one CPU/GPU and one model instance as its
whole compute budget, so parallel *model calls* just queue or time-slice with no real speedup.
What does genuinely parallelize: when the model returns several tool calls in one response, the
underlying work behind read-only ones (disk I/O, numpy math in `search_codebase`) releases
Python's GIL, so threads really do overlap even on one CPU.

Added `ToolRegistry._batch_is_parallel_safe()` and `execute_batch()`. Deliberately conservative
about what counts as safe: write/execute tools always force the whole batch sequential (side
effects, each needs its own uninterrupted permission prompt); read-only tools gated by
`request_read()` (`read_file`, `read_document`, `list_symbols`, `list_directory`, `db_schema`,
`db_query`) only parallelize once session-wide read trust is already established - otherwise,
concurrent threads could try to show overlapping permission prompts, which could genuinely garble
a terminal; `search_codebase`/`grep_codebase` never prompt at all, so they're always safe. A
single call, or any batch with even one unsafe tool, falls back to the exact prior sequential
behavior. Restructured both `agent/tool_loop.py` and `gui/agent_loop_gui.py` to parse all calls
in a turn upfront, check batch safety once, and branch - factored the per-result bookkeeping
(task history/scratchpad sync, memory storage, logging) into a shared `_record_tool_result()`
helper used by both paths so behavior is identical regardless of which one ran. Needed zero
system prompt changes - the model already could return multiple tool calls per turn; this only
changes execution speed, invisibly.

Tested at every level: the safety-gating logic across 5 cases (single call; always-safe tools;
read-gated with and without trust; a mixed batch with a write tool - all passed); a genuine
measured wall-clock speedup (5 calls at ~0.3s each: 1.50s sequential vs. 0.30s parallel, a real
5x); results preserved in original call order despite finishing in a different order; one call's
exception correctly isolated from the others; real disk I/O (unmocked) reading actual files
concurrently; and full loop-level tests through the CLI confirming both the parallel path
(3 real file reads, correct results landing in memory) and that a mixed read+write batch
correctly still falls back to identical sequential behavior with no parallel message shown.
- `agent/tools.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `README.md`

## 41. Solving memory pressure without lowering context_window
At the user's explicit request: solve the 500-error-under-memory-pressure problem (from #40)
without changing `context_window`. Two changes, addressing different layers.

**Documented the actual root-cause fix**: `context_window` sets the ceiling Ollama allocates for;
the RAM cost of that ceiling depends on bytes-per-token, which is independently controllable via
`OLLAMA_KV_CACHE_TYPE=q8_0` (or `q4_0`) plus the required `OLLAMA_FLASH_ATTENTION=1`, set on the
machine running the Ollama server before it starts. This roughly halves (or better) the memory
cost of the same configured window - the correct lever for "keep the ceiling, make it cost less,"
which this project's own code cannot set on the user's behalf since these are server-launch-time
environment variables, not request parameters.

**Built an automatic safety net inside the project itself**: `agent/ollama_client.py`'s `chat()`
and `chat_stream()` now retry a failed request exactly once, at half the context window, purely
as a per-request `num_ctx` override - `self.context_window` (what's in `config.yaml`) is never
touched. Added a `status_code` attribute to `OllamaError` (via `_request_error`) so this decision
doesn't need to parse error message text. Guarded carefully: only attempted for a 5xx status (not
a genuine connection failure - `chat_stream`'s new `_ctx_override` fallback path checks this
explicitly), only if zero content has streamed back yet (retrying after partial output could
duplicate or garble what's shown), and bounded to exactly one attempt (no recursive spiral through
ever-smaller windows). Surfaced as a new `context_fallback` event, handled visibly in both loops
(a yellow notice in the CLI, a status event in the GUI) rather than happening silently.

Tested directly against the client with mocked HTTP responses, not just designed and assumed:
confirmed a 500-then-success sequence retries at exactly half the context and leaves
`context_window` unchanged afterward; confirmed a genuine connection failure does NOT trigger a
wasted fallback attempt; confirmed a 500 arriving after partial content correctly does NOT retry;
confirmed a 500 that persists even at the smaller fallback stops after exactly one attempt.
Also ran a full loop-level test (not just the raw client) confirming the fallback event displays
correctly and the turn completes normally afterward.
- `agent/ollama_client.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `README.md`

## 40. Fixed conflated error messages: HTTP 500 was shown as "could not reach"
A user's screenshot showed a self-contradicting message: "Could not reach a local Ollama server"
alongside a "500 Server Error" detail. A 500 status means Ollama DID respond - it's running and
reachable, the request itself failed on Ollama's side - a fundamentally different problem from a
real connection failure, needing different troubleshooting advice entirely. The bug:
`requests.HTTPError` (raised by `raise_for_status()` for any non-2xx response, including a 500)
was being caught by the exact same `except requests.RequestException` handler as a genuine
connection failure (refused/timeout/DNS), in `chat()`, `chat_stream()`, and `embed()`.

Added `_request_error()`, which checks for a response object on the exception - if present, it's
not unreachability. For 5xx specifically, the new message says so plainly and points at the most
likely real cause given this project's hardware profile: running out of memory while processing a
large context, naming `context_window` in `config.yaml` directly, since an intermittent 500 is a
much more plausible symptom of that than of a firewall issue, especially given the recent bump to
a much larger window. Surfaces Ollama's own response body when available and points at checking
Ollama's own logs, since this HTTP client genuinely can't see why Ollama's process failed, only
that it did. Tested against both a simulated real 500 (confirmed the new, correctly-targeted
message) and a genuine connection failure (confirmed the original checklist is unchanged for that
case - purely additive, not a regression risk).

Also investigated the same screenshot's apparently-stuck "Agent is working" bar: checked every
path in both `gui/agent_loop_gui.py` and `gui/static/app.js` that should clear it, and every one
correctly fires/handles `turn_complete`. Most likely a screenshot catching the async gap between
an error rendering and its `turn_complete` event landing, not a persistent bug - flagged as
unconfirmed rather than fixed, since it couldn't be reproduced directly.
- `agent/ollama_client.py`, `README.md`

## 39. Dependency graph + file summaries + episodic memory
Implemented all three gaps identified when evaluating a proposed alternative architecture against
the existing one - most of that diagram turned out to already exist under different names
(Task Planner=`update_plan`, Working Memory=Scratchpad+Task History, Context Planner=Context
Selector, code embeddings=Project Knowledge, Prompt Assembler=`build_prompt()`,
Validator/Tests=syntax checks+`run_tests`), and a few pieces (a separate Intent Classifier/Task
Planner model call, a distinct "Reflection" step) were argued against specifically because extra
inference round-trips are expensive on a 9B CPU-only setup in a way they wouldn't be on a
cloud-hosted frontier model.

**Dependency graph + file summaries were folded into the existing `list_symbols` tool rather than
becoming two new tools** - deliberately, given this project already learned that tool count hurts
reliability on a small model. `build_dependency_graph()` (`agent/indexer.py`) does pure static
analysis (Python via `ast`, JS/TS via regex) with no LLM calls, rebuilt on every reindex.
`list_symbols` now shows a one-line summary (docstring/leading comment) plus what a file imports
and what imports it, before the existing symbol list. Tested against real files with genuine
import relationships: confirmed forward and reverse dependencies resolve correctly for both
Python and JS, and confirmed the combined output renders correctly in one call.

**Episodic memory is a genuine seventh context store**, the first thing in this project that
survives across separate sessions rather than resetting when the process closes. The Scratchpad
now persists to `.local_agent/SCRATCHPAD.md` on every update and reloads automatically next
session; a rolling episode log (`.local_agent/EPISODES.md`) gets one deterministic, no-LLM-call
summary line appended per session (derived from the final plan state via
`summarize_task_history_for_episode()`), with the last few shown at the start of future sessions.
Tested as an actual two-session simulation: session 1 learned facts and worked partway through a
plan, then ended; a fresh `ContextManager` instance confirmed both the scratchpad and episode
summary loaded correctly AND actually reached the assembled prompt (not just internal state);
session 2 continued and ended, correctly appending rather than overwriting; a separate test
confirmed the episode log caps at the most recent entries rather than growing unbounded.

Flagged directly in the README: this sits close to the hard-reset mechanism rolled back in #38,
but is a different concern (cross-session continuity vs. within-session context pressure),
requested explicitly with that adjacency already known.
- New: nothing (all additions to existing modules)
- Touched: `agent/indexer.py` (`build_dependency_graph`), `agent/tools.py` (dependency graph
  storage, enhanced `list_symbols`, updated `update_scratchpad` description), `agent/context_manager.py`
  (episodic memory store, `load_episodic_memory`/`record_episode`/`summarize_task_history_for_episode`),
  `agent/main.py`, `gui/server.py`, `agent/config.py`, `config.yaml`, `agent/prompts.py`, `README.md`

## 38. Rolled back #37 (persisted plan file + hard context reset) at the user's request
The user asked to discard #37 and return to the prior version, without giving a specific reason.
Reverted fully rather than partially: removed `plan_file_path`/`context_window`/
`hard_reset_fraction` and the `Path` import from `agent/context_manager.py`, removed
`_persist_plan_file()`/`needs_hard_reset()`/`hard_reset()` entirely, reverted `update_task_history()`
to its pre-#37 form, removed the hard-reset check block from both `agent/tool_loop.py` and
`gui/agent_loop_gui.py`, reverted the `ContextManager(...)` construction calls in `agent/main.py`
and `gui/server.py`, removed the two new properties from `agent/config.py`, removed the two new
settings from `config.yaml`, and removed the README section describing the feature (since the
README should describe what the shipped code actually does, not a feature that no longer exists
in it). Confirmed with a full-codebase search afterward that zero references to `hard_reset` or
`plan_file_path` remain anywhere in `agent/` or `gui/`.

Entry #37 below is left intact rather than deleted - it's an honest record that the feature was
designed, built, and tested, even though it didn't end up staying. The behavior is back to
exactly what #36 shipped: Task History lives in memory only again (no file persistence), and
`compact()` remains the only context-management mechanism (no separate hard-reset tier).

## 37. Persisted plan file + automatic hard context reset for very long tasks
At the user's request: the plan should survive running out of context, not just live in memory,
so a long multi-step task can keep going indefinitely instead of the agent "feeling lost" once
context fills up. Building this surfaced a real gap in the existing architecture: soft compaction
(`compact()`, from #28) summarizes old messages into a growing summary string once raw chat
exceeds `history_soft_limit_tokens` - but the summary itself was never capped, so across many
compactions on a very long task, it could keep growing indefinitely. That's the same failure mode
being addressed here, just one soft compaction alone doesn't actually solve.

Two additions: `ContextManager.update_task_history()` now also calls `_persist_plan_file()`,
writing the plan to a real file (`.local_agent/PLAN.md`, configurable via `agent.plan_file_path`)
in the same `[x]`/`[~]`/`[ ]` checklist format already shown in-context - readable with a plain
`read_file` call, no special tool needed. `needs_hard_reset()` checks raw chat AND the
accumulated summary together against a threshold near the actual `context_window` ceiling
(`agent.hard_reset_fraction`, default 0.85) - distinct from and higher than the existing soft
limit, and accounting for summary growth which the soft-compaction trigger doesn't. `hard_reset()`
wipes both messages and summary completely (not just trims) and adds a message pointing the model
at the plan file to find the next incomplete step. Wired into both loops at the top of each
iteration, bounded to once per turn, and - critically - the loop continues automatically in the
same turn afterward rather than waiting for a new user message, so a long task keeps making
progress without the user needing to re-prompt it.

Tested in three stages: plan persistence produces a clean, human-readable file on disk; the
threshold correctly stays false with little history and flips true once padded past it, and
`hard_reset()` genuinely empties both messages and summary while leaving the plan file completely
untouched; then the full loop end-to-end - forced a reset mid-turn, confirmed the very next model
call received a prompt with only 2 messages (down from 30+) containing the reorientation note and
plan file path, and confirmed a scripted model reacting to that note (reading the plan file)
flowed through to a normal completed turn with no further user input needed. Deliberately NOT
added to the persistent system prompt - the reorientation message delivers the instruction
exactly when a reset happens, so there's no reason to spend tokens on every turn explaining a
mechanism that only matters occasionally.
- `agent/context_manager.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `agent/main.py`,
  `gui/server.py`, `agent/config.py`, `config.yaml`, `README.md`

## 36. Session logs + reduced redundant replies + fixed a packaging bug
Three changes at the user's request, plus one bug found while working on them.

**Session logs**: added `agent/session_log.py`'s `SessionLogger`, writing every session's activity
(user messages, tool calls and results, final responses, timestamped) to
`.local_agent/sessions/session_<timestamp>.log` - the one durable record of a session now that
everything else lives only in memory. Flushes after every write rather than buffering; tested by
killing a logger mid-session with no clean shutdown at all and confirming everything written so
far was still fully readable on disk. Wired into both loops (`AgentLoop`, `GuiAgentLoop`) and both
entry points (`main.py`, `gui/server.py`/`gui/launch.py`, including a `close_session()` hook so the
GUI gets a proper closing entry when the pywebview window closes or Ctrl+C is pressed in the
browser-fallback case). Verified end-to-end through a real turn with an actual tool call, not just
the logger in isolation.

**Reduced redundant replies**: added one tight clause to the system prompt (folded into an
existing sentence, not a new paragraph - mindful of #33's lesson) telling the model not to
restate a plan/intention across turns and to prioritize delivering the actual output over
describing it. Targets the exact pattern from an earlier bug report's screenshot: the agent
repeating close variations of "I'll create a comprehensive PostgreSQL schema..." across 3-4 turns
without the schema ever getting built. Still only a ~7% prompt size increase (944→1017 tokens) -
the 69% reduction from #33 still holds.

**Bug found and fixed**: `.gitignore` was missing from the project entirely. Traced it to the
zip-packaging command used to hand over each update - `-x '*.git*'` was meant to exclude a `.git/`
directory, but that glob also matches `.gitignore` itself, so every zip shipped since has silently
dropped it. No `.git/` directory has actually existed in this project at any point, so the
exclude flag was pure downside with no offsetting benefit - recreated the file and dropped the
flag from future zips.
- New: `agent/session_log.py`
- Also touched: `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `agent/main.py`, `gui/server.py`,
  `gui/launch.py`, `agent/prompts.py`, `.gitignore` (recreated), `README.md`

## 35. GUI input blocking + real context deduplication + context_window to 20000
Three related changes requested together.

**GUI loading/input-blocking**: the Send button being disabled while a turn was in progress
wasn't sufficient - the text input itself remained typeable, so a message typed and sent then
would silently do nothing. Fixed by applying the native HTML `disabled` attribute to the input
itself (genuinely un-typeable, not just a JS-level check that silently no-ops) plus a visible
"Agent is working" bar with a pulsing-dots CSS animation so the reason is clear rather than the
input just going quietly grey. Confirmed the CLI needed no equivalent change: its REPL loop is
fully synchronous, so there's structurally no way to type during a turn - the terminal isn't even
prompting for input until `run_turn()` returns.

**New Context Selector optimization - deduplicating superseded reads**: added
`build_tool_message()` (tags stored tool results with an identifiable key extracted from their
arguments, e.g. `[read_file|app.py]`, since a tool's own result text never repeats the path it
read) and `_dedupe_superseded_reads()` (if the same read-type tool was called with the same target
more than once, only the last call's full content is sent to the model - earlier ones collapse to
a short placeholder). Deliberately scoped to read-only tools (`read_file`, `read_document`,
`read_files`, `list_symbols`, `list_directory`, `db_schema`, `search_codebase`, `grep_codebase`) -
write/execute tools are never touched, since repeating those is meaningful history, not waste.
Only affects what's assembled for the model THIS turn; `self.messages` itself (used for
compaction and the true record) is completely unaffected. Tested against the exact repeated-read
pattern from an earlier bug report (same file read 3 times): confirmed only the last read
survives in full (saved ~2,255 tokens in that specific test), confirmed different files stay
fully independent of each other, confirmed write/edit/run_command are never collapsed under any
circumstance, and confirmed the full loop integration (not just the context manager in isolation)
produces the same correct behavior.

**context_window to 20000** (from 8192), at the user's explicit request - flagged clearly before
making the change that this is a much bigger jump than the earlier 6144→8192 one and meaningfully
increases the 9B model's RAM footprint on already-tight 8GB/no-GPU hardware; included because it
was explicitly requested with a specific number, not because it's risk-free. Rebalanced
`history_soft_limit_tokens` to 14000 (from 6000) accordingly, leaving ~6000 tokens of headroom for
the system prompt, file index, task history, scratchpad, auto-retrieved project knowledge, and
the model's own response generation combined - a considerably better ratio than before, helped
both by the larger window itself and by #33's system-prompt condensing.
- `gui/static/index.html`, `gui/static/style.css`, `gui/static/app.js`, `agent/context_manager.py`,
  `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `config.yaml`, `agent/config.py`, `README.md`

## 34. Fixed read_document's silent, unrecoverable truncation
A user's screenshot showed the agent calling `read_document` on the same file twice with
identical arguments, getting identical truncated content back both times, and correctly
self-diagnosing out loud that the tool "is returning the same content each time." It was right:
`read_document` capped every call to the first 20,000 characters with `text[:20000]` and had no
parameter to ever request anything past that point, no matter how it was called.

Added an `offset` parameter, mirroring the fix already used for `read_file`'s `start_line`/
`end_line`: still capped at 20,000 characters per call, but the result text now states the exact
offset to pass next if the document continues, and using that offset returns genuinely different,
advancing content instead of repeating. Tested against a real 57KB text file: confirmed identical
arguments still (correctly) produce identical output - a deterministic function repeating its
input isn't the bug - but following the tool's own suggested offset advances correctly; confirmed
the final chunk doesn't suggest a nonexistent further offset; confirmed requesting an offset past
the end of the document says so plainly; confirmed a short document that fits in one call gets
clean output with no pagination clutter added at all. Added one clause to the existing system
prompt efficiency paragraph (not a new paragraph, mindful of #33's lesson about cumulative cost) -
71% reduction from the original prompt size still holds, barely nudged by this addition.

The same screenshot showed the agent apparently freezing mid-response afterward. Flagged plainly
that this fix addresses part of what could cause that (duplicate 20,000-character tool results
bloating the conversation for no benefit) without overclaiming it as the full explanation - a 9B
model generating a long response on CPU with no GPU can also just genuinely be slow, which can
look identical to frozen without a clear sense of how long to expect.
- `agent/tools.py`, `agent/prompts.py`, `README.md`

## 33. Condensed the system prompt - found it had grown to ~3,300 tokens of pure overhead
A user's screenshot showed the agent repeatedly narrating intentions across many turns without
ever building anything, needing to be prodded ("Start", "Build it", "Ok build it") each time -
and, critically, the agent said "as mentioned in rule 23," citing its own system prompt's rule
numbering back to the user in conversation. That was the real diagnostic signal. Measured the
actual prompt: 32 numbered rules (with sub-numbering like 8b/8c/8d from prior fixes), 13,223
characters, ~3,305 tokens - over 40% of the entire 8192-token context window consumed before any
real conversation, a genuine and quantified cause of degraded tool-calling reliability on a 9B
model, not a guess. Each individual addition across updates #9-#32 was reasonable in isolation;
none of them were ever weighed against the cumulative cost.

Rewrote it from a numbered list into grouped prose, cutting the verbose "why" behind each rule
(kept in this changelog/README for a human reader, not sent to the model every turn) while
preserving every behavioral safeguard. Moved away from explicit numbering specifically because of
the "rule 23" incident - a numbered list invites a model to treat the numbers as citable content.
Verified nothing was silently lost: a systematic check of every tool name mentioned in the old
prompt against the new one caught two real gaps in the first pass (the ambiguous-request
clarifying-question guidance, and the "explain your changes" guidance had both been dropped;
`db_schema` usage guidance was also missing) - all three were restored and reconfirmed present.
Final size: ~900 tokens, a 73% reduction. Measured the full assembled context too (condensed
prompt + a populated file index, task plan, and scratchpad): ~1,000 tokens total, versus the old
system prompt alone costing 3,300 - roughly 7,000 tokens of real headroom restored for actual
conversation instead of the window being nearly exhausted before a session even starts.
- `agent/prompts.py`, `README.md`

## 32. Generalized the "announced but uncalled tool" detector to natural narration
A second report of the same underlying failure from #29, but with different phrasing that the
existing fix completely missed: "I am reading the file" narrates the same undelivered action as
"I will use read_document to...", but never names a tool or matches any of the specific intent
phrases #29's detector looked for - confirmed with a direct test that the old detector returned
None for it. The user's second symptom ("which file are you talking about?" on a later turn) is
exactly the downstream consequence of the first narration never actually happening.

Rebuilt `find_announced_but_uncalled_tool()` as two tiers instead of one: Tier 1 keeps the
original precise check (an exact tool name right after "I will use"/"let me use"/etc.). Tier 2
is new - it catches present/future-tense action narration ("I am reading", "let me open", "now
checking") near a reference to "the file"/"the document"/an actual filename, without needing the
text to name a specific tool at all. Built from explicit (verb, tense-prefix) phrase
combinations rather than bare verb-stem matching specifically to avoid a real false-positive risk:
a stem search for "read" would incorrectly match past-tense mentions like "I already read the
document" too. Tested against 8 natural positive phrasings (all correctly detected, including the
exact reported "I am reading the file now"), the original Tier 1 case (still works), 4 past-tense
negatives (correctly silent), and 3 no-file-reference negatives (correctly silent) - 16 cases
total, all passing. Also reworded both loops' nudge message to read naturally for Tier 2's
generic "a file-reading tool" description, not just Tier 1's specific tool name, and added system
prompt rule 7 explicitly connecting the two symptoms (narrating a read vs. actually calling it)
so the model understands why this matters beyond the immediate turn.
- `agent/tools.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `agent/prompts.py`

## 31. Addressed "the agent always says it can't remember the last response" (hypothesis-driven)
A user reported this without a specific reproduction, so - unlike most bug fixes in this project,
which followed a confirmed root cause - this one started with an investigation. Tested the actual
context-assembly mechanics exhaustively: a basic two-turn exchange, a turn that goes through a
tool call, a turn that goes through the "announced but uncalled tool" nudge (#29), the GUI
server's state persistence across requests (confirmed `_state["loop"]` is created once and reused,
not recreated per-request), and compaction under real trimming pressure (confirmed the most recent
response survives even when older messages get summarized away). Found no bug in any of them - the
model's context genuinely contains its own previous response in every scenario tested.

This points at a well-documented LLM behavior instead: models are often trained to disclaim
memory of past conversations as a statement about NOT persisting state between separate sessions
(true) - but the same canned disclaimer sometimes gets applied even when asked about something
sitting in the CURRENT conversation's visible context (false; conflating the two). Added system
prompt rule 8d addressing this directly: if asked whether something from earlier in the same
conversation is remembered, check the actual messages first rather than defaulting to a generic
"I don't have memory" response - that disclaimer is about cross-session persistence, not
in-context visibility.

Flagged clearly to the user that this is the most likely explanation given what testing could
rule out, not a confirmed fix the way the tool-registration and announced-but-uncalled-tool bugs
were - both of those were only pinned down for certain once real reproduction text was available.
- `agent/prompts.py`

## 30. context_window and history_soft_limit_tokens set explicitly
At the user's request: `context_window` 6144→8192 (Ollama's un-trimmed default for this model -
fully reverses the RAM-saving trim made for the 9B model swap), `history_soft_limit_tokens`
3000→6000. Flagged before making the change, not after: with the context architecture from #28,
these two numbers leaving ~2192 tokens of headroom is shared across five things now (system
prompt, file index, task history, scratchpad, auto-retrieved project knowledge), not just a
system prompt like when 8192/6000 was the original pairing - usually enough, but tighter than the
same numbers looked before #28. Also fixed a latent inconsistency this surfaced: `config.py`'s
`context_window` fallback default was still `4096` (never updated when `config.yaml` was bumped
to `6144` in #28) - now matches at `8192`. `history_soft_limit_tokens`'s fallback had actually
been correct at `6000` the whole time; it was `config.yaml` that had drifted from it.
- `config.yaml`, `agent/config.py`, `README.md`

## 29. Detect and recover from "announced but never called" tool use
A user reported (with a screenshot of their own real session) the agent saying "I will use
read_document to open and analyze..." and then never actually reading anything - later turns
confirmed no content was ever retrieved. This is a distinct failure from the earlier "tool
registration" bug (#27, an argument-error message problem): here, no tool call was ever attempted
at all, the model just described an intention in plain text and the turn ended having done
nothing, while reading as if something had happened.

Added `find_announced_but_uncalled_tool()` (`agent/tools.py`), a conservative heuristic that
checks the model's text for an intent phrase ("I will use", "let me use", etc.) immediately
followed by a real tool name, when no tool call was actually made that turn. When detected, both
loops inject one bounded corrective reminder ("you said you would use X but didn't call it - do
so now") and give the model exactly one more attempt before giving up gracefully, rather than
looping forever if it doesn't recover. Also added system prompt rule 8c stating this directly:
saying you'll use a tool doesn't make it happen, only the actual tool call does.

Tested by reproducing the exact reported scenario end-to-end (with a mocked model matching the
screenshot's text verbatim) in both the CLI and GUI loops: confirmed recovery works when the
model corrects itself after the nudge (the tool actually gets called and its result flows through
normally), and separately confirmed the bound holds - a model that never recovers gets exactly
one nudge, not an infinite loop, and the turn ends with whatever text the model left rather than
hanging. Also spot-checked the detector against phrases that shouldn't trigger it (past-tense "I
read the document", "let me know if you have questions about read_file") to guard against false
positives nudging a turn that didn't need it.
- `agent/tools.py`, `agent/tool_loop.py`, `gui/agent_loop_gui.py`, `agent/prompts.py`, `README.md`

## 28. Context management architecture rework
Replaced `agent/memory.py`'s single growing-message-list-plus-summary approach with
`agent/context_manager.py`'s six-store architecture, at the user's request to follow a specific
diagram: Recent Chat, Long-term Memory, Task History, Scratchpad, File Index, and Project
Knowledge, feeding a Context Selector (`select_context()`) and then a Prompt Builder
(`build_prompt()`). Task History mirrors the existing `update_plan` state so it survives
summarization. Scratchpad is a new store fed by a new `update_scratchpad` tool (built using the
same flat-string-array pattern and defensive-argument handling already proven for `update_plan`,
for the same tool-calling-reliability reasons). File Index is a new cheap, cached directory
listing, refreshed on reindex. Project Knowledge reuses the existing semantic codebase index, but
now the Context Selector runs it proactively against the user's last message every turn (not just
when the model remembers to call `search_codebase`), skipping it entirely below a similarity
threshold rather than always injecting something.

Also bumped `context_window` 4096→6144 as requested ("a bit more"), and rebalanced
`history_soft_limit_tokens` 6000→3000 to actually fit under it - this exposed a real latent
miscalibration: the old 6000-token soft limit for raw chat history already exceeded the entire
4096-token window on its own, before a system prompt was even added, which the new context
blocks' fixed overhead made impossible to ignore any longer.

Tested end-to-end with genuine SQLite indexing and real cosine-similarity math (hand-picked
embedding vectors standing in for a live Ollama call, so the relevance math itself is real, not
mocked): a "cart totals" query correctly retrieved the matching function and left unrelated code
out; an unrelated query correctly retrieved nothing, confirming the relevance threshold actually
filters; the full six-store pipeline assembled correctly into one system prompt; compaction and
the `as_chat_messages()` convenience wrapper both still work exactly as before; and the
tool-call-to-context-manager sync pattern used by both the CLI and GUI loops was verified
directly. `agent/main.py` and `gui/server.py` were updated to construct `ContextManager` (passing
the codebase index for auto-retrieval and building the initial file index) in place of
`ConversationMemory`, and both loops now sync Task History/Scratchpad after `update_plan`/
`update_scratchpad` calls. `memory.py` was removed - nothing referenced it any longer.
- New: `agent/context_manager.py`
- Removed: `agent/memory.py`
- Also touched: `agent/indexer.py` (file index builder), `agent/tools.py` (`update_scratchpad`
  tool, `search_codebase` description update), `agent/tool_loop.py`, `gui/agent_loop_gui.py`,
  `agent/main.py`, `gui/server.py`, `agent/config.py`, `config.yaml`, `agent/prompts.py`,
  `README.md`

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
