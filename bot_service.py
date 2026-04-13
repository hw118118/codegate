#!/usr/bin/env python3
"""
Feishu Bot service for Claude Code.

Connects to Feishu via WebSocket long connection, receives messages,
routes them to Claude Code CLI sessions, and replies with results.

Supports multiple concurrent Claude Code projects — pick via /list and /use.

Usage:
    export FEISHU_APP_ID="cli_xxx"
    export FEISHU_APP_SECRET="xxx"
    python3 bot_service.py
"""

import atexit
import collections
import json
import os
import signal
import subprocess
import sys
import threading
import time

import lark_oapi as lark
from lark_oapi.ws import Client as WsClient
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

from feishu_utils import markdown_to_rich_text, build_post_content

# ---------------------------------------------------------------------------
# Configuration (all from environment variables)
# ---------------------------------------------------------------------------

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "claude")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects/")
SESSIONS_FILE = os.path.expanduser("~/.claude/feishu_active_sessions.json")
DEFAULT_WORKSPACE = os.environ.get("DEFAULT_WORKSPACE", os.path.expanduser("~/workspace"))
ACTIVE_THRESHOLD_MINUTES = int(os.environ.get("ACTIVE_THRESHOLD_MINUTES", "15"))
CLAUDE_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "1800"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "10"))

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

user_project_selection = {}   # {open_id: cwd}
user_request_queue = {}       # {open_id: [request_entry, ...]}
lock = threading.Lock()
processed_messages = collections.OrderedDict()
DEDUP_MAX_SIZE = 1000

# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def resolve_project_path(dir_name):
    """Resolve a Claude project dir name back to its real filesystem path.

    Claude stores projects under ~/.claude/projects/ with directory names like
    ``-home-user-workspace-my-project``. Because path segments themselves may
    contain hyphens, we use DFS to try all possible splits and check which
    path actually exists on disk.
    """
    parts = dir_name.lstrip("-").split("-")

    def dfs(idx, current_path):
        if idx == len(parts):
            return current_path if os.path.isdir(current_path) else None
        for end in range(len(parts), idx, -1):
            segment = "-".join(parts[idx:end])
            candidate = os.path.join(current_path, segment)
            if end == len(parts):
                if os.path.isdir(candidate):
                    return candidate
            elif os.path.isdir(candidate):
                result = dfs(end, candidate)
                if result:
                    return result
        return None

    return dfs(0, "/")


def get_running_claude_cwds():
    """Return the working directories of running interactive Claude processes."""
    try:
        result = subprocess.run(
            ["bash", "-c",
             "ps -eo pid,args | grep '[c]laude' | grep -v -- '--print' | awk '{print $1}'"],
            capture_output=True, text=True, timeout=5,
        )
        cwds = set()
        for pid in result.stdout.strip().split("\n"):
            pid = pid.strip()
            if not pid:
                continue
            try:
                cwds.add(os.readlink(f"/proc/{pid}/cwd"))
            except (OSError, FileNotFoundError):
                pass
        return cwds
    except Exception:
        return set()


def scan_active_sessions():
    """Scan for active Claude Code sessions.

    Primary signal: running ``claude`` processes detected via /proc.
    Secondary signal: recently-modified JSONL conversation files.
    """
    sessions = {}
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return sessions

    running_cwds = get_running_claude_cwds()
    now = time.time()
    cutoff = now - ACTIVE_THRESHOLD_MINUTES * 60

    for project_dir_name in os.listdir(CLAUDE_PROJECTS_DIR):
        project_path = os.path.join(CLAUDE_PROJECTS_DIR, project_dir_name)
        if not os.path.isdir(project_path):
            continue

        latest_mtime = 0
        latest_session_id = ""
        for fname in os.listdir(project_path):
            if fname.endswith(".jsonl"):
                fpath = os.path.join(project_path, fname)
                mtime = os.path.getmtime(fpath)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_session_id = fname.replace(".jsonl", "")

        cwd = resolve_project_path(project_dir_name)
        if not cwd:
            continue

        is_running = cwd in running_cwds
        is_recent = latest_mtime >= cutoff
        if not is_running and not is_recent:
            continue

        sessions[cwd] = {
            "session_id": latest_session_id,
            "project_name": os.path.basename(cwd),
            "cwd": cwd,
            "last_active": latest_mtime,
            "status": "running" if is_running else "recent",
        }

    return sessions


