#!/usr/bin/env python3
"""Instagram DM bot powered by Groq (Llama 3.3 70B)."""

import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from dotenv import load_dotenv
from groq import Groq
from gtts import gTTS
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ClientError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
)

from config import (
    CONTEXT_MESSAGES,
    POLL_MAX,
    POLL_MIN,
    REACT_TO_REELS as _REACT_TO_REELS_DEFAULT,
    REPLY_COOLDOWN,
    REPLY_DELAY_MAX,
    REPLY_DELAY_MIN,
    REPLY_ONLY_WHEN_MENTIONED,
    TTS_LANGUAGE,
    VOICE_CHANCE as _VOICE_CHANCE_DEFAULT,
)

# --- Constants ---
SESSION_FILE = Path(__file__).parent / "session.json"
SYSTEM_PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"
RATE_LIMIT_BACKOFF = 300
MAX_LOGIN_RETRIES = 3
VOICE_CHANCE = _VOICE_CHANCE_DEFAULT

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Suppress noisy HTTP-level logs from Gemini SDK
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --- Global state ---
running = True
_web_tokens = {}
_web_session = None


def handle_signal(signum, frame):
    global running
    running = False
    log.info("Shutdown signal received, stopping after current cycle...")


def load_system_prompt(bot_display_name):
    if SYSTEM_PROMPT_FILE.exists():
        prompt = SYSTEM_PROMPT_FILE.read_text().strip()
        return prompt.replace("{BOT_DISPLAY_NAME}", bot_display_name)
    log.warning("system_prompt.txt not found, using default prompt")
    return f"You are {bot_display_name}, a casual participant in an Instagram group chat. Keep replies short and funny."


def login_instagram(username, password):
    cl = Client()
    cl.delay_range = [2, 5]

    if SESSION_FILE.exists():
        log.info("Loading existing session...")
        cl.load_settings(SESSION_FILE)

    try:
        cl.login(username, password)
        log.info("Login successful")
    except ChallengeRequired:
        log.critical(
            "Instagram challenge required! Open the Instagram app on your phone, "
            "approve the login attempt, then restart the bot."
        )
        sys.exit(1)
    except Exception as e:
        log.warning(f"Login with session failed: {e}")
        if SESSION_FILE.exists():
            log.info("Deleting old session and retrying fresh login...")
            SESSION_FILE.unlink()
            cl = Client()
            cl.delay_range = [2, 5]
            try:
                cl.login(username, password)
                log.info("Fresh login successful")
            except ChallengeRequired:
                log.critical(
                    "Instagram challenge required! Open the Instagram app on your phone, "
                    "approve the login attempt, then restart the bot."
                )
                sys.exit(1)

    # Verify session is alive
    try:
        cl.user_info(cl.user_id)
    except LoginRequired:
        log.warning("Session verification failed, doing fresh login...")
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        cl = Client()
        cl.delay_range = [2, 5]
        cl.login(username, password)

    cl.dump_settings(SESSION_FILE)
    return cl


def get_username(ig_client, user_id, cache):
    user_id_str = str(user_id)
    if user_id_str in cache:
        return cache[user_id_str]
    try:
        info = ig_client.user_info(user_id)
        cache[user_id_str] = info.username
        return info.username
    except Exception:
        fallback = f"user_{user_id}"
        cache[user_id_str] = fallback
        return fallback


def extract_reel_media(msg):
    """Extract reel media info from a DM message. Returns (Media_or_None, media_pk) or (None, None)."""
    if msg.clip is not None:
        return msg.clip, int(msg.clip.pk)
    if msg.media_share is not None and getattr(msg.media_share, 'product_type', '') == 'clips':
        return msg.media_share, int(msg.media_share.pk)
    if msg.felix_share and isinstance(msg.felix_share, dict):
        video = msg.felix_share.get("video", {})
        pk = video.get("pk")
        if pk:
            return None, int(pk)
    if msg.item_type == "xma_clip" and msg.xma_share:
        from urllib.parse import urlparse, parse_qs
        video_url = getattr(msg.xma_share, 'video_url', None)
        if video_url:
            id_param = parse_qs(urlparse(str(video_url)).query).get('id', [None])[0]
            if id_param:
                media_pk = int(id_param.split('_')[0])
                return None, media_pk
        return None, None
    return None, None


