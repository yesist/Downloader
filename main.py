import os
import re
import io
import glob
import logging
import requests
import pymongo
import yt_dlp
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler,
    CallbackQueryHandler, Filters, CallbackContext
)
from telegram.utils.helpers import mention_html

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════

BOT_TOKEN     = os.getenv("BOT_TOKEN",     "8811538930:AAFcdFaJ0hU92dWSTWnuYYxjPQ0DmtNMzYw")
LOGS_GROUP_ID = int(os.getenv("LOGS_GROUP_ID", "-1002854086015"))
OWNER_ID      = int(os.getenv("OWNER_ID",  "6663845789"))
MONGO_URL     = os.getenv("MONGO_URL",     "mongodb://universal:universal@ac-5uptcsf-shard-00-00.xbri4n0.mongodb.net:27017,ac-5uptcsf-shard-00-01.xbri4n0.mongodb.net:27017,ac-5uptcsf-shard-00-02.xbri4n0.mongodb.net:27017/?ssl=true&replicaSet=atlas-nprng0-shard-0&authSource=admin&appName=universal")
DOWNLOAD_DIR  = os.getenv("DOWNLOAD_DIR",  "downloads")
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "http://localhost:8081/bot")
RAPIDAPI_KEY  = os.getenv("RAPIDAPI_KEY",  "257958bfffmsh1c707de1d328bc2p1863c8jsn9e469f4b5a9d")

# ═══════════════════════════════════════════════════════
# MONGODB
# ═══════════════════════════════════════════════════════

client    = pymongo.MongoClient(MONGO_URL)
db        = client['savenode']
users_col = db['users']
chats_col = db['chats']
config_col = db['config']  # stores cookies + settings

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# COOKIES HELPERS (dynamic, stored in MongoDB)
# ═══════════════════════════════════════════════════════

def get_cookies(platform: str) -> str | None:
    """Get cookies content from MongoDB for given platform."""
    doc = config_col.find_one({"key": f"cookies_{platform}"})
    return doc["value"] if doc else None

def save_cookies(platform: str, content: str) -> None:
    """Save cookies to MongoDB and write to disk."""
    config_col.update_one(
        {"key": f"cookies_{platform}"},
        {"$set": {"value": content, "updated_at": datetime.utcnow()}},
        upsert=True
    )
    # Write to disk for yt-dlp
    path = f"cookies_{platform}.txt"
    with open(path, 'w') as f:
        f.write(content)
    logger.info(f"Cookies saved for {platform}")

def get_cookies_path(platform: str) -> str | None:
    """Write cookies from MongoDB to disk and return path. None if not set."""
    content = get_cookies(platform)
    if not content:
        return None
    path = f"cookies_{platform}.txt"
    with open(path, 'w') as f:
        f.write(content)
    return path

# Load cookies to disk on startup
def load_all_cookies():
    for platform in ["youtube", "instagram"]:
        content = get_cookies(platform)
        if content:
            with open(f"cookies_{platform}.txt", 'w') as f:
                f.write(content)
            logger.info(f"Loaded {platform} cookies from MongoDB")

# ═══════════════════════════════════════════════════════
# PLATFORM DETECTION
# ═══════════════════════════════════════════════════════

PLATFORM_PATTERNS = {
    "youtube_profile": re.compile(
        r'(https?://)?(www\.)?youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w-]+|user/[\w-]+)/?$'
    ),
    "youtube": re.compile(
        r'(https?://)?(www\.)?(youtube\.com/(watch|shorts|embed)|youtu\.be)/.+'
    ),
    "instagram_profile": re.compile(
        r'(https?://)?(www\.)?instagram\.com/(?!p/|reel/|tv/|stories/)([\w.]+)/?$'
    ),
    "instagram": re.compile(
        r'(https?://)?(www\.)?instagram\.com/(p|reel|tv|stories)/.+'
    ),
    "tiktok": re.compile(
        r'(https?://)?(www\.|vm\.)?tiktok\.com/.+'
    ),
    "pinterest_profile": re.compile(
        r'(https?://)?(www\.)?pinterest\.(com|co\.\w+|ca|fr|de|es|pt|jp|au|nz|ru|it)/(?!pin/)([\w-]+)/?$'
    ),
    "pinterest": re.compile(
        r'(https?://)?(www\.)?(pinterest\.(com|co\.\w+|ca|fr|de|es|pt|jp|au|nz|ru|it)|pin\.it)/.+'
    ),
    "twitter": re.compile(
        r'(https?://)?(www\.)?(twitter\.com|x\.com)/.+/status/.+'
    ),
    "facebook": re.compile(
        r'(https?://)?(www\.)?facebook\.com/.+'
    ),
    "reddit": re.compile(
        r'(https?://)?(www\.)?reddit\.com/r/.+/comments/.+'
    ),
}

