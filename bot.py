#!/usr/bin/env python3
"""Instagram group chat bot that responds to @mentions using Groq (Llama 3.3 70B)."""

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

# --- Constants ---
SESSION_FILE = Path(__file__).parent / "session.json"
SYSTEM_PROMPT_FILE = Path(__file__).parent / "system_prompt.txt"
POLL_MIN = 10
POLL_MAX = 15
CONTEXT_MESSAGES = 20
REPLY_DELAY_MIN = 1
REPLY_DELAY_MAX = 3
RATE_LIMIT_BACKOFF = 300
MAX_LOGIN_RETRIES = 3
REPLY_COOLDOWN = 15  # seconds between replies
VOICE_CHANCE = 1.0 # X% chance to reply with voice message instead of text
TTS_LANGUAGE = os.getenv("TTS_LANGUAGE", "en")

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

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


def fetch_messages(ig_client, thread_id):
    messages = ig_client.direct_messages(thread_id, amount=CONTEXT_MESSAGES)
    # direct_messages returns newest first, reverse for chronological order
    messages.reverse()
    return messages


def find_new_messages(messages, last_timestamp, replied_timestamps, bot_user_id):
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
        new_msgs.append(msg)
    return new_msgs


def get_latest_timestamp(messages):
    if not messages:
        return None
    return max(msg.timestamp for msg in messages)


def format_context(messages, ig_client, username_cache):
    lines = []
    for msg in messages:
        if not msg.text:
            continue
        username = get_username(ig_client, msg.user_id, username_cache)
        lines.append(f"[{username}]: {msg.text}")
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




def _get_web_session(username, password):
    global _web_session
    if _web_session is not None:
        return _web_session

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    })

    # GET homepage to pick up initial csrftoken cookie
    session.get("https://www.instagram.com/")
    csrf = session.cookies.get("csrftoken", "")

    # Web login
    resp = session.post(
        "https://www.instagram.com/accounts/login/ajax/",
        data={
            "username": username,
            "enc_password": f"#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{password}",
            "queryParams": "{}",
            "optIntoOneTap": "false",
        },
        headers={
            "X-CSRFToken": csrf,
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        },
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("authenticated"):
        raise RuntimeError(f"Web login failed: {data}")

    log.info("Web login successful")
    _web_session = session
    return _web_session


def fetch_web_tokens(username, password):
    global _web_tokens
    if _web_tokens:
        return _web_tokens

    session = _get_web_session(username, password)
    resp = session.get("https://www.instagram.com/direct/")
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


def upload_web_audio(username, password, audio_path):
    session = _get_web_session(username, password)
    tokens = fetch_web_tokens(username, password)

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
            "X-CSRFToken": session.cookies.get("csrftoken", ""),
            "X-ASBD-ID": "359341",
            "X-IG-App-ID": "936619743392459",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/direct/",
        },
        files={"farr": ("reply.m4a", audio_data, "audio/mp4")},
    )

    log.info(f"Upload response status: {resp.status_code}")
    log.info(f"Upload response body: {resp.text[:2000]}")
    resp.raise_for_status()

    body = resp.text
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


def send_web_voice(username, password, thread_id, audio_id):
    session = _get_web_session(username, password)
    tokens = fetch_web_tokens(username, password)

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
            "X-CSRFToken": session.cookies.get("csrftoken", ""),
            "X-IG-App-ID": "936619743392459",
            "X-FB-LSD": tokens["lsd"],
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

    if resp.status_code in (401, 403):
        _web_tokens.clear()
        raise RuntimeError(f"GraphQL auth error: {resp.status_code}")

    resp.raise_for_status()
    log.info("Voice message sent via GraphQL")


def send_voice_reply(username, password, thread_id, text):
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

        audio_id = upload_web_audio(username, password, m4a_path)
        send_web_voice(username, password, thread_id, audio_id)
        log.info(f"Sent voice reply: {text[:80]}{'...' if len(text) > 80 else ''}")
    finally:
        for path in (mp3_path, m4a_path):
            try:
                os.remove(path)
            except OSError:
                pass


def main():
    load_dotenv()

    # Validate env vars
    username = os.getenv("INSTAGRAM_USERNAME")
    password = os.getenv("INSTAGRAM_PASSWORD")
    groq_api_key = os.getenv("GROQ_API_KEY")
    thread_id_str = os.getenv("GROUP_THREAD_ID")
    bot_name = os.getenv("BOT_DISPLAY_NAME")

    missing = []
    if not username:
        missing.append("INSTAGRAM_USERNAME")
    if not password:
        missing.append("INSTAGRAM_PASSWORD")
    if not groq_api_key:
        missing.append("GROQ_API_KEY")
    if not thread_id_str:
        missing.append("GROUP_THREAD_ID")
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
                mentions = find_new_messages(messages, last_timestamp, replied_timestamps, bot_user_id)
                new_latest = get_latest_timestamp(messages)
                if new_latest and (last_timestamp is None or new_latest > last_timestamp):
                    last_timestamp = new_latest
                log.debug(f"Poll state: last_timestamp={last_timestamp}, replied_timestamps={replied_timestamps}")
                log.debug(f"Found {len(mentions)} new message(s)")

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
                                    send_voice_reply(username, password, thread_id, reply)
                                except Exception as e:
                                    log.warning(f"Voice reply failed, falling back to text: {e}")
                                    send_reply(ig_client, thread_id, reply)
                            else:
                                send_reply(ig_client, thread_id, reply)
                            last_reply_time = time.time()
                            replied_timestamps.add(trigger.timestamp)
                        else:
                            log.warning("LLM returned empty response, skipping reply")

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
