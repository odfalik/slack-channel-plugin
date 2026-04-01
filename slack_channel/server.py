"""Slack channel plugin — MCP server over stdio with Socket Mode listener.

Connects to Slack via Socket Mode, declares claude/channel capability,
emits notifications/claude/channel on incoming messages, and exposes
reply + read tools.

Architecture:
  - One Socket Mode connection across all instances (leader election via lock file)
  - Leader writes events to a shared JSONL file (event bus)
  - ALL instances tail the event bus and send channel notifications for their threads
  - This ensures every session gets real-time push for threads it created
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
from dotenv import load_dotenv
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

import mcp.types as types
from mcp.server.lowlevel.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

# Load env vars. Priority: real env vars > plugin channels dir > local .env
# The channels dir (~/.claude/channels/slack-channel/.env) is the standard
# location for plugin credentials, written by /slack-channel:configure.
_channels_env = Path.home() / ".claude" / "channels" / "slack-channel" / ".env"
_pkg_dir = Path(__file__).resolve().parent.parent
load_dotenv(_channels_env)
load_dotenv(_pkg_dir / ".env")
load_dotenv(_pkg_dir.parent / ".env")

logger = logging.getLogger("slack-channel")

# ---------------------------------------------------------------------------
# Globals (set during startup)
# ---------------------------------------------------------------------------
_session: ServerSession | None = None
_slack_client: Any = None  # AsyncWebClient from bolt app
_bot_user_id: str | None = None
_pending_eyes: dict[str, str] = {}  # thread_ts → message_ts (messages with eyes to remove)
_user_name_cache: dict[str, str] = {}

CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")

# Shared state directory
_STATE_DIR = Path(
    os.environ.get("SLACK_CHANNEL_STATE_DIR", "")
) if os.environ.get("SLACK_CHANNEL_STATE_DIR") else (
    Path.home() / ".config" / "slack-channel"
)
_EVENTS_FILE = _STATE_DIR / "events.jsonl"
_LOCK_FILE = _STATE_DIR / "listener.lock"

# Thread tracking — persisted per conversation ID.
# The conversation ID is stable across --resume (it's what you pass to --resume).
# Resolved via a SessionStart hook that writes it to:
#   ~/.config/slack-channel/sessions/{claude-code-pid}.json
# The MCP server reads it using os.getppid() (its parent = Claude Code).
THREAD_TTL_DAYS = 7
_THREADS_FILE = _STATE_DIR / "threads.json"
_SESSIONS_DIR = _STATE_DIR / "sessions"
_conversation_id: str | None = None
_owned_threads: dict[str, str] = {}  # thread_ts → channel


def _get_ppid(pid: int) -> int:
    """Get the parent PID of a process (macOS/Linux)."""
    try:
        import subprocess
        result = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        return int(result.stdout.strip())
    except Exception:
        return 0


def _resolve_conversation_id() -> str | None:
    """Read the conversation ID written by the SessionStart hook.

    The hook writes ~/.config/slack-channel/sessions/{claude-code-pid}.json.
    We walk up the process tree (ppid → grandppid → ...) to find the matching
    session file, since there may be intermediate processes (e.g. uv/uvx)
    between Claude Code and us.
    """
    pid = os.getpid()
    for _ in range(5):  # walk up at most 5 levels
        pid = _get_ppid(pid)
        if pid <= 1:
            break
        session_file = _SESSIONS_DIR / f"{pid}.json"
        try:
            data = json.loads(session_file.read_text())
            return data.get("conversation_id")
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _ensure_conversation_id() -> str | None:
    """Resolve and cache the conversation ID. Retries until found."""
    global _conversation_id
    if _conversation_id is None:
        _conversation_id = _resolve_conversation_id()
        if _conversation_id:
            logger.info("Conversation ID: %s", _conversation_id)
            # Load persisted threads
            loaded = _load_threads()
            _owned_threads.update(loaded)
            if loaded:
                logger.info("Loaded %d threads for conversation %s", len(loaded), _conversation_id)
    return _conversation_id


def _load_threads() -> dict[str, str]:
    """Load threads for the current conversation from the shared threads file."""
    conv_id = _ensure_conversation_id()
    if not conv_id or not _THREADS_FILE.exists():
        return {}
    try:
        with open(_THREADS_FILE) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = time.time() - THREAD_TTL_DAYS * 86400
    return {
        t["thread_ts"]: t["channel"]
        for t in data
        if t.get("conversation_id") == conv_id
        and float(t["thread_ts"].split(".")[0]) > cutoff
    }


def _save_threads() -> None:
    """Persist this conversation's threads to the shared file."""
    conv_id = _ensure_conversation_id()
    if not conv_id:
        return
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - THREAD_TTL_DAYS * 86400

    with open(_THREADS_FILE, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            all_entries = json.load(f)
        except (json.JSONDecodeError, ValueError):
            all_entries = []
        # Keep other conversations' non-stale entries
        other = [
            t for t in all_entries
            if t.get("conversation_id") != conv_id
            and float(t["thread_ts"].split(".")[0]) > cutoff
        ]
        # Add this conversation's entries
        mine = [
            {"thread_ts": ts, "channel": ch, "conversation_id": conv_id}
            for ts, ch in _owned_threads.items()
        ]
        f.seek(0)
        f.truncate()
        json.dump(other + mine, f, indent=2)
        f.write("\n")

# Leader election state
_is_leader = False
_lock_fd: Any = None  # file descriptor held for lifetime




def _try_become_leader() -> bool:
    """Try to acquire the listener lock. Non-blocking."""
    global _lock_fd
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except OSError:
        _lock_fd.close()
        _lock_fd = None
        return False


def _append_event(event: dict) -> None:
    """Append an event to the shared event bus (JSONL)."""
    with open(_EVENTS_FILE, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# MCP server (low-level for full control over the run loop)
# ---------------------------------------------------------------------------
server = Server("slack-channel")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="reply",
            description="Send a message to a Slack channel or thread",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message text (supports Slack mrkdwn)"},
                    "channel": {
                        "type": "string",
                        "description": "Channel ID. Defaults to SLACK_CHANNEL_ID env var.",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": "Thread timestamp to reply in an existing thread",
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="add_reaction",
            description="Add an emoji reaction to a message",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "timestamp": {"type": "string", "description": "Message timestamp"},
                    "reaction": {
                        "type": "string",
                        "description": "Emoji name without colons (e.g. thumbsup, eyes)",
                    },
                },
                "required": ["channel", "timestamp", "reaction"],
            },
        ),
        types.Tool(
            name="remove_reaction",
            description="Remove an emoji reaction from a message",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "timestamp": {"type": "string", "description": "Message timestamp"},
                    "reaction": {
                        "type": "string",
                        "description": "Emoji name without colons (e.g. eyes)",
                    },
                },
                "required": ["channel", "timestamp", "reaction"],
            },
        ),
        types.Tool(
            name="list_channels",
            description="List Slack channels the bot has access to",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max channels (default 100)"},
                },
            },
        ),
        types.Tool(
            name="read_history",
            description="Read recent messages from a Slack channel",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "limit": {"type": "integer", "description": "Max messages (default 25)"},
                },
                "required": ["channel"],
            },
        ),
        types.Tool(
            name="get_thread",
            description="Get all replies in a Slack thread",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel ID"},
                    "thread_ts": {"type": "string", "description": "Thread timestamp"},
                },
                "required": ["channel", "thread_ts"],
            },
        ),
        types.Tool(
            name="debug",
            description="Show internal plugin state (owned threads, leader status, etc.)",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    args = arguments or {}
    handlers = {
        "reply": _handle_reply,
        "add_reaction": _handle_add_reaction,
        "remove_reaction": _handle_remove_reaction,
        "list_channels": _handle_list_channels,
        "read_history": _handle_read_history,
        "get_thread": _handle_get_thread,
        "debug": _handle_debug,
    }
    handler = handlers.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(args)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _handle_reply(args: dict) -> list[types.TextContent]:
    channel = args.get("channel") or CHANNEL_ID
    if not channel:
        return [types.TextContent(type="text", text="Error: no channel — set SLACK_CHANNEL_ID or pass channel")]

    thread_ts = args.get("thread_ts")
    result = await _slack_client.chat_postMessage(
        channel=channel,
        text=args["text"],
        thread_ts=thread_ts,
    )
    ts = result.get("ts", "?")

    # Track thread for routing (persisted per conversation ID).
    owned_ts = thread_ts or ts
    if owned_ts not in _owned_threads:
        _owned_threads[owned_ts] = channel
        _save_threads()
        logger.info("Tracking thread %s in %s", owned_ts, channel)

    # Auto-remove eyes reaction from the message we're responding to
    effective_thread = thread_ts or ts
    if effective_thread in _pending_eyes:
        eyes_ts = _pending_eyes.pop(effective_thread)
        try:
            await _slack_client.reactions_remove(
                channel=channel, timestamp=eyes_ts, name="eyes",
            )
        except Exception:
            pass

    return [types.TextContent(type="text", text=f"Sent (ts={ts}, channel={channel})")]


async def _handle_add_reaction(args: dict) -> list[types.TextContent]:
    await _slack_client.reactions_add(
        channel=args["channel"],
        timestamp=args["timestamp"],
        name=args["reaction"],
    )
    return [types.TextContent(type="text", text=f"Reacted :{args['reaction']}:")]


async def _handle_remove_reaction(args: dict) -> list[types.TextContent]:
    try:
        await _slack_client.reactions_remove(
            channel=args["channel"],
            timestamp=args["timestamp"],
            name=args["reaction"],
        )
    except Exception:
        pass  # Ignore if reaction wasn't there
    return [types.TextContent(type="text", text=f"Removed :{args['reaction']}:")]


async def _handle_list_channels(args: dict) -> list[types.TextContent]:
    limit = args.get("limit", 100)
    result = await _slack_client.conversations_list(
        types="public_channel,private_channel",
        exclude_archived=True,
        limit=limit,
    )
    lines = []
    for ch in result.get("channels", []):
        name = ch.get("name", "?")
        cid = ch["id"]
        members = ch.get("num_members", "?")
        lines.append(f"#{name}  {cid}  ({members} members)")
    return [types.TextContent(type="text", text="\n".join(lines) or "No channels found.")]


async def _handle_read_history(args: dict) -> list[types.TextContent]:
    channel = args["channel"]
    limit = args.get("limit", 25)
    result = await _slack_client.conversations_history(channel=channel, limit=limit)

    lines = []
    for msg in reversed(result.get("messages", [])):
        user = await _resolve_user(msg.get("user", ""))
        ts = msg.get("ts", "")
        text = msg.get("text", "")
        thread_indicator = ""
        if msg.get("reply_count"):
            thread_indicator = f" [{msg['reply_count']} replies]"
        lines.append(f"[{ts}] {user}: {text}{thread_indicator}")
    return [types.TextContent(type="text", text="\n".join(lines) or "No messages.")]


async def _handle_get_thread(args: dict) -> list[types.TextContent]:
    channel = args["channel"]
    thread_ts = args["thread_ts"]
    result = await _slack_client.conversations_replies(
        channel=channel, ts=thread_ts, limit=200,
    )

    lines = []
    for msg in result.get("messages", []):
        user = await _resolve_user(msg.get("user", ""))
        text = msg.get("text", "")
        ts = msg.get("ts", "")
        lines.append(f"[{ts}] {user}: {text}")
    return [types.TextContent(type="text", text="\n".join(lines) or "No replies.")]


async def _handle_debug(args: dict) -> list[types.TextContent]:
    # Dump client_params if available
    client_info = None
    if _session and _session.client_params:
        cp = _session.client_params
        client_info = {
            "protocolVersion": cp.protocolVersion,
            "clientInfo": {"name": cp.clientInfo.name, "version": cp.clientInfo.version} if cp.clientInfo else None,
        }
        # Check for extra fields
        try:
            client_info["raw"] = cp.model_dump(mode="json")
        except Exception:
            pass
    info = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "conversation_id": _conversation_id,
        "is_leader": _is_leader,
        "bot_user_id": _bot_user_id,
        "owned_threads": _owned_threads,
        "channel_filter": CHANNEL_ID,
    }
    return [types.TextContent(type="text", text=json.dumps(info, indent=2))]


