import os, time, gc, torch
os.environ["HF_HUB_OFFLINE"]="1"; os.environ["TRANSFORMERS_OFFLINE"]="1"
from PIL import Image
pil = Image.open("../exampleCheques/00132990000025.png").convert("RGB")  # truth 00132990000025
def ds(im, mx):
    if max(im.size) <= mx: return im
    s = mx/max(im.size); return im.resize((round(im.width*s), round(im.height*s)))

# ---- GOT-OCR 2.0 ----
from transformers import AutoProcessor, AutoModelForImageTextToText
t=time.time()
gp=AutoProcessor.from_pretrained("stepfun-ai/GOT-OCR-2.0-hf", local_files_only=True)
gm=AutoModelForImageTextToText.from_pretrained("stepfun-ai/GOT-OCR-2.0-hf", torch_dtype=torch.float32, local_files_only=True).eval()
print("GOT load", round(time.time()-t,1), flush=True)
im=ds(pil,1200); inp=gp(im, return_tensors="pt"); n=inp["input_ids"].shape[1]
t=time.time(); g=gm.generate(**inp, max_new_tokens=64, do_sample=False)
print("GOT infer", round(time.time()-t,1), "->", repr(gp.decode(g[0,n:], skip_special_tokens=True)[:140]), flush=True)
del gm, gp; gc.collect()

# ---- Qwen2.5-VL-3B ----
from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor as QAP
t=time.time()
qm=Qwen2_5_VLForConditionalGeneration.from_pretrained("Qwen/Qwen2.5-VL-3B-Instruct", torch_dtype=torch.float32, local_files_only=True).eval()
qp=QAP.from_pretrained("Qwen/Qwen2.5-VL-3B-Instruct", local_files_only=True)
print("QWEN load", round(time.time()-t,1), flush=True)
im=ds(pil,1000)
msg=[{"role":"user","content":[{"type":"image","image":im},{"type":"text","text":"Read the handwritten bank account number on this cheque back. Reply with ONLY digits."}]}]
chat=qp.apply_chat_template(msg, tokenize=False, add_generation_prompt=True)
inp=qp(text=[chat], images=[im], return_tensors="pt"); n=inp.input_ids.shape[1]
t=time.time(); g=qm.generate(**inp, max_new_tokens=40, do_sample=False)
print("QWEN infer", round(time.time()-t,1), "->", repr(qp.batch_decode(g[:,n:], skip_special_tokens=True)[0][:140]), flush=True)
print("DONE", flush=True)
