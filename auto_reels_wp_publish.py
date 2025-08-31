# auto_reels_wp_publish.py
# -*- coding: utf-8 -*-

import os, io, time, json, logging, subprocess, textwrap, html, re
from typing import Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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
    "CAT_BAR_Y": 1100,     # POSIÇÃO Y da faixa (meio da tela)
    "CAT_BAR_COLOR": (225, 41, 23),
    "CAT_FONT": "Roboto-Black.ttf",
    "CAT_FONT_SIZE": 70,  # TAMANHO do texto na faixa

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
    "AUDIO_PATH": "audio_fundo.mp3",
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

# ------------------------ AMBIENTE --------------------------
load_dotenv()
OUT_DIR = "out"
os.makedirs(OUT_DIR, exist_ok=True)

# WP / Graph / Cloudinary
WP_URL = os.getenv("WP_URL", "").rstrip("/")
USER_ACCESS_TOKEN = os.getenv("USER_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "")
INSTAGRAM_ID = os.getenv("INSTAGRAM_ID", "")

CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUD_KEY  = os.getenv("CLOUDINARY_API_KEY")
CLOUD_SEC  = os.getenv("CLOUDINARY_API_SECRET")

# ---------------------- HTTP SESSION ------------------------
def build_http_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"])
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

http = build_http_session()

# ---------------------- FUNÇÕES WP --------------------------
def wp_fetch_posts(limit: int = 5) -> list:
    """
    Busca posts mais recentes COM EMBED, para obter a imagem destacada.
    Só consideraremos imagem destacada. Sem isso, pulamos.
    """
    url = f"{WP_URL}/wp-json/wp/v2/posts?_embed&per_page={limit}&orderby=date"
    # campos úteis adicionais via _fields podem remover _embed -> então deixo completo
    r = http.get(url, timeout=30)
    r.raise_for_status()
    posts = r.json()
    logging.info("→ Recebidos %d posts", len(posts))
    return posts

def wp_get_featured_image_url(post: dict) -> Optional[str]:
    """
    Retorna SOMENTE a URL da IMAGEM DESTACADA:
    - via _embedded['wp:featuredmedia'][0]['source_url']
    - se não existir, tenta 'jetpack_featured_media_url' (alguns WP expõem)
    Sem destacada => None (vamos pular esse post).
    """
    # _embedded caminho padrão
    emb = post.get("_embedded") or {}
    medias = emb.get("wp:featuredmedia") or []
    if medias and isinstance(medias, list):
        src = (medias[0] or {}).get("source_url")
        if isinstance(src, str) and src.startswith("http"):
            return src

    # alternativa: jetpack_featured_media_url
    jfm = post.get("jetpack_featured_media_url")
    if isinstance(jfm, str) and jfm.startswith("http"):
        return jfm

    return None

# ---------------------- ASSETS / FONTS ----------------------
def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        logging.warning("⚠️  Fonte %s não encontrada — usando fallback.", path)
        return ImageFont.load_default()

def text_box_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> tuple[list[str], int]:
    """
    Quebra em linhas para caber no max_width.
    Retorna (linhas, altura_total).
    """
    lines = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        # wrap por palavras
        words = paragraph.split()
        line = ""
        for w in words:
            test = (line + " " + w).strip()
            bbox = draw.textbbox((0,0), test, font=font)
            w_px = bbox[2] - bbox[0]
            if w_px <= max_width:
                line = test
            else:
                if line:
                    lines.append(line)
                line = w
        if line:
            lines.append(line)
    # altura total = soma das alturas das linhas (aprox pela métrica do font)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + 6  # um respiro
    total_h = line_h * len(lines)
    return lines, total_h

def draw_rounded_rect(im: Image.Image, xy: tuple, radius: int, fill):
    x1, y1, x2, y2 = xy
    corner = Image.new("L", (radius*2, radius*2), 0)
    draw_c = ImageDraw.Draw(corner)
    draw_c.pieslice((0,0, radius*2, radius*2), 180, 270, fill=255)
    alpha = Image.new("L", im.size, 255)
    alpha_draw = ImageDraw.Draw(alpha)
    alpha_draw.rectangle((x1+radius, y1, x2-radius, y2), fill=0)
    alpha_draw.rectangle((x1, y1+radius, x2, y2-radius), fill=0)
    alpha.paste(corner, (x1, y1))
    alpha.paste(corner.rotate(90), (x2 - radius*2, y1))
    alpha.paste(corner.rotate(180), (x2 - radius*2, y2 - radius*2))
    alpha.paste(corner.rotate(270), (x1, y2 - radius*2))
    overlay = Image.new("RGB", im.size, fill)
    im.paste(overlay, mask=alpha)

# ---------------------- DOWNLOAD / IMAGEM -------------------
def download_image(url: str) -> Optional[Image.Image]:
    try:
        r = http.get(url, timeout=30)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        # normaliza para RGB
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        return img
    except Exception as e:
        logging.warning("⚠️  Falha ao baixar imagem destacada: %s", e)
        return None

def cover_resize(src: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Redimensiona em "cover": preenche target cortando excesso.
    """
    sw, sh = src.size
    if sw == 0 or sh == 0:
        return src.resize((target_w, target_h), Image.LANCZOS)
    scale = max(target_w / sw, target_h / sh)
    nw, nh = int(sw * scale), int(sh * scale)
    img = src.resize((nw, nh), Image.LANCZOS)
    # crop central
    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))

# ---------------------- ARTE (MANTENDO SUA CFG) ------------
def gerar_arte(bg_img: Image.Image, titulo: str, categoria: str, post_id: int) -> str:
    """
    Gera a arte seguindo EXATAMENTE a sua configuração aprovada.
    """
    W, H = CFG["W"], CFG["H"]
    base = Image.new("RGB", (W, H), CFG["BG_COLOR"])
    draw = ImageDraw.Draw(base)

    # 1) Imagem topo (cover)
    img_h = CFG["IMG_HEIGHT"]
    img_top = CFG["IMG_TOP"]
    bg_cover = cover_resize(bg_img, W, img_h)
    base.paste(bg_cover, (0, img_top))

    # Divisória preta fina entre imagem e faixa? (opcional)
    # draw.rectangle((0, img_h-3, W, img_h), fill=(0,0,0))

    # 2) Faixa vermelha da categoria
    cat_bar_h = CFG["CAT_BAR_H"]
    cat_bar_y = CFG["CAT_BAR_Y"]
    draw.rectangle(
        (0, cat_bar_y, W, cat_bar_y + cat_bar_h),
        fill=CFG["CAT_BAR_COLOR"]
    )

    # Categoria (centralizado na faixa)
    font_cat = load_font(CFG["CAT_FONT"], CFG["CAT_FONT_SIZE"])
    cat_text = (categoria or "").upper()
    bbox = draw.textbbox((0, 0), cat_text, font=font_cat)
    cat_w = bbox[2] - bbox[0]
    cat_h = bbox[3] - bbox[1]
    cat_x = (W - cat_w) // 2
    cat_y = cat_bar_y + (cat_bar_h - cat_h) // 2
    draw.text((cat_x, cat_y), cat_text, font=font_cat, fill=(255, 255, 255))

    # 3) LOGO (sem cobrir o vermelho: sua Y está acima da faixa)
    try:
        logo = Image.open(CFG["LOGO_PATH"])
        if logo.mode == "RGBA":
            # converte para RGB com fundo branco
            bg_rgb = Image.new("RGB", logo.size, (255, 255, 255))
            bg_rgb.paste(logo, mask=logo.split()[-1])
            logo = bg_rgb
        else:
            logo = logo.convert("RGB")
        target_w = CFG["LOGO_TARGET_W"]
        w0, h0 = logo.size
        ratio = target_w / float(w0)
        new_size = (target_w, int(h0 * ratio))
        logo = logo.resize(new_size, Image.LANCZOS)
        logo_x = (W - logo.size[0]) // 2
        logo_y = CFG["LOGO_Y"]
        base.paste(logo, (logo_x, logo_y))
    except Exception as e:
        logging.warning("⚠️  Logo falhou: %s", e)

    # 4) Caixa branca do TÍTULO
    title = (html.unescape(titulo or "")).strip()
    font_title = load_font(CFG["TITLE_FONT"], CFG["TITLE_FONT_SIZE"])

    box_y = CFG["TITLE_BOX_Y"]
    box_h = CFG["TITLE_BOX_H"]
    margin_x = CFG["TITLE_BOX_MARGIN_X"]
    box_x1 = margin_x
    box_x2 = W - margin_x
    box_y1 = box_y
    box_y2 = box_y + box_h

    # desenha caixa arredondada branca
    # (para performance, desenhamos shape manual simples)
    radius = CFG["TITLE_BOX_RADIUS"]
    # forma simplificada (sem alpha): retângulo + cantos aproximados
    draw.rounded_rectangle(
        (box_x1, box_y1, box_x2, box_y2),
        radius=radius,
        fill=CFG["TITLE_BOX_COLOR"]
    )

    # quebra do título para caber na caixa
    max_text_w = (box_x2 - box_x1) - 40
    lines, total_h = text_box_size(draw, title, font_title, max_text_w)

    # se altura estourar, corta linhas pelo fim
    ascent, descent = font_title.getmetrics()
    line_h = ascent + descent + 6
    max_lines = max(1, box_h // line_h)
    if len(lines) > max_lines:
        # reduz e adiciona "…"
        lines = lines[:max_lines]
        if lines:
            # garante reticências na última
            last = lines[-1]
            # cabe "…"? se não, corta 3 chars
            while True:
                bbox = draw.textbbox((0,0), last + "…", font=font_title)
                if (bbox[2]-bbox[0]) <= max_text_w or len(last) <= 3:
                    lines[-1] = last + "…"
                    break
                last = last[:-1]

    # escreve centralizado verticalmente na caixa
    cur_y = box_y1 + (box_h - (line_h * len(lines))) // 2
    for ln in lines:
        bbox_ln = draw.textbbox((0,0), ln, font=font_title)
        ln_w = bbox_ln[2] - bbox_ln[0]
        x = box_x1 + ( (box_x2 - box_x1) - ln_w ) // 2
        draw.text((x, cur_y), ln, fill=(0,0,0), font=font_title)
        cur_y += line_h

    # 5) @handle (rodapé)
    font_handle = load_font(CFG["HANDLE_FONT"], CFG["HANDLE_FONT_SIZE"])
    handle = CFG["HANDLE_TEXT"]
    hb = draw.textbbox((0,0), handle, font=font_handle)
    handle_w = hb[2]-hb[0]
    handle_x = (W - handle_w)//2
    handle_y = CFG["HANDLE_Y"]
    draw.text((handle_x, handle_y), handle, font=font_handle, fill=CFG["HANDLE_COLOR"])

    # salva
    out_path = os.path.join(OUT_DIR, f"arte_{post_id}.jpg")
    base.save(out_path, "JPEG", quality=92, optimize=True, progressive=True)
    logging.info("✅ Arte pronta: %s", out_path)
    return out_path

# ---------------------- VÍDEO (FFMPEG) ----------------------
def make_video_from_image(img_path: str, out_mp4: str, seconds: int, audio_path: Optional[str]) -> bool:
    """
    Gera MP4 vertical 9:16, H.264 + AAC, com imagem estática.
    Se tiver áudio, mixa.
    """
    ffmpeg = CFG["FFMPEG_BIN"]
    if audio_path and os.path.isfile(audio_path):
        cmd = [
            ffmpeg,
            "-y",
            "-loop", "1",
            "-t", str(seconds),
            "-i", img_path,
            "-i", audio_path,
            "-vf", "format=yuv420p,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "-r", "25",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.0",
            "-preset", "medium",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            out_mp4
        ]
    else:
        cmd = [
            ffmpeg,
            "-y",
            "-loop", "1",
            "-t", str(seconds),
            "-i", img_path,
            "-vf", "format=yuv420p,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "-r", "25",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-level", "4.0",
            "-preset", "medium",
            "-crf", "23",
            out_mp4
        ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        logging.error("❌ ffmpeg falhou: %s", e)
        return False

# ---------------------- CLOUDINARY (opcional IG) ------------
def cloudinary_upload(local_path: str) -> Optional[str]:
    """
    Sobe o vídeo no Cloudinary e retorna secure_url (pública).
    Se não tiver credenciais, retorna None.
    """
    if not (CLOUD_NAME and CLOUD_KEY and CLOUD_SEC):
        return None
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(
            cloud_name=CLOUD_NAME,
            api_key=CLOUD_KEY,
            api_secret=CLOUD_SEC,
            secure=True
        )
        resp = cloudinary.uploader.upload_large(
            local_path,
            resource_type="video",
            folder="auto_reels",
            overwrite=True
        )
        return resp.get("secure_url")
    except Exception as e:
        logging.error("❌ Cloudinary falhou: %s", e)
        return None

# ---------------------- FACEBOOK PUBLISH --------------------
def publish_video_to_facebook(file_path: str, description: str) -> bool:
    page_id = FACEBOOK_PAGE_ID
    token   = USER_ACCESS_TOKEN
    if not (page_id and token):
        logging.error("❌ Faltam FACEBOOK_PAGE_ID/USER_ACCESS_TOKEN no .env")
        return False

    url = f"https://graph.facebook.com/v23.0/{page_id}/videos?access_token={token}"
    for attempt in (1, 2):
        try:
            with open(file_path, "rb") as f:
                r = http.post(url, data={"description": description[:2200]},
                              files={"source": f}, timeout=600)
            r.raise_for_status()
            vid = r.json().get("id")
            logging.info("📘 Publicado na Página (vídeo): id=%s", vid)
            return True
        except Exception as e:
            body = getattr(e, "response", None).text if hasattr(e, "response") and e.response else ""
            logging.error("❌ Facebook falhou (tentativa %s): %s | resp=%s", attempt, e, body)
            time.sleep(3)
    return False

# ---------------------- INSTAGRAM REELS ---------------------
def publish_reel_to_ig(video_public_url: str, caption: str) -> bool:
    """
    Fluxo recomendado:
      1) POST /{ig-id}/media (media_type=REELS, video_url, caption)
      2) GET  /{ig-id}/media_publish_status?creation_id=...
      3) POST /{ig-id}/media_publish {creation_id}
    Poll até FINISHED. 1 retry se ERROR/TIMEOUT.
    """
    ig_id = INSTAGRAM_ID
    token = USER_ACCESS_TOKEN
    if not (ig_id and token):
        logging.error("❌ Faltam INSTAGRAM_ID/USER_ACCESS_TOKEN no .env")
        return False

    base = f"https://graph.facebook.com/v23.0/{ig_id}"
    def _create() -> Optional[str]:
        try:
            payload = {
                "media_type": "REELS",
                "video_url": video_public_url,
                "caption": caption[:2200],
                "access_token": token,
            }
            r = http.post(f"{base}/media", data=payload, timeout=60)
            r.raise_for_status()
            return r.json().get("id")
        except Exception as e:
            body = getattr(e, "response", None).text if hasattr(e, "response") and e.response else ""
            logging.error("❌ IG /media falhou: %s | %s", e, body)
            return None

    def _poll(creation_id: str, max_wait=150) -> str:
        t0 = time.time()
        while time.time() - t0 < max_wait:
            r = http.get(
                f"{base}/media_publish_status",
                params={"creation_id": creation_id, "access_token": token},
                timeout=20
            )
            if r.status_code == 200:
                js = r.json()
                st = js.get("status", "")
                if st in ("FINISHED", "ERROR"):
                    logging.info("⏳ IG status: %s", st)
                    return st
                logging.info("⏳ IG status: %s", st or "IN_PROGRESS")
            time.sleep(6)
        return "TIMEOUT"

    def _publish(creation_id: str) -> bool:
        try:
            r = http.post(f"{base}/media_publish",
                          data={"creation_id": creation_id, "access_token": token},
                          timeout=60)
            r.raise_for_status()
            logging.info("🎬 IG Reels publicado!")
            return True
        except Exception as e:
            body = getattr(e, "response", None).text if hasattr(e, "response") and e.response else ""
            logging.error("❌ IG /media_publish falhou: %s | %s", e, body)
            return False

    # tentativa 1
    cid = _create()
    if not cid:
        return False
    st = _poll(cid)
    if st == "FINISHED":
        return _publish(cid)
    logging.error("❌ IG status final inesperado: %s", st)

    # retry único
    time.sleep(4)
    cid = _create()
    if not cid:
        return False
    st = _poll(cid)
    if st == "FINISHED":
        return _publish(cid)
    logging.error("❌ IG retry: status final inesperado: %s", st)
    return False

# ---------------------- CAPTION -----------------------------
def build_caption(post: dict) -> str:
    """
    Legenda: Título + Link. (Mantendo simples)
    """
    title = html.unescape((post.get("title", {}) or {}).get("rendered", "")).strip()
    link  = post.get("link", "")
    cap = f"{title}\n\nMais em: jornalvozdolitoral.com"
    if link:
        cap += f"\n{link}"
    return cap[:2200]

def get_category_name_from_post(post: dict) -> str:
    """
    Tenta achar o nome da categoria pelo _embedded.
    Se não vier, retorna 'NOTÍCIAS'.
    """
    emb = post.get("_embedded") or {}
    terms = emb.get("wp:term") or []
    # wp:term é lista de listas (categorias/tags). Procurar taxonomia 'category'
    for group in terms:
        for t in group or []:
            if t.get("taxonomy") == "category":
                name = t.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return "NOTÍCIAS"

# ---------------------- MAIN LOOP ---------------------------
def process_once():
    posts = wp_fetch_posts(CFG["WP_POSTS"])
    for post in posts:
        pid = post.get("id")
        img_url = wp_get_featured_image_url(post)
        if not img_url:
            logging.info("post %s: sem imagem — pulando", pid)
            continue

        # baixa IMAGEM DESTACADA
        bg = download_image(img_url)
        if not bg:
            logging.info("post %s: falha ao baixar imagem — pulando", pid)
            continue

        # dados
        raw_title = (post.get("title", {}) or {}).get("rendered", "")
        titulo = html.unescape(raw_title).strip()
        categoria = get_category_name_from_post(post)
        caption = build_caption(post)

        # Gera ARTE (sem alterar sua CFG)
        arte_path = gerar_arte(bg, titulo, categoria, pid)

        # Gera VÍDEO
        video_path = os.path.join(OUT_DIR, f"reel_{pid}.mp4")
        okv = make_video_from_image(
            arte_path,
            video_path,
            CFG["VIDEO_SECONDS"],
            CFG.get("AUDIO_PATH")
        )
        if not okv:
            continue

        # Publica no Facebook
        publish_video_to_facebook(video_path, caption)

        # Publica no Instagram: precisa URL pública (Cloudinary)
        video_url = cloudinary_upload(video_path)
        if video_url:
            publish_reel_to_ig(video_url, caption)
        else:
            logging.warning("⚠️  Sem Cloudinary configurado — não publiquei no IG.")

    logging.info("⏳ Fim do ciclo.")

def main():
    logging.info("🚀 Auto Reels (WP→FB+IG) iniciado")
    while True:
        try:
            process_once()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logging.exception("❌ Erro no ciclo: %s", e)
        time.sleep(CFG["SLEEP_BETWEEN"])

if __name__ == "__main__":
    main()
