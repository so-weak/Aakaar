"""
Generate an animated marketing video for AAKAAR using OpenAI's Sora-2 model.

AAKAAR — AI Assist: Know-it, Automate-it, Audit-it, Run-it.
(Sanskrit आकार — "form / shape": the platform that gives form to intent.)

Usage:
    python generate_video.py

Loads OPENAI_API_KEY from the aakar backend's .env, submits a video job to
Sora-2, polls until rendered, and writes the MP4 to ./aakaar_intro.mp4.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).resolve().parent
BACKEND_ENV = ROOT / "aakar" / ".env"
OUTPUT_PATH = ROOT / f"aakaar_intro_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

MODEL = "sora-2-pro"  # OpenAI's flagship text-to-video model (higher fidelity tier)
SECONDS = "20"  # sora-2-pro supports {4, 8, 12, 16, 20}
SIZE = "720x1280"  # portrait 9:16


PROMPT = """
A 20-second cinematic 3D-animated explainer in PORTRAIT 9:16 (720x1280)
for a multi-tenant AI workflow platform called AAKAAR — Sanskrit आकार,
"form / shape" — backronymed "AI Assist: Know-it · Automate-it ·
Audit-it · Run-it." Tell the platform's story in one continuous
mythic-tech reality: Pracharya charters Avataras → each Avatara's
Acharya initiates Sadhakas → Sadhakas voice Sankalpas in Samvada and
witness Yajnas through Pratyaksha → underneath, every wire byte is
the same.

LOOK & WORLD.
Style: high-craft 3D motion-graphics, half temple, half data-center.
Palette: midnight indigo background, saffron and warm gold key-light,
deep teal accents, soft white volumetric god-rays. Materials:
black-glass slabs etched with faint Sanskrit-styled glyphs and circuit
traces; brushed brass; floating particles of luminous ash; thin gold
filigree borders. Camera: smooth handheld-feel, vertical-tracking
moves, shallow depth of field, gentle parallax, 24fps, anamorphic
flares only on ignitions. Stack key visuals VERTICALLY to use the tall
frame. NO on-screen text other than the proper nouns explicitly named
below. All non-English terms appear once each as small floating gold
serif labels next to their subject.

NARRATION.
A single calm, warm male voice — measured, ceremonial but modern,
mid-baritone, light reverb as if in a stone hall — delivers exactly
these lines in time with the beats below. No other dialogue. Speak
the proper nouns clearly:
  (0.5s)  "AAKAAR."
  (3.5s)  "Pracharya charters Avataras."
  (7.5s)  "Each Acharya initiates Sadhakas."
  (10.5s) "Sadhakas voice Sankalpas in Samvada…"
  (14.5s) "…and witness Yajnas through Pratyaksha."
  (17.5s) "Underneath — every wire byte is the same."

MUSIC BED (entire 20s).
Sub-bass tanpura drone in indigo register, a slow tabla heartbeat
entering at 3s and building, a soft bansuri (bamboo flute) phrase
rising at 10s, resolving into one sustained sitar-and-gong chord on
the final mandala. Mix narration on top, music ducked beneath it.

BEAT 1 — ORIGIN (0.0–3.0s).
Open in pure black with a single sub-bass drone breath. A luminous
mandala blooms open at the vertical center, petals unfolding like a
clockwork lotus; faint ash-particles drift upward. Camera dollies
slowly in along the tall axis. The wordmark "AAKAAR" carves itself
from the mandala's heart in glowing devanagari-styled English, then
four small chambers light up around it in a vertical column reading,
top to bottom: KNOW · AUTOMATE · AUDIT · RUN.
SFX: long inhaled breath; a low conch (shankha) note; a single soft
gong at the moment AAKAAR resolves; gentle particle shimmer.

BEAT 2 — PRACHARYA CHARTERS AVATARAS (3.0–7.0s).
Cut wide. PRACHARYA — the Principal, "scholar of scholars" — fills
the upper half of the frame: a serene robed figure in indigo and
saffron, body woven from constellations and fine circuit traces, eyes
two calm points of gold light. They unroll a long glowing CHARTER
scroll downward; brass seals along its length click as it opens. From
the unfurling scroll, three glass spheres (AVATARAS) precipitate in
order and settle into the lower half of the frame stacked vertically
— each a miniature floating city of dashboards, vault-cubes, and
DAG-graphs, each receiving a glowing seal-stamp at birth. Small gold
labels appear: "Pracharya", "Avatara".
SFX: parchment unrolling; three deep brass seal-stamps in rhythm with
the spheres precipitating; tabla heartbeat fades in; choral pad
beneath narration.

