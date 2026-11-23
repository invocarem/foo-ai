"""Fit a stored transcript to the provider context window.

Below 80% of the window the session is sent unchanged. At the trigger, tool
results are compressed, then one summary can replace the older prefix. Discard
runs only at the hard ceiling, 256 tokens under the window. Assistant messages
that stay in the transcript keep reasoning_content.
"""

from __future__ import annotations

COMPACT_RATIO_NUMERATOR = 80
TAIL_RATIO_NUMERATOR = 16
PROTOCOL_RESERVE = 256
PRUNE_RUNES = 8192
PRUNE_HEAD = 4096
PRUNE_TAIL = 1024
MIN_FOLD_TOKENS = 400
SUMMARY_CHARS = 8_000
SUMMARY_SNIP_CHARS = 300
DEFAULT_TOKENS_PER_CHAR = 0.25
PRUNE_MARKER = "[... tool result middle pruned ...]"

SUMMARY_PROMPT = (
    "Summarize this coding session for the assistant who will continue it. "
    "Record the user's goal, files changed, commands and their results, and unfinished work. "
    "Omit reasoning traces. Be concise."
)

SUMMARY_PREFIX = "Earlier conversation, summarized:\n"
SUMMARY_FALLBACK = "Earlier turns were removed to fit the context window."
DISCARD_MARKER = "Earlier turns were removed to fit the context window."


