import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import JSZip from "jszip";

async function presentationFixture() {
  const zip = new JSZip();
  const relationship = "http://schemas.openxmlformats.org/package/2006/relationships";
  const drawing = "http://schemas.openxmlformats.org/drawingml/2006/main";
  const presentation = "http://schemas.openxmlformats.org/presentationml/2006/main";
  const officeRel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
  zip.file("[Content_Types].xml", `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
        <Default Extension="xml" ContentType="application/xml"/>
        <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
        <Default Extension="png" ContentType="image/png"/>
        </Types>`);
  zip.file("_rels/.rels", `<Relationships xmlns="${relationship}">
        <Relationship Id="root" Type="${officeRel}/officeDocument" Target="ppt/presentation.xml"/>
        </Relationships>`);
  zip.file("ppt/presentation.xml", `<p:presentation xmlns:p="${presentation}" xmlns:r="${officeRel}">
        <p:sldIdLst>
        <p:sldId id="256" r:id="one"/>
        <p:sldId id="257" r:id="two"/>
        </p:sldIdLst>
        <p:sldSz cx="9144000" cy="5143500"/>
        </p:presentation>`);
  zip.file("ppt/_rels/presentation.xml.rels", `<Relationships xmlns="${relationship}">
        <Relationship Id="one" Type="${officeRel}/slide" Target="slides/slide1.xml"/>
        <Relationship Id="two" Type="${officeRel}/slide" Target="slides/slide2.xml"/>
        </Relationships>`);
  const text = (value: string) => `<a:txBody>
        <a:bodyPr/>
        <a:lstStyle/>
        <a:p>
        <a:r>
        <a:rPr sz="2000"/>
        <a:t>${value}</a:t>
        </a:r>
        </a:p>
        </a:txBody>`;
  const cell = (value: string) => `<a:tc>${text(value)}<a:tcPr marL="10972800" marR="10972800" marT="0" marB="0"/>
        </a:tc>`;
  for (let index = 1; index <= 2; index++) {
    zip.file(`ppt/slides/slide${index}.xml`, `<p:sld xmlns:p="${presentation}" xmlns:a="${drawing}" xmlns:r="${officeRel}">
        <p:cSld>
        <p:spTree>
        <p:sp>
        <p:nvSpPr>
        <p:cNvPr id="2" name="title"/>
        <p:cNvSpPr/>
        <p:nvPr/>
        </p:nvSpPr>
        <p:spPr>
        <a:xfrm>
        <a:off x="300000" y="300000"/>
        <a:ext cx="7000000" cy="600000"/>
        </a:xfrm>
        <a:prstGeom prst="rect">
        <a:avLst/>
        </a:prstGeom>
        </p:spPr>${text(index === 1 ? "第一页" : "第二页").replaceAll("a:txBody", "p:txBody")}</p:sp>
        <p:graphicFrame>
        <p:nvGraphicFramePr>
        <p:cNvPr id="3" name="table"/>
        <p:cNvGraphicFramePr/>
        <p:nvPr/>
        </p:nvGraphicFramePr>
        <p:xfrm>
        <a:off x="300000" y="1400000"/>
        <a:ext cx="8000000" cy="1000000"/>
        </p:xfrm>
        <a:graphic>
        <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">
        <a:tbl>
        <a:tblPr/>
        <a:tblGrid>
        <a:gridCol w="3000000"/>
        <a:gridCol w="5000000"/>
        </a:tblGrid>
        <a:tr h="1000000">${cell("边界")}${cell("单元格")}</a:tr>
        </a:tbl>
        </a:graphicData>
        </a:graphic>
        </p:graphicFrame>
        <p:pic>
        <p:nvPicPr>
        <p:cNvPr id="4" name="external"/>
        <p:cNvPicPr/>
        <p:nvPr/>
        </p:nvPicPr>
        <p:blipFill>
        <a:blip r:link="outside"/>
        </p:blipFill>
        <p:spPr>
        <a:xfrm>
        <a:off x="0" y="3000000"/>
        <a:ext cx="1000000" cy="1000000"/>
        </a:xfrm>
        </p:spPr>
        </p:pic>
        </p:spTree>
        </p:cSld>
        </p:sld>`);
    zip.file(`ppt/slides/_rels/slide${index}.xml.rels`, `<Relationships xmlns="${relationship}">
        <Relationship Id="outside" Type="${officeRel}/image" TargetMode="External" Target="https://presentation.invalid/track.png"/>
        </Relationships>`);
  }
  return zip.generateAsync({ type: "base64" });
}

