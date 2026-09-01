function increment(value) {
  return value + 1;
}

function decrement(value) {
  return value - 1;
}

if (typeof document !== "undefined") {
  const output = document.querySelector("#value");
  document.querySelector("#increment").addEventListener("click", () => {
    output.value = String(increment(Number(output.value)));
  });
  document.querySelector("#decrement").addEventListener("click", () => {
    output.value = String(decrement(Number(output.value)));
  });
}

if (typeof module !== "undefined") {
  module.exports = { increment, decrement };
}