def detect_platform(url: str) -> str | None:
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.match(url):
            return platform
    return None

def extract_username(url: str, platform: str) -> str:
    """Extract username from profile URLs."""
    if platform == "instagram_profile":
        m = re.search(r'instagram\.com/([\w.]+)/?$', url)
        return m.group(1) if m else ""
    if platform == "youtube_profile":
        m = re.search(r'youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w-]+|user/[\w-]+)', url)
        return m.group(1) if m else ""
    if platform == "pinterest_profile":
        m = re.search(r'pinterest\.[^/]+/([\w-]+)/?$', url)
        return m.group(1) if m else ""
    return ""

def extract_yt_id(url: str) -> str | None:
    patterns = [
        r'youtu\.be/([\w-]{11})',
        r'youtube\.com/watch\?v=([\w-]{11})',
        r'youtube\.com/shorts/([\w-]{11})',
        r'youtube\.com/embed/([\w-]{11})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

# ═══════════════════════════════════════════════════════
# RAPIDAPI HELPERS
# ═══════════════════════════════════════════════════════

def rapidapi_get(host: str, url: str, params: dict = None) -> dict:
    resp = requests.get(
        url,
        headers={
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": host,
            "Content-Type": "application/json",
        },
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

def rapidapi_post(host: str, url: str, json_body: dict = None, form_body: str = None) -> dict:
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": host,
    }
    if form_body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = requests.post(url, headers=headers, data=form_body, timeout=60)
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(url, headers=headers, json=json_body, timeout=60)
    resp.raise_for_status()
    return resp.json()

def download_file(url: str, dest: str) -> str:
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    return dest

# ═══════════════════════════════════════════════════════
# INSTAGRAM PROFILE LOOKUP
# ═══════════════════════════════════════════════════════

def instagram_profile_lookup(username: str) -> dict:
    data = rapidapi_post(
        "instagram120.p.rapidapi.com",
        "https://instagram120.p.rapidapi.com/api/instagram/userInfo",
        json_body={"username": username}
    )
    user = data["result"][0]["user"]
    return {
        "full_name":      user.get("full_name", ""),
        "username":       user.get("username", ""),
        "biography":      user.get("biography", ""),
        "followers":      user.get("follower_count", 0),
        "following":      user.get("following_count", 0),
        "posts":          user.get("media_count", 0),
        "is_verified":    user.get("is_verified", False),
        "is_private":     user.get("is_private", False),
        "is_business":    user.get("is_business", False),
        "profile_pic":    user.get("profile_pic_url", ""),
        "external_url":   user.get("external_url", ""),
    }

def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

# ═══════════════════════════════════════════════════════
# PINTEREST PROFILE LOOKUP
# ═══════════════════════════════════════════════════════

def pinterest_profile_lookup(username: str) -> dict:
    data = rapidapi_get(
        "pinterest-video-and-image-downloader.p.rapidapi.com",
        "https://pinterest-video-and-image-downloader.p.rapidapi.com/pinterest-user",
        params={"username": username}
    )
    return data

# ═══════════════════════════════════════════════════════
# YOUTUBE — GET VIDEO INFO (for quality buttons)
# ═══════════════════════════════════════════════════════

def yt_get_info(video_id: str) -> dict:
    """Fetch video info from yt-api (API 1)."""
    return rapidapi_get(
        "yt-api.p.rapidapi.com",
        "https://yt-api.p.rapidapi.com/dl",
        params={"id": video_id}
    )

def yt_parse_qualities(info: dict) -> list[dict]:
    """
    Parse available qualities from yt-api response.
    Returns list of {label, itag, url, has_audio}
    """
    qualities = {}

    # formats = combined video+audio (e.g. 360p)
    for fmt in info.get("formats", []):
        label = fmt.get("qualityLabel", "")
        url   = fmt.get("url", "")
        if label and url and "mp4" in fmt.get("mimeType", ""):
            qualities[label] = {
                "label": label,
                "url": url,
                "has_audio": True,
                "itag": fmt.get("itag")
            }

    # adaptiveFormats = video only (need merge) — skip for simplicity, use formats only
    # But add best available from adaptive if format missing
    seen_labels = set(qualities.keys())
    for fmt in info.get("adaptiveFormats", []):
        label = fmt.get("qualityLabel", "")
        mime  = fmt.get("mimeType", "")
        url   = fmt.get("url", "")
        # Only mp4, video only adaptive formats (no audio) — skip
        if label and url and "video/mp4" in mime and label not in seen_labels:
            qualities[label] = {
                "label": label,
                "url": url,
                "has_audio": False,
                "itag": fmt.get("itag")
            }
            seen_labels.add(label)

    # Sort by resolution descending
    order = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]
    sorted_q = []
    for lbl in order:
        if lbl in qualities:
            sorted_q.append(qualities[lbl])

    return sorted_q

