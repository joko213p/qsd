from __future__ import annotations

# =============================================================================
#  Instagram → Telegram downloader bot
#  Récupère posts / reels / stories / "à la une" d'un profil Instagram et les
#  envoie sur Telegram. Conçu pour Railway + proxy Webshare.io.
#
#  Auth Instagram : par COOKIES (sessionid) — méthode la plus stable sur serveur
#  (pas de mot de passe ni de 2FA à gérer en production).
#
#  ⚠️  À n'utiliser que sur ton propre compte ou du contenu public que tu as le
#      droit d'archiver. Respecte les CGU d'Instagram et la vie privée d'autrui.
# =============================================================================

import os
import re
import time
import random
import base64
import shutil
import asyncio
import logging
import tempfile
import threading
import http.cookiejar as cookiejar
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional, Tuple

import instaloader
from instaloader import exceptions as ie

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import (
    RetryAfter,
    TimedOut,
    NetworkError,
    BadRequest,
    Forbidden,
    TelegramError,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger("ig-bot")

# ── Configuration (variables d'environnement) ─────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")

# IMPORTANT : on lit le proxy puis on SUPPRIME les variables proxy standard de
# l'environnement, pour que NI requests (instaloader) NI httpx (Telegram) ne les
# utilisent automatiquement. Le proxy ne sera appliqué QU'À Instagram/yt-dlp.
PROXY_URL = (
    os.environ.get("PROXY")
    or os.environ.get("HTTPS_PROXY")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("http_proxy")
    or ""
).strip()
for _v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_v, None)

IG_COOKIES_B64 = os.environ.get("IG_COOKIES_B64", "").strip()
IG_COOKIES = os.environ.get("IG_COOKIES", "")  # alternative : contenu Netscape brut

_raw_group = os.environ.get("GROUP_ID", "").strip()
GROUP_ID: Optional[int] = int(_raw_group) if _raw_group.lstrip("-").isdigit() else None

# Liste blanche (optionnelle mais recommandée) : seuls ces IDs Telegram peuvent
# utiliser le bot. Vide = tout le monde.
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "").strip()) if x.isdigit()
}

MAX_POSTS = int(os.environ.get("MAX_POSTS", "30"))      # nb max de publications/reels
MAX_FILES = int(os.environ.get("MAX_FILES", "250"))     # garde-fou global d'envoi
PER_ITEM_SLEEP = (1.0, 2.5)                              # délai aléatoire entre posts (anti-ban)

TG_MAX_BYTES = 49 * 1024 * 1024     # limite d'envoi Bot API (~50 Mo)
TG_PHOTO_MAX = 10 * 1024 * 1024     # au-delà → envoyé en document

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IG_WEB_APP_ID = "936619743392459"  # X-IG-App-ID de l'app web Instagram

COOKIE_FILE: Optional[str] = None  # rempli au démarrage si cookies fournis

# Sérialise tout le travail Instagram (1 job lourd à la fois → plus gentil pour l'IP)
IG_LOCK = asyncio.Lock()
# Conversations en cours, pour éviter les doubles déclenchements
BUSY: set[int] = set()

RESERVED = {
    "p", "reel", "reels", "stories", "story", "explore", "accounts",
    "tv", "direct", "about", "developer", "legal",
}


