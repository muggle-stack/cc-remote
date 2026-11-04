import { parse as parseJS } from "acorn";
import { generate, parse as parseCSS, walk as walkCSS, type CssNode } from "css-tree";
import parseSrcset from "parse-srcset";
import { Resources, VIRTUAL_ORIGIN } from "./resources";
import type { MediaAsset } from "./media";

interface Node { type: string; start: number; end: number; [key: string]: unknown }
interface Module { id: string; url: string; source?: string; compiled?: string }
interface Script { type: "module" | "classic"; source: string }
interface Edit { start: number; end: number; value: string }

function walk(node: unknown, visit: (node: Node) => void) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) { node.forEach((child) => walk(child, visit)); return; }
  const value = node as Node;
  if (typeof value.type === "string") visit(value);
  for (const [key, child] of Object.entries(value)) {
    if (!["start", "end", "type"].includes(key)) walk(child, visit);
  }
}

function apply(source: string, edits: Edit[]) {
  return edits.sort((a, b) => b.start - a.start).reduce(
    (result, edit) => result.slice(0, edit.start) + edit.value + result.slice(edit.end), source);
}

export class Compiler {
  private modules = new Map<string, Module>();
  private imports: Record<string, string> = Object.create(null);
  private styles = new Map<string, Promise<string>>();
  private styleEdges = new Map<string, Set<string>>();
  private count = 0;
  private resources: Resources;
  private scripts: Set<string>;
  private entry: string;

  constructor(resources: Resources, scripts: Set<string>, entry: string) {
    this.resources = resources; this.scripts = scripts; this.entry = entry;
  }

  private specifier(value: string, base: string): string {
    const urlLike = /^(?:\.?\.?\/|\/|[a-z][a-z0-9+.-]*:)/i.test(value);
    const key = urlLike ? new URL(value, base).href : value;
    const exact = this.imports[key];
    const prefix = exact ? undefined : Object.keys(this.imports)
      .filter((item) => item.endsWith("/") && key.startsWith(item))
      .sort((a, b) => b.length - a.length)[0];
    if (exact) value = exact;
    else if (prefix) {
      const target = this.imports[prefix];
      value = new URL(key.slice(prefix.length), target).href;
      if (!value.startsWith(target)) throw new Error("模块路径超出 import map 前缀范围。");
    } else if (!urlLike) {
      throw new Error(`未支持的模块别名：${value.slice(0, 120)}`);
    }
    const url = new URL(value, base);
    if (url.username || url.password) throw new Error("模块地址不能包含凭据。");
    if (url.origin !== VIRTUAL_ORIGIN) {
      if (url.protocol !== "https:" || !this.scripts.has(url.origin))
        throw new Error(`未登记的脚本来源：${url.origin}`);
      return url.href;
    }
    let module = this.modules.get(url.href);
    if (!module) {
      if (this.modules.size >= 256) throw new Error("预览模块数量超过上限。");
      module = { id: `@cc-viewer/${++this.count}`, url: url.href };
      this.modules.set(url.href, module);
    }
    return module.id;
  }

  private compileJS(source: string, url: string, module: boolean) {
    this.resources.checkCode(source, url);
    let ast;
    try { ast = parseJS(source, { ecmaVersion: "latest", sourceType: module ? "module" : "script" }); }
    catch { throw new Error(`无法解析预览脚本：${new URL(url).pathname}。`); }
    const edits: Edit[] = [];
    walk(ast, (node) => {
      if (["ImportDeclaration", "ExportNamedDeclaration", "ExportAllDeclaration", "ImportExpression"].includes(node.type)) {
        const target = node.source as Node | null;
        if (!target) return;
        if (target.type !== "Literal" || typeof target.value !== "string")
          throw new Error("此预览包含运行时动态模块地址，Bridge 暂不支持；请使用 Isolated 模式。");
        edits.push({ start: target.start, end: target.end, value: JSON.stringify(this.specifier(target.value, url)) });
      }
      if (node.type === "MemberExpression") {
        const object = node.object as Node, property = node.property as Node;
        if (object.type === "MetaProperty" && (object.meta as Node).name === "import"
            && ((!node.computed && property.type === "Identifier" && property.name === "url") || property.value === "url"))
          edits.push({ start: node.start, end: node.end, value: JSON.stringify(url) });
      }
      if (node.type === "NewExpression") {
        const api = String((node.callee as Node).name);
        if (["Worker", "SharedWorker"].includes(api))
          throw new Error(`此页面使用 ${api}，Bridge 暂不支持。请使用不依赖 Worker 的静态页面。`);
        if (["WebSocket", "XMLHttpRequest", "EventSource"].includes(api))
          throw new Error(`此页面使用 ${api}，Bridge 暂不支持。当前预览不代理后台连接，切换 Isolated 模式也无法接通后台服务。`);
      }
      if (node.type === "CallExpression") {
        const target = node.callee as Node;
        if (target.type === "MemberExpression" && (target.object as Node).name === "document"
            && ["write", "writeln"].includes(String((target.property as Node).name ?? (target.property as Node).value)))
          throw new Error("Bridge 暂不支持 document.write，请使用静态导出或 Isolated 模式。");
      }
    });
    return apply(source, edits);
  }

