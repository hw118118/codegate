#!/usr/bin/env python3
"""
Feishu notification script for Claude Code.

Called by the Claude Code **Stop hook** — reads hook JSON from stdin,
extracts the last assistant message, and posts it to a Feishu group chat
as a rich-text message.

Configure via environment variables (typically set in ~/.claude/settings.json):
    FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID

Hook configuration in ~/.claude/settings.json:
    {
      "hooks": {
        "Stop": [{
          "hooks": [{
            "type": "command",
            "command": "python3 /path/to/notify.py",
            "timeout": 10
          }]
        }]
      }
    }
"""

import json
import os
import sys

import requests

from feishu_utils import build_post_content

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "")
SESSIONS_FILE = os.path.expanduser("~/.claude/feishu_active_sessions.json")


def get_tenant_access_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    resp.raise_for_status()
    return resp.json()["tenant_access_token"]


def register_session(cwd, session_id):
    """Register this session so bot_service can discover it."""
    sessions = {}
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE) as f:
                sessions = json.load(f)
        except (json.JSONDecodeError, IOError):
            sessions = {}

    sessions[cwd] = {
        "session_id": session_id,
        "project_name": os.path.basename(cwd) if cwd else "unknown",
        "cwd": cwd,
    }
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)


def send_rich_text(title, content_text):
    """Send a rich-text (post) message to the configured Feishu group."""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET or not FEISHU_CHAT_ID:
        return False

    token = get_tenant_access_token()
    content = build_post_content(title, content_text)

    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": FEISHU_CHAT_ID,
            "msg_type": "post",
            "content": json.dumps(content),
        },
    )
    return resp.json().get("code") == 0


def send_plain_text(message):
    """Fallback: send a plain-text message."""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET or not FEISHU_CHAT_ID:
        return False

    token = get_tenant_access_token()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": FEISHU_CHAT_ID,
            "msg_type": "text",
            "content": json.dumps({"text": message}),
        },
    )
    return resp.json().get("code") == 0


if __name__ == "__main__":
    raw_input = sys.stdin.read().strip()

    message = ""
    project_name = ""
    try:
        hook_data = json.loads(raw_input)
        message = hook_data.get("last_assistant_message", "")
        cwd = hook_data.get("cwd", "")
        session_id = hook_data.get("session_id", "")
        project_name = os.path.basename(cwd) if cwd else ""

        if cwd and session_id:
            register_session(cwd, session_id)
    except (json.JSONDecodeError, TypeError):
        message = raw_input

    if not message and len(sys.argv) > 1:
        message = " ".join(sys.argv[1:])

    if not message:
        message = "Task completed."

    if len(message) > 4000:
        message = message[:3997] + "..."

    title = f"Claude Code [{project_name}]" if project_name else "Claude Code"

    try:
        send_rich_text(title, message)
    except Exception:
        send_plain_text(f"[{title}] {message}")
