import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

const svg = (body: string) => `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="240" height="240">${body}</svg>`;
const animatedSvg = svg(`
  <defs><g id="tile"><rect width="20" height="20" fill="red"/></g>
    <path id="route" d="M0 0 L100 0"/>
    <filter id="tint"><feColorMatrix id="matrix" type="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 1 0">
      <animate attributeName="values" from="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 1 0" to="0 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 1 0" dur="2s" repeatCount="indefinite"/>
    </feColorMatrix></filter></defs>
  <rect id="fade" width="20" height="20"><animate attributeName="opacity" from="0" to="1" dur="2s" repeatCount="indefinite"/></rect>
  <rect id="spin" y="30" width="20" height="10"><animateTransform attributeName="transform" type="rotate" from="0" to="180" dur="2s" repeatCount="indefinite"/></rect>
  <circle id="move" r="5" cy="70"><animateMotion dur="2s" repeatCount="indefinite"><mpath xlink:href="#route"/></animateMotion></circle>
  <rect id="ease" y="90" width="10" height="10"><animate attributeName="x" values="0;100" keyTimes="0;1" calcMode="spline" keySplines="0.42 0 1 1" dur="2s" repeatCount="indefinite"/></rect>
  <rect id="paint" y="110" width="20" height="20" fill="red"><animate attributeName="fill" from="#ff0000" to="#0000ff" dur="2s" repeatCount="indefinite"/></rect>
  <rect id="reveal" y="140" width="20" height="20" opacity="0"><set attributeName="opacity" to="1" begin="0.5s" dur="1s"/></rect>
  <rect id="first" y="170" width="20" height="20"><animate id="phase" attributeName="opacity" from="0" to="1" dur="0.5s"/></rect>
  <rect id="second" y="200" width="20" height="20"/>
  <animate xlink:href="#second" attributeName="x" from="0" to="100" begin="phase.end" dur="1s" fill="freeze"/>
  <rect id="relative" x="10" width="10" height="10"><animate attributeName="x" by="100" dur="2s" repeatCount="indefinite"/></rect>
  <path id="morph" d="M0 0 L20 0 L20 20 Z"><animate attributeName="d" from="M0 0 L20 0 L20 20 Z" to="M0 0 L60 0 L60 20 Z" dur="2s" repeatCount="indefinite"/></path>
  <use id="copy" href="#tile" x="180" y="180"/>
`);

async function open(page: Page, source: string, mode = "docked") {
  await page.addInitScript(data => {
    (window as unknown as { svgFixture: string }).svgFixture = data;
    const blobs = new Map<string, Blob>();
    (window as unknown as { svgBlobs: Map<string, Blob> }).svgBlobs = blobs;
    const create = URL.createObjectURL.bind(URL);
    URL.createObjectURL = value => {
      const url = create(value);
      if (value instanceof Blob) blobs.set(url, value);
      return url;
    };
  }, Buffer.from(source).toString("base64"));
  const csp = readFileSync(new URL("../../deploy/Caddyfile", import.meta.url), "utf8")
    .match(/Content-Security-Policy "([^"]+)"/)![1];
  await page.route("**/tests/history-browser.html*", async route => {
    const response = await route.fetch();
    const body = await response.text();
    const preamble = body.match(/<script type="module">([\s\S]*?)<\/script>/)?.[1];
    const policy = preamble ? csp.replace("script-src 'self'",
      `script-src 'self' 'sha256-${createHash("sha256").update(preamble).digest("base64")}'`) : csp;
    await route.fulfill({ response, body, headers: { ...response.headers(), "content-security-policy": policy } });
  });
  await page.goto(`/tests/history-browser.html?artifact-${mode === "markdown" ? "markdown-svg" : "svg"}=1${mode === "centered" ? "&image-centered=1" : ""}`);
  const image = page.getByRole("img", { name: mode === "markdown" ? "diagram" : "diagram.svg", exact: true });
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate(node => (node as HTMLImageElement).naturalWidth)).toBe(240);
  return image;
}