# ---------------------------------------------------------------------------
# User name resolution (cached)
# ---------------------------------------------------------------------------

async def _resolve_user(user_id: str) -> str:
    if not user_id:
        return "unknown"
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    if user_id == _bot_user_id:
        _user_name_cache[user_id] = "(bot)"
        return "(bot)"
    try:
        info = await _slack_client.users_info(user=user_id)
        profile = info["user"]["profile"]
        name = profile.get("display_name") or info["user"].get("real_name") or user_id
        _user_name_cache[user_id] = name
        return name
    except Exception:
        return user_id


# ---------------------------------------------------------------------------
# Channel notifications → MCP client
# ---------------------------------------------------------------------------

async def _send_channel_notification(event: dict) -> None:
    """Push a notifications/claude/channel message over the MCP session.

    Claude Code expects params = { content: str, meta: dict }.
    It renders as: <channel source="slack-channel" ...meta>content</channel>
    """
    if _session is None:
        return

    sender = event.get("_sender_name", event.get("user", "unknown"))
    text = event.get("text", "")

    meta: dict[str, str] = {
        "channel": event.get("channel", ""),
        "sender": sender,
        "sender_id": event.get("user", ""),
        "ts": event.get("ts", ""),
    }
    thread_ts = event.get("thread_ts")
    if thread_ts:
        meta["thread_ts"] = thread_ts

    try:
        # Build the raw JSON-RPC notification and write directly to the
        # session's write stream.  We bypass session.send_message() because
        # the MCP SDK validates against ServerNotificationType (a closed union
        # of standard methods) and rejects our custom method.
        notification = types.JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={
                "content": f"{sender}: {text}",
                "meta": meta,
            },
        )
        raw_msg = SessionMessage(message=types.JSONRPCMessage(notification))
        await _session._write_stream.send(raw_msg)
    except Exception:
        logger.debug("Failed to send channel notification", exc_info=True)


