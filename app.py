"""
Service de génération lafrise — API HTTP pour Make
======================================================

Ce service est LE SEUL morceau de code du pipeline qui doit rester en code
(la génération d'image IA, l'overlay texte pixel-perfect et l'assemblage
vidéo ne sont pas faisables proprement en no-code). Tout le reste du
pipeline (détection, extraction, rédaction, validation Telegram,
publication Instagram/X) est orchestré depuis Make.com, sans code.

Ce service est déployé UNE FOIS sur Render et ne devrait quasiment jamais
être modifié — Make lui envoie une requête HTTP à chaque sujet à illustrer,
il répond avec les URLs publiques des visuels finaux.

Endpoint principal :
  POST /generate
  Body JSON :
    {
      "sujet_id": "recXXXXXXXX",
      "titre": "...",
      "niveau_verification": "A" / "B" / "C" / "D" / "non évalué",
      "slides": ["texte slide 1", "texte slide 2", ...],
      "generer_video": true
    }
  Réponse JSON :
    {
      "sujet_id": "recXXXXXXXX",
      "slides_urls": ["https://.../slide_1_final.png", ...],
      "video_url": "https://.../video_reel.mp4"  (ou null si non demandée)
    }

Authentification : header "X-API-Key" à faire correspondre à GENERATION_API_KEY
(protège l'endpoint, sert vu qu'il sera exposé publiquement pour que Make
puisse l'appeler).

Prérequis : voir requirements.txt. Variables d'environnement dans .env
(OPENAI_API_KEY, GENERATION_API_KEY, PORT).
"""

import os
import re
import json
import base64
import logging
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI
from dotenv import load_dotenv

from moviepy import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    vfx,
    afx,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lafrise.generation_service")

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GENERATION_API_KEY = os.environ.get("GENERATION_API_KEY")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")  # ex: https://lafrise-generation.onrender.com

OUTPUT_DIR = Path("output")
FONTS_DIR = Path(__file__).parent / "fonts"

# --- Charte graphique (identique aux Modules 3.1/3.2 déjà validés) ---------

STYLE_MARQUE = (
    "technical engraving / wire-service illustration style, monochrome line art "
    "on cream paper, fine cross-hatching, topographic map texture in background, "
    "precise technical drafting quality, no color except one accent tone reserved "
    "for text overlay, no text, no typography, no words, empty space reserved at "
    "top and bottom for text overlay"
)

CANVAS_SIZE = (1080, 1350)
BANNER_HEIGHT = 90
STAMP_MARGIN = 40

COLOR_BLACK = (17, 17, 17)
COLOR_CREAM = (245, 240, 230)
COLOR_BRONZE = (169, 137, 90)
COLOR_BRICK = (182, 67, 44)

STAMPS_BY_NIVEAU = {
    "A": "FAIT VÉRIFIÉ",
    "B": "FAIT VÉRIFIÉ",
    "C": "DÉBAT NON TRANCHÉ",
    "D": "À CONFIRMER",
    "non évalué": "À CONFIRMER",
}

IMAGE_QUALITY = os.environ.get("LAFRISE_IMAGE_QUALITY", "medium")
TARGET_VIDEO_SIZE = (1080, 1920)
DURATION_PER_SLIDE = 3.5
TRANSITION_DURATION = 0.5
ZOOM_FACTOR = 1.08
FPS = 30


def load_font(name, size):
    path = FONTS_DIR / name
    if not path.exists():
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size)


# ---------------------------------------------------------------------------
# Étape 1 — génération du fond (GPT Image 2)
# ---------------------------------------------------------------------------

def generate_background(prompt, output_path, dry_run=False):
    if dry_run or not OPENAI_API_KEY:
        # Mode test : crée un fond uni plutôt que d'appeler l'API (utile pour
        # vérifier tout le pipeline sans consommer de crédits OpenAI).
        img = Image.new("RGB", (1024, 1024), color=(230, 225, 210))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path)
        return

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.images.generate(
        model="gpt-image-2", prompt=prompt, size="1024x1024", quality=IMAGE_QUALITY, n=1
    )
    image_bytes = base64.b64decode(response.data[0].b64_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_bytes)