test("artifact-svg retains native animation values, easing, timing and local references", async ({ page }) => {
  const image = await open(page, animatedSvg);
  const clean = await image.evaluate(node => (window as unknown as {
    svgBlobs: Map<string, Blob>;
  }).svgBlobs.get((node as HTMLImageElement).src)!.text());
  const results = await page.evaluate(source => {
    const host = document.createElement("div");
    host.innerHTML = source;
    document.body.append(host);
    const root = host.querySelector("svg")!;
    root.pauseAnimations();
    const sample = (time: number) => {
      root.setCurrentTime(time);
      const get = (id: string) => host.querySelector<SVGGraphicsElement>(`#${id}`)!;
      const x = (id: string) => (get(id) as SVGRectElement).x.animVal.value;
      return {
        fade: Number(getComputedStyle(get("fade")).opacity),
        spin: get("spin").transform.animVal.getItem(0).angle,
        move: get("move").getCTM()!.e,
        ease: x("ease"), paint: getComputedStyle(get("paint")).fill,
        reveal: Number(getComputedStyle(get("reveal")).opacity),
        second: x("second"), relative: x("relative"),
        morph: get("morph").getBBox().width,
        copy: get("copy").getBBox().width,
        filter: host.querySelector<SVGFEColorMatrixElement>("#matrix")!.values.animVal.getItem(0).value,
      };
    };
    const result = { early: sample(0.25), middle: sample(1), late: sample(1.75) };
    host.remove();
    return result;
  }, clean);
  expect(results.early.fade).toBeCloseTo(0.125, 2);
  expect(results.middle.fade).toBeCloseTo(0.5, 2);
  expect(results.middle.spin).toBeCloseTo(90, 1);
  expect(results.middle.move).toBeCloseTo(50, 1);
  expect(results.middle.ease).toBeGreaterThan(25);
  expect(results.middle.ease).toBeLessThan(40);
  expect(results.middle.paint).not.toBe(results.early.paint);
  expect(results.early.reveal).toBe(0);
  expect(results.middle.reveal).toBe(1);
  expect(results.late.reveal).toBe(0);
  expect(results.middle.second).toBeCloseTo(50, 1);
  expect(results.late.second).toBeCloseTo(100, 1);
  expect(results.middle.relative).toBeCloseTo(60, 1);
  expect(results.middle.morph).toBeCloseTo(40, 1);
  expect(results.middle.copy).toBeCloseTo(20, 1);
  expect(results.middle.filter).toBeCloseTo(0.5, 2);
});

test("artifact-svg preserves gradient and pattern transform animation timelines", async ({ page }) => {
  const definitions = ["gradient", "pattern"].flatMap(kind =>
    ["values", "from-to"].map(syntax => {
      const id = `${kind}-${syntax}`;
      const animation = `<animateTransform attributeName="${kind}Transform" type="rotate" ${syntax === "values" ? 'values="0;180"' : 'from="0" to="180"'} dur="2s" repeatCount="indefinite"/>`;
      return kind === "gradient"
        ? `<linearGradient id="${id}"><stop stop-color="red"/><stop offset="1" stop-color="blue"/>${animation}</linearGradient>`
        : `<pattern id="${id}" width="20" height="20" patternUnits="userSpaceOnUse"><rect width="10" height="20" fill="green"/>${animation}</pattern>`;
    })).join("");
  const image = await open(page, svg(`<defs>${definitions}</defs>
    <rect width="120" height="120" fill="url(#gradient-values)"/>
    <rect x="120" width="120" height="120" fill="url(#gradient-from-to)"/>
    <rect y="120" width="120" height="120" fill="url(#pattern-values)"/>
    <rect x="120" y="120" width="120" height="120" fill="url(#pattern-from-to)"/>`));
  const clean = await image.evaluate(node => (window as unknown as {
    svgBlobs: Map<string, Blob>;
  }).svgBlobs.get((node as HTMLImageElement).src)!.text());
  const frames = await page.evaluate(source => {
    const host = document.createElement("div");
    host.innerHTML = source;
    document.body.append(host);
    const root = host.querySelector("svg")!;
    root.pauseAnimations();
    const sample = (time: number) => {
      root.setCurrentTime(time);
      return [...root.querySelectorAll<SVGLinearGradientElement | SVGPatternElement>("linearGradient,pattern")].map(element => {
        const transforms = ("gradientTransform" in element
          ? element.gradientTransform : element.patternTransform).animVal;
        return transforms.numberOfItems ? transforms.getItem(0).angle : null;
      });
    };
    const result = [sample(0.5), sample(1), sample(1.5)];
    host.remove();
    return result;
  }, clean);
  for (const [index, frame] of frames.entries()) {
    expect(frame).toHaveLength(4);
    for (const angle of frame) expect(angle).toBeCloseTo(45 * (index + 1), 2);
  }
});

for (const mode of ["docked", "centered", "markdown"]) {
  test(`artifact-svg plays SMIL and CSS animations in the ${mode} image preview`, async ({ page }) => {
    const image = await open(page, svg(`
      <style>@keyframes pulse {from {opacity:.1} to {opacity:1}} .pulse {animation:pulse 0.8s linear infinite}</style>
      <defs><rect id="square" width="240" height="120"/></defs>
      <use href="#square" fill="red"><animate attributeName="fill" values="red;blue;red" dur="0.8s" repeatCount="indefinite"/></use>
      <rect y="120" width="240" height="120" fill="green" class="pulse"/>`), mode);
    const bounds = (await image.boundingBox())!;
    const frames = [new Set<string>(), new Set<string>()];
    for (let frame = 0; frame < 4; frame++) {
      for (let half = 0; half < 2; half++) {
        // Sample each animation independently: one working animation must not
        // hide a frozen SMIL or CSS half of the image.
        const shot = await page.screenshot({ animations: "allow", clip: {
          x: bounds.x + bounds.width / 4,
          y: bounds.y + bounds.height * (half / 2 + 0.125),
          width: bounds.width / 2, height: bounds.height / 4,
        } });
        frames[half].add(shot.toString("base64"));
      }
      await page.waitForTimeout(110);
    }
    expect(frames[0].size).toBeGreaterThan(1);
    expect(frames[1].size).toBeGreaterThan(1);
  });
}

