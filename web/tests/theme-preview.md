# Theme review preview

The theme fixture uses production UI components with example conversations. It
does not connect to a model or an existing session.

For local development, run `npm run dev` in `web` and open
`/tests/theme-preview.html?engine=codex`. This entry imports TSX and requires
Vite. Do not share the source HTML through Remote Viewer.

For a phone or Remote Viewer review, use Node 24 and run:

```bash
npm run build:theme-preview
```

Share the generated `web/dist-theme-preview/tests/theme-preview.html` file.
The export includes relative JavaScript, CSS and font assets and needs no
development server. If registering it manually, use `web/dist-theme-preview`
as the resource root and allow only `/tests/theme-preview.html` and `/assets/`.
Keep the Wrapper online and reopen the preview after rebuilding the files.

The export retains the production theme controls and UI components, but omits
diagram and equation renderers unused by the fixed example conversation. It
bundles scripts together and enforces at most six files, each below 1.5 MiB:
Bridge reads and revalidates even lazy dependencies before the first paint, so
testing a large graph only on loopback would hide mobile relay startup delays.

Validate the export through the real Bridge channel before sharing it:

```bash
VIEWER_TEST_KIND=theme \
VIEWER_TEST_SOURCE="$PWD/dist-theme-preview" \
VIEWER_TEST_ENTRY=/tests/theme-preview.html \
VIEWER_TEST_PATHS='["/tests/theme-preview.html","/assets/"]' \
npx playwright test -c playwright.viewer.config.ts remote-viewer-theme.spec.ts
```

The Bridge sandbox blocks browser storage. Theme changes still work within the
open preview; persistent preferences are verified by `themes.spec.ts` in the
normal application context.
