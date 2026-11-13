"""Public projections of native questions and their human answers."""

import json

from cc_remote.protocol import AskQuestion, AskUser


def native_question(event: AskUser) -> dict:
    pages = event.questions or [event]
    questions = []
    for page in pages:
        value = page.model_dump(include=set(AskQuestion.model_fields))
        if page.secret:
            value = {
                "question": "私密输入确认",
                "options": [],
                "allow_text": True,
                "secret": True,
                "multi_select": False,
            }
        questions.append(value)
    return {
        "kind": "native",
        "ask_id": event.ask_id,
        "status": "pending",
        "questions": questions,
    }


def question_text(questions: list[dict]) -> str:
    sections = []
    for question in questions:
        lines = [question["question"]]
        for option in question.get("options", []):
            label = option["label"]
            description = option.get("ds")
            lines.append(f"- {label}" + (f" — {description}" if description else ""))
        if question.get("plan_document"):
            lines.append(question["plan_document"])
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def answer_text(questions: list[dict], answers: list) -> str:
    return "补充回答：\n\n" + "\n\n".join(
        f"问题：{question['question']}\n回答："
        + ("\n".join(answer) if isinstance(answer, list) else answer)
        for question, answer in zip(questions, answers, strict=True)
    )


def async_question(message_id: str, questions: list[dict]) -> dict:
    return {
        "kind": "async",
        "ask_id": message_id,
        "status": "pending",
        "questions": [
            {
                "question": question["title"],
                "options": [
                    {"label": option} for option in question.get("options") or []
                ],
                "allow_text": True,
                "multi_select": False,
            }
            for question in questions
        ],
    }


def match_async_answer(question: dict, prompt: str) -> list[dict] | None:
    """Match the native supplemental-answer envelope without guessing from prose."""
    if not prompt.startswith("补充回答：\n\n"):
        return None
    remaining = prompt[len("补充回答：\n\n") :]
    reference = ""
    if remaining.startswith("问题 ID："):
        line, separator, remaining = remaining.partition("\n\n")
        try:
            question_id = json.loads(line[len("问题 ID：") :])
        except (ValueError, TypeError):
            return None
        if not separator or question_id != question["ask_id"]:
            return None
        reference = f"问题 ID：{json.dumps(question_id, ensure_ascii=False, separators=(',', ':'))}\n\n"
    pages = question["questions"]
    answers = []
    next_index = 0
    while remaining:
        index = next(
            (
                index
                for index in range(next_index, len(pages))
                if remaining.startswith(f"问题：{pages[index]['question']}\n回答：")
            ),
            None,
        )
        if index is None:
            return None
        title = pages[index]["question"]
        body = remaining[len(f"问题：{title}\n回答：") :]
        boundaries = [
            body.find(f"\n\n问题：{page['question']}\n回答：")
            for page in pages[index + 1 :]
        ]
        end = min(
            (position for position in boundaries if position >= 0), default=len(body)
        )
        answer = body[:end]
        if not answer.strip() or answer != answer.strip():
            return None
        answers.append({"question": title, "answer": answer})
        remaining = "" if end == len(body) else body[end + 2 :]
        next_index = index + 1
    rebuilt = (
        "补充回答：\n\n"
        + reference
        + "\n\n".join(
            f"问题：{item['question']}\n回答：{item['answer']}" for item in answers
        )
    )
    return answers if answers and rebuilt == prompt else None
