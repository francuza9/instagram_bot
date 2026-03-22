#!/usr/bin/env python3
"""Instagram group chat bot that responds to @mentions using Groq (Llama 3.3 70B)."""

import logging
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
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

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# --- Global state ---
running = True


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


def find_new_messages(messages, last_timestamp, replied_timestamps):
    """Find new text messages, filtering out non-text and already-replied."""
    new_msgs = []
    for msg in messages:
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
    log.info(f"Sent reply: {text[:80]}{'...' if len(text) > 80 else ''}")


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

    print()
    print("=" * 50)
    print(f"  Instagram Bot Active")
    print(f"  Account:  @{username}")
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
                first_run = False
            else:
                mentions = find_new_messages(messages, last_timestamp, replied_timestamps)
                new_latest = get_latest_timestamp(messages)
                if new_latest and (last_timestamp is None or new_latest > last_timestamp):
                    last_timestamp = new_latest

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
