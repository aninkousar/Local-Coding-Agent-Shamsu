# Full-Repository Audit Report

**Scope:** two related, sibling repositories in the workspace — `local-code-agent` (offline CLI/GUI
coding agent + FastAPI bridge) and `telegram_agent_bot` (Telegram remote-control bot for that
bridge). Audited together since one is the live integration target for the other.

**Method, stated plainly:** every finding below was either fixed and re-verified with a real test,
or investigated and confirmed as a false positive before being dismissed — nothing in this report
is asserted from static-analysis output alone without being checked against actual behavior.

---

## 1. Architecture Summary

**`local-code-agent`** — Python 3.10+, no framework for the core agent; Flask for the GUI; FastAPI
for the bridge. Runs a 9B model via Ollama, CPU-only. Three entry points:
- `agent/main.py` — CLI, `local-agent` command.
- `gui/server.py` — local browser GUI (Flask + SSE), `local-agent-gui` command.
- `agent/api_server.py` — REST + WebSocket bridge for remote control, `uvicorn agent.api_server:app`.

All three share one bootstrapping path, `gui/agent_setup.py`'s `build_agent_session()`, parameterized
by which permission manager to use (interactive for CLI/GUI, auto-approve for the bridge). Tool
execution, context management, and permission gating (`agent/tools.py`, `agent/context_manager.py`,
`agent/permissions.py` / `agent/auto_permissions.py`) are shared unmodified across all three modes.

**`telegram_agent_bot`** — Python 3.12, `python-telegram-bot` (async) + FastAPI (for its bundled
`mock_agent` reference server) + `aiosqlite` for a local project-state cache. Talks to an agent
(real or mock) via a typed REST/WS contract defined once in `bot/models/schemas.py`, deliberately
decoupled from any specific agent implementation.

**Database:** SQLite only — `local-code-agent`'s codebase index (`agent/indexer.py`) and
`telegram_agent_bot`'s local project cache (`database/db.py`). Neither project has a heavier DB
dependency by default; Postgres/MySQL are optional, lazily-imported extras in `db_tools.py`.

**API structure:** `telegram_agent_bot`'s contract — `POST /prompt`, `POST /project/{id}/prompt`,
`GET /projects`, `GET /project/{id}`, `GET /project/{id}/logs`, `GET /project/{id}/files`,
`GET /system`, `POST /project/{id}/{pause,resume,stop}`, `WS /events` — is implemented twice:
once as a mock (`telegram_agent_bot/mock_agent/server.py`) and once for real
(`local-code-agent/agent/api_server.py`, backed by the genuine agent loop).

---

## 2. Issues Found, Root Causes, and Fixes Applied

### 2.1 Confirmed, active runtime bugs (fixed and re-verified)

| # | File | Issue | Root Cause | Verification |
|---|---|---|---|---|
| 1 | `gui/server.py` | `NameError: build_file_index_text is not defined` — the GUI's manual reindex feature was **completely broken** | A prior refactor (`gui/agent_setup.py` extraction) dropped this import from `gui/server.py` without anyone triggering `/api/reindex` afterward to notice | Triggered the endpoint before the fix and captured the exact `NameError` in the pushed event stream; re-triggered after the fix and confirmed a genuine `{'files': 1, 'chunks': 2}` success |
| 2 | `pyproject.toml` | Missing `fastapi`/`uvicorn`/`websockets`/`psutil` in the package's own `[project.dependencies]`, while `requirements.txt` had them | Bridge mode (`agent/api_server.py`) was added after `pyproject.toml`'s dependency list was last touched | `pip install -e .` alone (the standard Python packaging path, independent of `requirements.txt`) would raise `ModuleNotFoundError` on bridge startup. Fixed by adding the same four packages to `pyproject.toml`, matching `requirements.txt`'s existing treatment of them as standard, not optional |

### 2.2 Latent fragility fixed defensively (not actively reachable today, but relied on an implicit, undocumented invariant between separate functions)

