const TYPES: [RegExp, string, string, string][] = [
  [/\.(?:wav|wave|mp3|m4a|m4b|aac|flac|ogg|oga|opus|webm)$/, "音频", "AUD", "violet"],
  [/^(?:dockerfile(?:\.|$)|containerfile$)/, "Docker", "DK", "blue"],
  [/^(?:makefile|justfile)$|\.(?:sh|bash|zsh|fish|ps1)$/, "Shell / 构建脚本", "$_", "green"],
  [/\.(?:py|pyi|pyw)$/, "Python", "PY", "blue"],
  [/\.(?:ts|tsx|mts|cts)$/, "TypeScript", "TS", "blue"],
  [/\.(?:js|jsx|mjs|cjs)$/, "JavaScript", "JS", "amber"],
  [/\.(?:c|cc|cpp|cxx|h|hpp)$/, "C / C++", "C", "blue"],
  [/\.rs$/, "Rust", "RS", "amber"],
  [/\.go$/, "Go", "GO", "cyan"],
  [/\.(?:md|mdx|markdown)$/, "Markdown", "MD", "neutral"],
  [/\.(?:json|jsonc|jsonl)$/, "JSON", "{}", "amber"],
  [/\.(?:yaml|yml|toml|ini|conf|env)$|^\.(?:env|git)/, "配置文件", "CFG", "violet"],
  [/\.(?:css|scss|sass|less)$/, "样式文件", "#", "violet"],
  [/\.(?:html|htm|xml|svg|vue|svelte)$/, "页面 / 标记", "</>", "amber"],
];

/** Presentation only: retain the original path for preview/diff identity. */
export function fileType(path: string): { label: string; badge: string; tone: string } {
  const name = path.replaceAll("\\", "/").split("/").at(-1)?.toLowerCase() ?? "";
  const row = TYPES.find(([pattern]) => pattern.test(name));
  return { label: row?.[1] ?? "文件", badge: row?.[2] ?? "", tone: row?.[3] ?? "neutral" };
}

export function presentChangedPaths(paths: string[]) {
  const split = paths.map((path) => path.replaceAll("\\", "/").split("/"));
  const common = split[0]?.slice(0, -1) ?? [];
  for (const parts of split.slice(1)) {
    let count = 0;
    while (count < common.length && count < parts.length - 1 && common[count] === parts[count]) count++;
    common.length = count;
  }
  return paths.map((path, index) => ({
    path,
    name: split[index].at(-1) || path,
    // A lone file still gets its immediate parent for context. Multi-file
    // lists omit only the proven shared directory, never basename-dedupe.
    directory: split[index].slice(paths.length === 1 ? -2 : common.length, -1).join("/"),
    type: fileType(path),
  }));
}