# ---------------------------------------------------------------------------
# Cold replies (leader only) — for messages no active session owns
# ---------------------------------------------------------------------------

NO_RESPONSE_SENTINEL = "NO_RESPONSE"
REACT_PREFIX = "REACT:"


async def _fetch_thread_context(channel: str, thread_ts: str) -> str:
    """Fetch all messages in a thread and format as context."""
    try:
        result = await _slack_client.conversations_replies(
            channel=channel, ts=thread_ts, limit=50,
        )
    except Exception:
        return "(failed to fetch thread)"

    lines = []
    for msg in result.get("messages", []):
        user = await _resolve_user(msg.get("user", ""))
        text = msg.get("text", "")
        lines.append(f"{user}: {text}")
    return "\n".join(lines)


async def _cold_reply(event: dict, channel: str, thread_ts: str, msg_ts: str) -> None:
    """Spawn claude -p to respond to an unowned message."""
    import subprocess as sp

    sender = event.get("_sender_name", "someone")
    logger.info("Cold reply: thread=%s sender=%s", thread_ts, sender)

    # Add eyes reaction
    try:
        await _slack_client.reactions_add(channel=channel, timestamp=msg_ts, name="eyes")
    except Exception:
        pass

    # Fetch thread context
    context = await _fetch_thread_context(channel, thread_ts)

    system_prompt = (
        "You are a helpful AI assistant replying in a Slack thread. "
        "Keep responses concise. "
        "You have three response modes:\n"
        f"1. If no response is needed — respond with exactly: {NO_RESPONSE_SENTINEL}\n"
        f"2. If a simple emoji reaction is better — respond with: {REACT_PREFIX}<emoji_name> "
        f"(e.g. REACT:thumbsup, REACT:white_check_mark)\n"
        "3. Otherwise, respond normally with text.\n"
        "Use your judgment — a thumbs-up is often better than a wordy acknowledgment."
    )

    full_prompt = f"{system_prompt}\n\n## Thread context\n{context}"

    try:
        result = sp.run(
            [
                "claude", "-p", full_prompt,
                "--allowedTools",
                "Read", "Glob", "Grep",
                "mcp__slack-channel__reply",
                "mcp__slack-channel__get_thread",
                "mcp__slack-channel__read_history",
                "mcp__slack-channel__add_reaction",
                "mcp__claude_ai_Google_Calendar__*",
                "mcp__claude_ai_Gmail__gmail_search_messages",
                "mcp__claude_ai_Gmail__gmail_read_message",
                "mcp__claude_ai_Gmail__gmail_read_thread",
                "mcp__claude_ai_Gmail__gmail_get_profile",
                "mcp__claude_ai_Gmail__gmail_list_labels",
                "mcp__claude_ai_Notion__*",
            ],
            capture_output=True, text=True, timeout=300,
        )
        output = result.stdout.strip() or "(no response)"
    except sp.TimeoutExpired:
        output = "(timed out)"
    except Exception as e:
        output = f"(error: {e})"

    # Remove eyes
    try:
        await _slack_client.reactions_remove(channel=channel, timestamp=msg_ts, name="eyes")
    except Exception:
        pass

    # Handle response modes
    stripped = output.strip()
    if stripped == NO_RESPONSE_SENTINEL:
        logger.info("Cold reply: no response needed for %s", thread_ts)
        return

    if stripped.startswith(REACT_PREFIX):
        emoji = stripped[len(REACT_PREFIX):].strip()
        logger.info("Cold reply: reacting with :%s: to %s", emoji, thread_ts)
        try:
            await _slack_client.reactions_add(channel=channel, timestamp=msg_ts, name=emoji)
        except Exception:
            pass
        return

    # Send text reply
    try:
        if len(output) <= 4000:
            await _slack_client.chat_postMessage(
                channel=channel, text=output, thread_ts=thread_ts,
            )
        else:
            for i in range(0, len(output), 3900):
                await _slack_client.chat_postMessage(
                    channel=channel, text=output[i:i + 3900], thread_ts=thread_ts,
                )
    except Exception:
        logger.error("Cold reply: failed to send response", exc_info=True)


