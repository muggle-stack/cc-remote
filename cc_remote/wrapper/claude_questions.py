"""Validation and structured outcomes for Claude's built-in user questions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cc_remote.protocol import (
    ASK_OPTION_DESCRIPTION_MAX_CHARS,
    ASK_OPTION_LABEL_MAX_CHARS,
    ASK_QUESTION_MAX_CHARS,
)

CLAUDE_QUESTION_MAX_COUNT = 4
CLAUDE_QUESTION_OPTION_MAX_COUNT = 4


class AskUnavailable(Exception):
    """The prompt ended without a user-provided answer."""


class AskTimeout(AskUnavailable):
    """No controlling client answered before the bounded deadline."""


class AskDeclined(AskUnavailable):
    """The user rejected a native elicitation form."""


class AskCancelled(AskUnavailable):
    """The surrounding turn or control task cancelled the prompt."""


class AskSuperseded(AskUnavailable):
    """A newer prompt intentionally replaced this one."""


@dataclass(frozen=True)
class ClaudeQuestion:
    question: str
    header: str | None
    options: tuple[dict[str, str], ...]
    multi_select: bool


def normalize_claude_questions(tool_input: dict[str, Any]) -> tuple[ClaudeQuestion, ...]:
    """Validate AskUserQuestion fields while preserving unknown SDK metadata."""
    raw_questions = tool_input.get("questions")
    if (not isinstance(raw_questions, list)
            or not 1 <= len(raw_questions) <= CLAUDE_QUESTION_MAX_COUNT):
        raise ValueError("AskUserQuestion requires 1-4 questions")

    normalized: list[ClaudeQuestion] = []
    seen_questions: set[str] = set()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            raise ValueError("AskUserQuestion contains an invalid question")
        question = raw.get("question")
        if (not isinstance(question, str) or not question.strip()
                or len(question) > ASK_QUESTION_MAX_CHARS):
            raise ValueError("AskUserQuestion question is missing or too long")
        if question in seen_questions:
            raise ValueError("AskUserQuestion question text must be unique")
        seen_questions.add(question)

        header = raw.get("header")
        if header is not None:
            if not isinstance(header, str) or len(header) > 512:
                raise ValueError("AskUserQuestion header is invalid")
            header = header.strip() or None

        multi_select = raw.get("multiSelect", False)
        if not isinstance(multi_select, bool):
            raise ValueError("AskUserQuestion multiSelect must be boolean")

        raw_options = raw.get("options")
        if (not isinstance(raw_options, list)
                or not 2 <= len(raw_options) <= CLAUDE_QUESTION_OPTION_MAX_COUNT):
            raise ValueError("AskUserQuestion requires 2-4 options")
        options: list[dict[str, str]] = []
        labels: set[str] = set()
        for option in raw_options:
            if not isinstance(option, dict):
                raise ValueError("AskUserQuestion contains an invalid option")
            label = option.get("label")
            if (not isinstance(label, str) or not label.strip()
                    or len(label) > ASK_OPTION_LABEL_MAX_CHARS):
                raise ValueError("AskUserQuestion option label is invalid")
            if label in labels:
                raise ValueError("AskUserQuestion option labels must be unique")
            labels.add(label)
            description = option.get("description", "")
            if (not isinstance(description, str)
                    or len(description) > ASK_OPTION_DESCRIPTION_MAX_CHARS):
                raise ValueError("AskUserQuestion option description is invalid")
            item = {"label": label}
            if description:
                item["ds"] = description
            options.append(item)

        normalized.append(ClaudeQuestion(
            question=question,
            header=header,
            options=tuple(options),
            multi_select=multi_select,
        ))
    return tuple(normalized)