BEAT 3 — ACHARYA INITIATES SADHAKAS (7.0–10.0s).
Push vertically into the middle sphere; the city becomes our world.
An ACHARYA (tenant admin), robed silhouette outlined in saffron rim-
light, stands at the top of the portrait frame on a low stone dais
and touches a flame to a hanging column of small brass oil lamps.
The lamps light one after another down the column; from each flame a
glowing humanoid figure rises — the SADHAKAS (users) — forming a
vertical line of soft-gold figures with circuit-trace robes. Labels:
"Acharya", "Sadhakas".
SFX: a soft striking-of-flint; each lamp ignites with a warm "fwoom"
in a quick descending sequence; small temple bells chime per lamp;
distant tabla heartbeat continues.

BEAT 4 — SAMVADA & SANKALPA (10.0–14.0s).
A single Sadhaka steps forward to the bottom of the frame before a
tall translucent black-glass chat slab (SAMVADA) that fills the
vertical height; faint glyphs scroll on its surface. They breathe
in and speak; a SANKALPA (prompt) blossoms from their mouth as a
folded lotus of glyphs, rises up the slab, and unfolds into a
vertically flowing directed acyclic graph — nodes as faceted gold
gems, edges as thin lines of light — auto-validating top-to-bottom
with soft chimes and small gold check-marks landing on each node.
Labels: "Samvada", "Sankalpa".
SFX: a soft breath; a clean ascending bell tone as the lotus rises;
a quick sequence of crisp "ting" validation chimes on each
checkmark; bansuri flute phrase rises beneath.

BEAT 5 — YAJNA & PRATYAKSHA (14.0–17.0s).
The validated DAG ignites: every node becomes a flame in a tall
vertical pillar of sacred fire down the center of the frame — the
YAJNA (run). Flanking the fire, two ghostly translucent browser
windows materialize stacked vertically and stream live screenshots
(PRATYAKSHA / live view): a payments dashboard scrolling, a table of
ticked rows, a captcha challenge appearing and being solved, a
receipt printing. Sadhakas in silhouette watch from below, faces lit
warm by the fire and the screens. Labels: "Yajna", "Pratyaksha".
SFX: a deep whoosh as the pillar ignites; steady crackle of fire;
rapid soft UI clicks and tap-tones from the browsers; brief
typewriter patter; a single soft alert ping on the captcha; tabla
intensifies.

BEAT 6 — ONE SUBSTRATE, ONE FORM (17.0–20.0s).
Camera pulls back fast along the vertical axis. The altar, the
sphere, all three Avataras, and Pracharya dissolve and resolve into a
single circuit board filling the portrait frame — every trace and
wire glowing the same warm gold, pulsing in unison once, twice. The
board folds inward and the mandala from Beat 1 reforms centered with
"AAKAAR" at its heart; the four chambers KNOW · AUTOMATE · AUDIT ·
RUN softly pulse once in a vertical stack and hold.
SFX: a low rising hum as the world collapses inward; two synchronized
heart-pulse thumps; one sustained sitar-and-gong chord resolving as
the mandala holds; then near-silence with only the tanpura drone.

GLOBAL CONSTRAINTS.
- 24fps, sharp, no motion blur smear.
- All Sanskrit-origin proper nouns pronounced clearly by the narrator.
- No watermarks, no fake UI brand names beyond "AAKAAR".
- No human faces in close-up; figures are silhouettes or stylized.
- Strictly portrait 9:16, vertical composition throughout.
""".strip()


def load_api_key() -> str:
    if not BACKEND_ENV.exists():
        sys.exit(f"backend .env not found at {BACKEND_ENV}")
    load_dotenv(BACKEND_ENV)
    # Empty OPENAI_BASE_URL in .env confuses the SDK — pop it (matches backend behavior).
    if os.environ.get("OPENAI_BASE_URL", "").strip() == "":
        os.environ.pop("OPENAI_BASE_URL", None)
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("OPENAI_API_KEY missing from aakar/.env")
    return key


def main() -> None:
    api_key = load_api_key()
    client = OpenAI(api_key=api_key)

    print(f"submitting video job · model={MODEL} · {SECONDS}s · {SIZE}")
    job = client.videos.create(
        model=MODEL,
        prompt=PROMPT,
        seconds=SECONDS,
        size=SIZE,
    )
    print(f"job id: {job.id} · status: {job.status}")

    while job.status in ("queued", "in_progress"):
        time.sleep(10)
        job = client.videos.retrieve(job.id)
        progress = getattr(job, "progress", None)
        print(f"  · {job.status}" + (f" ({progress}%)" if progress is not None else ""))

    if job.status != "completed":
        err = getattr(job, "error", None)
        sys.exit(f"job ended with status={job.status} error={err}")

    print(f"downloading → {OUTPUT_PATH}")
    content = client.videos.download_content(job.id, variant="video")
    content.write_to_file(str(OUTPUT_PATH))
    print(f"done · {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