# =============================================================================
#  Moteur Instagram
# =============================================================================
class IGEngine:
    """Instance instaloader unique, authentifiée par cookies, configurée proxy."""

    def __init__(self) -> None:
        self.L: Optional[instaloader.Instaloader] = None
        self.authed: bool = False
        self.logged_user: Optional[str] = None

    def setup(self) -> None:
        L = instaloader.Instaloader(
            quiet=True,
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            storyitem_metadata_txt_pattern="",
            iphone_support=False,           # évite les erreurs liées à l'API iPhone
            request_timeout=30.0,
            max_connection_attempts=3,
        )
        session = L.context._session  # requests.Session interne (stable)

        if PROXY_URL:
            session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
            logger.info("Proxy Instagram actif.")
        else:
            logger.warning("Aucun proxy configuré (variable PROXY).")

        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "X-IG-App-ID": IG_WEB_APP_ID,
        })

        self.authed = load_cookies(session)
        self.L = L

        # IMPORTANT : instaloader exige une "session connectée" pour get_stories /
        # get_highlights. Avec une auth par cookies (sans login()), il faut le lui
        # signaler explicitement, sinon il lève LoginRequiredException à tort.
        if self.authed:
            who = None
            try:
                who = L.test_login()
            except Exception as exc:
                logger.debug("test_login : %s", exc)
            if who:
                L.context.username = who
                self.logged_user = who
                logger.info("Connecté à Instagram en tant que @%s.", who)
            else:
                L.context.username = "session"  # marque la session comme connectée
                logger.info("Cookies présents (compte non confirmé, on continue).")

    def session(self):
        return self.L.context._session


IG = IGEngine()


# ── Cookies ───────────────────────────────────────────────────────────────────
def _clean_b64(s: str) -> str:
    """Nettoie une valeur base64 collée à la main (erreurs fréquentes)."""
    s = s.strip()
    # Erreur classique : on a collé "IG_COOKIES_B64=...." dans le champ valeur.
    for pref in ("IG_COOKIES_B64=", "IG_COOKIES=", "IG_COOKIES_B64:", "base64:"):
        if s.startswith(pref):
            s = s[len(pref):].strip()
    # Guillemets éventuels ajoutés par l'UI.
    s = s.strip().strip('"').strip("'").strip()
    # Retire tout caractère d'espacement interne (retours à la ligne, espaces).
    s = re.sub(r"\s+", "", s)
    # Corrige le padding manquant (= en fin).
    missing = (-len(s)) % 4
    if missing:
        s += "=" * missing
    return s


def _materialize_cookie_file() -> Optional[str]:
    """Écrit le fichier cookies Netscape sur disque (pour yt-dlp et instaloader)."""
    global COOKIE_FILE
    data: Optional[bytes] = None
    if IG_COOKIES_B64:
        cleaned = _clean_b64(IG_COOKIES_B64)
        try:
            data = base64.b64decode(cleaned, validate=False)
        except Exception as exc:
            logger.error("IG_COOKIES_B64 invalide (base64) : %s", exc)
    elif IG_COOKIES.strip():
        data = IG_COOKIES.encode("utf-8")

    if not data:
        COOKIE_FILE = None
        return None

    path = "/tmp/ig_cookies.txt"
    with open(path, "wb") as f:
        f.write(data)
    COOKIE_FILE = path

    # Journalise un aperçu utile pour le diagnostic (sans révéler les valeurs).
    try:
        txt = data.decode("utf-8", errors="replace")
        has_sid = "sessionid" in txt
        logger.info(
            "Fichier cookies écrit : %d octets, %d lignes, sessionid=%s.",
            len(data), txt.count("\n") + 1, "oui" if has_sid else "NON",
        )
        if not has_sid:
            logger.error(
                "Le fichier cookies ne contient pas 'sessionid' → la valeur "
                "IG_COOKIES_B64 est probablement tronquée ou mal copiée."
            )
    except Exception:
        pass
    return path


def load_cookies(session) -> bool:
    path = _materialize_cookie_file()
    if not path:
        logger.warning("Aucun cookie fourni → stories / 'à la une' / comptes privés indisponibles.")
        return False
    try:
        jar = cookiejar.MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        for c in jar:
            session.cookies.set(c.name, c.value, domain=c.domain, path=c.path or "/")
        try:
            sid = session.cookies.get("sessionid", domain=".instagram.com")
        except Exception:
            sid = session.cookies.get("sessionid")
        if not sid:
            logger.error("Cookie 'sessionid' absent → connexion Instagram non valide.")
            return False
        logger.info("Cookies Instagram chargés (sessionid présent).")
        return True
    except Exception as exc:
        logger.error("Erreur de chargement des cookies : %s", exc)
        return False


