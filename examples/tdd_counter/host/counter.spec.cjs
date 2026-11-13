const { test, expect } = require('D:/Github/web-bench/projects/demo/node_modules/@playwright/test');

test('counter cannot decrement below zero', async ({ page }) => {
  await page.goto(process.env.BROWSER_TEST_BASE_URL);
  await page.locator('#decrement').click();
  await expect(page.locator('#value')).toHaveValue('0');
});

test('increment and decrement preserve positive values', async ({ page }) => {
  await page.goto(process.env.BROWSER_TEST_BASE_URL);
  await page.locator('#increment').click();
  await page.locator('#increment').click();
  await expect(page.locator('#value')).toHaveValue('2');
  await page.locator('#decrement').click();
  await expect(page.locator('#value')).toHaveValue('1');
});