def fetch_messages(ig_client, thread_id):
    messages = ig_client.direct_messages(thread_id, amount=CONTEXT_MESSAGES)
    # direct_messages returns newest first, reverse for chronological order
    messages.reverse()
    return messages


def find_new_messages(messages, last_timestamp, replied_timestamps, bot_user_id, bot_display_name=None):
    """Find new text messages, filtering out non-text, already-replied, and own messages."""
    new_msgs = []
    for msg in messages:
        if int(msg.user_id) == int(bot_user_id):
            log.debug(f"Skipping self-message (user_id={msg.user_id}): {msg.text[:60] if msg.text else '<no text>'}")
            continue
        if last_timestamp is not None and msg.timestamp <= last_timestamp:
            continue
        if msg.timestamp in replied_timestamps:
            continue
        if not msg.text:
            continue
        if REPLY_ONLY_WHEN_MENTIONED and bot_display_name:
            if f"@{bot_display_name}".lower() not in msg.text.lower():
                continue
        new_msgs.append(msg)
    return new_msgs


def find_new_reels(messages, last_timestamp, replied_timestamps, bot_user_id):
    """Find new reel messages that haven't been reacted to yet."""
    new_reels = []
    for msg in messages:
        if int(msg.user_id) == int(bot_user_id):
            continue
        if last_timestamp is not None and msg.timestamp <= last_timestamp:
            continue
        if msg.timestamp in replied_timestamps:
            continue
        _, media_pk = extract_reel_media(msg)
        if media_pk is not None:
            log.debug(f"Found reel message: item_type={msg.item_type}, media_pk={media_pk}")
            new_reels.append(msg)
    return new_reels


def get_latest_timestamp(messages):
    if not messages:
        return None
    return max(msg.timestamp for msg in messages)


def format_context(messages, ig_client, username_cache):
    lines = []
    for msg in messages:
        username = get_username(ig_client, msg.user_id, username_cache)
        if msg.text:
            lines.append(f"[{username}]: {msg.text}")
        else:
            media, media_pk = extract_reel_media(msg)
            if media_pk is not None:
                caption = getattr(media, 'caption_text', '') if media else ''
                if caption:
                    lines.append(f"[{username}] sent a reel: \"{caption[:200]}\"")
                else:
                    lines.append(f"[{username}] sent a reel")
    return "\n".join(lines)


def generate_response(groq_client, context, system_prompt):
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context},
            ],
            temperature=0.9,
            max_tokens=256,
        )
        reply = response.choices[0].message.content
        if reply:
            reply = reply.strip()
            reply = re.sub(r'^(\[.*?\]|[\w]+):\s*', '', reply)
        return reply if reply else None
    except Exception as e:
        log.error(f"Groq API error: {e}")
        return None


def send_reply(ig_client, thread_id, text):
    delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
    log.info(f"Waiting {delay:.1f}s before replying...")
    time.sleep(delay)
    ig_client.direct_send(text, thread_ids=[thread_id])
    log.info(f"Sent text reply: {text[:80]}{'...' if len(text) > 80 else ''}")




def _get_web_session():
    global _web_session
    if _web_session is not None:
        return _web_session

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    })

    cookies = {
        "csrftoken": os.getenv("WEB_CSRFTOKEN"),
        "datr": os.getenv("WEB_DATR"),
        "ds_user_id": os.getenv("WEB_DS_USER_ID"),
        "ig_did": os.getenv("WEB_IG_DID"),
        "mid": os.getenv("WEB_MID"),
        "rur": os.getenv("WEB_RUR"),
        "sessionid": os.getenv("WEB_SESSIONID"),
    }

    missing = [k for k, v in cookies.items() if not v]
    if missing:
        raise RuntimeError(f"Missing web cookies in .env: {', '.join(missing)}")

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    session.headers["Cookie"] = cookie_str

    log.info("Web session created from browser cookies")
    _web_session = session
    return _web_session