def load_sessions():
    """Load sessions: filesystem scan + registered entries."""
    sessions = scan_active_sessions()

    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE) as f:
                registered = json.load(f)
            for cwd, info in registered.items():
                if cwd not in sessions:
                    sessions[cwd] = info
        except (json.JSONDecodeError, IOError):
            pass

    return sessions


# ---------------------------------------------------------------------------
# Claude CLI interaction
# ---------------------------------------------------------------------------


def call_claude(user_message, cwd, progress_callback=None):
    """Send a message to Claude Code CLI and return the response text.

    Uses ``--output-format stream-json`` to read events as they arrive so
    there is no hard subprocess timeout.  A watchdog thread enforces
    CLAUDE_TIMEOUT_SECONDS as a safety net.
    """
    try:
        proc = subprocess.Popen(
            [
                CLAUDE_CLI,
                "--print",
                "--continue",
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "-p", user_message,
            ],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        result_text = ""
        last_progress_time = time.time()
        timed_out = False

        def _kill_after_timeout():
            nonlocal timed_out
            try:
                proc.wait(timeout=CLAUDE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                print(f"[bot] Claude timeout ({CLAUDE_TIMEOUT_SECONDS}s), killing", file=sys.stderr)
                proc.kill()

        watchdog = threading.Thread(target=_kill_after_timeout, daemon=True)
        watchdog.start()

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                event_type = event.get("type", "")

                if event_type == "result":
                    result_text = event.get("result", "")
                    break

                if progress_callback and event_type == "content_block_start":
                    cb = event.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        tool_name = cb.get("name", "unknown")
                        now = time.time()
                        if now - last_progress_time >= 60:
                            progress_callback(f"Executing: {tool_name}...")
                            last_progress_time = now
            except json.JSONDecodeError:
                continue

        proc.wait()

        if timed_out:
            return f"[Error] Claude CLI timed out after {CLAUDE_TIMEOUT_SECONDS}s."

        if not result_text:
            stderr = proc.stderr.read().strip()
            if stderr:
                error_lines = [l for l in stderr.split("\n") if not l.startswith("Warning:")]
                if error_lines:
                    return f"[Error] {chr(10).join(error_lines)}"
            if proc.returncode != 0:
                return f"[Error] Claude CLI exited with code {proc.returncode}"

        return result_text or "[No response from Claude]"
    except FileNotFoundError:
        return f"[Error] Claude CLI not found at: {CLAUDE_CLI}"
    except Exception as e:
        return f"[Error] {type(e).__name__}: {e}"


def start_claude_session(cwd):
    """Register a directory as an active session."""
    if not os.path.isdir(cwd):
        return False, f"Directory not found: `{cwd}`"

    sessions = {}
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE) as f:
                sessions = json.load(f)
        except (json.JSONDecodeError, IOError):
            sessions = {}

    sessions[cwd] = {
        "session_id": "feishu-started",
        "project_name": os.path.basename(cwd),
        "cwd": cwd,
    }
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)

    return True, os.path.basename(cwd)


# ---------------------------------------------------------------------------
# Feishu message helpers
# ---------------------------------------------------------------------------


def extract_text(event):
    """Extract plain text from a Feishu message event, stripping @mentions."""
    msg = event.event.message
    if msg.message_type != "text":
        return ""
    content = json.loads(msg.content)
    text = content.get("text", "")
    parts = text.split()
    cleaned = [p for p in parts if not p.startswith("@_user_")]
    return " ".join(cleaned).strip()


