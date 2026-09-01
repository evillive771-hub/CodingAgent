const test = require("node:test");
const assert = require("node:assert/strict");
const { increment, decrement } = require("./counter.js");

test("increment adds one", () => {
  assert.equal(increment(2), 3);
});

test("decrement clamps at zero", () => {
  assert.equal(decrement(2), 1);
  assert.equal(decrement(0), 0);
});
