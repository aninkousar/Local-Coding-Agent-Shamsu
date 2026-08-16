# Telegram Agent Bot

A remote Telegram control interface for a local coding agent - authenticate, submit prompts,
watch live progress, browse project files, and manage running tasks, all from Telegram.

## Important: about the "local coding agent" this connects to

This bot is built against a **specific REST + WebSocket API contract** (see [API
Specification](#api-specification) below) — it is deliberately independent from any particular
agent implementation, exactly as the design brief requires.

**As shipped, it talks to `mock_agent/`** — a reference implementation of that exact contract,
included so the whole system is genuinely runnable and testable today, simulating a project
progressing through realistic states (queued → running → completed) with believable logs and
progress events.

If you have a different local coding agent (including a general-purpose `local-code-agent`
CLI/GUI tool) that does **not already expose this REST + WebSocket API**, this bot will not
control it out of the box. You would need to build an adapter layer on that agent's side that
speaks the same contract `mock_agent/` does. That adapter is *not* part of this project.

## Quick start (with the included mock agent)

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN (from @BotFather) and ALLOWED_USER_IDS (your Telegram user ID)

# Terminal 1: the agent (mock, or your own real implementation of the same API)
python -m mock_agent.server

# Terminal 2: the bot
python -m bot.main
```

Open Telegram, message your bot, send `/start`.

## Running the tests

```bash
pip install -r requirements.txt   # includes pytest/pytest-asyncio
pytest -v
```

23 tests covering settings/whitelist validation, the authorization decorator, the database layer
(including the foreign-key-enforced local cache), and MarkdownV2 message formatting (using a
strict validator in `tests/markdown_validator.py`, since there's no bot token available in an
automated test environment to validate against the real Telegram API).

## Architecture

```
Telegram Bot  <-->  api_client (typed, retrying HTTP client)  <-->  Agent's REST API
     |                                                                    |
     v                                                                    v
Local SQLite cache (database/)                                    WebSocket /events
     ^                                                                    |
     +--------- bot/services/websocket_listener.py streams live progress in ---+
```

- **`bot/`** — the Telegram-facing half: handlers (one module per screen), keyboards (all
  `callback_data` formats defined in one place), services (auth, formatting, the WS listener),
  and the shared Pydantic models everything speaks.
- **`api_client/`** — the *only* module that knows the agent speaks HTTP. Every other part of the
  bot only ever sees typed Pydantic models. Retries on connection failures and 5xx responses with
  exponential backoff; never retries a 4xx, since that means the request itself was invalid.
- **`database/`** — a local SQLite cache/mirror of project state, kept in sync as the bot receives
  data from the agent. Not the source of truth (the agent is) - a fast local store for
  listing/browsing without a round-trip on every interaction.
- **`mock_agent/`** — reference implementation of the agent-side API contract, for testing and
  running this bot standalone.
- **`config/`** — typed settings loaded from `.env`, including the authorization whitelist.

### Conversation state

A user's next plain-text message is either a new project prompt or a follow-up to a specific
project, tracked in `context.user_data["awaiting_prompt_for"]` (`None` = new project, a project ID
= follow-up), set by the New Prompt / Send Follow-up Prompt buttons and cleared once the message
is sent.

### Live progress streaming

`bot/services/websocket_listener.py` maintains one persistent connection to the agent's `WS
/events` endpoint. When a project is being watched (started automatically after a new prompt or
follow-up), incoming events edit a single Telegram message rather than sending a new one each
time, per the design brief. Two real constraints shape this:

- **Telegram rate-limits message edits** (roughly one/second per chat) - a minimum interval
  between edits is enforced, coalescing bursts of fast-arriving events.
- **Telegram rejects an edit identical to the message's current text** ("message is not
  modified") - a dedup guard skips redundant edits rather than treating this as an error.

## API Specification

The bot expects the agent to expose:

| Method | Path | Purpose |
|---|---|---|
| POST | `/prompt` | Start a new project from a prompt |
| POST | `/project/{id}/prompt` | Follow-up prompt, same project's context |
| GET | `/projects` | List all projects |
| GET | `/project/{id}` | One project's full detail |
| GET | `/project/{id}/logs?limit=&offset=` | Paginated logs |
| GET | `/project/{id}/files?path=` | Directory listing, or file content if `path` is a file |
| GET | `/system` | CPU/RAM/agent status |
| POST | `/project/{id}/{pause\|resume\|stop}` | Project controls |
| WS | `/events` | Live progress/status/log events, broadcast to all connected clients |

Exact request/response shapes: `bot/models/schemas.py` (the single source of truth every part of
the bot validates against).

## Configuration

See `.env.example` for every variable. The two that matter most:

- `TELEGRAM_BOT_TOKEN` - from [@BotFather](https://t.me/BotFather).
- `ALLOWED_USER_IDS` - comma-separated Telegram user IDs. **The bot refuses to start if this is
  empty** - find your own ID via [@userinfobot](https://t.me/userinfobot).

## Folder structure

```
telegram_agent_bot/
├── bot/
│   ├── handlers/       # one module per screen: start, prompt, projects, logs, system, files
│   ├── keyboards/       # every inline keyboard builder, callback_data formats defined once
│   ├── services/        # auth, message formatting, the WebSocket listener
│   ├── models/           # shared Pydantic schemas - the contract everything validates against
│   └── main.py            # wires everything together, registers handlers, starts polling
├── api_client/           # typed async HTTP client with retry logic
├── database/              # local SQLite cache + schema
├── config/                 # typed settings, .env loading, the auth whitelist
├── utils/                   # logging config, shared time utilities
├── mock_agent/               # reference agent API implementation, for testing/demo
├── tests/                     # pytest suite + the MarkdownV2 validator used by it
├── .env.example
├── requirements.txt
└── pytest.ini
```

## What's NOT included

- An adapter making a specific third-party coding agent (including `local-code-agent`) actually
  speak this API - stated plainly rather than implied to already work.
- Persistent multi-worker deployment (systemd unit, Docker, etc.) - this is `run_polling()`-based,
  suitable for a single long-running process.
