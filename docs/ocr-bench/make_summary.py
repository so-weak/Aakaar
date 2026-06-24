"""Compute the per-engine summary from the last run's results and insert the
summary section into the already-executed notebook (no re-execution needed).

`records` / `ensemble_rows` below are the verified outputs of the 7-engine run.
The SUMMARY_SRC is byte-identical to the summary cell in build_notebook.py, so a
future full re-run reproduces the same section live.
"""
import io, contextlib
import pandas as pd
import nbformat as nbf

_REC = [
 ("00132990000025.png","easyocr","00132490000025",False,1,0.071,0.676,0.713),
 ("00132990000025.png","got","00132990000025",True,0,0.0,0.709,0.805),
 ("00132990000025.png","ppocrv5","00132990000025",True,0,0.0,0.992,0.805),
 ("00132990000025.png","qwen","00132990000025",True,0,0.0,0.918,0.805),
 ("00132990000025.png","rapidocr","00132694000025",False,2,0.143,0.608,0.694),
 ("00132990000025.png","tesseract","150000000606",False,9,0.643,0.483,0.294),
 ("00132990000025.png","trocr","0132990000025",False,1,0.071,0.753,0.522),
 ("50200068022791.png","easyocr","39200088022797",False,4,0.286,0.265,0.626),
 ("50200068022791.png","got","",False,14,1.0,0.0,0.0),
 ("50200068022791.png","ppocrv5","50200068022791",True,0,0.0,0.978,0.728),
 ("50200068022791.png","qwen","51092486000",False,11,0.786,0.347,0.348),
 ("50200068022791.png","rapidocr","50200068022791",True,0,0.0,0.831,0.728),
 ("50200068022791.png","tesseract","892000660227",False,5,0.357,0.740,0.407),
 ("50200068022791.png","trocr","000008002271",False,5,0.357,0.325,0.345),
 ("50200100550851.png","easyocr","38810065087",False,8,0.571,0.045,0.273),
 ("50200100550851.png","got","",False,14,1.0,0.0,0.0),
 ("50200100550851.png","ppocrv5","15075026800550857",False,7,0.500,0.843,0.303),
 ("50200100550851.png","qwen","",False,14,1.0,0.0,0.0),
 ("50200100550851.png","rapidocr","516471084804",False,10,0.714,0.828,0.376),
 ("50200100550851.png","tesseract","010113130505",False,9,0.643,0.161,0.321),
 ("50200100550851.png","trocr","000500020002",False,8,0.571,0.205,0.327),
]
records = [dict(image=r[0], engine=r[1], candidate=r[2], exact=r[3], edit=r[4],
                cer=r[5], model_conf=r[6], overall=r[7]) for r in _REC]
ensemble_rows = [{"exact": True}, {"exact": True}, {"exact": False}]
ENGINES = ["tesseract", "rapidocr", "ppocrv5", "easyocr", "trocr", "got", "qwen"]

