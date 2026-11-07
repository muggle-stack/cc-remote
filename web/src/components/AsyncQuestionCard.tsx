import type { AsyncQuestionSpec } from "../protocol";
import { Icon } from "../icons";
import QuestionMarkdown from "./QuestionMarkdown";

/** The question stays in the conversation; its editor lives outside virtual rows. */
export default function AsyncQuestionCard({ questions, answered, onOpen }: {
  questions: AsyncQuestionSpec[];
  answered: boolean;
  onOpen: () => void;
}) {
  return <button type="button" className="async-question-card"
    aria-haspopup="dialog" onClick={onOpen}>
    <span className="async-question-entry-icon" aria-hidden="true"><Icon name="message" size={20} /></span>
    <span className="async-question-entry-copy">
      <span className="async-question-entry-title"><QuestionMarkdown links={false}>
        {questions[0]?.title || "助手想确认一下"}
      </QuestionMarkdown></span>
      <span className={`async-question-entry-state${answered ? " answered" : ""}`}>
        {answered ? "已回答 · 查看问题" : "待回答 · 助手询问"}
        {questions.length > 1 && ` · ${questions.length} 个问题`}
      </span>
    </span>
    <span className="async-question-entry-chevron" aria-hidden="true"><Icon name="chevron-right" size={17} /></span>
  </button>;
}
