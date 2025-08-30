# auto_reels_wp_publish.py
# WP -> Arte (padrão aprovado) -> MP4 10s -> Cloudinary -> Facebook Vídeo + Instagram Reels
# Python 3.11+ | Requer: requests, python-dotenv, Pillow, cloudinary, ffmpeg no PATH

import os, io, re, time, json, math, logging, textwrap, subprocess
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv

from PIL import Image, ImageDraw, ImageFont, ImageOps
import cloudinary
import cloudinary.uploader

# ----------------------- AJUSTES RÁPIDOS --------------------
CFG = {
    # Tela / base
    "W": 1080,
    "H": 1920,
    "BG_COLOR": (0, 0, 0),

    # Bloco da imagem superior (imagem de matéria)
    # ocupa ~60% da altura e corta em "cover"
    "IMG_TOP": 0,
    "IMG_HEIGHT": 1150,   # deixe entre 1080 e 1200 p/ mais/menos imagem

    # LOGO (sobre a faixa divisória preta, sem cobrir o vermelho)
    "LOGO_PATH": "logo_boca.png",
    "LOGO_TARGET_W": 360,
    "LOGO_Y": 790,        # SUBIR/DECER o logo aqui

    # Faixa vermelha da CATEGORIA
    "CAT_BAR_H": 120,     # ALTURA da faixa
    "CAT_BAR_Y": 1100,    # POSIÇÃO Y da faixa (meio da tela)
    "CAT_BAR_COLOR": (225, 41, 23),
    "CAT_FONT": "Anton-Regular.ttf",   # mais "impact" na categoria (legível)
    "CAT_FONT_SIZE": 80,  # TAMANHO do texto na faixa

    # Caixa branca do TÍTULO
    "TITLE_BOX_MARGIN_X": 60,
    "TITLE_BOX_Y": 1240,
    "TITLE_BOX_H": 260,       # ALTURA da caixa (ajuste para títulos longos)
    "TITLE_BOX_RADIUS": 22,
    "TITLE_BOX_COLOR": (255, 255, 255),
    "TITLE_FONT": "Anton-Regular.ttf",
    "TITLE_FONT_SIZE": 65,    # TAMANHO da fonte do título
    "TITLE_LETTER_SPACING": 0,

    # @handle (rodapé)
    "HANDLE_TEXT": "@BOCANOTROMBONELITORAL",
    "HANDLE_FONT": "Roboto-Bold.ttf",
    "HANDLE_FONT_SIZE": 42,
    "HANDLE_COLOR": (255, 204, 0),
    "HANDLE_Y": 1600,     # SUBIR/DESCER o @ aqui

    # Vídeo
    "VIDEO_SECONDS": 10,
    "AUDIO_PATH": "audio_fundo.mp3",   # opcional; se não existir, gera sem áudio
    "FFMPEG_BIN": "ffmpeg",

    # Loop/ciclo
    "WP_POSTS": 5,
    "SLEEP_BETWEEN": 300,  # 5 min
}

# ------------------------ LOGGING ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("auto-reels")

# ------------------------ ENV / SESSÃO ----------------------
load_dotenv()
WP_URL      = os.getenv("WP_URL", "").strip().rstrip("/")
PAGE_TOKEN  = os.getenv("USER_ACCESS_TOKEN", "").strip()
PAGE_ID     = os.getenv("FACEBOOK_PAGE_ID", "").strip()
IG_ID       = os.getenv("INSTAGRAM_ID", "").strip()

cloud_name  = os.getenv("CLOUDINARY_CLOUD_NAME")
cloud_key   = os.getenv("CLOUDINARY_API_KEY")
cloud_sec   = os.getenv("CLOUDINARY_API_SECRET")

if cloud_name and cloud_key and cloud_sec:
    cloudinary.config(cloud_name=cloud_name, api_key=cloud_key, api_secret=cloud_sec, secure=True)

os.makedirs("out", exist_ok=True)

def session_with_retry():
    s = requests.Session()
    retries = Retry(
        total=3, backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET","POST"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

HTTP = session_with_retry()

# ========================== WP ==============================

def strip_tags(html):
    if not html:
        return ""
    txt = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"&nbsp;", " ", txt)
    txt = re.sub(r"&amp;", "&", txt)
    txt = re.sub(r"\s+\n", "\n", txt)
    return txt.strip()

