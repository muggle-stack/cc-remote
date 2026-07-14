import type { Notice, NoticeCategory } from "../protocol";
import { Icon } from "../icons";

const CATEGORY_LABEL: Record<NoticeCategory, string> = {
  runtime: "运行时",
  guardian: "安全守护",
  config: "配置",
  deprecation: "兼容性",
  security: "安全",
  rate_limit: "使用限额",
};

export function NoticeStack({ notices, onDismiss }: {
  notices: Notice[];
  onDismiss: (noticeId: string) => void;
}) {
  if (!notices.length) return null;
  return <section className="notice-stack" aria-label="Codex 通知" aria-live="polite">
    {notices.map((notice) => <article
      key={notice.notice_id}
      className={`notice-bar ${notice.severity}`}
      role={notice.severity === "warning" ? "alert" : "status"}
    >
      <span className="notice-mark" aria-hidden="true" />
      <div className="notice-copy">
        <div className="notice-title">
          <small>{CATEGORY_LABEL[notice.category]}</small>
          <b>{notice.title}</b>
        </div>
        <p>{notice.message}</p>
        {notice.detail && <details>
          <summary>查看详情</summary>
          <pre>{notice.detail}</pre>
        </details>}
      </div>
      <button type="button" className="notice-dismiss"
        onClick={() => onDismiss(notice.notice_id)}
        aria-label={`关闭通知：${notice.title}`} title="关闭这条通知">
        <Icon name="close" size={15} />
      </button>
    </article>)}
  </section>;
}