# ---------------------------------------------------------------------------
# Event bus: leader writes, all instances read
# ---------------------------------------------------------------------------

async def _watch_event_bus() -> None:
    """Tail the shared event bus file and send notifications for owned threads.

    Polls every 0.5s for new lines. Each instance filters for its own threads.
    """
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Start from end of file (don't replay old events)
    try:
        pos = _EVENTS_FILE.stat().st_size
    except OSError:
        pos = 0

    while True:
        await anyio.sleep(0.5)

        try:
            size = _EVENTS_FILE.stat().st_size
        except OSError:
            continue

        if size <= pos:
            if size < pos:
                pos = 0  # file was truncated
            continue

        try:
            with open(_EVENTS_FILE) as f:
                f.seek(pos)
                new_data = f.read()
                pos = f.tell()
        except OSError:
            continue


        # Reload threads from disk (another instance may have added threads via reply)
        _owned_threads.update(_load_threads())

        for line in new_data.strip().split("\n"):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            thread_ts = event.get("thread_ts")

            if thread_ts and thread_ts in _owned_threads:
                # Reply to an owned thread → notify this session
                logger.info(
                    "Event bus → notify: thread=%s sender=%s",
                    thread_ts, event.get("_sender_name", "?"),
                )
                msg_ts = event.get("ts", "")
                msg_channel = event.get("channel", "")
                if msg_ts and msg_channel:
                    try:
                        await _slack_client.reactions_add(
                            channel=msg_channel, timestamp=msg_ts, name="eyes",
                        )
                        _pending_eyes[thread_ts] = msg_ts
                    except Exception:
                        pass
                await _send_channel_notification(event)
            elif _is_leader:
                # Unowned message (including @mentions, new threads) → cold reply
                msg_ts = event.get("ts", "")
                msg_channel = event.get("channel", "")
                if msg_channel:
                    import asyncio
                    asyncio.get_event_loop().create_task(
                        _cold_reply(event, msg_channel, thread_ts or msg_ts, msg_ts)
                    )


# ---------------------------------------------------------------------------
# Slack Socket Mode listener (leader only)
# ---------------------------------------------------------------------------

def _create_slack_app(bot_token: str) -> AsyncApp:
    """Create the Slack Bolt async app with message handler."""
    app = AsyncApp(token=bot_token)

    @app.event("message")
    async def on_message(event: dict, say: Any) -> None:
        # Ignore subtypes (edits, deletes, joins, …) but allow file_share
        subtype = event.get("subtype")
        if subtype and subtype not in ("file_share",):
            return
        # Ignore bot messages
        if event.get("bot_id"):
            return
        # Ignore our own messages
        if event.get("user") == _bot_user_id:
            return
        # If a channel filter is set, only watch that channel
        if CHANNEL_ID and event.get("channel") != CHANNEL_ID:
            return

        # Resolve user name (cache it in the event for downstream)
        sender = await _resolve_user(event.get("user", ""))
        event["_sender_name"] = sender

        logger.info(
            "Socket Mode event: user=%s channel=%s thread=%s",
            sender, event.get("channel"), event.get("thread_ts"),
        )

        # Write to event bus — all instances will pick this up
        _append_event(event)

    return app


async def _run_slack_listener(app: AsyncApp, app_token: str) -> None:
    """Connect to Slack Socket Mode and listen forever (leader only)."""
    global _bot_user_id

    try:
        auth = await app.client.auth_test()
        _bot_user_id = auth["user_id"]
        logger.info("Leader: Slack connected — bot user %s (%s)", auth.get("user"), _bot_user_id)
    except Exception:
        logger.error("Slack auth_test failed", exc_info=True)
        return

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.start_async()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def _main() -> None:
    global _session, _slack_client, _is_leader, _bot_user_id

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")

    if not bot_token:
        logger.error("SLACK_BOT_TOKEN is required")
        sys.exit(1)

    # All instances need a Slack client for tools
    from slack_sdk.web.async_client import AsyncWebClient
    _slack_client = AsyncWebClient(token=bot_token)

    # Resolve bot user ID (needed for filtering even in non-leader instances)
    try:
        auth = await _slack_client.auth_test()
        _bot_user_id = auth["user_id"]
    except Exception:
        logger.warning("Could not resolve bot user ID", exc_info=True)

    # Leader election: try to grab the Socket Mode lock
    slack_app: AsyncApp | None = None
    if app_token:
        _is_leader = _try_become_leader()
        if _is_leader:
            logger.info("Elected as listener leader (pid=%d)", os.getpid())
            slack_app = _create_slack_app(bot_token)
            _slack_client = slack_app.client  # Use bolt's client (same token)
        else:
            logger.info("Another instance is the listener leader — watching event bus only")
    else:
        logger.warning("SLACK_APP_TOKEN not set — real-time notifications disabled")

    async with stdio_server() as (read_stream, write_stream):
        init_options = InitializationOptions(
            server_name="slack-channel",
            server_version="0.1.0",
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={"claude/channel": {}},
            ),
            instructions=(
                "Slack channel plugin. Listens for messages via Socket Mode and "
                "emits notifications/claude/channel for replies to threads you "
                "created and @mentions. Use the reply tool to send messages. "
                "Threads are automatically tracked per conversation and survive "
                "--resume. When you receive a channel notification, an eyes emoji "
                "reaction is automatically added to the message and removed when "
                "you reply in the thread."
            ),
        )

        async with AsyncExitStack() as stack:
            lifespan_ctx = await stack.enter_async_context(server.lifespan(server))
            session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, init_options)
            )
            _session = session

            async with anyio.create_task_group() as tg:
                # Leader: run Socket Mode listener
                if slack_app and app_token:
                    tg.start_soon(_run_slack_listener, slack_app, app_token)

                # ALL instances: watch event bus for notifications
                if app_token:
                    tg.start_soon(_watch_event_bus)

                # MCP message loop
                async for message in session.incoming_messages:
                    tg.start_soon(
                        server._handle_message,
                        message, session, lifespan_ctx, False,
                    )


def run() -> None:
    """CLI entrypoint."""
    anyio.run(_main)


if __name__ == "__main__":
    run()
