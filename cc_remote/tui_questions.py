"""Native async question metadata, separate from blocking ask-user RPCs."""

from cc_remote.tui import _safe_remote_text


def supplemental_answer_prompt(answers):
    """Match Web's async-question-presentation.ts envelope exactly."""
    return "补充回答：\n\n" + "\n\n".join(
        f"问题：{question}\n回答：{answer}"
        for question, answer in answers
    )


def matches_reply(prompt, questions):
    if not prompt.startswith("补充回答：\n\n"):
        return False
    rest = prompt[len("补充回答：\n\n"):]
    answers = []
    next_index = 0
    while rest:
        index = next((
            i for i in range(next_index, len(questions))
            if rest.startswith(f"问题：{questions[i]['title']}\n回答：")
        ), None)
        if index is None:
            return False
        title = questions[index]["title"]
        body = rest[len(f"问题：{title}\n回答："):]
        boundaries = [
            end for q in questions[index + 1:]
            if (end := body.find(f"\n\n问题：{q['title']}\n回答：")) >= 0
        ]
        end = min(boundaries, default=len(body))
        answer = body[:end]
        if not answer or answer != answer.strip():
            return False
        answers.append((title, answer))
        rest = body[end + 2:] if end < len(body) else ""
        next_index = index + 1
    return bool(answers) and supplemental_answer_prompt(answers) == prompt


def message_metadata(message: dict) -> dict:
    if message.get("delivery") != "async":
        return {}
    result = {"delivery": "async"}
    questions = message.get("questions")
    if isinstance(questions, list):
        result["questions"] = [
            {"title": q["title"], "options": q.get("options") or []}
            for q in questions[:16]
            if isinstance(q, dict) and isinstance(q.get("title"), str)
        ]
    return result


def question_text(questions: list[dict], text: str = "") -> str:
    titles = [q["title"] for q in questions]
    lines = [text] if text.strip() and text.strip() not in titles else []
    for q in questions:
        lines.append(q["title"])
        lines.extend(
            f"  {index}. {label}"
            for index, label in enumerate(q.get("options") or [], 1)
        )
    return _safe_remote_text("\n".join(lines))


def pending_async(view) -> list:
    """Keep unrelated questions pending after a canonical supplemental reply.

    Derive this from the shared narrative, including history and other clients'
    replies. A transport-pending answer temporarily disables resubmission;
    rejection removes that receipt and makes the question answerable again.
    """
    in_flight = {
        identity
        for block in view.pending_messages.values()
        for identity in block.data.get("async_questions", [])
    }
    pending = {}
    for block in view.blocks:
        if block.role == "user" and block.data.get("status") != "failed":
            matches = [
                identity for identity, question in pending.items()
                if matches_reply(block.text, question.data["questions"])
            ]
            if len(matches) == 1:
                pending.pop(matches[0])
            elif not block.text.startswith("补充回答：\n\n"):
                pending.clear()
        if block.data.get("questions"):
            pending.setdefault(block.id, block)
    return [b for b in pending.values() if b.id not in in_flight]
