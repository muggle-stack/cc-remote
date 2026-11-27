"""Shared message format and MCP surface; no model credentials or engine ownership.

References are device-local, engine-qualified Wrapper routes. They are display
metadata, not capabilities. The receiving Wrapper validates every route.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

MAX_PROMPT = 32_000
MAX_FRAME = 256 * 1024
_REF = re.compile(r"^ccr:(claude|codex):([A-Za-z0-9][A-Za-z0-9._@-]{0,110})$")
_AGY_REF = re.compile(r"^ccr:antigravity:([A-Za-z0-9][A-Za-z0-9._-]{0,110})$")
_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPEN, _CLOSE = "<cc_remote_message>", "</cc_remote_message>"


def reference(engine: str, sid: str) -> str:
    component = sid.removeprefix("agy@") if engine == "antigravity" else sid
    value = f"ccr:{engine}:{component}"
    if split_reference(value) != (engine, sid):
        raise ValueError("Invalid Code session routing ID")
    return value


def split_reference(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        if match := _AGY_REF.fullmatch(value):
            return "antigravity", "agy@" + match[1]
        if len(value) <= 128 and (match := _REF.fullmatch(value)):
            return match[1], match[2]
    raise ValueError("Use an exact session_ref returned by list_sessions")


def request_id(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST.fullmatch(value):
        raise ValueError("request_id must be 1–128 letters, digits, dots, hyphens or underscores")
    return value


@dataclass(frozen=True)
class SessionMessage:
    source_thread_id: str
    prompt: str


def envelope(source: str, prompt: str) -> str:
    split_reference(source)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise ValueError("message must contain 1–32000 characters")
    return _OPEN + json.dumps({"source": source, "message": prompt}, ensure_ascii=False) + _CLOSE


def parse_message(text: object) -> SessionMessage | None:
    if not isinstance(text, str) or len(text) > MAX_FRAME:
        return None
    text = text.strip()
    if not text.startswith(_OPEN) or not text.endswith(_CLOSE):
        return None
    try:
        body = json.loads(text[len(_OPEN):-len(_CLOSE)])
        if not isinstance(body, dict) or set(body) != {"source", "message"}:
            return None
        envelope(body["source"], body["message"])
        return SessionMessage(body["source"], body["message"])
    except (ValueError, TypeError, KeyError):
        return None


def message_target(tool: object, arguments: object, server: object = None) -> str | None:
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        return None
    if not (tool in {"mcp__cc_remote_messages__send_message", "mcp__cc-remote-ask__send_message"}
            or (tool in {"send_message", "cc_remote_messages.send_message"}
                and (server == "cc_remote_messages"
                     or arguments.get("namespace") == "cc_remote_messages"))):
        return None
    try:
        target = arguments.get("session_ref")
        split_reference(target)
        return target
    except ValueError:
        return None


def receipt_status(content: object) -> str:
    """Only our explicit acceptance receipt can become 'sent'."""
    try:
        payload = json.loads(content) if isinstance(content, str) else content
        # Claude MCP text content can survive as an array in a native result.
        if isinstance(payload, list) and len(payload) == 1:
            return receipt_status(payload[0].get("text"))
        if isinstance(payload, dict) and payload.get("status") == "accepted":
            return "sent"
        if isinstance(payload, dict) and payload.get("status") == "rejected":
            return "failed"
        if isinstance(payload, dict) and payload.get("status") == "queued":
            return "queued"
    except (ValueError, TypeError, AttributeError):
        pass
    return "sending"  # unresolved; the UI says unconfirmed once the turn ends


def make_server(call):
    """One schema for the caller-bound Code session tools."""
    from mcp.server import Server
    from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

    server = Server("cc_remote_messages")
    schemas = {
        "list_sessions": ("Find recent Code sessions on this device, across Claude, Codex and Gemini. "
                          "Read-only; does not start or focus a session.",
                          {"search": {"type": "string", "maxLength": 200}}, []),
        "send_message": ("Send a user-authorized message to another session_ref from list_sessions. "
                         "May start model work with the TARGET's permissions, not the caller's sandbox. "
                         "Busy targets are durably queued until idle; external targets are rejected, never interrupted. "
                         "Use a stable request_id for retries: unknown means DO NOT resend under a new ID. "
                         "accepted means the native input was accepted, not that its task finished. "
                         "queued means not yet sent; use message_status to check later. "
                         "No automatic reply is sent back; explicitly ask the target to reply if needed.",
                         {"session_ref": {"type": "string", "maxLength": 128},
                          "message": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT},
                          "request_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                         ["session_ref", "message", "request_id"]),
        "message_status": ("Read this caller's retained send receipt. Never sends or resumes anything.",
                           {"request_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                           ["request_id"]),
    }

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description=description, inputSchema={
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False,
        }, annotations=ToolAnnotations(readOnlyHint=name != "send_message",
                                       destructiveHint=name == "send_message", openWorldHint=True))
                for name, (description, properties, required) in schemas.items()]

    @server.call_tool()
    async def call_tool(name, args):
        try:
            result = await call(name, args, server.request_context.meta)
            return CallToolResult(isError=result.get("status") == "rejected", content=[
                TextContent(type="text", text=json.dumps(result, ensure_ascii=False))])
        except ValueError as exc:
            return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(
                {"status": "rejected", "error": str(exc)}, ensure_ascii=False))])
        except (OSError, TimeoutError, ConnectionError):
            return CallToolResult(content=[TextContent(type="text", text=json.dumps({"status": "unknown", "error":
                "Message service unavailable. Delivery may be unconfirmed; retry only with the SAME request_id."}))])

    return server


async def rpc(path: Path, request: dict) -> dict:
    info = path.lstat()
    parent = path.parent.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("Message socket must be private and owned by this user")
    async with asyncio.timeout(45):
        reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_FRAME)
        try:
            writer.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            try:
                response = json.loads(await reader.readline())
                if not isinstance(response, dict):
                    raise ValueError("Invalid response")
            except (ValueError, UnicodeError) as exc:
                # A malformed/lost response follows transmission. It does not
                # prove rejection and must never become a safe-to-retry error.
                raise ConnectionError("Message acknowledgment unavailable") from exc
            if "error" in response and "status" not in response:
                raise ValueError(response["error"])
            return response
        finally:
            writer.close()
            await writer.wait_closed()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Cross-session messaging MCP for a local Wrapper")
    parser.add_argument("command", choices=["mcp"])
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    args = parser.parse_args(argv)

    async def serve():
        from mcp.server.stdio import stdio_server
        from cc_remote.task_store import request_thread

        async def call(name, arguments, meta):
            return await rpc(args.socket, {
                "codex_home": str(args.codex_home.expanduser().resolve()),
                "thread_id": request_thread(meta), "tool": name, "arguments": arguments,
            })

        server = make_server(call)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