# =============================================================================
#  Helpers
# =============================================================================
def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def human_size(n: float) -> str:
    for unit in ("o", "Ko", "Mo"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} Go"


def slug(text: str, maxlen: int = 30) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
    return (s[:maxlen] or "x").strip("_") or "x"


def parse_input(text: str) -> Tuple[str, Optional[str]]:
    """
    Retourne (kind, value) :
      ("shortcode", "<code>")  → publication / reel unique
      ("profile",   "<user>")  → profil
      ("none", None)           → non reconnu
    """
    if not text:
        return "none", None
    t = text.strip()

    # Publication / reel / igtv unique
    m = re.search(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", t)
    if m:
        return "shortcode", m.group(1)

    # Profil via URL
    t2 = t.split("?")[0].rstrip("/")
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", t2)
    if m and m.group(1).lower() not in RESERVED:
        return "profile", m.group(1)

    # @username ou username brut
    m = re.fullmatch(r"@?([A-Za-z0-9_.]{1,30})", t2)
    if m and m.group(1).lower() not in RESERVED:
        return "profile", m.group(1)

    return "none", None


def _download(session, url: Optional[str], dest: str) -> Optional[str]:
    """Télécharge une URL média (CDN Instagram) vers `dest`. None si échec/vide."""
    if not url:
        return None
    try:
        with session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 15):
                    if chunk:
                        f.write(chunk)
    except Exception as exc:
        logger.warning("Échec téléchargement %s : %s", os.path.basename(dest), exc)
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        return None
    try:
        if os.path.getsize(dest) == 0:
            os.remove(dest)
            return None
    except OSError:
        return None
    return dest


def _fetch_storyitem(session, item, temp_dir: str, prefix: str) -> Optional[str]:
    is_video = bool(getattr(item, "is_video", False))
    url = item.video_url if is_video else item.url
    ext = "mp4" if is_video else "jpg"
    name = f"{prefix}_{getattr(item, 'mediaid', random.randint(1, 1_000_000))}.{ext}"
    return _download(session, url, os.path.join(temp_dir, name))


def _fetch_post(session, post, temp_dir: str) -> List[str]:
    out: List[Optional[str]] = []
    typename = getattr(post, "typename", "")
    if typename == "GraphSidecar":
        for i, node in enumerate(post.get_sidecar_nodes()):
            is_video = bool(getattr(node, "is_video", False))
            url = node.video_url if is_video else node.display_url
            ext = "mp4" if is_video else "jpg"
            out.append(_download(session, url, os.path.join(temp_dir, f"post_{post.shortcode}_{i + 1}.{ext}")))
    elif getattr(post, "is_video", False):
        out.append(_download(session, post.video_url, os.path.join(temp_dir, f"post_{post.shortcode}.mp4")))
    else:
        out.append(_download(session, post.url, os.path.join(temp_dir, f"post_{post.shortcode}.jpg")))
    return [p for p in out if p]


