function filterProducts(products, query) {
  return products.filter((product) => product.name.includes(query));
}

if (typeof module !== "undefined") {
  module.exports = { filterProducts };
}
