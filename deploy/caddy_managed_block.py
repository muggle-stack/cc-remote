#!/usr/bin/env python3
"""Merge cc-remote's Caddy site into a shared Caddyfile safely."""

from __future__ import annotations

import argparse
from pathlib import Path


BEGIN_MARKER = "# BEGIN cc-remote managed site"
END_MARKER = "# END cc-remote managed site"
GLOBAL_BEGIN_MARKER = "# BEGIN cc-remote managed server limits"
GLOBAL_END_MARKER = "# END cc-remote managed server limits"

_MANAGED_SERVER_LIMITS = """\
\t# BEGIN cc-remote managed server limits
\tservers {
\t\ttimeouts {
\t\t\tread_header 10s
\t\t\tread_body 15s
\t\t\twrite 30s
\t\t\tidle 2m
\t\t}
\t\tmax_header_size 64KB
\t}
\t# END cc-remote managed server limits
"""


class CaddyMergeError(ValueError):
    """The existing Caddyfile cannot be updated without manual review."""


def _global_block_span(text: str) -> tuple[int, int] | None:
    """Return the braces of the optional first global options block.

    Caddy allows comments before the block.  A small lexer is preferable to a
    brace-counting regex because quoted header values and comments may contain
    literal braces that must not terminate the block.
    """

    length = len(text)
    pos = 0
    while pos < length:
        if text[pos].isspace():
            pos += 1
            continue
        if text[pos] == "#":
            newline = text.find("\n", pos)
            pos = length if newline < 0 else newline + 1
            continue
        break
    if pos >= length or text[pos] != "{":
        return None

    opening = pos
    depth = 0
    quote: str | None = None
    escaped = False
    in_comment = False
    while pos < length:
        char = text[pos]
        if in_comment:
            if char == "\n":
                in_comment = False
            pos += 1
            continue
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            pos += 1
            continue
        if char == "#":
            in_comment = True
        elif char in {'"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return opening, pos
            if depth < 0:
                break
        pos += 1
    raise CaddyMergeError("unterminated Caddy global options block")


def _marker_line_spans(text: str, marker: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        if line.rstrip("\r\n").strip() == marker:
            spans.append((offset, end))
        offset = end
    # splitlines() returns nothing for an empty string and does not need a
    # special tail case: a non-newline-terminated marker is still one line.
    return spans


def _has_top_level_servers(text: str, opening: int, closing: int) -> bool:
    """Whether the global block already has an unmanaged ``servers`` option."""

    pos = opening + 1
    depth = 1
    quote: str | None = None
    escaped = False
    in_comment = False
    token = ""
    at_directive_start = True
    while pos < closing:
        char = text[pos]
        if in_comment:
            if char == "\n":
                in_comment = False
                if depth == 1:
                    at_directive_start = True
            pos += 1
            continue
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            pos += 1
            continue
        if char == "#":
            in_comment = True
            token = ""
        elif char in {'"', "`"}:
            quote = char
            token = ""
        elif char == "{":
            if depth == 1 and token:
                if at_directive_start and token == "servers":
                    return True
                at_directive_start = False
            depth += 1
            token = ""
        elif char == "}":
            depth -= 1
            token = ""
        elif (depth == 1 and at_directive_start
              and (char.isalnum() or char in "_-")):
            token += char
        else:
            if depth == 1:
                if token:
                    if token == "servers":
                        return True
                    token = ""
                    at_directive_start = False
                if char == "\n":
                    at_directive_start = True
        pos += 1
    return depth == 1 and at_directive_start and token == "servers"


def _render_global_limits(current_text: str) -> str:
    """Add or update the one managed catch-all Caddy ``servers`` block.

    Existing global options are preserved byte-for-byte.  An unmanaged
    ``servers`` option is rejected: silently adding a second catch-all (or one
    shadowed by listener-specific blocks) would make the timeout guarantee
    ambiguous, so that case needs an administrator to reconcile it explicitly.
    """

    begins = _marker_line_spans(current_text, GLOBAL_BEGIN_MARKER)
    ends = _marker_line_spans(current_text, GLOBAL_END_MARKER)
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1:
            raise CaddyMergeError(
                "expected exactly one complete managed server-limit marker pair"
            )
        begin_start, _begin_end = begins[0]
        end_start, end_end = ends[0]
        if begin_start >= end_start:
            raise CaddyMergeError("managed server-limit markers are out of order")
        global_span = _global_block_span(current_text)
        if global_span is None:
            raise CaddyMergeError(
                "managed server-limit markers must be inside the global options block"
            )
        opening, closing = global_span
        if not (opening < begin_start < end_start < closing):
            raise CaddyMergeError(
                "managed server-limit markers must be inside the global options block"
            )
        without_managed = (
            current_text[:begin_start]
            + (" " * (end_end - begin_start))
            + current_text[end_end:]
        )
        if _has_top_level_servers(without_managed, opening, closing):
            raise CaddyMergeError(
                "an unmanaged global servers block conflicts with cc-remote limits"
            )
        return (
            current_text[:begin_start]
            + _MANAGED_SERVER_LIMITS
            + current_text[end_end:]
        )

    global_span = _global_block_span(current_text)
    if global_span is None:
        managed_global = "{\n" + _MANAGED_SERVER_LIMITS + "}\n\n"
        return managed_global + current_text

    opening, closing = global_span
    if _has_top_level_servers(current_text, opening, closing):
        raise CaddyMergeError(
            "the existing global options block already contains an unmanaged "
            "servers option; merge the cc-remote timeouts manually"
        )
    prefix = current_text[:closing]
    separator = "" if prefix.endswith(("\n", "\r")) else "\n"
    return prefix + separator + _MANAGED_SERVER_LIMITS + current_text[closing:]


def render_managed_caddyfile(
    current_text: str, site_text: str, domain: str
) -> str:
    """Return a full Caddyfile with exactly one managed cc-remote block.

    Content outside the marker pair is preserved byte-for-byte. A Caddyfile
    written by the legacy installer (the rendered site template and nothing
    else) is migrated automatically. An unmarked occurrence of the requested
    domain is rejected rather than risking a duplicate site definition.
    """

    site = site_text.strip("\r\n")
    if not site:
        raise CaddyMergeError("cc-remote Caddy site template is empty")
    if domain not in site:
        raise CaddyMergeError("rendered Caddy site does not contain the domain")
    site_lines = site.splitlines()
    if BEGIN_MARKER in site_lines or END_MARKER in site_lines:
        raise CaddyMergeError("site template must not contain managed markers")

    managed = f"{BEGIN_MARKER}\n{site}\n{END_MARKER}\n"
    lines = current_text.splitlines(keepends=True)
    begin_positions = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == BEGIN_MARKER
    ]
    end_positions = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == END_MARKER
    ]

    if begin_positions or end_positions:
        if len(begin_positions) != 1 or len(end_positions) != 1:
            raise CaddyMergeError("expected exactly one complete managed marker pair")
        begin = begin_positions[0]
        end = end_positions[0]
        if begin >= end:
            raise CaddyMergeError("managed Caddy markers are out of order")
        prefix = "".join(lines[:begin])
        suffix = "".join(lines[end + 1 :])
        if domain in prefix or domain in suffix:
            raise CaddyMergeError(
                f"{domain} also appears outside the cc-remote managed block"
            )
        return _render_global_limits(prefix + managed + suffix)

    # setup-vps.sh historically replaced the entire Caddyfile with this exact
    # rendered template. Wrap that known legacy layout without duplicating it.
    if current_text.rstrip("\r\n") == site.rstrip("\r\n"):
        return _render_global_limits(managed)

    if domain in current_text:
        raise CaddyMergeError(
            f"{domain} already appears in an unmarked Caddy config; "
            "move it between the cc-remote managed markers before retrying"
        )

    base = current_text
    if base:
        if not base.endswith(("\n", "\r")):
            base += "\n"
        if not base.endswith(("\n\n", "\r\n\r\n")):
            base += "\n"
    return _render_global_limits(base + managed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True, type=Path)
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--domain", required=True)
    args = parser.parse_args()

    current = args.current.read_text() if args.current.exists() else ""
    site = args.site.read_text()
    try:
        merged = render_managed_caddyfile(current, site, args.domain)
    except CaddyMergeError as exc:
        parser.error(str(exc))
    args.output.write_text(merged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