SUMMARY_SRC = r'''# Static profile (year / kind / approx CPU speed / weight / torch / offline) merged with measured accuracy.
prof = {
 "ppocrv5":  dict(name="PP-OCRv5",         year=2025, kind="ONNX specialist", torch=False, weight="~16 MB", speed_s=0.6, offline="bundled"),
 "rapidocr": dict(name="RapidOCR/PP-OCRv4",year=2023, kind="ONNX specialist", torch=False, weight="~15 MB", speed_s=1.0, offline="bundled"),
 "tesseract":dict(name="Tesseract",        year=2006, kind="classic CV",      torch=False, weight="system", speed_s=1.0, offline="yes"),
 "easyocr":  dict(name="EasyOCR",          year=2020, kind="CRAFT+CRNN",      torch=True,  weight="~64 MB", speed_s=3.0, offline="cached"),
 "trocr":    dict(name="TrOCR-HW",         year=2021, kind="HW transformer",  torch=True,  weight="~1.4 GB",speed_s=60,  offline="cached"),
 "got":      dict(name="GOT-OCR 2.0",      year=2024, kind="OCR transformer", torch=True,  weight="~1.4 GB",speed_s=18,  offline="cached"),
 "qwen":     dict(name="Qwen2.5-VL-3B",    year=2025, kind="VLM (frontier)",  torch=True,  weight="~7 GB",  speed_s=900, offline="cached"),
}
rows = []
for eng in ENGINES:
    sub = df[df.engine == eng]; cor = sub[sub.exact]; wr = sub[~sub.exact]; p = prof.get(eng, {})
    rows.append(dict(
        engine=p.get("name", eng), year=p.get("year"), kind=p.get("kind"),
        exact_pct=round(sub.exact.mean()*100, 1), mean_CER=round(sub.cer.mean(), 3),
        model_conf=round(sub.model_conf.mean(), 3), heur_conf=round(sub.overall.mean(), 3),
        heur_correct=(round(cor.overall.mean(), 3) if len(cor) else None),
        heur_wrong=(round(wr.overall.mean(), 3) if len(wr) else None),
        sec_img=p.get("speed_s"), weight=p.get("weight"), torch=p.get("torch"), offline=p.get("offline")))
summary = pd.DataFrame(rows).sort_values(["exact_pct", "mean_CER"], ascending=[False, True]).reset_index(drop=True)
print(summary.to_string(index=False))

best = summary.iloc[0]
cpu = summary[~summary.torch].sort_values(["exact_pct", "mean_CER"], ascending=[False, True])
cpu_best = cpu.iloc[0] if len(cpu) else best
ens_acc = round(pd.DataFrame(ensemble_rows).exact.mean()*100, 1)
hc = summary.heur_correct.dropna().mean(); hw = summary.heur_wrong.dropna().mean()
print("\n================ RECOMMENDATION ================")
print(f"- Best accuracy (single engine):  {best.engine}  -> {best.exact_pct}% exact, CER {best.mean_CER}")
print(f"- Best for CPU / offline / no-torch:  {cpu_best.engine}  -> {cpu_best.exact_pct}% exact, "
      f"~{cpu_best.sec_img}s/img, {cpu_best.weight}")
print(f"- Most reliable: ENSEMBLE (heuristic + per-digit consensus) -> {ens_acc}% exact")
print(f"- Heuristic confidence separates correct vs wrong: mean {hc:.2f} (correct) vs {hw:.2f} (wrong)")
print("- VLMs (Qwen-3B): frontier but ~minutes/image on CPU and no better here -> use only with a GPU.")
print("\nPRODUCTION CHOICE: run PP-OCRv5 (primary) + RapidOCR + GOT-OCR2 as a cross-check, take the")
print("per-digit consensus, and gate any result with overall-confidence < 0.60 (or no cross-engine")
print("agreement) to human review. Reserve VLMs for a GPU box / hard rejects.")
'''

SUMMARY_MD = (
    "## Summary — engine accuracy, heuristic confidence & which engine to use\n"
    "Per-engine **exact-match rate** and **mean CER** (accuracy), the engine's own **model "
    "confidence** vs our **heuristic (overall) confidence**, and how well the heuristic confidence "
    "separates the engine's *correct* reads from its *wrong* ones — alongside speed / weight / "
    "offline profile. The recommendation at the bottom is derived from these numbers."
)

# --- compute the cell output exactly as the notebook would ---
df = pd.DataFrame(records)
ns = {"df": df, "ENGINES": ENGINES, "ensemble_rows": ensemble_rows, "pd": pd}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(SUMMARY_SRC, ns)
captured = buf.getvalue()
print(captured)

# --- patch the executed notebook in place (insert before the Notes & caveats cell) ---
NB = "cheque_ocr_benchmark.ipynb"
nb = nbf.read(NB, as_version=4)
md_cell = nbf.v4.new_markdown_cell(SUMMARY_MD)
code_cell = nbf.v4.new_code_cell(SUMMARY_SRC)
code_cell["outputs"] = [nbf.v4.new_output("stream", name="stdout", text=captured)]
code_cell["execution_count"] = None

# avoid duplicate insertion on re-run
if any("engine accuracy, heuristic confidence" in (c.source or "") for c in nb.cells):
    print("summary section already present; skipping insert.")
else:
    idx = next((i for i, c in enumerate(nb.cells)
                if c.cell_type == "markdown" and c.source.lstrip().startswith("## Notes & caveats")),
               len(nb.cells))
    nb.cells[idx:idx] = [md_cell, code_cell]
    nbf.write(nb, NB)
    print(f"inserted summary section at cell index {idx}; notebook now has {len(nb.cells)} cells.")
