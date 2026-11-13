const { defineConfig } = require('D:/Github/web-bench/projects/demo/node_modules/@playwright/test');
module.exports = defineConfig({
  testDir: __dirname, testMatch: 'counter.spec.cjs', workers: 1, retries: 0, forbidOnly: true,
  reporter: [['json']], outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR,
  use: { browserName: 'chromium', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
});
