import type { DOMPurify as DOMPurifyInstance } from "dompurify";

export const MAX_SAFE_SVG_SOURCE_CHARS = 4 * 1024 * 1024;
export const MAX_SAFE_SVG_ELEMENTS = 10_000;
export const MAX_SAFE_SVG_DEPTH = 64;

const UNSAFE_CSS =
  /(?:@import|expression\s*\(|javascript:|url\s*\(\s*(?!['"]?#))/i;
const URL_ATTRIBUTES = new Set(["href", "xlink:href", "src"]);
const ANIMATION_TAGS = new Set([
  "animate", "set", "animatecolor", "animatemotion", "animatetransform",
]);
const ANIMATION_ATTRIBUTES = new Set(["from", "to", "calcmode"]);
const TRANSFORM_ATTRIBUTES = new Set([
  "transform", "gradientTransform", "patternTransform",
]);
// Only visual properties may change after sanitization. In particular, an
// animation must never restore a URL, event handler, style or another
// animation's attributeName after those have already been checked.
const ANIMATABLE_ATTRIBUTES = new Set([
  "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry",
  "width", "height", "dx", "dy", "d", "points", "pathLength", "viewBox",
  "preserveAspectRatio", "transform", "opacity", "visibility", "display",
  "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
  "stroke-opacity", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap",
  "stroke-linejoin", "stroke-miterlimit", "color", "color-interpolation",
  "paint-order", "vector-effect", "overflow", "transform-origin",
  "color-interpolation-filters", "color-rendering", "shape-rendering",
  "text-rendering", "image-rendering", "clip", "clip-path", "clip-rule",
  "mask", "filter", "marker-start", "marker-mid", "marker-end",
  "font-size", "font-weight", "font-stretch", "font-style", "font-family", "font-variant",
  "letter-spacing", "word-spacing", "text-anchor", "text-decoration",
  "baseline-shift", "dominant-baseline", "alignment-baseline", "rotate",
  "textLength", "lengthAdjust", "startOffset", "offset", "stop-color",
  "stop-opacity", "flood-color", "flood-opacity", "lighting-color",
  "gradientTransform", "gradientUnits", "spreadMethod", "fx", "fy", "fr",
  "patternTransform", "patternUnits", "patternContentUnits", "maskUnits",
  "maskContentUnits", "clipPathUnits", "markerWidth", "markerHeight",
  "markerUnits", "refX", "refY", "orient", "filterUnits", "primitiveUnits",
  "stdDeviation", "baseFrequency", "numOctaves", "seed", "scale",
  "surfaceScale", "diffuseConstant", "specularConstant", "specularExponent",
  "limitingConeAngle", "azimuth", "elevation", "pointsAtX", "pointsAtY",
  "pointsAtZ", "z", "k1", "k2", "k3", "k4", "kernelMatrix",
  "kernelUnitLength", "divisor", "bias", "targetX", "targetY", "radius",
  "amplitude", "exponent", "intercept", "slope", "tableValues",
  "values", "type", "operator", "in", "in2", "result", "order", "edgeMode",
  "stitchTiles", "preserveAlpha", "xChannelSelector", "yChannelSelector",
]);
const INTERNAL_REFERENCE = /^#[a-zA-Z_][\w:.-]*$/;

function sanitizeSvgReferencesAndAnimations(root: Element): void {
  const elements = [root, ...root.querySelectorAll("*")];
  const ids = new Map<string, Element>();
  for (const element of elements) {
    if (element.id && !ids.has(element.id)) ids.set(element.id, element);
  }
  const reference = (element: Element): Element | null => {
    const href = element.getAttribute("href") ?? element.getAttribute("xlink:href");
    return href && INTERNAL_REFERENCE.test(href) ? ids.get(href.slice(1)) ?? null : null;
  };
  const uses = new Map<Element, Element>();
  for (const element of elements) {
    const tag = element.localName.toLowerCase();
    if (tag === "use" || tag === "mpath") {
      const target = reference(element);
      if (!target || (tag === "mpath" && target.localName !== "path")) {
        element.remove();
      } else if (tag === "use") {
        uses.set(element, target);
      }
    }
    if (!ANIMATION_TAGS.has(tag)) continue;
    const hasReference = element.hasAttribute("href") || element.hasAttribute("xlink:href");
    const target = hasReference ? reference(element) : element.parentElement;
    const attribute = element.getAttribute("attributeName") ?? "";
    const badValue = ["from", "to", "by", "values"].some(name => {
      const value = element.getAttribute(name) ?? "";
      // CSS escapes/comments must not hide a URL from the resource check.
      // eslint-disable-next-line no-control-regex -- Controls can disguise resource URLs.
      return UNSAFE_CSS.test(value) || /\\|\/\*|\*\/|[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);
    });
    if (!target || !root.contains(target)
        || ANIMATION_TAGS.has(target.localName.toLowerCase())
        || ["style", "script", "mpath"].includes(target.localName)
        || (tag !== "animatemotion" && !ANIMATABLE_ATTRIBUTES.has(attribute))
        || (tag === "animatetransform" && !TRANSFORM_ATTRIBUTES.has(attribute))
        || badValue) {
      element.remove();
    }
  }

  // A tiny source can expand exponentially through <use>. Apply the same
  // size/depth limits to the referenced tree, and reject recursive references.
  if (!uses.size) return;
  const visiting = new Set<Element>();
  const sizes = new Map<Element, { size: number; depth: number }>();
  const measure = (element: Element, level: number): { size: number; depth: number } => {
    if (level > MAX_SAFE_SVG_DEPTH) throw new Error("SVG 引用嵌套过深");
    if (visiting.has(element)) throw new Error("SVG 存在循环引用");
    const cached = sizes.get(element);
    if (cached) return cached;
    visiting.add(element);
    const children = [...element.children];
    const target = uses.get(element);
    if (target) children.push(target);
    let size = 1;
    let depth = 1;
    for (const child of children) {
      const measured = measure(child, level + 1);
      size += measured.size;
      depth = Math.max(depth, measured.depth + 1);
      if (size > MAX_SAFE_SVG_ELEMENTS) throw new Error("SVG 引用元素过多");
      if (depth > MAX_SAFE_SVG_DEPTH) throw new Error("SVG 引用嵌套过深");
    }
    visiting.delete(element);
    const result = { size, depth };
    sizes.set(element, result);
    return result;
  };
  measure(root, 1);
}

export function removeSvgRootLayoutStyles(
  root: Element & ElementCSSInlineStyle,
): void {
  root.style.removeProperty("width");
  root.style.removeProperty("height");
  root.style.removeProperty("max-width");
  root.style.removeProperty("max-height");
  if (!root.getAttribute("style")?.trim()) root.removeAttribute("style");
}

function assertSvgComplexity(root: Element): void {
  const elements = [root, ...root.querySelectorAll("*")];
  if (elements.length > MAX_SAFE_SVG_ELEMENTS) {
    throw new Error("SVG 元素过多");
  }
  const stack: Array<{ element: Element; depth: number }> = [
    { element: root, depth: 1 },
  ];
  while (stack.length) {
    const current = stack.pop()!;
    if (current.depth > MAX_SAFE_SVG_DEPTH) {
      throw new Error("SVG 嵌套过深");
    }
    for (const child of Array.from(current.element.children)) {
      stack.push({ element: child, depth: current.depth + 1 });
    }
  }
}

export function sanitizeSvgMarkup(
  svg: string,
  DOMPurify: DOMPurifyInstance,
  options: { removeRootLayout?: boolean } = {},
): string {
  if (!svg.trim()) throw new Error("SVG 内容为空");
  if (svg.length > MAX_SAFE_SVG_SOURCE_CHARS) {
    throw new Error("SVG 超过 4 MiB 安全限制");
  }
  const clean = DOMPurify.sanitize(svg, {
    RETURN_DOM_FRAGMENT: true,
    USE_PROFILES: { svg: true, svgFilters: true },
    ADD_TAGS: ["animate", "set", "use"],
    ADD_ATTR: (name, tag) => ANIMATION_TAGS.has(tag)
      && ANIMATION_ATTRIBUTES.has(name),
    FORBID_TAGS: [
      "script",
      "foreignObject",
      "iframe",
      "object",
      "embed",
      "image",
    ],
    FORBID_ATTR: ["src", "target"],
  });
  // DOMPurify's HTML string serialization can turn a numeric SVG entity into
  // &nbsp;, which is undefined in XML. Serialize the sanitized DOM as XML so
  // text and namespaces survive without allowing DTD/entity resolution.
  const sanitized = new XMLSerializer().serializeToString(clean);
  const namespaced = sanitized.includes("xlink:")
    && !/\sxmlns:xlink=/.test(sanitized)
    ? sanitized.replace(
        /^<svg\b/,
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink"',
      )
    : sanitized;
  const document = new DOMParser().parseFromString(
    namespaced,
    "image/svg+xml",
  );
  const root = document.documentElement;
  if (document.querySelector("parsererror") || root.localName !== "svg") {
    throw new Error("SVG 根元素无效");
  }
  assertSvgComplexity(root);
  if (options.removeRootLayout) removeSvgRootLayoutStyles(root);

  for (const anchor of Array.from(root.querySelectorAll("a"))) {
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    for (const attribute of Array.from(anchor.attributes)) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || URL_ATTRIBUTES.has(name)
          || name === "target") continue;
      group.setAttribute(attribute.name, attribute.value);
    }
    while (anchor.firstChild) group.append(anchor.firstChild);
    anchor.replaceWith(group);
  }

  sanitizeSvgReferencesAndAnimations(root);

  for (const element of [root, ...root.querySelectorAll("*")]) {
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      const internalReference = (name === "href" || name === "xlink:href")
        && INTERNAL_REFERENCE.test(value);
      if (name.startsWith("on")
          || (URL_ATTRIBUTES.has(name) && !internalReference)) {
        element.removeAttribute(attribute.name);
      } else if (name === "style" && UNSAFE_CSS.test(value)) {
        element.removeAttribute(attribute.name);
      }
    }
  }
  for (const style of root.querySelectorAll("style")) {
    if (UNSAFE_CSS.test(style.textContent || "")) style.remove();
  }
  return new XMLSerializer().serializeToString(root);
}