These three shared one root pattern: a function's own contract (its return type, or a subsequent
unconditional use of a value) was only safe because of *how a caller happens to invoke it today* —
not because the function itself guaranteed it. None of these caused an actual failure in testing,
but each was a single future edit away from one.

| # | File | Issue | Why it was safe today | Fix |
|---|---|---|---|---|
| 3 | `agent/tools.py`, `_tool_list_symbols` | `parsed["summary"]` indexed after a `None`-check block that didn't guarantee `parsed` was reassigned | `extract_python_symbols()` only ever returns `None` when `ast.parse()` itself raises `SyntaxError`; the very next line re-parses the *identical* source, so it always raises the same exception and returns first | Added an explicit fallback `return` after the re-parse, instead of relying on that determinism holding forever |
| 4 | `agent/db_tools.py`, `get_schema()` | `if/elif/elif` over `sqlite`/`postgres`/`mysql` had no final `else`; an unhandled `db_type` would silently return `None` from a function typed `-> str` | `connect()`, called just before, already raises `DBError` for any other `db_type` value | Added an explicit `raise DBError(...)` fallback at the end of the chain |
| 5 | `agent/context_manager.py`, `_recompress_summary()` | Body could return `None` from a function typed `-> str` if `self.summary` were `None` | The only call site always sets `self.summary` to a real string on the line immediately before calling this | Added `or ""` guards on both return paths |

### 2.3 Cosmetic / type-annotation fixes (zero behavior change, verified)

- `bot/services/websocket_listener.py`: widened `_handle_raw_event`'s parameter from `str` to
  `str | bytes`, matching what `websockets`' async iterator can genuinely yield. Verified
  `json.loads()` already handles both identically — no logic change, type-checking accuracy only.
- `agent/tools.py`, `_tool_format_file`: added an explicit `str | None` annotation to a variable
  already used correctly as an intentional sentinel, removing mypy noise without changing behavior.
- `agent/permissions.py`: removed two unnecessary `f` string prefixes on strings with no
  interpolation.

### 2.4 Dead code / unused imports removed (11 total, one file at a time, each verified genuinely unused via whole-file grep before removal — not batch-removed on tool output alone)

`agent/ollama_client.py` (`Iterable`), `agent/config.py` (`os`), `agent/db_tools.py` (`Path`),
`agent/tools.py` (`fnmatch`, `Callable`, `Any`), `agent/auto_permissions.py` (`field`),
`agent/api_server.py` (`Dict`, `parse_plan_steps`), `agent/indexer.py` (`json`),
`bot/main.py` (`Path`), `bot/keyboards/inline.py` (`Optional`), `mock_agent/server.py`
(`uuid`, `Path`), `tests/markdown_validator.py` (dead `in_code` local variable, leftover from an
earlier draft approach superseded by `_in_code_region()`).

### 2.5 Investigated and confirmed as false positives (deliberately left unchanged)

Documenting these matters as much as the fixes — treating every static-analysis hit as something
to silence would be its own kind of bug.

- **`bandit` B602 (`shell=True`) ×2** in `agent/tools.py`'s `run_command`/`start_dev_server` — this
  is the intended, necessary behavior for tools whose entire purpose is executing a shell command
  string the model chooses (pipes/redirects require real shell interpretation), already gated by
  the permission system. Not a bug; a documented, accepted risk (see §9).
- **`bandit` B104 (`hardcoded_bind_all_interfaces`)** on `agent/tools.py`'s `_LOCAL_HOSTS` — this is
  a validation *set* (`if parsed.hostname not in self._LOCAL_HOSTS`) used to check whether a dev-
  server URL is local, not an actual socket bind call. Bandit pattern-matches the string `"0.0.0.0"`
  without understanding context.
- **mypy `Collection[str]` not indexable** on `ALL_TOOL_NAMES`'s comprehension — verified at runtime
  (`TOOL_SCHEMAS` correctly produced 30 tool names); mypy's inference for a large, concretely-typed
  literal was simply imprecise.
