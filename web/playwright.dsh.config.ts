import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./tests", testMatch: ["dsh-browser.spec.ts"], workers: 1,
  reporter: "line", use: { baseURL: "http://127.0.0.1:4186", trace: "retain-on-failure" },
  webServer: { command: "npx vite --host 127.0.0.1 --port 4186", url: "http://127.0.0.1:4186", reuseExistingServer: false },
  projects: [ { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: {width:1280,height:900} } },
    { name: "mobile", use: { ...devices["iPhone 15"] } },
    { name: "android", use: { ...devices["Pixel 7"] } } ],
});