# ═══════════════════════════════════════════════════════
# YOUTUBE DOWNLOAD — 3 API FALLBACK CHAIN
# ═══════════════════════════════════════════════════════

def yt_download_by_url(video_url: str, video_id: str, label: str) -> str:
    """Download video from direct URL (yt-api response)."""
    dest = os.path.join(DOWNLOAD_DIR, f"yt_{video_id}_{label}.mp4")
    return download_file(video_url, dest)

def yt_api2_download(video_id: str) -> str:
    """all-media-downloader — API 2 fallback."""
    import urllib.parse
    encoded = urllib.parse.quote(f"https://youtu.be/{video_id}", safe='')
    data = rapidapi_post(
        "all-media-downloader1.p.rapidapi.com",
        "https://all-media-downloader1.p.rapidapi.com/all",
        form_body=f"url={encoded}"
    )
    # Find best mp4 URL in response
    links = data.get("links", []) or data.get("formats", []) or []
    if isinstance(data, dict):
        for key in ["url", "download", "mp4", "video"]:
            val = data.get(key)
            if isinstance(val, str) and val.startswith("http"):
                dest = os.path.join(DOWNLOAD_DIR, f"yt_{video_id}_api2.mp4")
                return download_file(val, dest)
        if links:
            for item in links:
                url = item.get("url") or item.get("link") or ""
                if url.startswith("http"):
                    dest = os.path.join(DOWNLOAD_DIR, f"yt_{video_id}_api2.mp4")
                    return download_file(url, dest)
    raise ValueError(f"API2 no usable URL: {data}")

def yt_api3_download(video_id: str) -> str:
    """youtube-video-fast-downloader — API 3 last resort."""
    # Get available quality first
    info = rapidapi_get(
        "youtube-video-fast-downloader-24-7.p.rapidapi.com",
        f"https://youtube-video-fast-downloader-24-7.p.rapidapi.com/get_available_quality/{video_id}",
        params={"response_mode": "default"}
    )
    # Pick best quality itag
    qualities = info.get("qualities", []) or info.get("available_qualities", [])
    if not qualities:
        raise ValueError("API3 no qualities found")
    best_itag = qualities[0].get("itag") or qualities[0].get("quality")

    # Download
    dl_info = rapidapi_get(
        "youtube-video-fast-downloader-24-7.p.rapidapi.com",
        f"https://youtube-video-fast-downloader-24-7.p.rapidapi.com/download_video/{video_id}",
        params={"quality": str(best_itag)}
    )
    url = dl_info.get("url") or dl_info.get("download_url")
    if not url:
        raise ValueError(f"API3 no URL: {dl_info}")
    dest = os.path.join(DOWNLOAD_DIR, f"yt_{video_id}_api3.mp4")
    return download_file(url, dest)

