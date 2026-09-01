const test = require("node:test");
const assert = require("node:assert/strict");
const { filterProducts } = require("./search.js");

const products = [
  { id: 1, name: "Wireless Mouse" },
  { id: 2, name: "Mechanical Keyboard" },
  { id: 3, name: "USB-C Cable" },
];

test("search ignores case and surrounding whitespace", () => {
  assert.deepEqual(filterProducts(products, "  mouse "), [products[0]]);
  assert.deepEqual(filterProducts(products, "KEYBOARD"), [products[1]]);
});

test("empty normalized search returns all products without mutation", () => {
  const snapshot = structuredClone(products);
  assert.deepEqual(filterProducts(products, "   "), products);
  assert.deepEqual(products, snapshot);
});