def build_image_prompt(titre, slide_text, slide_index):
    return (
        f"An illustration evoking the theme: '{slide_text}' (part of a series about '{titre}'). "
        f"{STYLE_MARQUE}. This is slide {slide_index + 1} of a series — keep the illustration "
        f"thematically distinct from other slides while preserving the exact same visual style."
    )


# ---------------------------------------------------------------------------
# Étape 2 — overlay texte (identique au Module 3.2)
# ---------------------------------------------------------------------------

def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def compose_slide(background_path, slide_text, niveau, is_last_slide, output_path):
    bg = Image.open(background_path).convert("RGB").resize(CANVAS_SIZE)
    draw = ImageDraw.Draw(bg)

    # Bandeau
    draw.rectangle([(0, 0), (CANVAS_SIZE[0], BANNER_HEIGHT)], fill=COLOR_BLACK)
    font_banner = load_font("SpaceMono-Bold.ttf", 36)
    text = "LAFRISE // WIRE"
    bbox = draw.textbbox((0, 0), text, font=font_banner)
    x = (CANVAS_SIZE[0] - (bbox[2] - bbox[0])) // 2
    y = (BANNER_HEIGHT - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), text, font=font_banner, fill=COLOR_CREAM)

    # Tampon
    label = STAMPS_BY_NIVEAU.get(niveau, "À CONFIRMER")
    font_stamp = load_font("SpaceMono-Bold.ttf", 26)
    bbox = draw.textbbox((0, 0), label, font=font_stamp)
    pad_x, pad_y = 20, 12
    box_w, box_h = (bbox[2] - bbox[0]) + 2 * pad_x, (bbox[3] - bbox[1]) + 2 * pad_y
    x0 = CANVAS_SIZE[0] - box_w - STAMP_MARGIN
    y0 = BANNER_HEIGHT + STAMP_MARGIN
    accent = COLOR_BRICK if niveau == "C" else COLOR_BRONZE
    draw.rectangle([(x0, y0), (x0 + box_w, y0 + box_h)], outline=accent, width=3)
    draw.text((x0 + pad_x, y0 + pad_y - bbox[1]), label, font=font_stamp, fill=accent)

    # Texte de la slide
    font_text = load_font("SpaceMono-Bold.ttf", 52)
    margin_x = 70
    lines = wrap_text(draw, slide_text, font_text, CANVAS_SIZE[0] - 2 * margin_x)
    line_height = 64
    text_block_height = len(lines) * line_height + 80
    band_y0 = CANVAS_SIZE[1] - text_block_height
    draw.rectangle([(0, band_y0), (CANVAS_SIZE[0], CANVAS_SIZE[1])], fill=COLOR_BLACK)
    ty = band_y0 + 40
    for line in lines:
        draw.text((margin_x, ty), line, font=font_text, fill=COLOR_CREAM)
        ty += line_height

    # Logo (dernière slide uniquement)
    if is_last_slide:
        font_logo = load_font("Spectral-Regular.ttf", 44)
        bbox = draw.textbbox((0, 0), "lafrise", font=font_logo)
        text_w = bbox[2] - bbox[0]
        logo_y = CANVAS_SIZE[1] - 140
        lx = (CANVAS_SIZE[0] - text_w) // 2
        draw.text((lx, logo_y), "lafrise", font=font_logo, fill=COLOR_CREAM)
        ruler_y = logo_y + 60
        draw.line([(lx, ruler_y), (lx + text_w, ruler_y)], fill=COLOR_BRONZE, width=3)
        for tick_x in range(int(lx), int(lx + text_w), 15):
            draw.line([(tick_x, ruler_y), (tick_x, ruler_y + 8)], fill=COLOR_BRONZE, width=2)
        draw.ellipse([(lx + text_w - 7, ruler_y - 7), (lx + text_w + 7, ruler_y + 7)], fill=COLOR_BRICK)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(output_path, "PNG")