def yt_ytdlp_download(url: str, quality_label: str = "best") -> list[str]:
    """yt-dlp download with cookies fallback."""
    cookies_path = get_cookies_path("youtube")
    fmt_map = {
        "360p":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best",
        "720p":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "best":  "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    }
    opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        'format': fmt_map.get(quality_label, fmt_map["best"]),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    if cookies_path:
        opts['cookiefile'] = cookies_path

    before = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    after = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
    new_files = sorted(after - before)
    if not new_files:
        raise FileNotFoundError("yt-dlp no output")
    return new_files

# ═══════════════════════════════════════════════════════
# INSTAGRAM DOWNLOAD
# ═══════════════════════════════════════════════════════

def ig_download(url: str) -> list[str]:
    """Try yt-dlp with cookies first, then RapidAPI."""
    # Try yt-dlp
    try:
        cookies_path = get_cookies_path("instagram")
        opts = {
            'outtmpl': f'{DOWNLOAD_DIR}/ig_%(id)s.%(ext)s',
            'format': 'best',
            'noplaylist': False,
            'quiet': True,
            'no_warnings': True,
        }
        if cookies_path:
            opts['cookiefile'] = cookies_path
        before = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        after = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
        new_files = sorted(after - before)
        if new_files:
            return new_files
    except Exception as e:
        logger.warning(f"IG yt-dlp failed: {e}")

    # RapidAPI fallback
    resp = rapidapi_post(
        "instagram120.p.rapidapi.com",
        "https://instagram120.p.rapidapi.com/api/instagram/posts",
        json_body={"url": url}
    )
    media_urls = []
    if isinstance(resp, list):
        for item in resp:
            u = item.get("url") or item.get("video_url") or item.get("image_url")
            if u:
                media_urls.append(u)
    elif isinstance(resp, dict):
        for key in ["url", "video_url", "image_url"]:
            v = resp.get(key)
            if isinstance(v, str) and v:
                media_urls.append(v)
            elif isinstance(v, list):
                media_urls.extend([x for x in v if isinstance(x, str)])

    if not media_urls:
        raise ValueError("Instagram: no media found")

    files = []
    for i, mu in enumerate(media_urls[:10]):
        ext = "mp4" if ".mp4" in mu or "video" in mu.lower() else "jpg"
        dest = os.path.join(DOWNLOAD_DIR, f"ig_{i}.{ext}")
        files.append(download_file(mu, dest))
    return files

# ═══════════════════════════════════════════════════════
# TIKTOK DOWNLOAD
# ═══════════════════════════════════════════════════════

def tiktok_download(url: str) -> list[str]:
    """RapidAPI TikTok (no watermark) first, then yt-dlp."""
    try:
        data = rapidapi_get(
            "tiktok-video-no-watermark2.p.rapidapi.com",
            "https://tiktok-video-no-watermark2.p.rapidapi.com/",
            params={"url": url, "hd": "1"}
        )
        d = data.get("data", {})
        video_url = d.get("hdplay") or d.get("play")
        if video_url:
            dest = os.path.join(DOWNLOAD_DIR, "tiktok.mp4")
            return [download_file(video_url, dest)]
    except Exception as e:
        logger.warning(f"TikTok API failed: {e}")

    # yt-dlp fallback
    before = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
    with yt_dlp.YoutubeDL({'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s', 'quiet': True}) as ydl:
        ydl.download([url])
    after = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
    return sorted(after - before)

# ═══════════════════════════════════════════════════════
# PINTEREST DOWNLOAD
# ═══════════════════════════════════════════════════════

def pinterest_download(url: str) -> list[str]:
    """yt-dlp first, then RapidAPI."""
    try:
        before = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
        with yt_dlp.YoutubeDL({
            'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
            'format': 'best',
            'quiet': True,
            'no_warnings': True,
        }) as ydl:
            ydl.download([url])
        after = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
        new_files = sorted(after - before)
        if new_files:
            return new_files
    except Exception as e:
        logger.warning(f"Pinterest yt-dlp failed: {e}")

    # RapidAPI fallback
    data = rapidapi_get(
        "pinterest-video-and-image-downloader.p.rapidapi.com",
        "https://pinterest-video-and-image-downloader.p.rapidapi.com/pinterest",
        params={"url": url}
    )
    media_url = None
    for key in ["url", "video", "image", "media_url", "download_url"]:
        if data.get(key):
            media_url = data[key]
            break
    if not media_url and isinstance(data.get("data"), dict):
        for key in ["url", "video", "image"]:
            if data["data"].get(key):
                media_url = data["data"][key]
                break
    if not media_url:
        raise ValueError(f"Pinterest: no media in response")
    ext = "mp4" if ".mp4" in media_url or "video" in media_url.lower() else "jpg"
    dest = os.path.join(DOWNLOAD_DIR, f"pin.{ext}")
    return [download_file(media_url, dest)]

# ═══════════════════════════════════════════════════════
# GENERIC DOWNLOAD (Twitter, Facebook, Reddit)
# ═══════════════════════════════════════════════════════

def generic_download(url: str, platform: str) -> list[str]:
    opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        'format': 'best[ext=mp4]/best',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }
    before = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    after = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
    new_files = sorted(after - before)
    if not new_files:
        raise FileNotFoundError(f"{platform}: no output file")
    return new_files