  private async css(source: string, base: string, ancestors = new Set<string>()): Promise<string> {
    this.resources.checkCode(source, base);
    const ast = parseCSS(source);
    const changes: Promise<void>[] = [];
    walkCSS(ast, (node: CssNode) => {
      if (node.type === "Atrule" && node.name.toLowerCase() === "import") {
        const target = node.prelude?.type === "AtrulePrelude" ? node.prelude.children.first : null;
        if (target?.type === "String" || target?.type === "Url") {
          const value = target.value;
          changes.push(this.stylesheet(value, base, ancestors).then((url) => { target.value = url; }));
        } else throw new Error("无法解析预览样式导入。");
        return walkCSS.skip;
      }
      if (node.type === "Url" && !node.value.startsWith("#")) {
        changes.push(this.resources.asset(node.value, base).then((url) => { node.value = url; }));
      }
    });
    await Promise.all(changes);
    return generate(ast);
  }

  private stylesheet(value: string, base: string, ancestors = new Set<string>()): Promise<string> {
    const url = this.resources.url(value, base).href;
    const edges = this.styleEdges.get(base) ?? new Set<string>();
    edges.add(url); this.styleEdges.set(base, edges);
    const visited = new Set<string>();
    const reachesBase = (candidate: string): boolean => {
      if (candidate === base) return true;
      if (visited.has(candidate)) return false;
      visited.add(candidate);
      return [...(this.styleEdges.get(candidate) ?? [])].some(reachesBase);
    };
    if (ancestors.has(url) || reachesBase(url)) return Promise.reject(new Error("预览样式含循环导入，Bridge 暂不支持。"));
    if (this.styles.size >= 256) return Promise.reject(new Error("预览样式数量超过上限。"));
    let promise = this.styles.get(url);
    if (!promise) {
      promise = this.resources.text(url).then((source) => this.css(source, url, new Set([...ancestors, url])))
        .then((source) => this.resources.blob(source, "text/css"));
      this.styles.set(url, promise);
    }
    return promise;
  }

