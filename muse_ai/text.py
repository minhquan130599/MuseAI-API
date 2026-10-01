from __future__ import annotations

import json
from typing import Any

ASSISTANT_ROLES = {"assistant", "model", "ai", "bot", "agent"}
TEXT_KEYS = ("text", "output_text", "reply", "response_text")
ROLE_KEYS = ("role", "author", "sender", "speaker")


def _role_of(node: dict[str, Any]) -> str | None:
    for key in ROLE_KEYS:
        value = node.get(key)
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, dict):
            for nested_key in ("role", "name", "type"):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    return nested.strip().lower()
    return None


def _append(out: list[str], value: str) -> None:
    text = value.strip()
    if not text:
        return
    if text not in out:
        out.append(text)


def extract_text_fragments(
    value: Any,
    *,
    assistant_only: bool = False,
) -> list[str]:
    """Extract human-readable text from Muse stream/history structures.

    Muse responses have changed shape across web-client revisions. This walker
    intentionally supports the common forms seen in chat streams/history:
    role+content messages, typed text blocks, delta objects, and JSON strings.
    """

    out: list[str] = []

    def walk(node: Any, inherited_assistant: bool = False) -> None:
        if node is None:
            return

        if isinstance(node, str):
            stripped = node.strip()
            if stripped.startswith(("{", "[")):
                try:
                    walk(json.loads(stripped), inherited_assistant)
                    return
                except (TypeError, ValueError):
                    pass
            if inherited_assistant or not assistant_only:
                _append(out, node)
            return

        if isinstance(node, list):
            for item in node:
                walk(item, inherited_assistant)
            return

        if not isinstance(node, dict):
            return

        role = _role_of(node)
        is_assistant = inherited_assistant or role in ASSISTANT_ROLES
        allow_text = is_assistant or not assistant_only
        consumed: set[str] = set()

        if allow_text:
            for key in TEXT_KEYS:
                raw = node.get(key)
                if isinstance(raw, str):
                    _append(out, raw)
                    consumed.add(key)

        # Common chat message content shapes:
        # {"role":"assistant","content":"..."}
        # {"role":"assistant","content":[{"type":"text","text":"..."}]}
        if "content" in node:
            content = node.get("content")
            if isinstance(content, str):
                if allow_text:
                    _append(out, content)
            else:
                walk(content, is_assistant)
            consumed.add("content")

        # Delta/message wrappers can contain nested text.
        for key in ("delta", "message", "output", "result", "data", "item"):
            if key in node:
                walk(node.get(key), is_assistant)
                consumed.add(key)

        # Typed text blocks without an explicit role.
        kind = node.get("type") or node.get("kind")
        if isinstance(kind, str) and kind.lower() in {
            "text",
            "output_text",
            "assistant_text",
            "message_text",
        }:
            raw = node.get("text")
            if isinstance(raw, str) and allow_text:
                _append(out, raw)
                consumed.add("text")

        # Recurse through nested structures, but don't scan arbitrary scalar
        # metadata such as IDs, URLs, MIME types, labels, etc.
        for key, child in node.items():
            if key in consumed or key in ROLE_KEYS or key in TEXT_KEYS:
                continue
            if isinstance(child, (dict, list)):
                walk(child, is_assistant)

    walk(value)
    return out


def extract_assistant_texts(value: Any) -> list[str]:
    return extract_text_fragments(value, assistant_only=True)


def coalesce_text_fragments(fragments: list[str]) -> str:
    """Combine stream fragments while handling cumulative delta snapshots."""

    clean = [fragment.strip() for fragment in fragments if fragment.strip()]
    if not clean:
        return ""

    # If each newer fragment includes the previous fragment, the stream is
    # sending cumulative snapshots. Return the longest/final snapshot.
    monotonic = True
    previous = clean[0]
    for current in clean[1:]:
        if not current.startswith(previous):
            monotonic = False
            break
        previous = current
    if monotonic:
        return clean[-1]

    # Otherwise treat them as incremental chunks. Avoid duplicate neighboring
    # chunks and preserve punctuation/spacing exactly as supplied.
    parts: list[str] = []
    for fragment in clean:
        if parts and fragment == parts[-1]:
            continue
        parts.append(fragment)

    # Full messages from history are usually separate strings. If any fragment
    # is much larger than the combined small deltas, prefer the longest one.
    longest = max(parts, key=len)
    if len(longest) >= 32 and all(
        piece == longest or piece in longest for piece in parts
    ):
        return longest

    return "".join(parts)