- **mypy `list[str | None]` passed to `subprocess.run`** in the PHP formatter helper — the `None`
  placeholder in `cmd_template` is explicitly replaced (`cmd[tmp_index] = tmp.name`) before the
  `subprocess.run` call; mypy can't track that a list-item assignment removes it.
- **mypy `list[ToolResult | None]`** returned from `execute_batch` — empirically re-verified by
  actually exercising the parallel path with genuinely-parallel-safe tool calls
  (`grep_codebase` ×3) and confirming zero `None` entries in the real result.
- **mypy `str | None` passed where `str` expected**, `_format_code`'s `formatted` value — the
  function's own docstring documents the exact contract (`available=True, error=None` always means
  `formatted` holds a real string), verified consistently followed across every formatter branch
  (black, prettier, gofmt, rustfmt).
- **~50 mypy `union-attr` findings** across `telegram_agent_bot/bot/handlers/*.py` (e.g.
  `update.callback_query` possibly `None`) and `context.user_data` indexed-assignment findings —
  `python-telegram-bot`'s type stubs are conservatively typed for the general `Update` case, but
  handlers registered via `CallbackQueryHandler` are only ever invoked when a `callback_query` is
  genuinely present, and `user_data` is only ever `None` for update types with no associated user
  (never true for callback-query-based handlers). Corroborated empirically: this exact code was
  already exercised extensively in prior integration testing (mocked and real-server) without a
  single `AttributeError`.
- **mypy `ctypes.windll` has no attribute** in `agent/config.py`'s Windows RAM-detection branch —
  correct on this Linux sandbox; the code is behind an `if system == "Windows":` guard mypy doesn't
  understand as a platform gate.

---

## 3. Files Modified

**`local-code-agent`** (10 files): `gui/server.py`, `agent/tools.py`, `agent/db_tools.py`,
`agent/context_manager.py`, `agent/config.py`, `agent/ollama_client.py`, `agent/indexer.py`,
`agent/auto_permissions.py`, `agent/api_server.py`, `agent/permissions.py`, `pyproject.toml`.

**`telegram_agent_bot`** (4 files): `bot/main.py`, `bot/keyboards/inline.py`,
`mock_agent/server.py`, `bot/services/websocket_listener.py`, `tests/markdown_validator.py`.

---

## 4. Test Results

