const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("index.html", "utf8");

function tagWithId(id) {
  const match = html.match(new RegExp(`<[^>]+id="${id}"[^>]*>`));
  assert.ok(match, `missing element with id=${id}`);
  return match[0];
}

test("trigger references the login dialog", () => {
  const trigger = tagWithId("open-login");
  assert.match(trigger, /^<button\b/);
  assert.match(trigger, /\baria-controls="login-dialog"/);
});

test("dialog is modal and labelled by its heading", () => {
  const dialog = tagWithId("login-dialog");
  assert.match(dialog, /^<section\b/);
  assert.match(dialog, /\baria-modal="true"/);
  assert.match(dialog, /\baria-labelledby="login-title"/);
});

test("icon-only close button has an accessible name", () => {
  const close = tagWithId("close-login");
  assert.match(close, /^<button\b/);
  assert.match(close, /\baria-label="关闭登录弹窗"/);
});
