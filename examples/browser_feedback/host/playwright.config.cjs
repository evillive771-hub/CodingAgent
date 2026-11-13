const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'counter.spec.cjs',
  retries: 0,
  workers: 1,
  forbidOnly: true,
  reporter: [['json']],
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR,
  use: { browserName: 'chromium', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
});