# ═══════════════════════════════════════════════════════
# SEND HELPERS
# ═══════════════════════════════════════════════════════

INLINE_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("📢 Updates", url="https://t.me/alcyonebots"),
    InlineKeyboardButton("🆘 Support", url="https://t.me/alcyone_support"),
]])

def is_image(path: str) -> bool:
    return path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))

def send_files(update: Update, files: list[str]) -> None:
    if len(files) == 1:
        path = files[0]
        with open(path, 'rb') as f:
            if is_image(path):
                update.message.reply_photo(f, reply_markup=INLINE_KB)
            else:
                update.message.reply_video(
                    f, reply_markup=INLINE_KB, supports_streaming=True
                )
        return
    handles, media_group = [], []
    for path in files[:10]:
        fh = open(path, 'rb')
        handles.append(fh)
        media_group.append(
            InputMediaPhoto(fh) if is_image(path) else InputMediaVideo(fh)
        )
    try:
        update.message.reply_media_group(media_group)
        update.message.reply_text("⬆️ Your files!", reply_markup=INLINE_KB)
    finally:
        for fh in handles:
            fh.close()

def cleanup(files: list[str]) -> None:
    for f in files:
        try:
            os.remove(f)
        except Exception:
            pass

# ═══════════════════════════════════════════════════════
# DB HELPERS
# ═══════════════════════════════════════════════════════

def add_user(user_id: int) -> None:
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id})

def add_chat(chat_id: int) -> None:
    if not chats_col.find_one({"chat_id": chat_id}):
        chats_col.insert_one({"chat_id": chat_id})

def get_users_count() -> int: return users_col.count_documents({})
def get_chats_count() -> int: return chats_col.count_documents({})

# ═══════════════════════════════════════════════════════
# STATE: waiting for cookies input
# ═══════════════════════════════════════════════════════

# { user_id: "youtube" | "instagram" }
WAITING_COOKIES: dict[int, str] = {}

# ═══════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════

def start(update: Update, context: CallbackContext) -> None:
    user = update.message.from_user
    chat = update.message.chat
    add_user(user.id)
    add_chat(chat.id)

    log = (f"<b>New User</b>\n"
           f"User: {mention_html(user.id, user.first_name)}\n"
           f"ID: <code>{user.id}</code>")
    if chat.type != 'private':
        log += f"\nGroup: {chat.title} (<code>{chat.id}</code>)"
    try:
        context.bot.send_message(LOGS_GROUP_ID, log, parse_mode='HTML')
    except Exception:
        pass

    bot_username = context.bot.get_me().username
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Updates", url="https://t.me/alcyonebots"),
            InlineKeyboardButton("🆘 Support", url="https://t.me/alcyone_support"),
        ],
        [InlineKeyboardButton(
            "➕ Add me to your group",
            url=f"https://t.me/{bot_username}?startgroup=true"
        )],
    ])
    update.message.reply_photo(
        photo="https://i.ibb.co/9sH98zC/file-248.jpg",
        caption=(
            "👋 <b>Welcome to SaveNode!</b>\n\n"
            "Send me any link and I'll download it for you.\n\n"
            "<b>Supported Platforms:</b>\n"
            "▸ YouTube — Videos & Shorts\n"
            "▸ Instagram — Reels, Posts, Carousels\n"
            "▸ TikTok — Videos (No Watermark)\n"
            "▸ Pinterest — Images & Videos\n"
            "▸ Twitter / X — Videos\n"
            "▸ Facebook — Videos\n"
            "▸ Reddit — Videos\n\n"
            "<b>Profile Lookup:</b>\n"
            "▸ Send an Instagram profile URL\n"
            "▸ Send a Pinterest profile URL\n\n"
            "Just send a link — that's it! 🎬"
        ),
        reply_markup=kb,
        parse_mode='HTML'
    )


