"""Host-owned requirement tests; candidate code is selected by the host environment."""
import os
import subprocess
from pathlib import Path


def evaluate(expression: str) -> None:
    counter = Path(os.environ["REGRESSION_WORKSPACE"]) / "counter.js"
    script = (
        "const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');"
        "const context={document:{querySelector:()=>({value:'0',addEventListener(){}})}};"
        "vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);" + expression
    )
    result = subprocess.run(["node", "-e", script, str(counter)], capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


def test_zero_decrement():
    evaluate("assert.equal(context.decrement(0),0);")


def test_positive_operations():
    evaluate("assert.equal(context.increment(3),4);assert.equal(context.decrement(3),2);")