def get_posts():
    # usa _embed pra pegar imagem destacada direto
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    params = {
        "per_page": CFG["WP_POSTS"],
        "orderby": "date",
        "_embed": "1",
        "_fields": "id,title,content,link,categories,_embedded",
    }
    r = HTTP.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def first_image_from_content_html(html):
    if not html:
        return None
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
    if m:
        return m.group(1)
    return None

def featured_image_from_embed(post):
    # WP _embedded => featured media
    emb = post.get("_embedded") or {}
    meds = emb.get("wp:featuredmedia") or []
    if meds and isinstance(meds, list):
        item = meds[0] or {}
        src = item.get("source_url")
        if src:
            return src
    return None

def category_name(post):
    # simples: usa primeira categoria (se quiser nomes reais, dá pra consultar /wp/v2/categories)
    cats = post.get("categories") or []
    if cats:
        return "Notícia"
    return "Notícia"

# ======================== ARTE ==============================

def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        log.warning("⚠️  Fonte não encontrada (%s). Usando fonte padrão.", path)
        return ImageFont.load_default()

def text_bbox(draw, text, font):
    # compatível com Pillow novo (substitui textsize)
    left, top, right, bottom = draw.textbbox((0,0), text, font=font)
    return right-left, bottom-top

def draw_rounded_rect(draw, xy, radius, fill):
    x1, y1, x2, y2 = xy
    ImageDraw.DrawRoundedRectangle = getattr(ImageDraw, "DrawRoundedRectangle", None)
    # implementação manual (compat)
    draw.rounded_rectangle(xy, radius=radius, fill=fill)

def fit_cover(im, target_w, target_h):
    # corta a imagem com "cover"
    w, h = im.size
    scale = max(target_w/w, target_h/h)
    nw, nh = int(w*scale), int(h*scale)
    im2 = im.resize((nw, nh), Image.LANCZOS)
    # recorte central
    left = (nw - target_w)//2
    top  = (nh - target_h)//2
    return im2.crop((left, top, left+target_w, top+target_h))

def wrap_text_to_box(draw, text, font, max_width, max_lines):
    # quebra título em linhas pra não sair da caixa
    words = text.split()
    lines = []
    curr = ""
    for w in words:
        test = (curr + " " + w).strip()
        tw, _ = text_bbox(draw, test, font)
        if tw <= max_width:
            curr = test
        else:
            if curr:
                lines.append(curr)
            curr = w
        if len(lines) >= max_lines:
            break
    if curr and len(lines) < max_lines:
        lines.append(curr)
    # se estourou, põe "..."
    if len(lines) == max_lines:
        tw, _ = text_bbox(draw, lines[-1] + " …", font)
        if tw <= max_width:
            lines[-1] += " …"
    return lines

def baixar_imagem(url):
    r = HTTP.get(url, timeout=30)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    # uniformiza modo
    if img.mode not in ("RGB","RGBA"):
        img = img.convert("RGB")
    return img

