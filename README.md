# ccbot

A Feishu (Lark) bot that lets you interact with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions from your phone through a Feishu group chat.

**Use case:** You have Claude Code running on a remote server. You step away from your desk. With ccbot, you can monitor progress, send follow-up instructions, and switch between projects — all from the Feishu app on your phone.

## Features

- **Multi-session support** — Discover and switch between multiple Claude Code projects
- **Full context continuity** — Uses `--continue` to maintain conversation history with each project
- **Streaming output** — No hard timeout; streams Claude's response as it works
- **Progress notifications** — Reports which tool Claude is executing during long tasks
- **Stop hook notifications** — Automatically posts Claude's final response to the group when a task finishes
- **Session discovery** — Auto-detects running Claude Code processes via `/proc` and JSONL file timestamps
- **Request queue** — Track, inspect, and cancel pending requests

## Architecture

```
┌──────────┐  WebSocket   ┌──────────────┐  claude --print  ┌─────────────┐
│  Feishu  │◄────────────►│ bot_service  │────────────────►│ Claude Code │
│  Group   │              │   .py        │◄────────────────│   CLI       │
└──────────┘              └──────────────┘  stream-json     └─────────────┘
     ▲                                                            │
     │ HTTP POST          ┌──────────────┐   Stop hook (stdin)    │
     └────────────────────│  notify.py   │◄───────────────────────┘
                          └──────────────┘
```

- **bot_service.py** — Long-running service. Connects to Feishu via WebSocket, receives messages, forwards them to `claude --print --continue`, and replies with results.
- **notify.py** — Lightweight script called by Claude Code's [Stop hook](https://docs.anthropic.com/en/docs/claude-code/hooks). Reads the last assistant message from stdin and posts it to the group chat via REST API.
- **feishu_utils.py** — Shared Markdown → Feishu rich text conversion.

## Prerequisites

- Python 3.8+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Feishu (Lark) custom app with bot capabilities

## Setup

### 1. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn) → Create App → Custom App
2. Add the **Bot** capability
3. Under **Permissions**, add:
   - `im:message` — Send and receive messages
   - `im:message:send_as_bot` — Send messages as bot
4. Under **Events & Callbacks**:
   - Add event: `im.message.receive_v1`
   - Set subscription mode to **Long Connection** (长连接)
5. Publish the app (at least to test version)
6. Add the bot to your target group chat
7. Note your **App ID**, **App Secret**, and the **Chat ID** of the group

### 2. Install ccbot

```bash
git clone https://github.com/YOUR_USERNAME/ccbot.git
cd ccbot
pip install -r requirements.txt
```

### 3. Start the bot service

```bash
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="your_secret"

# Run in foreground
python3 bot_service.py

# Or with nohup
nohup python3 bot_service.py > nohup.out 2>&1 &
```

### 4. Configure the Stop hook (optional)

Add to `~/.claude/settings.json` to get notifications when Claude finishes a task:

```json
{
  "env": {
    "FEISHU_APP_ID": "cli_xxx",
    "FEISHU_APP_SECRET": "your_secret",
    "FEISHU_CHAT_ID": "oc_xxx"
  },
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/ccbot/notify.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

## Usage

In the Feishu group chat, @mention the bot followed by a command:

```
@bot /list                    # List active Claude Code sessions
@bot /use 1                   # Select project #1
@bot /use my-project          # Select by name (partial match)
@bot /start ~/workspace/foo   # Register a new project
@bot /stop                    # Disconnect from current session
@bot /status                  # Show session info and request counts
@bot /queue                   # Show pending requests
@bot /cancel 2                # Cancel queued request #2
@bot /cancel all              # Cancel all queued requests
@bot /help                    # Show help

@bot Fix the failing test     # Send a message to the selected project's Claude
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FEISHU_APP_ID` | Yes | — | Feishu app ID |
| `FEISHU_APP_SECRET` | Yes | — | Feishu app secret |
| `FEISHU_CHAT_ID` | For notify.py | — | Target group chat ID |
| `CLAUDE_CLI` | No | `claude` | Path to Claude Code binary |
| `DEFAULT_WORKSPACE` | No | `~/workspace` | Root for relative `/start` paths |
| `CLAUDE_TIMEOUT_SECONDS` | No | `1800` | Safety timeout for Claude calls (seconds) |
| `ACTIVE_THRESHOLD_MINUTES` | No | `15` | Minutes to consider a session "recent" |
| `MAX_RETRIES` | No | `10` | Connection retry attempts on startup |

## How It Works

### Session Discovery

ccbot finds active Claude Code sessions by:
1. Scanning running processes for `claude` and reading their working directory via `/proc/{pid}/cwd`
2. Checking `~/.claude/projects/` for recently-modified JSONL conversation files

### Message Flow

1. User sends a message in Feishu group, @mentioning the bot
2. Feishu delivers the event over WebSocket
3. bot_service deduplicates by message_id and dispatches to a worker thread
4. The worker runs `claude --print --continue --output-format stream-json -p "<message>"` in the selected project directory
5. Stream events are read in real-time; progress updates are sent back for long-running tool executions
6. The final result is posted as a rich-text reply

### Graceful Shutdown

The bot registers signal handlers for SIGTERM and SIGINT to send a proper WebSocket close frame on exit. This prevents "ghost connections" that count against Feishu's per-app connection limit (max 50).

## Limitations

- **Linux only** — Session discovery uses `/proc` which is Linux-specific
- **`--continue` shares context** — The bot appends to the same conversation as interactive Claude sessions. Messages sent via the bot become part of the project's conversation history.
- **One reply per message** — Feishu threading means each user message gets one final reply (plus optional progress updates)

## License

MIT
