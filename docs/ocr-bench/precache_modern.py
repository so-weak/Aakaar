"""Download + smoke-test the modern OCR engines so the notebook can run offline.
Prints the exact output shape + CPU timing for each so we wire integration correctly.
"""
import time, traceback
import numpy as np
from PIL import Image

IMG = "../exampleCheques/00132990000025.png"   # truth 00132990000025
pil = Image.open(IMG).convert("RGB")
if max(pil.size) > 1600:
    s = 1600 / max(pil.size); pil = pil.resize((round(pil.width*s), round(pil.height*s)))
arr = np.array(pil)

def section(name):
    print("\n" + "="*8, name, "="*8)

# ---- PP-OCRv5 (new rapidocr, ONNX) -------------------------------------------
section("PP-OCRv5 (rapidocr)")
try:
    from rapidocr import RapidOCR
    t=time.time(); eng = RapidOCR(); out = eng(arr); dt=time.time()-t
    print("elapsed", round(dt,1), "type", type(out).__name__)
    print("public attrs:", [a for a in dir(out) if not a.startswith("_")])
    try:
        print("txts:", list(out.txts)[:6])
        print("scores:", [round(float(x),3) for x in list(out.scores)[:6]])
    except Exception as e:
        print("attr access:", e)
except Exception:
    traceback.print_exc()

# ---- GOT-OCR 2.0 (transformers) ----------------------------------------------
section("GOT-OCR 2.0")
try:
    import torch
    from transformers import AutoProcessor, AutoModelForImageTextToText
    gp = AutoProcessor.from_pretrained("stepfun-ai/GOT-OCR-2.0-hf")
    gm = AutoModelForImageTextToText.from_pretrained("stepfun-ai/GOT-OCR-2.0-hf", torch_dtype=torch.float32).eval()
    t=time.time()
    inp = gp(pil, return_tensors="pt")
    with torch.no_grad():
        g = gm.generate(**inp, max_new_tokens=64, do_sample=False)
    txt = gp.decode(g[0, inp["input_ids"].shape[1]:], skip_special_tokens=True)
    print("elapsed", round(time.time()-t,1), "text:", repr(txt[:160]))
except Exception:
    traceback.print_exc()

# ---- Qwen2.5-VL-3B-Instruct (frontier VLM) -----------------------------------
section("Qwen2.5-VL-3B")
try:
    import torch
    from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor
    qm = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        "Qwen/Qwen2.5-VL-3B-Instruct", torch_dtype=torch.float32).eval()
    qp = AutoProcessor.from_pretrained("Qwen/Qwen2.5-VL-3B-Instruct")
    msg = [{"role":"user","content":[
        {"type":"image","image":pil},
        {"type":"text","text":"This is the back of an Indian bank cheque. Read the handwritten "
         "recipient account number. Reply with ONLY the digits, no spaces."}]}]
    chat = qp.apply_chat_template(msg, tokenize=False, add_generation_prompt=True)
    inp = qp(text=[chat], images=[pil], return_tensors="pt")
    t=time.time()
    with torch.no_grad():
        gen = qm.generate(**inp, max_new_tokens=40, do_sample=False)
    out = qp.batch_decode(gen[:, inp.input_ids.shape[1]:], skip_special_tokens=True)[0]
    print("elapsed", round(time.time()-t,1), "text:", repr(out[:160]))
except Exception:
    traceback.print_exc()

print("\nDONE_PRECACHE_SMOKE")
