import { lazy, Suspense, type ComponentProps } from "react";

const Input = lazy(() => import("./ToolDetails").then((module) => ({ default: module.ToolInput })));
const Output = lazy(() => import("./ToolDetails").then((module) => ({ default: module.ToolOutput })));

export function ToolInput(props: ComponentProps<typeof Input>) {
  return <Suspense fallback={<span className="tool-lbl">读取参数…</span>}>
    <Input {...props} />
  </Suspense>;
}

export function ToolOutput(props: ComponentProps<typeof Output>) {
  return <Suspense fallback={<span className="tool-lbl">读取输出…</span>}>
    <Output {...props} />
  </Suspense>;
}