def reply_message(client, message_id, title, text):
    """Reply to a Feishu message with rich text."""
    if len(text) > 4000:
        text = text[:3997] + "..."

    content = build_post_content(title, text)

    body = ReplyMessageRequestBody()
    body.msg_type = "post"
    body.content = json.dumps(content)

    req = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
    resp = client.im.v1.message.reply(req)

    if not resp.success():
        print(f"[bot] Reply failed: code={resp.code} msg={resp.msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def handle_list_command(client, message_id):
    """Handle /list — show active Claude Code sessions."""
    sessions = load_sessions()
    if not sessions:
        reply_message(client, message_id, "Active Sessions",
                      "No active Claude Code sessions found.\n\nMake sure Claude Code is running.")
        return

    sorted_sessions = sorted(sessions.items(),
                             key=lambda x: x[1].get("last_active", 0), reverse=True)
    lines = []
    for i, (cwd, info) in enumerate(sorted_sessions, 1):
        name = info.get("project_name", os.path.basename(cwd))
        badge = "[running]" if info.get("status") == "running" else "[recent]"
        last_active = info.get("last_active")
        if last_active:
            mins_ago = int((time.time() - last_active) / 60)
            if mins_ago < 1:
                ago = "just now"
            elif mins_ago < 60:
                ago = f"{mins_ago}m ago"
            else:
                ago = f"{mins_ago // 60}h{mins_ago % 60}m ago"
            lines.append(f"**{i}.** `{name}` {badge} — {ago}")
        else:
            lines.append(f"**{i}.** `{name}` {badge}")

    text = "\n".join(lines) + "\n\nUse `/use <number>` to select a project."
    reply_message(client, message_id, "Active Sessions", text)


def handle_use_command(client, message_id, sender_id, arg):
    """Handle /use <number|name> — select a project."""
    sessions = load_sessions()
    if not sessions:
        reply_message(client, message_id, "Error", "No active sessions. Start Claude Code first.")
        return

    session_list = sorted(sessions.items(),
                          key=lambda x: x[1].get("last_active", 0), reverse=True)

    # Try as number
    try:
        idx = int(arg) - 1
        if 0 <= idx < len(session_list):
            cwd, info = session_list[idx]
            with lock:
                user_project_selection[sender_id] = cwd
            name = info.get("project_name", os.path.basename(cwd))
            reply_message(client, message_id, "Project Selected",
                          f"Now chatting with: **{name}**\n`{cwd}`\n\nSend any message to interact with Claude.")
            return
    except ValueError:
        pass

    # Try as name match
    for cwd, info in session_list:
        name = info.get("project_name", os.path.basename(cwd))
        if arg.lower() in name.lower() or arg.lower() in cwd.lower():
            with lock:
                user_project_selection[sender_id] = cwd
            reply_message(client, message_id, "Project Selected",
                          f"Now chatting with: **{name}**\n`{cwd}`")
            return

    reply_message(client, message_id, "Error",
                  f"No project matching `{arg}`. Use `/list` to see available projects.")


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------


def make_message_handler(client):
    def handle_message(data: P2ImMessageReceiveV1):
        msg = data.event.message
        message_id = msg.message_id
        sender_id = data.event.sender.sender_id.open_id

        # Deduplicate (Feishu may deliver the same event more than once)
        with lock:
            if message_id in processed_messages:
                return
            processed_messages[message_id] = True
            while len(processed_messages) > DEDUP_MAX_SIZE:
                processed_messages.popitem(last=False)

        user_text = extract_text(data)
        if not user_text:
            return

        print(f"[bot] From {sender_id}: {user_text}", file=sys.stderr)
        cmd = user_text.strip()

        # --- Commands ---

        if cmd == "/list":
            handle_list_command(client, message_id)
            return

        if cmd.startswith("/use "):
            handle_use_command(client, message_id, sender_id, cmd[5:].strip())
            return

        if cmd.startswith("/start "):
            path = os.path.expanduser(cmd[7:].strip())
            if not os.path.isabs(path):
                path = os.path.join(DEFAULT_WORKSPACE, path)
            ok, name = start_claude_session(path)
            if ok:
                with lock:
                    user_project_selection[sender_id] = path
                reply_message(client, message_id, "Session Started",
                              f"Started session in: **{name}**\n`{path}`\n\nSend any message to chat.")
            else:
                reply_message(client, message_id, "Error", name)
            return

        if cmd == "/stop":
            with lock:
                cwd = user_project_selection.pop(sender_id, None)
            if cwd:
                reply_message(client, message_id, "Session Stopped",
                              f"Disconnected from `{os.path.basename(cwd)}`.")
            else:
                reply_message(client, message_id, "No Session", "No active session to stop.")
            return

        if cmd == "/status":
            with lock:
                cwd = user_project_selection.get(sender_id)
                queue = list(user_request_queue.get(sender_id, []))
            if not cwd:
                reply_message(client, message_id, "Status",
                              "No project selected. Use `/list` then `/use <number>`.")
            else:
                name = os.path.basename(cwd)
                running = [r for r in queue if r["status"] == "running"]
                queued = [r for r in queue if r["status"] == "queued"]
                done = [r for r in queue if r["status"] == "done"]
                lines = [
                    f"**Project:** `{name}`",
                    f"**Path:** {cwd}",
                    f"**Running:** {len(running)}  |  **Queued:** {len(queued)}  |  **Done:** {len(done)}",
                ]
                if running:
                    lines += ["", "**Currently running:**"]
                    for r in running:
                        preview = r["text"][:50] + "..." if len(r["text"]) > 50 else r["text"]
                        lines.append(f"  `{preview}`")
                if queued:
                    lines += ["", "**Queued:**"]
                    for i, r in enumerate(queued, 1):
                        preview = r["text"][:50] + "..." if len(r["text"]) > 50 else r["text"]
                        lines.append(f"  {i}. `{preview}`")
                reply_message(client, message_id, "Status", "\n".join(lines))
            return

        if cmd == "/queue":
            with lock:
                queue = list(user_request_queue.get(sender_id, []))
            active = [r for r in queue if r["status"] in ("running", "queued")]
            if not active:
                reply_message(client, message_id, "Queue", "No pending requests.")
            else:
                lines = []
                for i, r in enumerate(active, 1):
                    preview = r["text"][:60] + "..." if len(r["text"]) > 60 else r["text"]
                    badge = "[running]" if r["status"] == "running" else "[queued]"
                    lines.append(f"**{i}.** {badge} `{preview}`")
                lines += ["", "Use `/cancel <number>` or `/cancel all`."]
                reply_message(client, message_id, "Queue", "\n".join(lines))
            return

        if cmd.startswith("/cancel"):
            arg = cmd[7:].strip()
            with lock:
                queue = user_request_queue.get(sender_id, [])
                active = [r for r in queue if r["status"] in ("running", "queued")]
                if arg == "all":
                    cancelled = sum(1 for r in queue if r["status"] == "queued")
                    for r in queue:
                        if r["status"] == "queued":
                            r["status"] = "cancelled"
                    reply_message(client, message_id, "Cancelled",
                                  f"Cancelled {cancelled} queued request(s).")
                elif arg:
                    try:
                        idx = int(arg) - 1
                        if 0 <= idx < len(active):
                            target = active[idx]
                            if target["status"] == "queued":
                                target["status"] = "cancelled"
                                preview = target["text"][:50] + "..." if len(target["text"]) > 50 else target["text"]
                                reply_message(client, message_id, "Cancelled", f"Cancelled: `{preview}`")
                            else:
                                reply_message(client, message_id, "Cannot Cancel",
                                              "This request is already running.")
                        else:
                            reply_message(client, message_id, "Error",
                                          "Invalid number. Use `/queue` to see the list.")
                    except ValueError:
                        reply_message(client, message_id, "Error",
                                      "Usage: `/cancel <number>` or `/cancel all`")
                else:
                    reply_message(client, message_id, "Error",
                                  "Usage: `/cancel <number>` or `/cancel all`")
            return

        if cmd == "/help":
            reply_message(client, message_id, "Help", (
                "**/list** — List active Claude Code sessions\n"
                "**/use <number|name>** — Select a project\n"
                "**/start <path>** — Register a new project directory\n"
                "**/stop** — Disconnect from current session\n"
                "**/status** — Show current session and request counts\n"
                "**/queue** — Show pending requests\n"
                "**/cancel <number|all>** — Cancel queued requests\n"
                "**/help** — Show this help\n"
                "\nAny other message is forwarded to the selected project's Claude Code."
            ))
            return

        # --- Forward to Claude ---

        with lock:
            cwd = user_project_selection.get(sender_id)

        # Auto-select if only one session exists
        if not cwd:
            sessions = load_sessions()
            if len(sessions) == 1:
                cwd = list(sessions.keys())[0]
                with lock:
                    user_project_selection[sender_id] = cwd
            elif len(sessions) > 1:
                reply_message(client, message_id, "Select Project",
                              "Multiple sessions active. Use `/list` then `/use <number>`.")
                return
            else:
                reply_message(client, message_id, "No Session",
                              "No active Claude Code sessions. Start Claude Code first.")
                return

        # Verify session still exists
        sessions = load_sessions()
        if cwd not in sessions:
            with lock:
                user_project_selection.pop(sender_id, None)
            reply_message(client, message_id, "Session Lost",
                          f"Session for `{cwd}` is no longer active. Use `/list`.")
            return

        project_name = sessions[cwd].get("project_name", os.path.basename(cwd))

        # Track request
        request_entry = {"id": message_id, "text": user_text, "status": "queued"}
        with lock:
            if sender_id not in user_request_queue:
                user_request_queue[sender_id] = []
            user_request_queue[sender_id].append(request_entry)

        # Run in a separate thread
        def _run_claude(req, msg_id, proj_name, user_msg, working_dir):
            with lock:
                if req["status"] == "cancelled":
                    print(f"[bot] Skipping cancelled: {user_msg[:50]}", file=sys.stderr)
                    return
                req["status"] = "running"

            print(f"[bot] -> Claude ({proj_name}): {user_msg}", file=sys.stderr)

            def on_progress(text):
                print(f"[bot] ~  Claude ({proj_name}): {text}", file=sys.stderr)
                reply_message(client, msg_id, f"Claude [{proj_name}]", text)

            response = call_claude(user_msg, working_dir, progress_callback=on_progress)
            print(f"[bot] <- Claude ({proj_name}): {len(response)} chars", file=sys.stderr)

            with lock:
                req["status"] = "done"
                queue = user_request_queue.get(sender_id, [])
                done_count = sum(1 for r in queue if r["status"] in ("done", "cancelled"))
                if done_count > 20:
                    user_request_queue[sender_id] = [
                        r for r in queue if r["status"] not in ("done", "cancelled")
                    ]

            reply_message(client, msg_id, f"Claude [{proj_name}]", response)

        t = threading.Thread(
            target=_run_claude,
            args=(request_entry, message_id, project_name, user_text, cwd),
            daemon=True,
        )
        t.start()

    return handle_message


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        print("Error: Set FEISHU_APP_ID and FEISHU_APP_SECRET.", file=sys.stderr)
        sys.exit(1)

    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    handler = make_message_handler(client)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handler)
        .build()
    )

    ws_client = WsClient(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=event_handler,
    )

    # Graceful shutdown — send WebSocket close frame so Feishu releases the
    # server-side connection slot immediately instead of waiting for timeout.
    def _graceful_shutdown(signum=None, frame=None):
        print(f"\n[bot] Shutting down (signal={signum})...", file=sys.stderr)
        try:
            conn = ws_client._conn
            if conn is not None:
                import asyncio
                loop = asyncio.get_event_loop()
                loop.run_until_complete(conn.close())
                print("[bot] WebSocket closed gracefully.", file=sys.stderr)
        except Exception as e:
            print(f"[bot] Close error (non-fatal): {e}", file=sys.stderr)
        if signum is not None:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)
    atexit.register(_graceful_shutdown)

    print("[bot] Starting Feishu Bot (WebSocket)...", file=sys.stderr)
    print("[bot] Commands: /list, /use, /start, /stop, /status, /help", file=sys.stderr)
    print("[bot] Waiting for messages... (Ctrl+C to stop)", file=sys.stderr)

    attempt = 0
    while True:
        attempt += 1
        try:
            ws_client.start()
            break
        except KeyboardInterrupt:
            print("\n[bot] Interrupted.", file=sys.stderr)
            sys.exit(0)
        except Exception as e:
            err_str = str(e)

            if "1000040350" in err_str or "connections exceeded" in err_str.lower():
                print(f"[bot] Connection limit exceeded: {e}", file=sys.stderr)
                print("[bot] Kill other bot_service.py instances, or recreate the Feishu app.", file=sys.stderr)
                sys.exit(2)

            if attempt > MAX_RETRIES:
                print(f"[bot] Giving up after {MAX_RETRIES} attempts.", file=sys.stderr)
                sys.exit(1)

            wait = min(30 * attempt, 300)
            print(f"[bot] Connection failed ({attempt}/{MAX_RETRIES}): {e}", file=sys.stderr)
            print(f"[bot] Retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)


if __name__ == "__main__":
    main()