def trigger_tokens(context_window: int) -> int:
    """80% of the window. Zero or below disables maintenance."""
    if context_window <= 0:
        return 0
    return max(1, context_window * COMPACT_RATIO_NUMERATOR // 100)


def tail_budget_tokens(context_window: int) -> int:
    """Newest 16% of the window, kept verbatim around a summary."""
    if context_window <= 0:
        return 1
    return max(1, context_window * TAIL_RATIO_NUMERATOR // 100)


def hard_input_ceiling(context_window: int) -> int:
    """Physical bound. Discard starts here, not at the 80% trigger."""
    if context_window <= 0:
        return 0
    return max(1, context_window - PROTOCOL_RESERVE)


def message_size(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += len(message.get("content") or "")
        total += len(message.get("reasoning_content") or "")
        total += len(message.get("name") or "")
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += len(function.get("name") or "")
            total += len(function.get("arguments") or "")
    return total


def estimate_tokens(messages: list[dict], *, tokens_per_char: float = DEFAULT_TOKENS_PER_CHAR) -> int:
    return int(message_size(messages) * tokens_per_char)


def accept_token_ratio(prompt_tokens: int, characters: int) -> float | None:
    """Learn tokens per character from a normal round. Out-of-range ratios are ignored."""
    if prompt_tokens <= 0 or characters <= 0:
        return None
    ratio = prompt_tokens / characters
    if ratio <= 0.05 or ratio >= 2:
        return None
    return ratio


def prune_tool_results(messages: list[dict]) -> list[dict]:
    """Compress every oversized tool result. The same list comes back when nothing changes."""
    changed = False
    copied: list[dict] = []
    for message in messages:
        content = message.get("content") if message.get("role") == "tool" else None
        if not isinstance(content, str) or len(content) <= PRUNE_RUNES:
            copied.append(message)
            continue
        updated = dict(message)
        updated["content"] = content[:PRUNE_HEAD] + PRUNE_MARKER + content[-PRUNE_TAIL:]
        copied.append(updated)
        changed = True
    if not changed:
        return messages
    return copied


def summary_prefix(
    messages: list[dict], *, context_window: int, tokens_per_char: float = DEFAULT_TOKENS_PER_CHAR
) -> list[dict] | None:
    """Earlier turns to fold, or None when a summary would not shrink an over-trigger request.

    The latest user message through the end always stays. Older messages join that
    tail while they fit in 16% of the window. A prefix under 400 tokens is too
    small to spend a summary call on.
    """
    if trigger_tokens(context_window) <= 0:
        return None
    if estimate_tokens(messages, tokens_per_char=tokens_per_char) < trigger_tokens(context_window):
        return None
    head = 1 if messages and messages[0].get("role") == "system" else 0
    last_user = _latest_user(messages)
    if last_user is None or last_user <= head:
        return None
    start = last_user
    budget = tail_budget_tokens(context_window)
    if estimate_tokens(messages[start:], tokens_per_char=tokens_per_char) <= budget:
        while start > head:
            if estimate_tokens(messages[start - 1 :], tokens_per_char=tokens_per_char) > budget:
                break
            start -= 1
    while start > head and messages[start].get("role") == "tool":
        start -= 1
    if start <= head:
        return None
    prefix = messages[head:start]
    if estimate_tokens(prefix, tokens_per_char=tokens_per_char) < MIN_FOLD_TOKENS:
        return None
    return prefix


def transcript_for_summary(prefix: list[dict]) -> str:
    """Text of earlier turns, without reasoning traces and with short tool results."""
    parts: list[str] = []
    for message in prefix:
        role = message.get("role")
        if role == "tool":
            body = _snip_tool(message.get("content") or "")
            parts.append(f"[tool {message.get('name') or ''}] {body}")
            continue
        if role == "assistant":
            body = message.get("content") or ""
            names = [
                (call.get("function") or {}).get("name") or ""
                for call in message.get("tool_calls") or []
            ]
            called = ", ".join(name for name in names if name)
            if called:
                body = f"{body}\n[called {called}]" if body else f"[called {called}]"
            parts.append(f"[assistant] {body}")
            continue
        parts.append(f"[{role}] {message.get('content') or ''}")
    text = "\n".join(parts)
    if len(text) > 40_000:
        text = text[:40_000] + "\n…[truncated]"
    return text


def with_summary(messages: list[dict], summary: str, prefix: list[dict] | None = None) -> list[dict]:
    """Replace the summarized prefix with one user message. The tail stays."""
    head = messages[:1] if messages and messages[0].get("role") == "system" else []
    head_len = len(head)
    if prefix is None:
        prefix, _suffix = split_for_summary(messages)
    suffix = messages[head_len + len(prefix) :]
    text = summary.strip() or SUMMARY_FALLBACK
    if len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS] + "\n…[summary truncated]"
    folded = {"role": "user", "content": SUMMARY_PREFIX + text}
    return [*head, folded, *suffix]


def discard_to_trigger(
    messages: list[dict], *, context_window: int, tokens_per_char: float = DEFAULT_TOKENS_PER_CHAR
) -> list[dict]:
    """Drop the oldest groups once the transcript is at the hard ceiling.

    The same list comes back when the ceiling is not reached, when there is no
    user message, or when the live turn alone already fills the trigger.
    """
    if hard_input_ceiling(context_window) <= 0:
        return messages
    if estimate_tokens(messages, tokens_per_char=tokens_per_char) < hard_input_ceiling(context_window):
        return messages
    last_user = _latest_user(messages)
    if last_user is None:
        return messages
    head = messages[:1] if messages and messages[0].get("role") == "system" else []
    protected = messages[last_user:]
    if estimate_tokens([*head, *protected], tokens_per_char=tokens_per_char) >= trigger_tokens(context_window):
        return messages
    groups = _groups(messages[len(head) : last_user])
    kept = list(groups)
    dropped = False
    while kept and _discard_estimate(head, kept, protected, dropped, tokens_per_char) >= trigger_tokens(context_window):
        kept.pop(0)
        dropped = True
    if not dropped:
        return messages
    return _assemble_discard(head, kept, protected)


def split_for_summary(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split off the turns before the latest user message."""
    last_user = _latest_user(messages)
    head = 1 if messages and messages[0].get("role") == "system" else 0
    if last_user is None or last_user <= head:
        return [], messages
    return messages[head:last_user], messages[last_user:]


def _snip_tool(content: str) -> str:
    if len(content) > SUMMARY_SNIP_CHARS:
        return content[:SUMMARY_SNIP_CHARS] + f"\n…[snipped, {len(content)} chars total]"
    return content


def _latest_user(messages: list[dict]) -> int | None:
    return next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"), None)


def _groups(messages: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            groups.append(messages[index:end])
            index = end
            continue
        groups.append([message])
        index += 1
    return groups


def _discard_estimate(
    head: list[dict],
    kept: list[list[dict]],
    protected: list[dict],
    with_marker: bool,
    tokens_per_char: float,
) -> int:
    if with_marker:
        view = _assemble_discard(head, kept, protected)
    else:
        view = _assemble_kept(head, kept, protected)
    return estimate_tokens(view, tokens_per_char=tokens_per_char)


def _assemble_kept(head: list[dict], kept: list[list[dict]], protected: list[dict]) -> list[dict]:
    body: list[dict] = []
    for group in kept:
        body.extend(group)
    return [*head, *body, *protected]


def _assemble_discard(head: list[dict], kept: list[list[dict]], protected: list[dict]) -> list[dict]:
    body: list[dict] = [{"role": "user", "content": DISCARD_MARKER}]
    for group in kept:
        body.extend(group)
    return [*head, *body, *protected]