# ── Collecte profil (BLOQUANT → à lancer dans un thread) ───────────────────────
def collect_profile(username: str, kinds: List[str], temp_dir: str) -> Tuple[str, List[str], List[str]]:
    L = IG.L
    session = IG.session()
    errors: List[str] = []
    media: List[str] = []

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except ie.ProfileNotExistsException:
        if not IG.authed:
            return username, [], [
                f"Profil @{username} introuvable — mais la session Instagram "
                "n'est PAS connectée. C'est très probablement la vraie cause : "
                "Instagram bloque l'accès anonyme. Vérifie les cookies (/diag)."
            ]
        return username, [], [
            f"Profil @{username} introuvable. Si tu es sûr qu'il existe, ta "
            "session/ton proxy est peut-être bloqué par Instagram (/diag)."
        ]
    except ie.LoginRequiredException:
        return username, [], ["Connexion requise (cookies invalides ou expirés). Lance /diag."]
    except (ie.ConnectionException, ie.QueryReturnedBadRequestException) as exc:
        return username, [], [f"Instagram a refusé la requête (proxy bloqué ou limite de débit ?) : {exc}"]
    except Exception as exc:
        return username, [], [f"Impossible de récupérer le profil : {exc}"]

    display = profile.username

    if profile.is_private and not profile.followed_by_viewer:
        errors.append("Compte privé non suivi par ton compte → contenu inaccessible.")
        return display, media, errors

    want_posts = "posts" in kinds
    want_reels = "reels" in kinds
    reels_only = want_reels and not want_posts

    # Stories actives
    if "stories" in kinds:
        if not IG.authed:
            errors.append("Stories : connexion (cookies) requise.")
        else:
            try:
                for story in L.get_stories(userids=[profile.userid]):
                    for it in story.get_items():
                        f = _fetch_storyitem(session, it, temp_dir, "story")
                        if f:
                            media.append(f)
            except ie.LoginRequiredException:
                errors.append("Stories : connexion requise.")
            except Exception as exc:
                errors.append(f"Stories : {exc}")

    # "À la une" (highlights)
    if "highlights" in kinds:
        if not IG.authed:
            errors.append("À la une : connexion (cookies) requise.")
        else:
            try:
                for hl in L.get_highlights(profile):
                    pref = f"une_{slug(getattr(hl, 'title', 'une'))}"
                    for it in hl.get_items():
                        f = _fetch_storyitem(session, it, temp_dir, pref)
                        if f:
                            media.append(f)
            except ie.LoginRequiredException:
                errors.append("À la une : connexion requise.")
            except Exception as exc:
                errors.append(f"À la une : {exc}")

    # Publications / reels
    if want_posts or want_reels:
        try:
            n = 0
            for post in profile.get_posts():
                if reels_only and not getattr(post, "is_video", False):
                    continue
                try:
                    media.extend(_fetch_post(session, post, temp_dir))
                    n += 1
                except Exception as exc:
                    errors.append(f"post {getattr(post, 'shortcode', '?')} : {exc}")
                if n >= MAX_POSTS:
                    break
                time.sleep(random.uniform(*PER_ITEM_SLEEP))  # anti rate-limit
        except ie.PrivateProfileNotFollowedException:
            errors.append("Compte privé non suivi.")
        except ie.LoginRequiredException:
            errors.append("Publications : connexion requise.")
        except (ie.ConnectionException, ie.QueryReturnedBadRequestException) as exc:
            errors.append(f"Publications : limite de débit Instagram ? {exc}")
        except Exception as exc:
            errors.append(f"Publications : {exc}")

    return display, media, errors


# ── Collecte d'une URL unique (post/reel) ──────────────────────────────────────
def _glob_media(temp_dir: str) -> List[str]:
    exts = (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".mkv")
    out = []
    for root, _dirs, files in os.walk(temp_dir):
        for fn in files:
            if fn.lower().endswith(exts):
                p = os.path.join(root, fn)
                try:
                    if os.path.getsize(p) > 0:
                        out.append(p)
                except OSError:
                    pass
    return sorted(out)


