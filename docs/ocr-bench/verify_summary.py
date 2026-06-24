"""Verify the summary section executes LIVE in a kernel (non-destructive).
Runs the real notebook with the slow torch engines off (fast subset), so the new
summary cell runs against genuine run data, then prints its live output + any errors.
Writes the executed copy to /tmp/verify_nb.ipynb — does NOT touch the real notebook.
"""
import os
os.environ["HF_HUB_OFFLINE"]="1"; os.environ["TRANSFORMERS_OFFLINE"]="1"
import nbformat
from nbconvert.preprocessors import ExecutePreprocessor

nb = nbformat.read("cheque_ocr_benchmark.ipynb", as_version=4)
for c in nb.cells:
    if c.cell_type == "code" and "USE_QWEN = True" in c.source:
        c.source = (c.source.replace("USE_QWEN = True", "USE_QWEN = False")
                            .replace("USE_GOT = True", "USE_GOT = False")
                            .replace("USE_TROCR = True", "USE_TROCR = False"))
        print("[patched config: Qwen/GOT/TrOCR OFF for a fast live run]")

ep = ExecutePreprocessor(timeout=1200, kernel_name="python3")
ep.preprocess(nb, {"metadata": {"path": "."}})
nbformat.write(nb, "/tmp/verify_nb.ipynb")

errors = [(i, o.ename, o.evalue) for i, c in enumerate(nb.cells)
          for o in c.get("outputs", []) if o.output_type == "error"]
print("\n=== EXECUTION ERRORS:", errors if errors else "NONE")

def stream(cell):
    return "".join(o.get("text", "") for o in cell.get("outputs", []) if o.output_type == "stream")

for c in nb.cells:
    if c.cell_type == "code":
        s = stream(c)
        if "available engines:" in s: print("\n=== engines:", s.strip())
        if "truth=" in s and "->" in s: print("\n=== run-loop preds:\n" + s.strip())
        if "RECOMMENDATION" in (c.source or ""):
            print("\n=== LIVE SUMMARY CELL OUTPUT ===\n" + stream(c))