def gerar_arte(img_url, titulo, categoria, post_id):
    W, H = CFG["W"], CFG["H"]
    base = Image.new("RGB", (W,H), CFG["BG_COLOR"])

    # 1) IMAGEM DESTAQUE no topo (cover)
    try:
        im = baixar_imagem(img_url)
    except Exception as e:
        log.warning("⚠️  Falha ao baixar imagem: %s", e)
        # fallback liso (preto)
        im = Image.new("RGB", (W, CFG["IMG_HEIGHT"]), (0,0,0))
    im_fit = fit_cover(im, W, CFG["IMG_HEIGHT"])
    base.paste(im_fit, (0, CFG["IMG_TOP"]))

    draw = ImageDraw.Draw(base)

    # 2) Faixa vermelha da CATEGORIA
    cat_h = CFG["CAT_BAR_H"]
    cat_y = CFG["CAT_BAR_Y"]
    draw.rectangle([0, cat_y, W, cat_y + cat_h], fill=CFG["CAT_BAR_COLOR"])
    font_cat = load_font(CFG["CAT_FONT"], CFG["CAT_FONT_SIZE"])
    cat_text = (categoria or "NOTÍCIA").upper()
    cat_w, cat_h_meas = text_bbox(draw, cat_text, font_cat)
    draw.text(((W - cat_w)//2, cat_y + (cat_h - cat_h_meas)//2), cat_text, fill=(255,255,255), font=font_cat)

    # 3) Caixa branca do TÍTULO
    box_x1 = CFG["TITLE_BOX_MARGIN_X"]
    box_x2 = W - CFG["TITLE_BOX_MARGIN_X"]
    box_y1 = CFG["TITLE_BOX_Y"]
    box_y2 = box_y1 + CFG["TITLE_BOX_H"]
    draw_rounded_rect(draw, (box_x1, box_y1, box_x2, box_y2), CFG["TITLE_BOX_RADIUS"], CFG["TITLE_BOX_COLOR"])

    font_title = load_font(CFG["TITLE_FONT"], CFG["TITLE_FONT_SIZE"])
    # quebra em até 3 linhas
    max_width = box_x2 - box_x1 - 40
    lines = wrap_text_to_box(draw, titulo.strip(), font_title, max_width, max_lines=3)
    total_h = 0
    line_heights = []
    for ln in lines:
        _, lh = text_bbox(draw, ln, font_title)
        line_heights.append(lh)
        total_h += lh
    y_text = box_y1 + (CFG["TITLE_BOX_H"] - total_h)//2
    for i, ln in enumerate(lines):
        tw, lh = text_bbox(draw, ln, font_title)
        x = box_x1 + (max_width - tw)//2 + 20
        draw.text((x, y_text), ln, fill=(0,0,0), font=font_title)
        y_text += lh

    # 4) LOGO (centralizado na largura) — garantir RGBA
    try:
        logo = Image.open(CFG["LOGO_PATH"]).convert("RGBA")
        lw, lh = logo.size
        target_w = CFG["LOGO_TARGET_W"]
        scale = target_w / lw
        logo = logo.resize((int(lw*scale), int(lh*scale)), Image.LANCZOS)
        lx = (W - logo.size[0])//2
        ly = CFG["LOGO_Y"]
        base.paste(logo, (lx, ly), mask=logo)
    except Exception as e:
        log.warning("⚠️  Logo falhou: %s", e)

    # 5) HANDLE (@) no rodapé
    font_handle = load_font(CFG["HANDLE_FONT"], CFG["HANDLE_FONT_SIZE"])
    handle_text = CFG["HANDLE_TEXT"]
    hw, hh = text_bbox(draw, handle_text, font_handle)
    draw.text(((W - hw)//2, CFG["HANDLE_Y"]), handle_text, fill=CFG["HANDLE_COLOR"], font=font_handle)

    out_path = os.path.join("out", f"arte_{post_id}.jpg")
    base.save(out_path, "JPEG", quality=92)
    log.info("✅ Arte pronta: %s", out_path)
    return out_path

# ======================== VÍDEO ==============================

def make_video(image_path, out_mp4, seconds=10, audio_path=None):
    # Gera mp4 1080x1920 com áudio opcional
    cmd = [
        CFG["FFMPEG_BIN"], "-y",
        "-loop", "1", "-i", image_path,
    ]
    if audio_path and os.path.exists(audio_path):
        cmd += ["-stream_loop", "-1", "-i", audio_path, "-shortest"]
    cmd += [
        "-t", str(seconds),
        "-r", "25",
        "-vf", "format=yuv420p",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
    ]
    if audio_path and os.path.exists(audio_path):
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += [out_mp4]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_mp4

# ======================= PUBLISH =============================

def upload_cloudinary(local_mp4):
    up = cloudinary.uploader.upload(
        local_mp4,
        resource_type="video",
        folder="reels_auto",
        overwrite=True,
        use_filename=True,
        unique_filename=False,
    )
    return up["secure_url"]

def fb_publish_video(page_id, token, video_url, description):
    url = f"https://graph.facebook.com/v23.0/{page_id}/videos"
    data = {"file_url": video_url, "description": description}
    r = HTTP.post(url, data=data, params={"access_token": token}, timeout=60)
    r.raise_for_status()
    return r.json().get("id")

def ig_create_media(ig_id, token, video_url, caption):
    url = f"https://graph.facebook.com/v23.0/{ig_id}/media"
    data = {"media_type": "REELS", "video_url": video_url, "caption": caption}
    r = HTTP.post(url, data=data, params={"access_token": token}, timeout=60)
    r.raise_for_status()
    return r.json()["id"]

def ig_status(creation_id, token):
    url = f"https://graph.facebook.com/v23.0/{creation_id}"
    r = HTTP.get(url, params={"fields":"status_code","access_token":token}, timeout=30)
    r.raise_for_status()
    return r.json().get("status_code")

def ig_publish(ig_id, token, creation_id):
    url = f"https://graph.facebook.com/v23.0/{ig_id}/media_publish"
    r = HTTP.post(url, data={"creation_id": creation_id}, params={"access_token": token}, timeout=60)
    r.raise_for_status()
    return r.json()

# ================ CAPTION (TEXTO COMPLETO) ===================

def clean_caption(title, content_text, link):
    # Junta título + corpo + chamada final, respeitando limites (IG ~2.200).
    body = content_text.strip()
    final_line = f"\n\nLeia completo: {link}"
    caption_full = f"{title}\n\n{body}{final_line}"

    # IG: 2.200 chars aprox
    MAX_IG = 2200
    if len(caption_full) > MAX_IG:
        caption_full = caption_full[:MAX_IG-3] + "..."
    return caption_full

# ===================== LOOP PRINCIPAL ========================

def process_once():
    # pega posts
    posts = get_posts()
    if not posts:
        log.info("Nenhum post retornado.")
        return

    for p in posts:
        pid   = p.get("id")
        title = (p.get("title",{}).get("rendered") or "").strip()
        title = strip_tags(title)
        link  = p.get("link","").strip()
        html  = p.get("content",{}).get("rendered") or ""
        text  = strip_tags(html)
        cat   = category_name(p)  # "Notícia"

        # imagem destacada real (preferência)
        img_url = featured_image_from_embed(p)
        if not img_url:
            # fallback: 1ª imagem do conteúdo
            img_url = first_image_from_content_html(html)
        if not img_url:
            log.info("post %s: sem imagem — pulando", pid)
            continue

        # gera arte
        try:
            arte = gerar_arte(img_url, title, cat, pid)
        except Exception as e:
            log.error("❌ Falha arte post %s: %s", pid, e)
            continue

        # vídeo
        mp4 = os.path.join("out", f"reel_{pid}.mp4")
        try:
            make_video(arte, mp4, CFG["VIDEO_SECONDS"], CFG["AUDIO_PATH"])
        except Exception as e:
            log.error("❌ Falha vídeo post %s: %s", pid, e)
            continue

        # legenda
        caption = clean_caption(title, text, link)

        # upload cloudinary
        try:
            file_url = upload_cloudinary(mp4)
        except Exception as e:
            log.error("❌ Cloudinary falhou post %s: %s", pid, e)
            continue

        # FACEBOOK
        fb_ok = False
        try:
            fb_id = fb_publish_video(PAGE_ID, PAGE_TOKEN, file_url, caption)
            log.info("📘 Publicado na Página (vídeo): id=%s", fb_id)
            fb_ok = True
        except Exception as e:
            log.error("❌ Facebook falhou post %s: %s", pid, e)

        # INSTAGRAM
        try:
            creation = ig_create_media(IG_ID, PAGE_TOKEN, file_url, caption)
            status = ig_status(creation, PAGE_TOKEN)
            while status in ("IN_PROGRESS","IN_PROGRESS_WITH_ERRORS", None):
                log.info("⏳ IG status: %s", status)
                time.sleep(6)
                status = ig_status(creation, PAGE_TOKEN)
            if status == "FINISHED":
                ig_publish(IG_ID, PAGE_TOKEN, creation)
                log.info("🎬 IG Reels publicado!")
            else:
                log.error("❌ IG status final inesperado: %s", status)
        except Exception as e:
            log.error("❌ IG Reels falhou post %s: %s", pid, e)

        # pausa curta entre posts
        time.sleep(2)

def main():
    log.info("🚀 Auto Reels (WP→FB+IG) iniciado")
    if not WP_URL or not PAGE_TOKEN or not PAGE_ID or not IG_ID:
        log.error("❌ Faltam variáveis no .env (WP_URL, USER_ACCESS_TOKEN, FACEBOOK_PAGE_ID, INSTAGRAM_ID).")
        return
    try:
        posts = get_posts()
        log.info("→ Recebidos %d posts", len(posts))
    except Exception:
        pass

    while True:
        try:
            process_once()
        except Exception as e:
            log.error("Falha no ciclo: %s", e)
        log.info("⏳ Fim do ciclo.")
        time.sleep(CFG["SLEEP_BETWEEN"])

if __name__ == "__main__":
    main()