  async document() {
    const html = await this.resources.text(this.entry, "document");
    const doc = new DOMParser().parseFromString(html, "text/html");
    const base = this.entry;
    if (doc.querySelector("base, iframe, frame, object, embed"))
      throw new Error("此页面使用自定义 base 或嵌入页面，Bridge 暂不支持；请使用 Isolated 模式。");
    // HTTP response policy remains authoritative; project metadata cannot relax it.
    doc.querySelectorAll("meta[http-equiv]").forEach((node) => node.remove());
    for (const element of doc.querySelectorAll('script[type="importmap"]')) {
      const value = JSON.parse(element.textContent ?? "{}");
      if (value.scopes || !value.imports || typeof value.imports !== "object" || Array.isArray(value.imports))
        throw new Error("Bridge 暂不支持带 scope 的 import map。");
      for (const [key, target] of Object.entries(value.imports)) {
        if (!key || typeof target !== "string" || key.endsWith("/") && !target.endsWith("/"))
          throw new Error("无效的 import map 模块映射。");
        const normalized = /^(?:\.?\.?\/|\/|[a-z][a-z0-9+.-]*:)/i.test(key) ? new URL(key, base).href : key;
        this.imports[normalized] = new URL(target, base).href;
      }
      element.remove();
    }
    const scripts: Script[] = [];
    for (const node of doc.querySelectorAll("script")) {
      if (scripts.length >= 256) throw new Error("预览脚本数量超过上限。");
      const type = node.getAttribute("type")?.toLowerCase() ?? "";
      if (!["", "module", "text/javascript", "application/javascript"].includes(type)) continue;
      if (node.hasAttribute("async")) throw new Error("Bridge 暂不支持异步执行顺序的脚本。");
      const src = node.getAttribute("src");
      if (type === "module") {
        if (src) scripts.push({ type: "module", source: this.specifier(new URL(src, base).href, base) });
        else {
          const url = new URL(base); url.searchParams.set("__cc_inline", String(++this.count));
          const id = this.specifier(url.href, base);
          this.modules.get(url.href)!.source = node.textContent ?? "";
          scripts.push({ type: "module", source: id });
        }
      } else if (src && new URL(src, base).origin !== VIRTUAL_ORIGIN) {
        scripts.push({ type: "classic", source: this.specifier(src, base) });
      } else {
        const url = src ? this.resources.url(src, base).href : base;
        const source = src ? await this.resources.text(url) : node.textContent ?? "";
        scripts.push({ type: "classic", source: this.resources.blob(this.compileJS(source, url, false), "text/javascript") });
      }
      node.remove();
    }
    // Fetch each discovered frontier in bounded batches; compiling only after
    // the reads preserves deterministic module identities and cyclic ESM.
    for (;;) {
      const batch = [...this.modules.values()].filter(module => module.compiled === undefined).slice(0, 4);
      if (!batch.length) break;
      const sources = await Promise.all(batch.map(module => module.source ?? this.resources.text(module.url)));
      batch.forEach((module, index) => { module.compiled = this.compileJS(sources[index], module.url, true); });
    }
    const imports: Record<string, string> = Object.create(null);
    for (const module of this.modules.values()) imports[module.id] = this.resources.blob(module.compiled!, "text/javascript");
    // Also expose original bare aliases to registered CDN modules.
    for (const [key, value] of Object.entries(this.imports)) {
      const local = this.modules.get(value);
      // Local aliases have already been resolved in the parsed module graph.
      if (local) imports[key] = imports[local.id];
      else if (new URL(value).origin !== VIRTUAL_ORIGIN) imports[key] = this.specifier(value, base);
    }
    const assets: Promise<unknown>[] = [];
    for (const link of doc.querySelectorAll<HTMLLinkElement>("link[href]")) {
      if (link.rel === "stylesheet") assets.push(this.stylesheet(link.getAttribute("href")!, base).then((url) => {
        link.href = url; link.removeAttribute("integrity"); link.removeAttribute("crossorigin");
      }));
      else link.remove(); // Preloads would bypass the resource channel.
    }
    for (const node of doc.querySelectorAll("style")) assets.push(this.css(node.textContent ?? "", base).then((css) => { node.textContent = css; }));
    for (const node of doc.querySelectorAll<HTMLElement>("[style]")) {
      // Parse declarations wrapped in a selector so the same URL walker applies.
      assets.push(this.css(`x{${node.getAttribute("style")}}`, base).then((css) => {
        node.setAttribute("style", css.slice(css.indexOf("{") + 1, -1));
      }));
    }
    const media: MediaAsset[] = [];
    for (const element of doc.querySelectorAll<HTMLMediaElement>("audio,video")) {
      const sources = [...element.querySelectorAll<HTMLSourceElement>("source[src]")];
      const source = sources.find(node => !node.type || element.canPlayType(node.type));
      const src = element.getAttribute("src") ?? source?.getAttribute("src");
      element.removeAttribute("src");
      sources.forEach(node => node.removeAttribute("src"));
      if (!src) continue;
      if (src.startsWith("data:")) { element.src = src; continue; }
      // Registration is cheap; the media scheduler bounds live decoders and
      // buffers separately. Sample galleries commonly contain dozens of tags.
      if (media.length >= 256) throw new Error("预览音视频标签超过 256 个上限。");
      media.push({ element, url: this.resources.url(src, base).href });
    }
    for (const node of doc.querySelectorAll("img[src],source[src],video[poster],image[href]")) {
      for (const attr of ["src", "poster", "href"]) {
        if (node.hasAttribute(attr)) assets.push(this.resources.asset(node.getAttribute(attr)!, base)
          .then((url) => { node.setAttribute(attr, url); }));
      }
    }
    for (const node of doc.querySelectorAll("[srcset]")) {
      assets.push(Promise.all(parseSrcset(node.getAttribute("srcset")!).map(async (item) => {
        const url = await this.resources.asset(item.url, base);
        return `${url}${item.w ? ` ${item.w}w` : item.d ? ` ${item.d}x` : ""}`;
      })).then((values) => { node.setAttribute("srcset", values.join(", ")); }));
    }
    await Promise.all(assets);
    await this.resources.verify();
    return { doc, scripts, imports, media };
  }
}
