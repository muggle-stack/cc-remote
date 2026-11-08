import ReactMarkdown from "react-markdown";

/** Safe phrasing content for legends, option labels and compact entry buttons.
 * Keep the source untouched: native answer values are never rendered Markdown.
 * Images/HTML cannot fetch resources or create controls inside the question. */
export default function QuestionMarkdown({ children, links = true }: {
  children: string;
  links?: boolean;
}) {
  return <ReactMarkdown skipHtml unwrapDisallowed
    allowedElements={["p", "strong", "em", "code", "a", "br", "del"]}
    components={{
      p: ({ children }) => <span className="question-markdown-paragraph">{children}</span>,
      a: ({ children, href }) => links && href
        ? <a href={href} target="_blank" rel="noopener noreferrer"
          onClick={event => event.stopPropagation()}>{children}</a>
        : <span>{children}</span>,
    }}>{children}</ReactMarkdown>;
}
