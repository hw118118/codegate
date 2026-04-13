# ccbot — Feishu Bot for Claude Code

A Feishu (Lark) bot that bridges Claude Code CLI with Feishu group chat.

## Architecture

- **bot_service.py** — WebSocket long-connection service: receives Feishu messages, forwards to Claude CLI, replies with results
- **notify.py** — Called by Claude Code Stop hook: sends the last assistant message to Feishu as a notification
- **feishu_utils.py** — Shared Markdown-to-Feishu rich text conversion
- **config.py** — (Legacy) configuration reference

## Key Design Decisions

- Uses Feishu WebSocket (长连接) mode — works on internal networks with no public URL
- Uses `claude --print --continue --dangerously-skip-permissions` to maintain conversation context
- Streams output via `--output-format stream-json` — no hard timeout, progress updates for long tasks
- Claude calls run in separate threads to avoid blocking the message handler
- Message deduplication by message_id prevents duplicate replies from Feishu event retries
- Session discovery via `/proc/{pid}/cwd` to detect running Claude processes + JSONL mtime fallback
- Project path resolution uses DFS to handle directory names containing hyphens
- Graceful shutdown sends WebSocket close frame to release server-side connection slots

## Feishu Commands

- `/list` — List active Claude Code sessions (shows [running] or [recent])
- `/use <number|name>` — Select a project
- `/start <path>` — Register a new project directory
- `/stop` — Disconnect from current session
- `/status` — Show current session and request counts
- `/queue` — Show pending requests
- `/cancel <number|all>` — Cancel queued requests
- `/help` — Show help

## Configuration

All via environment variables:
- `FEISHU_APP_ID` — Feishu app ID
- `FEISHU_APP_SECRET` — Feishu app secret
- `FEISHU_CHAT_ID` — Target group chat ID (for notify.py)
- `DEFAULT_WORKSPACE` — Default workspace root for relative /start paths (default: ~/workspace)
- `CLAUDE_CLI` — Path to claude binary (default: claude)
- `CLAUDE_TIMEOUT_SECONDS` — Safety timeout for claude calls (default: 1800)
- `ACTIVE_THRESHOLD_MINUTES` — Minutes to consider a session "recent" (default: 15)