test("artifact-presentation navigation, table bounds, isolation and close release resources", async ({ page }) => {
  const data = await presentationFixture();
  await page.route("**/presentation-preview.html", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(),
      "content-security-policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:; media-src data: blob:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'",
    } });
  });
  const requests: string[] = [];
  page.on("request", request => {
    if (request.url().includes("presentation.invalid")) requests.push(request.url());
  });
  await page.addInitScript(data => {
    (window as unknown as { presentationFixture: string }).presentationFixture = data;
    const urls = new Set<string>();
    const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = blob => { const url = create(blob); urls.add(url); return url; };
    URL.revokeObjectURL = url => { urls.delete(url); revoke(url); };
    (window as unknown as { previewUrls: Set<string> }).previewUrls = urls;
  }, data);
  await page.goto("/tests/history-browser.html?artifact-presentation=1");
  const frame = page.frameLocator(".presentation-stage iframe");
  await expect(frame.locator("#slide").getByText("第一页", { exact: true })).toBeVisible();
  await expect(frame.locator("#slide").getByText("单元格", { exact: true }).first()).toBeVisible();
  await expect(page.locator(".presentation-pages")).toContainText("1 / 2");
  expect(await page.locator(".presentation-stage iframe").getAttribute("sandbox")).toBe("allow-scripts");
  expect(await frame.locator("body").evaluate(() => {
    try { void parent.document; return false; } catch { return true; }
  })).toBe(true);
  await expect.poll(() => frame.locator("td").first().evaluate(cell => {
    const span = cell.querySelector("span")!;
    return span.getBoundingClientRect().left < cell.getBoundingClientRect().right;
  })).toBe(true);
  if (await page.getByRole("button", { name: "下一页", exact: true }).count()) {
    await page.getByRole("button", { name: "下一页", exact: true }).click();
  } else {
    await page.getByRole("button", { name: "缩略图", exact: true }).click();
    await frame.getByRole("button", { name: "第 2 页", exact: true }).click();
    await expect(frame.getByRole("button", { name: "第 2 页", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.locator(".presentation-pages")).toContainText("2 / 2");
    await page.getByRole("button", { name: "缩略图", exact: true }).click();
  }
  await expect(frame.locator("#slide").getByText("第二页", { exact: true })).toBeVisible();
  const zoom = page.getByRole("combobox", { name: "幻灯片缩放" });
  expect(await zoom.evaluate(element => element.tagName)).toBe("BUTTON");
  expect(await zoom.evaluate(element => parseFloat(getComputedStyle(element).borderRadius))).toBeGreaterThanOrEqual(18);
  await zoom.click();
  const zoomMenu = page.getByRole("listbox", { name: "幻灯片缩放选项" });
  await expect(zoomMenu).toBeVisible();
  expect(await zoomMenu.evaluate(element => parseFloat(getComputedStyle(element).borderRadius))).toBeGreaterThanOrEqual(16);
  await page.getByRole("option", { name: "150%", exact: true }).click();
  await expect(zoom).toContainText("150%");
  await expect(zoomMenu).toHaveCount(0);
  await page.getByRole("button", { name: "缩略图", exact: true }).click();
  await frame.getByRole("button", { name: "第 1 页", exact: true }).click();
  expect(await frame.getByRole("button", { name: "第 1 页", exact: true })
    .evaluate(element => parseFloat(getComputedStyle(element).borderRadius))).toBeGreaterThanOrEqual(10);
  await expect(page.locator(".presentation-pages")).toContainText("1 / 2");
  await page.getByRole("button", { name: "居中展开文件预览" }).click();
  await expect(page.getByRole("dialog", { name: "文件预览", exact: true })).toBeVisible();
  await zoom.press("ArrowDown");
  await expect(zoomMenu).toBeVisible();
  await page.getByRole("option", { name: "150%", exact: true }).press("Escape");
  await expect(zoomMenu).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "文件预览", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "下载原文件" })).toHaveAttribute("download", "slides.pptx");
  for (const width of [320, 390, 1200]) {
    await page.setViewportSize({ width, height: 800 });
    if (width < 700) {
      await expect(page.locator(".presentation-preview")).toHaveClass(/mobile/);
      await expect(page.getByRole("button", { name: "下一页", exact: true })).toHaveCount(0);
      await expect(frame.locator("#slide [data-slide-index]")).toHaveCount(2);
      await expect.poll(() => frame.locator("#slide > [data-slide-index] > div:first-child").first()
        .evaluate(element => parseFloat(getComputedStyle(element).borderRadius))).toBeGreaterThanOrEqual(10);
    }
    expect(await page.locator(".presentation-controls").evaluate(element =>
      element.scrollWidth <= element.clientWidth)).toBe(true);
  }
  expect(requests).toEqual([]);
  await page.getByRole("button", { name: "收起", exact: true }).click();
  await expect(page.getByText("预览已关闭")).toBeVisible();
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as { previewUrls: Set<string> }).previewUrls.size)).toBe(0);
});

test("artifact-presentation invalid input reports a readable error", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { presentationFixture: string }).presentationFixture = btoa("invalid pptx");
  });
  await page.goto("/tests/history-browser.html?artifact-presentation=1");
  await expect(page.getByRole("alert")).toContainText("PPTX 无法解析");
});

test("artifact-presentation real deck renders every slide", async ({ page }) => {
  test.skip(!process.env.PPTX_PREVIEW_FIXTURE, "Requires a local, operator-supplied PPTX");
  const data = readFileSync(process.env.PPTX_PREVIEW_FIXTURE!).toString("base64");
  await page.addInitScript(data => {
    (window as unknown as { presentationFixture: string }).presentationFixture = data;
  }, data);
  await page.goto("/tests/history-browser.html?artifact-presentation=1");
  await expect(page.locator(".presentation-pages")).toContainText("/ 17");
  await page.getByRole("button", { name: "缩略图", exact: true }).click();
  const frame = page.frameLocator(".presentation-stage iframe");
  for (let pageNumber = 1; pageNumber <= 17; pageNumber++) {
    await frame.getByRole("button", { name: `第 ${pageNumber} 页`, exact: true }).click();
    await expect(page.locator(".presentation-pages span")).toHaveText(`${pageNumber} / 17`);
    await expect(page.locator(".presentation-loading")).toHaveCount(0);
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.locator(".presentation-notice")).toHaveCount(0);
    await expect(frame.locator("#slide")).not.toBeEmpty();
    if (pageNumber === 6) await expect(frame.locator("#slide").getByText("没说出口的维度", { exact: true })).toBeVisible();
  }
});
