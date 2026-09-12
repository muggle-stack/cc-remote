import { lazy, Suspense, useRef, useState, type ChangeEvent, type ReactNode } from "react";
import { Icon } from "../icons";
const CenteredSheet = lazy(() => import("./CenteredSheet").then(module => ({ default: module.CenteredSheet })));

export function AttachmentPicker({ onPick, disabled = false, className = "cmdbtn",
  label = "添加附件", children }: {
  onPick: (files: FileList) => void;
  disabled?: boolean;
  className?: string;
  label?: string;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const photos = useRef<HTMLInputElement>(null);
  const files = useRef<HTMLInputElement>(null);
  const camera = useRef<HTMLInputElement>(null);
  const imported = (event: ChangeEvent<HTMLInputElement>) => {
    // The importer snapshots only the files that fit the remaining attachment
    // capacity. Let it capture the FileList synchronously before resetting it.
    try {
      const selected = event.currentTarget.files;
      if (selected?.length) onPick(selected);
    } finally {
      event.currentTarget.value = "";
    }
  };
  return <>
    <button type="button" className={className} disabled={disabled}
      aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={open && !disabled}
      onClick={() => setOpen(!open)}>
      {children ?? <Icon name="plus" size={19} />}
    </button>
    <input ref={photos} type="file" accept="image/*" multiple hidden aria-label="添加照片" onChange={imported} />
    <input ref={files} type="file" multiple hidden aria-label="添加文件" onChange={imported} />
    <input ref={camera} type="file" accept="image/*" capture="environment" hidden aria-label="拍照" onChange={imported} />
    {open && !disabled && <Suspense fallback={null}><CenteredSheet open label={label} onClose={() => setOpen(false)} maxWidth={380}>
      <div className="sheet-scroll attachment-choices">
        {[
          { name: "照片", description: "从相册选择图片", icon: "image", input: photos },
          { name: "文件", description: "添加文档、表格或其他文件", icon: "read", input: files },
          { name: "拍照", description: "使用相机拍摄照片", icon: "camera", input: camera },
        ].map(choice => <button key={choice.name} type="button" className="cmd" onClick={() => {
          // Keep the native picker inside the original user activation, with no
          // async import or delayed callback between the tap and input.click().
          choice.input.current?.click();
          setOpen(false);
        }}>
          <span className="cmd-ic"><Icon name={choice.icon} size={20} /></span>
          <span className="cmd-tx"><span className="cmd-nm">{choice.name}</span>
            <span className="cmd-ds">{choice.description}</span></span>
          <Icon name="chevron-right" size={15} />
        </button>)}
      </div>
    </CenteredSheet></Suspense>}
  </>;
}