def stats(update: Update, context: CallbackContext) -> None:
    yt_cookies   = config_col.find_one({"key": "cookies_youtube"})
    ig_cookies   = config_col.find_one({"key": "cookies_instagram"})
    yt_status    = f"✅ Set ({yt_cookies['updated_at'].strftime('%d %b %Y')})" if yt_cookies else "❌ Not set"
    ig_status    = f"✅ Set ({ig_cookies['updated_at'].strftime('%d %b %Y')})" if ig_cookies else "❌ Not set"

    update.message.reply_text(
        f"📊 <b>Bot Stats</b>\n\n"
        f"👤 Users: <code>{get_users_count()}</code>\n"
        f"💬 Chats: <code>{get_chats_count()}</code>\n\n"
        f"<b>Cookies Status:</b>\n"
        f"YouTube: {yt_status}\n"
        f"Instagram: {ig_status}",
        parse_mode='HTML'
    )


def setcookies_cmd(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != OWNER_ID:
        return
    args = context.args
    if not args or args[0].lower() not in ["youtube", "instagram"]:
        update.message.reply_text(
            "Usage:\n/setcookies youtube\n/setcookies instagram"
        )
        return
    platform = args[0].lower()
    WAITING_COOKIES[OWNER_ID] = platform
    update.message.reply_text(
        f"📋 Send me the <b>cookies.txt</b> content for <b>{platform}</b> now.\n\n"
        f"You can get it using the <i>Get cookies.txt LOCALLY</i> browser extension.",
        parse_mode='HTML'
    )


def cookies_status(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != OWNER_ID:
        return
    for platform in ["youtube", "instagram"]:
        doc = config_col.find_one({"key": f"cookies_{platform}"})
        status = f"✅ Set on {doc['updated_at'].strftime('%d %b %Y %H:%M')} UTC" if doc else "❌ Not set"
        update.message.reply_text(f"<b>{platform.capitalize()} cookies:</b> {status}", parse_mode='HTML')


def broadcast(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != OWNER_ID:
        return
    message = " ".join(context.args)
    if not message:
        update.message.reply_text("Usage: /broadcast <message>")
        return
    sent = failed = 0
    for chat in chats_col.find():
        try:
            context.bot.send_message(chat['chat_id'], message)
            sent += 1
        except Exception as e:
            logger.warning(f"Broadcast failed {chat['chat_id']}: {e}")
            failed += 1
    update.message.reply_text(f"✅ Sent: {sent} | ❌ Failed: {failed}")


def handle_message(update: Update, context: CallbackContext) -> None:
    user = update.message.from_user
    text = update.message.text.strip()

    # ── Owner pasting cookies ──────────────────────────
    if user.id == OWNER_ID and user.id in WAITING_COOKIES:
        platform = WAITING_COOKIES.pop(user.id)
        if "Netscape" in text or "#" in text[:50] or "\t" in text:
            save_cookies(platform, text)
            update.message.reply_text(
                f"✅ <b>{platform.capitalize()} cookies saved!</b>\n"
                f"yt-dlp will use them automatically.",
                parse_mode='HTML'
            )
        else:
            update.message.reply_text(
                "❌ That doesn't look like a valid cookies.txt file.\n"
                "Make sure it starts with '# Netscape HTTP Cookie File'"
            )
        return

    # ── URL handling ───────────────────────────────────
    platform = detect_platform(text)
    if not platform:
        if update.message.chat.type != 'private':
            return
        update.message.reply_text(
            "❌ Unsupported link.\n\n"
            "Supported: YouTube, Instagram, TikTok, Pinterest, Twitter/X, Facebook, Reddit"
        )
        return

    # Profile lookups
    if platform == "instagram_profile":
        _handle_ig_profile(update, text)
        return

    if platform == "pinterest_profile":
        _handle_pinterest_profile(update, text)
        return

    if platform == "youtube" or platform == "youtube_profile":
        _handle_youtube(update, context, text)
        return

    # Direct downloads
    status = update.message.reply_text(f"⏳ Downloading from {platform.split('_')[0].capitalize()}...")
    files = []
    try:
        if platform == "instagram":
            files = ig_download(text)
        elif platform == "tiktok":
            files = tiktok_download(text)
        elif platform == "pinterest":
            files = pinterest_download(text)
        else:
            files = generic_download(text, platform)
        send_files(update, files)
    except Exception as e:
        err = str(e)
        logger.error(f"Download error [{platform}]: {err}")
        if any(k in err for k in ["Private", "login", "Sign in", "private"]):
            update.message.reply_text("🔒 This content is private.")
        else:
            update.message.reply_text(f"❌ Failed: {err[:200]}")
    finally:
        cleanup(files)
        try: status.delete()
        except: pass


def _handle_ig_profile(update: Update, url: str) -> None:
    status = update.message.reply_text("🔍 Looking up Instagram profile...")
    try:
        username = extract_username(url, "instagram_profile")
        p = instagram_profile_lookup(username)

        verified = "✅ Verified" if p["is_verified"] else "Not verified"
        private  = "🔒 Private" if p["is_private"] else "🌐 Public"
        business = " • 💼 Business" if p["is_business"] else ""

        caption = (
            f"<b>{p['full_name']}</b> (@{p['username']})\n"
            f"{verified} • {private}{business}\n\n"
            f"📝 {p['biography'] or 'No bio'}\n\n"
            f"👥 <b>{format_number(p['followers'])}</b> Followers  "
            f"• <b>{format_number(p['following'])}</b> Following\n"
            f"📸 <b>{format_number(p['posts'])}</b> Posts\n"
        )
        if p["external_url"]:
            caption += f"\n🔗 {p['external_url']}"

        update.message.reply_photo(
            photo=p["profile_pic"],
            caption=caption,
            parse_mode='HTML',
            reply_markup=INLINE_KB
        )
    except Exception as e:
        logger.error(f"IG profile lookup error: {e}")
        update.message.reply_text("❌ Could not fetch profile. It may be private or not exist.")
    finally:
        try: status.delete()
        except: pass


def _handle_pinterest_profile(update: Update, url: str) -> None:
    status = update.message.reply_text("🔍 Looking up Pinterest profile...")
    try:
        username = extract_username(url, "pinterest_profile")
        data = pinterest_profile_lookup(username)
        # Basic display — Pinterest API response varies
        update.message.reply_text(
            f"📌 <b>Pinterest: @{username}</b>\n\n"
            f"Profile found! Send a pin URL to download content.",
            parse_mode='HTML',
            reply_markup=INLINE_KB
        )
    except Exception as e:
        logger.error(f"Pinterest profile error: {e}")
        update.message.reply_text("❌ Could not fetch Pinterest profile.")
    finally:
        try: status.delete()
        except: pass


def _handle_youtube(update: Update, context: CallbackContext, url: str) -> None:
    """Fetch YT info and show quality buttons."""
    video_id = extract_yt_id(url)
    if not video_id:
        update.message.reply_text("❌ Could not extract YouTube video ID.")
        return

    status = update.message.reply_text("🔍 Fetching video info...")
    try:
        info      = yt_get_info(video_id)
        title     = info.get("title", "YouTube Video")
        duration  = info.get("lengthSeconds", 0)
        views     = info.get("viewCount", 0)
        minutes   = int(duration) // 60
        seconds   = int(duration) % 60
        qualities = yt_parse_qualities(info)

        # Store info in context for callback
        context.user_data[f"yt_{video_id}"] = {
            "url": url,
            "video_id": video_id,
            "qualities": qualities,
            "title": title,
        }

        # Build quality buttons
        buttons = []
        row = []
        for q in qualities[:6]:  # max 6 quality options
            row.append(InlineKeyboardButton(
                q["label"] + (" 🎵" if not q["has_audio"] else ""),
                callback_data=f"yt|{video_id}|{q['label']}"
            ))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        # Audio only button
        buttons.append([InlineKeyboardButton(
            "🎵 Audio Only", callback_data=f"yt|{video_id}|audio"
        )])

        kb = InlineKeyboardMarkup(buttons)
        update.message.reply_text(
            f"🎬 <b>{title}</b>\n"
            f"⏱ {minutes}:{seconds:02d}  •  👁 {format_number(int(views or 0))} views\n\n"
            f"Select quality:",
            reply_markup=kb,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"YT info error: {e}")
        # Fallback: just download best quality directly
        status2 = update.message.reply_text("⏳ Downloading best quality...")
        files = []
        try:
            files = yt_ytdlp_download(url)
            send_files(update, files)
        except Exception as e2:
            update.message.reply_text(f"❌ Download failed: {str(e2)[:200]}")
        finally:
            cleanup(files)
            try: status2.delete()
            except: pass
    finally:
        try: status.delete()
        except: pass


def handle_yt_quality_callback(update: Update, context: CallbackContext) -> None:
    """Handle quality button press for YouTube."""
    query = update.callback_query
    query.answer()

    _, video_id, label = query.data.split("|", 2)
    stored = context.user_data.get(f"yt_{video_id}", {})
    url       = stored.get("url", f"https://youtu.be/{video_id}")
    qualities = stored.get("qualities", [])

    # Edit message to show downloading
    query.edit_message_text(f"⏳ Downloading {label}...")

    files = []
    try:
        if label == "audio":
            # Use yt-dlp for audio
            cookies_path = get_cookies_path("youtube")
            opts = {
                'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
                'format': 'bestaudio[ext=m4a]/bestaudio',
                'noplaylist': True,
                'quiet': True,
            }
            if cookies_path:
                opts['cookiefile'] = cookies_path
            before = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            after = set(glob.glob(f"{DOWNLOAD_DIR}/*"))
            files = sorted(after - before)
            if files:
                with open(files[0], 'rb') as f:
                    query.message.reply_audio(f, reply_markup=INLINE_KB)
                return
        else:
            # Find direct URL for this quality from stored info
            quality_url = None
            for q in qualities:
                if q["label"] == label:
                    quality_url = q["url"]
                    break

            if quality_url:
                try:
                    dest = yt_download_by_url(quality_url, video_id, label)
                    files = [dest]
                except Exception as e:
                    logger.warning(f"Direct URL download failed: {e}, trying yt-dlp")
                    files = yt_ytdlp_download(url, label)
            else:
                # Try yt-dlp
                try:
                    files = yt_ytdlp_download(url, label)
                except Exception as e1:
                    logger.warning(f"yt-dlp failed: {e1}, trying API2")
                    try:
                        dest = yt_api2_download(video_id)
                        files = [dest]
                    except Exception as e2:
                        logger.warning(f"API2 failed: {e2}, trying API3")
                        dest = yt_api3_download(video_id)
                        files = [dest]

            if files:
                with open(files[0], 'rb') as f:
                    query.message.reply_video(
                        f, reply_markup=INLINE_KB, supports_streaming=True
                    )

    except Exception as e:
        logger.error(f"YT quality download error: {e}")
        query.message.reply_text(f"❌ Download failed: {str(e)[:200]}")
    finally:
        cleanup(files)
        try: query.message.delete()
        except: pass


def error_handler(update: Update, context: CallbackContext) -> None:
    logger.warning(f'Error: {context.error}')

# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

def main() -> None:
    load_all_cookies()

    updater = Updater(
        BOT_TOKEN,
        use_context=True,
        base_url=LOCAL_API_URL,
        base_file_url=LOCAL_API_URL.replace("/bot", "/file/bot"),
    )
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start",       start))
    dp.add_handler(CommandHandler("help",        start))
    dp.add_handler(CommandHandler("stats",       stats))
    dp.add_handler(CommandHandler("broadcast",   broadcast))
    dp.add_handler(CommandHandler("setcookies",  setcookies_cmd))
    dp.add_handler(CommandHandler("cookies",     cookies_status))
    dp.add_handler(CallbackQueryHandler(handle_yt_quality_callback, pattern=r'^yt\|'))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_error_handler(error_handler)

    updater.start_polling()
    logger.info("✅ SaveNode bot running")
    updater.idle()


if __name__ == '__main__':
    main()