test("artifact-svg cannot animate unsafe attributes or external resources after sanitization", async ({ page }) => {
  const requests: string[] = [];
  await page.route("https://svg-test.invalid/**", route => {
    requests.push(route.request().url());
    return route.abort();
  });
  const disallowed = ["href", "xlink:href", "HREF", "style", "onclick", "onload", "id", "class", "attributeName"];
  const image = await open(page, svg(`
    <rect id="safe" width="240" height="240" fill="red"/>
    ${disallowed.map((name, i) => `<set id="bad-${i}" href="#safe" attributeName="${name}" to="javascript:alert(1)" begin="0s"/>`).join("")}
    <animate id="external-target" href="https://svg-test.invalid/target.svg#x" attributeName="opacity" values="0;1" dur="1s"/>
    <animate id="outside-target" href="#outside" attributeName="opacity" values="0;1" dur="1s"/>
    <set id="external-paint" href="#safe" attributeName="fill" to="url(https://svg-test.invalid/a.svg#x)"/>
    <set id="escaped-paint" href="#safe" attributeName="fill" to="u\\72l(https://svg-test.invalid/a.svg#x)"/>
    <set id="comment-paint" href="#safe" attributeName="fill" to="url(/*x*/https://svg-test.invalid/a.svg#x)"/>
    <use id="external-use" href="https://svg-test.invalid/a.svg#x"/>
    <use id="outside-use" href="#outside"/>
    <script>window.svgScriptRan=true</script>
    <rect width="10" height="10" onload="window.svgScriptRan=true"/>
    <animate id="good" href="#safe" attributeName="opacity" from="0.2" to="1" dur="2s" repeatCount="indefinite"/>
    <set id="mutate-animation" href="#good" attributeName="attributeName" to="href"/>
    <animateTransform id="wrong-transform-target" href="#safe" attributeName="opacity" type="scale" values="1;2" dur="1s"/>
  `));
  const result = await image.evaluate(async node => {
    const text = await (window as unknown as {
      svgBlobs: Map<string, Blob>;
    }).svgBlobs.get((node as HTMLImageElement).src)!.text();
    const doc = new DOMParser().parseFromString(text, "image/svg+xml");
    return { text, animations: [...doc.querySelectorAll("animate,set,animateTransform")].map(el => el.id), uses: doc.querySelectorAll("use").length };
  });
  expect(result.animations).toEqual(["good"]);
  expect(result.uses).toBe(0);
  expect(result.text).not.toMatch(/<script|onload=|javascript:|svg-test\.invalid/);
  await page.evaluate(async text => {
    const outside = document.createElement("div");
    outside.id = "outside";
    document.body.append(outside);
    const host = document.createElement("div");
    host.innerHTML = text;
    document.body.append(host);
    host.querySelector("svg")!.setCurrentTime(1);
    await new Promise(resolve => requestAnimationFrame(resolve));
    if (getComputedStyle(outside).opacity !== "1") throw new Error("animation escaped its SVG");
    if ((window as unknown as { svgScriptRan?: boolean }).svgScriptRan) throw new Error("SVG script executed");
    host.remove(); outside.remove();
  }, result.text);
  expect(requests).toEqual([]);
});

test("artifact-svg bounds recursive and exponentially expanded use references", async ({ page }) => {
  await open(page, svg('<rect width="240" height="240"/>'));
  const expansion = Array.from({ length: 15 }, (_, index) =>
    `<g id="g${index}">${index ? `<use href="#g${index - 1}"/><use href="#g${index - 1}"/>` : '<rect width="1" height="1"/>'}</g>`).join("");
  const chain = Array.from({ length: 70 }, (_, index) =>
    `<g id="g${index}">${index ? `<use href="#g${index - 1}"/>` : '<rect width="1" height="1"/>'}</g>`).join("");
  const errors = await page.evaluate(async sources => {
    // @ts-expect-error Vite serves this module to the browser.
    const { sanitizeSvgMarkup } = await import("/src/safe-svg.ts");
    // @ts-expect-error Vite serves the installed browser dependency.
    const { default: DOMPurify } = await import("/node_modules/.vite/deps/dompurify.js");
    return sources.map(source => {
      try { sanitizeSvgMarkup(source, DOMPurify); return "accepted"; }
      catch (error) { return (error as Error).message; }
    });
  }, [svg('<g id="loop"><use href="#loop"/></g>'), svg(`<defs>${expansion}</defs><use href="#g14"/>`), svg(`<defs>${chain}</defs><use href="#g69"/>`)]);
  expect(errors[0]).toContain("循环引用");
  expect(errors[1]).toContain("元素过多");
  expect(errors[2]).toContain("嵌套过深");
});
