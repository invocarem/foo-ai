"""Phone notification through ntfy.sh.

The page posts one message when a request finishes, fails, or starts waiting
on the user. The topic is the address, and with no signup it is also the
password, so a new one is three words from ``words.py`` rather than a fixed
default. This module builds that message and sends it. The settings file and
the decision to send live in the hub.
"""

from __future__ import annotations

import secrets
import urllib.error
import urllib.request

from foo_ai.config import valid_topic
from foo_ai.gui.words import WORDS

SERVER = "https://ntfy.sh"
_PREFIX = "foo-ai"
_WORDS = 3
_LIMIT = 160

_KINDS = {
    "ready": ("Foo AI is ready", "white_check_mark"),
    "error": ("Foo AI hit an error", "warning"),
    "approval": ("Foo AI needs approval", "hourglass_flowing_sand"),
    "question": ("Foo AI has a question", "question"),
    "test": ("Foo AI", "white_check_mark"),
}
_TEST_BODY = "Foo AI can reach this phone."


class NotifyError(Exception):
    """ntfy.sh did not accept the message. The text is safe to show."""


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the topic to another host. Fail instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NotifyError(f"HTTP {code}")


def random_topic() -> str:
    """A new topic: ``foo-ai-`` and three words, drawn without repeats.

    Three words from the 1296-word list are about 31 bits, which nobody finds
    by trying the words, and short enough to type into the phone by hand.
    """
    words = secrets.SystemRandom().sample(WORDS, _WORDS)
    return "-".join([_PREFIX, *words])


def one_line(text: str) -> str:
    """The message body: one line, cut at 160 characters with an ellipsis."""
    line = " ".join(text.split())
    if len(line) <= _LIMIT:
        return line
    return line[: _LIMIT - 1].rstrip() + "…"


def question_text(questions: list[dict]) -> str:
    """The question lines, in order, joined for one notification."""
    parts: list[str] = []
    for question in questions:
        text = question.get("question")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return " · ".join(parts)


def compose(kind: str, text: str) -> tuple[str, str, str]:
    """Title, body, and ntfy tag for one moment."""
    title, tag = _KINDS[kind]
    if kind == "test":
        return title, _TEST_BODY, tag
    body = one_line(text)
    return title, body or title, tag


def post_ntfy(topic: str, title: str, message: str, tag: str, *, timeout: float = 10.0) -> None:
    """POST one message to ntfy.sh. Raise ``NotifyError`` when it does not land.

    The error text never includes the topic. A redirect is a failure, and the
    message is not sent on to the new address.
    """
    if not valid_topic(topic):
        raise NotifyError("notification has no topic")
    request = urllib.request.Request(
        f"{SERVER}/{topic}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": tag,
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        response = opener.open(request, timeout=timeout)
    except NotifyError:
        raise
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise NotifyError("the notification server did not answer") from None
    try:
        response.read()
    finally:
        response.close()