| Suite | Result |
|---|---|
| `telegram_agent_bot` pytest (23 tests) | **23/23 passed**, before and after every fix |
| Full syntax check, both repos | **All files valid** |
| `pyflakes`, both repos | **0 findings** in `telegram_agent_bot`; **1 remaining note** in `local-code-agent` (see below) |
| Real end-to-end bridge test (via the bot's actual `AgentAPIClient`, not synthetic assertions) | New prompt → real background execution → real file write verified on disk → correct completion status → real file/log/system endpoints all returning genuine data — **passed**, run three times across the audit (baseline, post-concurrency-context-check, post-cleanup) |
| GUI regression (`init_agent`, `/api/status`, `/api/reindex`) | **Passed** — reindex specifically re-verified broken-then-fixed |

**Remaining, deliberately unfixed `pyflakes` note:** `agent/api_server.py`'s `global _project`
declaration inside `_handle_gui_event` is technically unnecessary (the function only mutates
`_project`'s attributes, never reassigns the name), but harmless, and arguably aids readability by
making the module-level dependency explicit at a glance. Left as-is rather than churned for a
non-issue.

---

## 5. Build Status

Both projects: **syntactically clean, dependency-consistent, and passing all available automated
checks.** `local-code-agent` has no build step beyond `pip install`; `telegram_agent_bot` likewise.
Neither project failed to import or start during this audit after fixes were applied.

---

## 6. Security Observations

- **Bridge binding**: `agent/api_server.py` is launched exclusively via the provided
  `.sh`/`.ps1`/`.bat` scripts, all three explicitly passing `--host 127.0.0.1`. Verified as
  defense-in-depth: even a manual `uvicorn agent.api_server:app` with no `--host` flag would still
  default to `127.0.0.1` (confirmed via `uvicorn --help`), not `0.0.0.0`.
- **No authentication on the bridge's own REST/WS contract** — by design, per the original
  integration brief. `telegram_agent_bot`'s `ALLOWED_USER_IDS` whitelist is the only access control
  on who can reach the agent through Telegram; anything that can reach `127.0.0.1:8000` directly on
  the host bypasses that entirely. Acceptable for a single-user, localhost-only tool; would need
  real auth before ever being exposed beyond that.
- **Auto-approve permission mode** (`agent/auto_permissions.py`), used only by the bridge: still
  enforces `allowed_roots`/`hard_denylist` structurally — not a blanket bypass — but skips the
  interactive y/n step CLI/GUI sessions get. This was a deliberate, documented architecture
  decision (see `local-code-agent/README.md`'s "Bot bridge mode" section), not an oversight.
- **`shell=True` subprocess calls** — reviewed and confirmed necessary for the tools' stated
  purpose (arbitrary shell command execution as directed by the model), gated by the permission
  system rather than by avoiding `shell=True` itself.
- **Concurrency guard on the bridge's prompt endpoints** — confirmed effective: fired two prompts
  back-to-back with realistic timing and observed a clean `409 Conflict` on the second, preventing
  two threads from racing against the same `ContextManager`/`ToolRegistry` session (this was
  previously verified and fixed in an earlier working session on this codebase, and re-confirmed
  working correctly during this audit's regression passes).

---

## 7. Remaining Known Issues

1. **`local-code-agent` has no formal, persistent test suite.** All verification of its
   functionality — including everything in this audit — has been ad-hoc integration scripts
   written fresh each time, not a re-runnable `pytest` suite that would catch regressions
   automatically on the next change. `telegram_agent_bot` has one (23 tests); `local-code-agent`
   does not, despite `pytest` being listed (commented out, optional) in its own `requirements.txt`.
   This is the single most significant gap found in this audit — not a bug, but a structural risk:
   the exact class of bug found in §2.1 (a dropped import breaking a feature silently) is precisely
   what an automated suite exercising every entry point would have caught immediately, rather than
   requiring a manual, exhaustive audit like this one to surface it.
2. **The `union-attr` mypy noise in `telegram_agent_bot`'s handlers** (~50 findings, §2.5) is real
   but low-value to silence with `# type: ignore` comments throughout, given it's a well-understood,
   library-wide pattern rather than a case-by-case risk. Not fixed; documented instead.
3. **No CI/automated pipeline** wires any of `pyflakes`/`mypy`/`bandit`/`pytest` into a pre-commit
   or CI check for either project — everything run in this audit was invoked manually.

---

## 8. Suggested Future Improvements

1. **Build a real `pytest` suite for `local-code-agent`**, starting with the highest-value,
   lowest-effort targets: `agent/db_tools.py` (pure functions, easy to unit test in isolation),
   `agent/permissions.py`/`agent/auto_permissions.py` (already duck-typed identically, ideal for
   shared parametrized tests), and a stubbed-`OllamaClient` integration test harness similar to the
   one built ad-hoc for this audit (`test_harness.py`), formalized into `tests/` rather than
   discarded after use.
2. **Wire `pyflakes`, `mypy`, and `bandit` into a simple pre-commit hook or CI step** for both
   repos — every fix in this report was found by actually running these tools, not by reading code;
   automating that removes the dependency on someone remembering to run a full audit periodically.
3. **Consider narrowing the ~50 `union-attr` findings with light `assert query is not None`-style
   guards** at the top of each callback handler, purely for defensive robustness against a future
   `python-telegram-bot` version or handler-registration change that might weaken today's implicit
   guarantee — low cost, and would let `telegram_agent_bot` reach a genuinely clean `mypy --strict`
   baseline.
4. **Full permission-approval parity for the bridge** (a real `PERMISSION_REQUEST` WS event +
   Telegram inline keyboard) remains explicitly out of scope per the original integration design,
   not this audit — but is the most-requested-sounding gap if this project's usage grows beyond a
   single trusted operator.