def fetch_web_tokens():
    global _web_tokens
    if _web_tokens:
        return _web_tokens

    session = _get_web_session()
    resp = session.get("https://www.instagram.com/direct/", headers={
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    resp.raise_for_status()
    html = resp.text

    fb_dtsg_match = re.search(r'"DTSGInitData".*?"token"\s*:\s*"([^"]+)"', html, re.DOTALL)
    lsd_match = re.search(r'"LSD".*?"token"\s*:\s*"([^"]+)"', html, re.DOTALL)
    jazoest_match = re.search(r'jazoest=(\d+)', html)

    missing = []
    if not fb_dtsg_match:
        missing.append("fb_dtsg")
    if not lsd_match:
        missing.append("lsd")
    if not jazoest_match:
        missing.append("jazoest")
    if missing:
        # Dump snippets around known markers for debugging
        for keyword in ("DTSGInitData", "dtsg", "LSD", "jazoest"):
            idx = html.find(keyword)
            if idx != -1:
                snippet = html[max(0, idx - 20):idx + 120]
                log.debug(f"HTML near '{keyword}': ...{snippet}...")
            else:
                log.debug(f"'{keyword}' not found in HTML ({len(html)} chars)")
        raise RuntimeError(f"Failed to extract web tokens: {', '.join(missing)}")

    _web_tokens = {
        "fb_dtsg": fb_dtsg_match.group(1),
        "lsd": lsd_match.group(1),
        "jazoest": jazoest_match.group(1),
    }
    log.info("Fetched web tokens (fb_dtsg, lsd, jazoest)")
    return _web_tokens


def upload_web_audio(audio_path):
    session = _get_web_session()
    tokens = fetch_web_tokens()

    with open(audio_path, "rb") as f:
        audio_data = f.read()

    params = {
        "__d": "www",
        "__user": "0",
        "__a": "1",
        "__ccg": "GOOD",
        "__comet_req": "7",
        "__crn": "comet.igweb.PolarisDirectInboxRoute",
        "dpr": "1",
        "fb_dtsg": tokens["fb_dtsg"],
        "lsd": tokens["lsd"],
        "jazoest": tokens["jazoest"],
    }

    url = "https://www.instagram.com/ajax/mercury/upload.php"
    log.info(f"Upload URL: {url}")
    log.info(f"Upload headers: {dict(session.headers)}")
    log.info(f"Upload params: {params}")

    resp = session.post(
        url,
        params=params,
        headers={
            "X-FB-LSD": tokens["lsd"],
            "X-CSRFToken": os.getenv("WEB_CSRFTOKEN", ""),
            "X-ASBD-ID": "359341",
            "X-IG-App-ID": "936619743392459",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/direct/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
        files={"farr": ("reply.m4a", audio_data, "audio/mp4")},
    )

    log.info(f"Upload response status: {resp.status_code}")
    log.info(f"Upload response body: {resp.text[:2000]}")

    # Detect expired or invalid web cookies
    body = resp.text
    if (
        resp.status_code in (401, 403)
        or "not logged in" in body.lower()
        or body.strip().startswith("<!") or body.strip().startswith("<html")
    ):
        global VOICE_CHANCE, _web_session, _web_tokens
        _web_session = None
        _web_tokens = {}
        VOICE_CHANCE = 0
        raise RuntimeError(
            "Web cookies expired — voice messages disabled. "
            "Update WEB_* cookies in .env and restart the bot."
        )

    resp.raise_for_status()

    if body.startswith("for (;;);"):
        body = body[len("for (;;);"):]

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse upload JSON: {e}\nBody: {body[:500]}")
        raise

    try:
        audio_id = data["payload"]["metadata"]["0"]["audio_id"]
    except (KeyError, TypeError) as e:
        log.error(f"Failed to extract audio_id: {e}\nParsed data: {json.dumps(data, indent=2)[:1000]}")
        raise

    log.info(f"Uploaded audio, got audio_id={audio_id}")
    return audio_id


def send_web_voice(thread_id, audio_id):
    session = _get_web_session()
    tokens = fetch_web_tokens()

    variables = json.dumps({
        "attachment_fbid": str(audio_id),
        "thread_id": str(thread_id),
        "offline_threading_id": str(random.randint(10**17, 10**18 - 1)),
        "reply_to_message_id": None,
    })

    resp = session.post(
        "https://www.instagram.com/api/graphql",
        headers={
            "X-FB-Friendly-Name": "IGDirectMediaSendMutation",
            "X-CSRFToken": os.getenv("WEB_CSRFTOKEN", ""),
            "X-IG-App-ID": "936619743392459",
            "X-FB-LSD": tokens["lsd"],
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
        data={
            "fb_api_req_friendly_name": "IGDirectMediaSendMutation",
            "doc_id": "25604816565789936",
            "variables": variables,
            "fb_dtsg": tokens["fb_dtsg"],
            "lsd": tokens["lsd"],
            "jazoest": tokens["jazoest"],
            "__a": "1",
            "__d": "www",
            "__comet_req": "7",
            "server_timestamps": "true",
        },
    )

    if resp.status_code in (401, 403) or "not logged in" in resp.text.lower():
        global VOICE_CHANCE, _web_session, _web_tokens
        _web_session = None
        _web_tokens = {}
        VOICE_CHANCE = 0
        raise RuntimeError(
            "Web cookies expired — voice messages disabled. "
            "Update WEB_* cookies in .env and restart the bot."
        )

    resp.raise_for_status()
    log.info("Voice message sent via GraphQL")


def send_voice_reply(thread_id, text):
    mp3_path = "/tmp/reply.mp3"
    m4a_path = "/tmp/reply.m4a"
    try:
        delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
        log.info(f"Waiting {delay:.1f}s before replying (voice)...")
        time.sleep(delay)

        tts = gTTS(text, lang=TTS_LANGUAGE)
        tts.save(mp3_path)

        result = subprocess.run(
            ["ffmpeg", "-i", mp3_path, "-c:a", "aac", "-b:a", "64k", m4a_path, "-y"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()}")

        audio_id = upload_web_audio(m4a_path)
        send_web_voice(thread_id, audio_id)
        log.info(f"Sent voice reply: {text[:80]}{'...' if len(text) > 80 else ''}")
    finally:
        for path in (mp3_path, m4a_path):
            try:
                os.remove(path)
            except OSError:
                pass


def process_reel(ig_client, gemini_client, reel_msg, media_pk, context, system_prompt, username_cache):
    """Download a reel, send to Gemini for vision analysis, return reaction text."""
    from google.genai import types

    video_path = None
    gemini_file = None
    try:
        # Download video
        folder = Path("/tmp")
        video_path = ig_client.clip_download(media_pk, folder=folder)
        log.info(f"Downloaded reel {media_pk} to {video_path}")

        # Upload to Gemini File API
        gemini_file = gemini_client.files.upload(file=str(video_path), config={"mime_type": "video/mp4"})
        log.info("Uploaded reel to Gemini, waiting for processing...")

        # Wait for Gemini to process the video (max 60s)
        deadline = time.time() + 60
        while gemini_file.state.name == "PROCESSING":
            if time.time() > deadline:
                log.warning(f"Gemini processing timed out for reel {media_pk}")
                return None
            time.sleep(2)
            gemini_file = gemini_client.files.get(name=gemini_file.name)

        if gemini_file.state.name == "FAILED":
            log.error(f"Gemini video processing failed for reel {media_pk}")
            return None

        sender = get_username(ig_client, reel_msg.user_id, username_cache)

        reel_prompt = (
            f"{system_prompt}\n\n"
            f"Recent chat context:\n{context}\n\n"
            f"@{sender} just sent this reel in the chat. "
            f"Watch it and react naturally like you would in a group chat. "
            f"Keep it short (1-2 sentences max). Be funny, sarcastic, or savage as appropriate. "
            f"Don't describe the video — just react to it."
        )

        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[gemini_file, reel_prompt],
            config=types.GenerateContentConfig(
                temperature=0.9,
                max_output_tokens=256,
            ),
        )
        reply = response.text.strip() if response.text else None
        if reply:
            reply = re.sub(r'^(\[.*?\]|[\w]+):\s*', '', reply)
        log.info(f"Gemini reaction: {reply[:80] if reply else '<empty>'}")
        return reply

    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
            log.warning(f"Gemini rate limited, skipping reel {media_pk}")
        else:
            log.error(f"Error processing reel {media_pk}: {e}", exc_info=True)
        return None
    finally:
        if video_path:
            try:
                os.remove(str(video_path))
            except OSError:
                pass
        if gemini_file:
            try:
                gemini_client.files.delete(name=gemini_file.name)
            except Exception:
                pass


def process_reel_lite(ig_client, gemini_client, reel_msg, media_pk, context, system_prompt, username_cache):
    """Use reel thumbnail + caption for a fast Gemini reaction (LITE mode)."""
    from google.genai import types
    import tempfile

    tmp_path = None
    gemini_file = None
    try:
        # 1. Get preview image URL and caption via media_info
        preview_url = None
        caption = None
        if reel_msg.xma_share:
            preview_url = getattr(reel_msg.xma_share, 'preview_url', None)

        # Fetch media_info for thumbnail fallback and caption
        try:
            media_info = ig_client.media_info(media_pk)
            caption = getattr(media_info, 'caption_text', None)
            if not preview_url and media_info.thumbnail_url:
                preview_url = str(media_info.thumbnail_url)
        except Exception as e:
            log.debug(f"Could not fetch media_info for reel {media_pk}: {e}")

        if not preview_url:
            log.warning(f"No preview_url available for reel {media_pk} — skipping (LITE mode)")
            return None

        # 2. Download thumbnail image
        resp = requests.get(str(preview_url), timeout=15)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(resp.content)
            tmp_path = f.name
        log.info(f"Downloaded reel thumbnail for {media_pk} ({len(resp.content)} bytes)")

        # 4. Upload image to Gemini (no processing wait needed)
        gemini_file = gemini_client.files.upload(file=tmp_path, config={"mime_type": "image/jpeg"})
        log.info("Uploaded reel thumbnail to Gemini")

        # 5. Build prompt
        sender = get_username(ig_client, reel_msg.user_id, username_cache)

        caption_line = f'\nThe reel\'s caption is: "{caption}"' if caption else ""

        reel_prompt = (
            f"{system_prompt}\n\n"
            f"Recent chat context:\n{context}\n\n"
            f"@{sender} just sent a reel in the chat. "
            f"Here is the reel's thumbnail image.{caption_line}\n"
            f"React naturally like you would in a group chat. "
            f"Keep it short (1-2 sentences max). Be funny, sarcastic, or savage as appropriate. "
            f"Don't describe the image — just react to the reel."
        )

        # 6. Generate reaction
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[gemini_file, reel_prompt],
            config=types.GenerateContentConfig(
                temperature=0.9,
                max_output_tokens=256,
            ),
        )
        reply = response.text.strip() if response.text else None
        if reply:
            reply = re.sub(r'^(\[.*?\]|[\w]+):\s*', '', reply)
        log.info(f"Gemini LITE reaction: {reply[:80] if reply else '<empty>'}")
        return reply

    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
            log.warning(f"Gemini rate limited, skipping reel {media_pk}")
        else:
            log.error(f"Error processing reel {media_pk} (LITE): {e}", exc_info=True)
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if gemini_file:
            try:
                gemini_client.files.delete(name=gemini_file.name)
            except Exception:
                pass


def main():
    load_dotenv()

    # Validate env vars
    username = os.getenv("INSTAGRAM_USERNAME")
    password = os.getenv("INSTAGRAM_PASSWORD")
    groq_api_key = os.getenv("GROQ_API_KEY")
    thread_id_str = os.getenv("CHAT_THREAD_ID")
    bot_name = os.getenv("BOT_DISPLAY_NAME")

    missing = []
    if not username:
        missing.append("INSTAGRAM_USERNAME")
    if not password:
        missing.append("INSTAGRAM_PASSWORD")
    if not groq_api_key:
        missing.append("GROQ_API_KEY")
    if not thread_id_str:
        missing.append("CHAT_THREAD_ID")
    if not bot_name:
        missing.append("BOT_DISPLAY_NAME")

    if missing:
        log.critical(f"Missing required env vars: {', '.join(missing)}")
        log.critical("Copy .env.example to .env and fill in your values")
        sys.exit(1)

    thread_id = int(thread_id_str)

    # Setup
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    groq_client = Groq(api_key=groq_api_key)
    system_prompt = load_system_prompt(bot_name)

    log.info("Logging into Instagram...")
    ig_client = login_instagram(username, password)

    username_cache = {}
    replied_timestamps = set()
    last_timestamp = None
    last_reply_time = 0.0
    login_retries = 0
    first_run = True

    # Pre-cache all participant usernames from thread info
    try:
        thread = ig_client.direct_thread(thread_id)
        for user in thread.users:
            username_cache[str(user.pk)] = user.username
        username_cache[str(ig_client.user_id)] = username
        log.info(f"Cached {len(username_cache)} participant username(s) from thread info")
    except Exception as e:
        log.warning(f"Failed to pre-cache usernames from thread info: {e}")

    # Pre-cache web session & tokens for voice messages
    global VOICE_CHANCE
    try:
        _get_web_session()
        fetch_web_tokens()
        log.info("Web session and tokens cached — voice messages enabled.")
    except Exception as e:
        log.warning(f"Web session setup failed: {e} — voice messages disabled.")
        VOICE_CHANCE = 0

    # Reel reaction setup
    REACT_TO_REELS = str(_REACT_TO_REELS_DEFAULT).upper()
    # Backward compat: treat old boolean values
    if REACT_TO_REELS == "TRUE":
        REACT_TO_REELS = "FULL"
    elif REACT_TO_REELS == "FALSE":
        REACT_TO_REELS = "NONE"
    if REACT_TO_REELS not in ("FULL", "LITE", "NONE"):
        log.warning(f"Invalid REACT_TO_REELS value '{_REACT_TO_REELS_DEFAULT}' — defaulting to NONE")
        REACT_TO_REELS = "NONE"
    gemini_client = None
    if REACT_TO_REELS != "NONE":
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            log.warning("REACT_TO_REELS enabled but GEMINI_API_KEY not set — reel reactions disabled.")
            REACT_TO_REELS = "NONE"
        else:
            try:
                from google import genai
                gemini_client = genai.Client(api_key=gemini_api_key)
                log.info(f"Gemini configured — reel reactions enabled ({REACT_TO_REELS} mode).")
            except Exception as e:
                log.warning(f"Gemini setup failed: {e} — reel reactions disabled.")
                REACT_TO_REELS = "NONE"

    bot_user_id = int(ig_client.user_id)
    log.info(f"Bot user_id: {bot_user_id} (type: {type(bot_user_id).__name__})")

    print()
    print("=" * 50)
    print(f"  Instagram Bot Active")
    print(f"  Account:  @{username}")
    print(f"  User ID:  {bot_user_id}")
    print(f"  Thread:   {thread_id}")
    print(f"  Bot name: @{bot_name}")
    print(f"  Model:    llama-3.3-70b-versatile (Groq)")
    reels_status = f"{REACT_TO_REELS} (Gemini 2.5 Flash)" if REACT_TO_REELS != "NONE" else "disabled"
    print(f"  Reels:    {reels_status}")
    print("=" * 50)
    print()

    while running:
        try:
            messages = fetch_messages(ig_client, thread_id)
            login_retries = 0  # reset on success


            if first_run:
                last_timestamp = get_latest_timestamp(messages)
                if last_timestamp:
                    log.info(f"First run — skipping existing messages (latest: {last_timestamp})")
                else:
                    log.info("First run — no messages found in thread")
                log.debug(f"Initial last_timestamp set to {last_timestamp}")
                first_run = False
            else:
                mentions = find_new_messages(messages, last_timestamp, replied_timestamps, bot_user_id, bot_name)
                reel_triggers = find_new_reels(messages, last_timestamp, replied_timestamps, bot_user_id) if REACT_TO_REELS != "NONE" else []

                new_latest = get_latest_timestamp(messages)
                if new_latest and (last_timestamp is None or new_latest > last_timestamp):
                    last_timestamp = new_latest
                log.debug(f"Poll state: last_timestamp={last_timestamp}, replied_timestamps={replied_timestamps}")
                log.debug(f"Found {len(mentions)} new message(s), {len(reel_triggers)} new reel(s)")

                if mentions:
                    # Cooldown check — reply to most recent mention only
                    now = time.time()
                    if now - last_reply_time < REPLY_COOLDOWN:
                        log.info(f"Cooldown active, skipping {len(mentions)} message(s)")
                    else:
                        trigger = mentions[-1]  # most recent mention
                        trigger_user = get_username(ig_client, trigger.user_id, username_cache)
                        log.info(f"New message from @{trigger_user}: {trigger.text[:80]}")

                        context = format_context(messages, ig_client, username_cache)
                        reply = generate_response(groq_client, context, system_prompt)

                        if reply:
                            if random.random() < VOICE_CHANCE:
                                try:
                                    send_voice_reply(thread_id, reply)
                                except Exception as e:
                                    log.warning(f"Voice reply failed, falling back to text: {e}")
                                    send_reply(ig_client, thread_id, reply)
                            else:
                                send_reply(ig_client, thread_id, reply)
                            last_reply_time = time.time()
                            for m in mentions:
                                replied_timestamps.add(m.timestamp)
                        else:
                            log.warning("LLM returned empty response, skipping reply")
                            for m in mentions:
                                replied_timestamps.add(m.timestamp)

                # Reel reactions (independent of text replies)
                if REACT_TO_REELS != "NONE" and gemini_client and reel_triggers:
                    reel_msg = reel_triggers[-1]  # most recent reel
                    reel_user = get_username(ig_client, reel_msg.user_id, username_cache)
                    log.info(f"New reel from @{reel_user} (mode: {REACT_TO_REELS})")

                    _, media_pk = extract_reel_media(reel_msg)
                    if media_pk is None:
                        log.warning(f"Could not extract media_pk from reel — skipping")
                        for m in reel_triggers:
                            replied_timestamps.add(m.timestamp)
                    else:
                        context = format_context(messages, ig_client, username_cache)
                        if REACT_TO_REELS == "LITE":
                            reel_reply = process_reel_lite(
                                ig_client, gemini_client, reel_msg, media_pk,
                                context, system_prompt, username_cache
                            )
                        else:
                            reel_reply = process_reel(
                                ig_client, gemini_client, reel_msg, media_pk,
                                context, system_prompt, username_cache
                            )

                        if reel_reply:
                            delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
                            log.info(f"Waiting {delay:.1f}s before reel reply...")
                            time.sleep(delay)
                            ig_client.direct_send(
                                reel_reply,
                                thread_ids=[thread_id],
                                reply_to_message=reel_msg,
                            )
                            log.info(f"Replied to reel: {reel_reply[:80]}")
                        else:
                            log.warning("No reaction generated for reel, skipping")

                        # Always mark as processed to avoid re-processing
                        for m in reel_triggers:
                            replied_timestamps.add(m.timestamp)

            # Wait before next poll
            poll_interval = random.uniform(POLL_MIN, POLL_MAX)
            log.info(f"Next poll in {poll_interval:.0f}s")

            # Sleep in small increments so we can catch shutdown signals
            end_time = time.time() + poll_interval
            while running and time.time() < end_time:
                time.sleep(1)

        except LoginRequired:
            login_retries += 1
            if login_retries > MAX_LOGIN_RETRIES:
                log.critical("Max login retries exceeded, exiting")
                break
            log.warning(f"Session expired, re-logging in (attempt {login_retries}/{MAX_LOGIN_RETRIES})...")
            if SESSION_FILE.exists():
                SESSION_FILE.unlink()
            try:
                ig_client = login_instagram(username, password)
            except Exception as e:
                log.error(f"Re-login failed: {e}")
                time.sleep(60)

        except PleaseWaitFewMinutes:
            log.warning(f"Rate limited by Instagram, backing off for {RATE_LIMIT_BACKOFF}s...")
            time.sleep(RATE_LIMIT_BACKOFF)

        except (ChallengeRequired, FeedbackRequired) as e:
            log.critical(f"Instagram blocked the bot: {e}")
            log.critical("Open the Instagram app, resolve any challenges, then restart the bot.")
            break

        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            time.sleep(60)

    log.info("Bot stopped.")


if __name__ == "__main__":
    main()