def _ytdlp_download(url: str, temp_dir: str) -> List[str]:
    try:
        import yt_dlp
    except Exception as exc:
        logger.error("yt-dlp indisponible : %s", exc)
        return []
    opts = {
        "outtmpl": os.path.join(temp_dir, "ytdlp_%(id)s_%(autonumber)03d.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "noprogress": True,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "retries": 3,
        "user_agent": DESKTOP_UA,
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        logger.warning("yt-dlp : %s", exc)
    return _glob_media(temp_dir)


def collect_single(shortcode: str, url: str, temp_dir: str) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    # 1) instaloader par shortcode
    try:
        post = instaloader.Post.from_shortcode(IG.L.context, shortcode)
        files = _fetch_post(IG.session(), post, temp_dir)
        if files:
            return files, errors
        errors.append("instaloader : aucun média extrait.")
    except Exception as exc:
        errors.append(f"instaloader : {exc}")

    # 2) repli yt-dlp
    files = _ytdlp_download(url, temp_dir)
    if files:
        return files, errors
    errors.append("yt-dlp : aucun média téléchargé (contenu privé ou indisponible).")
    return [], errors


# =============================================================================
#  Envoi Telegram
# =============================================================================
async def _edit(msg: Optional[Message], text: str) -> None:
    if not msg:
        return
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.debug("edit_text : %s", exc)
    except TelegramError as exc:
        logger.debug("edit_text : %s", exc)


async def _send_one(bot, chat_id: int, thread_id: Optional[int], path: str, caption: str) -> bool:
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size > TG_MAX_BYTES:
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=f"⚠️ Trop volumineux pour Telegram (> 50 Mo), ignoré : {esc(os.path.basename(path))} ({human_size(size)})",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return False

    ext = os.path.splitext(path)[1].lower()
    base_kwargs = dict(chat_id=chat_id, caption=caption)
    if thread_id is not None:
        base_kwargs["message_thread_id"] = thread_id
    fname = os.path.basename(path)

    for attempt in range(3):
        try:
            with open(path, "rb") as fh:
                if ext in (".mp4", ".mov", ".mkv"):
                    await bot.send_video(video=fh, filename=fname, supports_streaming=True,
                                         write_timeout=300, read_timeout=120, **base_kwargs)
                elif ext in (".jpg", ".jpeg", ".png", ".webp") and size <= TG_PHOTO_MAX:
                    await bot.send_photo(photo=fh, write_timeout=180, read_timeout=120, **base_kwargs)
                else:
                    await bot.send_document(document=fh, filename=fname,
                                            write_timeout=300, read_timeout=120, **base_kwargs)
            return True
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except (TimedOut, NetworkError):
            await asyncio.sleep(3)
        except Forbidden:
            logger.warning("Bot bloqué / sans accès au chat %s", chat_id)
            return False
        except BadRequest as exc:
            logger.warning("send (%s) : %s", fname, exc)
            return False
        except TelegramError as exc:
            logger.warning("send (%s) : %s", fname, exc)
            await asyncio.sleep(2)
    return False


async def get_or_create_topic(bot, chat_id: int, username: str) -> Optional[int]:
    """Crée un sujet de forum si le chat est un supergroupe forum, sinon None."""
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=f"@{username}"[:120])
        return topic.message_thread_id
    except TelegramError:
        return None


async def deliver(context, dest_chat_id: int, thread_id: Optional[int],
                  media: List[str], errors: List[str], status: Optional[Message], label: str) -> None:
    if not media:
        reasons = "\n".join(f"• {esc(e)}" for e in errors) if errors else "Aucun média trouvé."
        await _edit(status, f"😕 Rien à envoyer pour <b>{esc(label)}</b>.\n\n{reasons}")
        return

    media = media[:MAX_FILES]
    total = len(media)
    await _edit(status, f"📤 Envoi de <b>{total}</b> fichier(s) — <b>{esc(label)}</b>…")

    sent = 0
    for i, path in enumerate(media, 1):
        try:
            await context.bot.send_chat_action(chat_id=dest_chat_id, action=ChatAction.UPLOAD_DOCUMENT,
                                                message_thread_id=thread_id)
        except TelegramError:
            pass
        ok = await _send_one(context.bot, dest_chat_id, thread_id, path, caption=label)
        if ok:
            sent += 1
        if i % 15 == 0:
            await _edit(status, f"📤 <b>{label}</b> — {i}/{total} traité(s)…")
        await asyncio.sleep(0.4)

    summary = f"✅ <b>{sent}/{total}</b> fichier(s) envoyé(s) — <b>{esc(label)}</b>."
    if errors:
        items = "\n".join(f"• {esc(e)}" for e in errors[:8])
        summary += f"\n\n⚠️ Remarques :\n{items}"
    await _edit(status, summary)


# =============================================================================
#  Autorisations
# =============================================================================
def authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


# =============================================================================
#  Handlers
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    auth = "✅ Connecté (cookies)" if IG.authed else "⚠️ Non connecté (stories/à la une indisponibles)"
    proxy = "✅ Proxy actif" if PROXY_URL else "⚠️ Sans proxy"
    await update.message.reply_text(
        "👋 <b>Instagram Downloader</b>\n\n"
        "Envoie-moi :\n"
        "• un <b>profil</b> : <code>https://instagram.com/natgeo</code> ou <code>@natgeo</code>\n"
        "• ou un <b>post/reel précis</b> : <code>https://instagram.com/reel/XXXX/</code>\n\n"
        f"🔐 {auth}\n🌐 {proxy}\n"
        f"📦 Limite : {MAX_POSTS} publications max, fichiers ≤ 50 Mo.\n\n"
        "🩺 Problème ? Tape /diag pour un diagnostic.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


def _run_diag() -> List[str]:
    """Diagnostic bloquant (réseau) — à lancer dans un thread."""
    lines: List[str] = []

    # Versions / dépendances
    lines.append(f"instaloader : {getattr(instaloader, '__version__', '?')}")
    try:
        import yt_dlp
        lines.append(f"yt-dlp : {getattr(yt_dlp.version, '__version__', 'présent')}")
    except Exception:
        lines.append("yt-dlp : ABSENT")

    # Cookies
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        try:
            txt = Path(COOKIE_FILE).read_text("utf-8", errors="replace")
            sid = "sessionid" in txt
            lines.append(f"Cookies : fichier OK ({os.path.getsize(COOKIE_FILE)} o), sessionid={'oui' if sid else 'NON'}")
        except Exception as exc:
            lines.append(f"Cookies : fichier illisible ({exc})")
    else:
        lines.append("Cookies : AUCUN (variable IG_COOKIES_B64 vide ou non lue)")

    # Proxy
    lines.append(f"Proxy : {'configuré' if PROXY_URL else 'AUCUN'}")

    # Test de connexion réel
    who = None
    try:
        who = IG.L.test_login()
    except Exception as exc:
        lines.append(f"test_login : erreur — {type(exc).__name__}: {exc}")
    if who:
        lines.append(f"Connexion Instagram : ✅ @{who}")
    else:
        lines.append("Connexion Instagram : ❌ non connecté (cookies expirés/invalides ou proxy bloqué)")

    # Test d'accès à un profil public connu
    try:
        p = instaloader.Profile.from_username(IG.L.context, "instagram")
        lines.append(f"Accès profil public test : ✅ @instagram ({p.mediacount} publications visibles)")
    except Exception as exc:
        lines.append(f"Accès profil public test : ❌ {type(exc).__name__}: {exc}")

    return lines


async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    msg = await update.message.reply_text("🔎 Diagnostic en cours…")
    try:
        async with IG_LOCK:
            lines = await asyncio.to_thread(_run_diag)
    except Exception as exc:
        await _edit(msg, f"❌ Diagnostic impossible : {esc(str(exc))}")
        return
    body = "🩺 <b>Diagnostic</b>\n\n" + "\n".join(f"• {esc(l)}" for l in lines)
    await _edit(msg, body)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    text = (update.message.text or "").strip()
    kind, value = parse_input(text)

    if kind == "none":
        await update.message.reply_text(
            "❌ Lien non reconnu.\nEnvoie un profil (<code>@nom</code> ou son lien) "
            "ou un lien de post/reel.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_key = update.effective_chat.id
    if chat_key in BUSY:
        await update.message.reply_text("⏳ Un téléchargement est déjà en cours, patiente.")
        return

    if kind == "shortcode":
        await run_single_job(update, context, value, text)
    else:  # profile
        context.user_data["ig_username"] = value
        keyboard = [
            [InlineKeyboardButton("📸 Posts", callback_data="t_posts"),
             InlineKeyboardButton("🎬 Reels", callback_data="t_reels")],
            [InlineKeyboardButton("📖 Stories", callback_data="t_stories"),
             InlineKeyboardButton("⭐ À la une", callback_data="t_highlights")],
            [InlineKeyboardButton("✅ Tout télécharger", callback_data="t_all")],
        ]
        await update.message.reply_text(
            f"📲 Profil : <b>@{esc(value)}</b>\n\nQue veux-tu télécharger ?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


TYPE_MAP = {
    "t_posts": (["posts"], "posts"),
    "t_reels": (["reels"], "reels"),
    "t_stories": (["stories"], "stories"),
    "t_highlights": (["highlights"], "à la une"),
    "t_all": (["stories", "highlights", "posts", "reels"], "tout"),
}


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not authorized(update):
        await query.edit_message_text("⛔ Accès non autorisé.")
        return

    username = context.user_data.get("ig_username")
    if not username:
        await query.edit_message_text("❌ Session expirée. Renvoie le lien du profil.")
        return

    kinds, label_kind = TYPE_MAP.get(query.data, (["posts"], "posts"))
    label = f"@{username} · {label_kind}"

    chat_key = update.effective_chat.id
    if chat_key in BUSY:
        await query.edit_message_text("⏳ Un téléchargement est déjà en cours, patiente.")
        return

    dest_chat_id = GROUP_ID if GROUP_ID else update.effective_chat.id
    status = await query.edit_message_text(
        f"⏳ <b>@{esc(username)}</b> — récupération en cours…", parse_mode=ParseMode.HTML
    )
    if not isinstance(status, Message):  # edit inline renvoie parfois True
        status = query.message

    BUSY.add(chat_key)
    temp_dir = tempfile.mkdtemp(prefix="ig_")
    try:
        async with IG_LOCK:
            display, media, errors = await asyncio.to_thread(collect_profile, username, kinds, temp_dir)
        thread_id = await get_or_create_topic(context.bot, dest_chat_id, display) if GROUP_ID else None
        await deliver(context, dest_chat_id, thread_id, media, errors, status, f"@{display} · {label_kind}")
    except Exception as exc:
        logger.exception("Erreur job profil")
        await _edit(status, f"❌ Erreur inattendue : {esc(str(exc))}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        BUSY.discard(chat_key)


async def run_single_job(update: Update, context: ContextTypes.DEFAULT_TYPE, shortcode: str, url: str) -> None:
    chat_key = update.effective_chat.id
    dest_chat_id = GROUP_ID if GROUP_ID else chat_key
    status = await update.message.reply_text("⏳ Téléchargement du contenu…")

    BUSY.add(chat_key)
    temp_dir = tempfile.mkdtemp(prefix="ig_")
    try:
        async with IG_LOCK:
            media, errors = await asyncio.to_thread(collect_single, shortcode, url, temp_dir)
        await deliver(context, dest_chat_id, None, media, errors, status, f"post {shortcode}")
    except Exception as exc:
        logger.exception("Erreur job unique")
        await _edit(status, f"❌ Erreur inattendue : {esc(str(exc))}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        BUSY.discard(chat_key)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception non gérée :", exc_info=context.error)


# =============================================================================
#  Serveur HTTP minimal (health check Railway)
# =============================================================================
def start_health_server() -> None:
    port = os.environ.get("PORT")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silencieux
            return

    try:
        srv = HTTPServer(("0.0.0.0", int(port)), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        logger.info("Serveur de santé sur le port %s.", port)
    except Exception as exc:
        logger.warning("Serveur de santé non démarré : %s", exc)


# =============================================================================
#  Main
# =============================================================================
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ Variable BOT_TOKEN (ou TELEGRAM_BOT_TOKEN) manquante.")

    start_health_server()

    logger.info("Initialisation Instagram…")
    IG.setup()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connection_pool_size(8)
        .pool_timeout(30)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(300)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^t_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)

    logger.info("Bot démarré (auth=%s, proxy=%s).", IG.authed, bool(PROXY_URL))
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