# ---------------------------------------------------------------------------
# Étape 3 — vidéo slideshow (identique au Module 3bis)
# ---------------------------------------------------------------------------

def ken_burns_clip(image_path, duration):
    clip = ImageClip(str(image_path)).with_duration(duration)
    clip = clip.resized(height=TARGET_VIDEO_SIZE[1])
    if clip.w < TARGET_VIDEO_SIZE[0]:
        clip = clip.resized(width=TARGET_VIDEO_SIZE[0])
    clip = clip.resized(lambda t: 1 + (ZOOM_FACTOR - 1) * (t / duration))
    clip = clip.with_position("center")
    return CompositeVideoClip([clip], size=TARGET_VIDEO_SIZE).with_duration(duration)


def build_video(slide_paths, output_path):
    clips = [ken_burns_clip(p, DURATION_PER_SLIDE) for p in slide_paths]
    faded = [clips[0]] + [c.with_effects([vfx.CrossFadeIn(TRANSITION_DURATION)]) for c in clips[1:]]
    final = concatenate_videoclips(faded, method="compose", padding=-TRANSITION_DURATION)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.write_videofile(str(output_path), fps=FPS, codec="libx264", logger=None)


# ---------------------------------------------------------------------------
# Endpoint principal
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/generate", methods=["POST"])
def generate():
    if request.headers.get("X-API-Key") != GENERATION_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True)
    sujet_id = payload.get("sujet_id", "sans_id")
    titre = payload.get("titre", "sujet")
    niveau = payload.get("niveau_verification", "non évalué")
    slides = payload.get("slides", [])
    generer_video = payload.get("generer_video", True)
    export_instagram_jpeg = payload.get("export_instagram_jpeg") is True
    dry_run = payload.get("dry_run", False)  # utile pour tester le service sans consommer de crédits OpenAI

    if not slides:
        return jsonify({"error": "aucune slide fournie"}), 400

    safe_name = re.sub(r"[^a-z0-9_-]", "", titre.lower().replace(" ", "_"))[:50]
    sujet_dir = OUTPUT_DIR / f"{safe_name}_{sujet_id}"
    final_dir = sujet_dir / "final"

    log.info(f"Génération pour '{titre}' ({len(slides)} slide(s), dry_run={dry_run})")

    final_paths = []
    for i, slide_text in enumerate(slides):
        bg_path = sujet_dir / f"bg_{i+1}.png"
        prompt = build_image_prompt(titre, slide_text, i)
        generate_background(prompt, bg_path, dry_run=dry_run)

        final_path = final_dir / f"slide_{i+1}_final.png"
        compose_slide(bg_path, slide_text, niveau, i == len(slides) - 1, final_path)
        final_paths.append(final_path)
        log.info(f"  Slide {i+1}/{len(slides)} composée.")

    video_url = None
    if generer_video:
        video_path = sujet_dir / "video_reel.mp4"
        build_video(final_paths, video_path)
        video_url = f"{PUBLIC_BASE_URL}/files/{sujet_dir.name}/video_reel.mp4"
        log.info("  Vidéo générée.")

    slides_urls = [f"{PUBLIC_BASE_URL}/files/{sujet_dir.name}/final/{p.name}" for p in final_paths]

    result = {"sujet_id": sujet_id, "slides_urls": slides_urls, "video_url": video_url}
    if export_instagram_jpeg:
        instagram_urls = []
        for path in final_paths:
            jpeg_path = path.with_suffix(".jpg")
            with Image.open(path) as slide:
                slide.convert("RGB").save(jpeg_path, format="JPEG", quality=95, subsampling=0)
            instagram_urls.append(f"{PUBLIC_BASE_URL}/files/{sujet_dir.name}/final/{jpeg_path.name}")
        result["instagram_image_urls"] = instagram_urls
    return jsonify(result)


@app.route("/files/<path:filepath>")
def serve_file(filepath):
    return send_from_directory(OUTPUT_DIR, filepath)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
