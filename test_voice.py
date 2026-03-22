#!/usr/bin/env python3
"""Standalone test for web voice upload — no instagrapi, no bot loop."""

import json
import os
import re
import subprocess
import time

import requests
from dotenv import load_dotenv
from gtts import gTTS

load_dotenv()

USERNAME = os.getenv("INSTAGRAM_USERNAME")
PASSWORD = os.getenv("INSTAGRAM_PASSWORD")
THREAD_ID = os.getenv("GROUP_THREAD_ID")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def step(msg):
    print(f"\n{'='*60}\n  {msg}\n{'='*60}")


# --- Step 0: Generate test M4A ---
step("Generating test audio")
tts = gTTS("This is a voice upload test", lang="en")
tts.save("/tmp/test_voice.mp3")
result = subprocess.run(
    ["ffmpeg", "-i", "/tmp/test_voice.mp3", "-c:a", "aac", "-b:a", "64k", "/tmp/test_voice.m4a", "-y"],
    capture_output=True,
)
if result.returncode != 0:
    print(f"ffmpeg failed: {result.stderr.decode()}")
    exit(1)
print("Created /tmp/test_voice.m4a")

# --- Step 1: Web login ---
step("Web login")
session = requests.Session()
session.headers.update({"User-Agent": UA})

print("GET https://www.instagram.com/ ...")
resp = session.get("https://www.instagram.com/")
print(f"  Status: {resp.status_code}")
print(f"  Cookies after homepage: {dict(session.cookies)}")

# Check for datr cookie — if missing, try /web/__mid/ endpoint
if "datr" not in session.cookies:
    print("\n  datr cookie missing, trying /web/__mid/ ...")
    resp2 = session.get("https://www.instagram.com/web/__mid/")
    print(f"  Status: {resp2.status_code}")
    print(f"  Cookies after __mid: {dict(session.cookies)}")
    if "datr" in session.cookies:
        print(f"  datr acquired: {session.cookies['datr'][:20]}...")
    else:
        print("  WARNING: datr still missing!")
else:
    print(f"  datr already present: {session.cookies['datr'][:20]}...")

csrf = session.cookies.get("csrftoken", "")
print(f"\n  CSRF token: {csrf[:20]}...")

print(f"\nPOST /accounts/login/ajax/ as {USERNAME}...")
resp = session.post(
    "https://www.instagram.com/accounts/login/ajax/",
    data={
        "username": USERNAME,
        "enc_password": f"#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{PASSWORD}",
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
print(f"  Status: {resp.status_code}")
print(f"  Response: {resp.text[:500]}")
print(f"  Cookies after login: {dict(session.cookies)}")

login_data = resp.json()
if not login_data.get("authenticated"):
    print("LOGIN FAILED!")
    exit(1)
print("Login OK!")

# Log cookie inventory
print(f"\n  Cookie inventory after login:")
for name in sorted(session.cookies.keys()):
    val = session.cookies[name]
    print(f"    {name}: {val[:30]}{'...' if len(val) > 30 else ''}")

# --- Step 2: Fetch web tokens ---
step("Fetching web tokens from /direct/")
resp = session.get("https://www.instagram.com/direct/")
print(f"  Status: {resp.status_code}")
print(f"  HTML length: {len(resp.text)}")

print(f"\n  Cookie inventory after /direct/:")
for name in sorted(session.cookies.keys()):
    val = session.cookies[name]
    print(f"    {name}: {val[:30]}{'...' if len(val) > 30 else ''}")

html = resp.text
fb_dtsg_match = re.search(r'"DTSGInitData".*?"token"\s*:\s*"([^"]+)"', html, re.DOTALL)
lsd_match = re.search(r'"LSD".*?"token"\s*:\s*"([^"]+)"', html, re.DOTALL)
jazoest_match = re.search(r'jazoest=(\d+)', html)

tokens = {}
for name, match in [("fb_dtsg", fb_dtsg_match), ("lsd", lsd_match), ("jazoest", jazoest_match)]:
    if match:
        tokens[name] = match.group(1)
        print(f"  {name}: {tokens[name][:30]}...")
    else:
        print(f"  {name}: NOT FOUND!")

if len(tokens) != 3:
    print("Token extraction failed!")
    exit(1)

# --- Step 3: Upload audio ---
step("Uploading audio to upload.php")

with open("/tmp/test_voice.m4a", "rb") as f:
    audio_data = f.read()
print(f"  Audio size: {len(audio_data)} bytes")

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

headers = {
    "X-FB-LSD": tokens["lsd"],
    "X-CSRFToken": session.cookies.get("csrftoken", ""),
    "X-ASBD-ID": "359341",
    "X-IG-App-ID": "936619743392459",
    "Origin": "https://www.instagram.com",
    "Referer": "https://www.instagram.com/direct/",
}

print(f"\n  Request params: {json.dumps(params, indent=4)}")
print(f"\n  Request headers: {json.dumps(headers, indent=4)}")
print(f"\n  Session cookies: {dict(session.cookies)}")

resp = session.post(
    "https://www.instagram.com/ajax/mercury/upload.php",
    params=params,
    headers=headers,
    files={"farr": ("reply.m4a", audio_data, "audio/mp4")},
)

print(f"\n  Response status: {resp.status_code}")
print(f"  Response headers: {dict(resp.headers)}")
print(f"  Response body: {resp.text[:2000]}")

# Parse response
body = resp.text
if body.startswith("for (;;);"):
    body = body[len("for (;;);"):]

try:
    data = json.loads(body)
    print(f"\n  Parsed JSON: {json.dumps(data, indent=2)[:2000]}")
except json.JSONDecodeError as e:
    print(f"\n  JSON parse error: {e}")
    exit(1)

try:
    audio_id = data["payload"]["metadata"]["0"]["audio_id"]
    print(f"\n  audio_id: {audio_id}")
except (KeyError, TypeError) as e:
    print(f"\n  Failed to extract audio_id: {e}")
    print(f"  Full data: {json.dumps(data, indent=2)}")
    exit(1)

# --- Step 4: Send via GraphQL ---
step(f"Sending voice to thread {THREAD_ID}")

import random
variables = json.dumps({
    "attachment_fbid": str(audio_id),
    "thread_id": str(THREAD_ID),
    "offline_threading_id": str(random.randint(10**17, 10**18 - 1)),
    "reply_to_message_id": None,
})

graphql_headers = {
    "X-FB-Friendly-Name": "IGDirectMediaSendMutation",
    "X-CSRFToken": session.cookies.get("csrftoken", ""),
    "X-IG-App-ID": "936619743392459",
    "X-FB-LSD": tokens["lsd"],
}

graphql_data = {
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
}

print(f"\n  GraphQL headers: {json.dumps(graphql_headers, indent=4)}")
print(f"\n  GraphQL data: {json.dumps(graphql_data, indent=4)}")

resp = session.post(
    "https://www.instagram.com/api/graphql",
    headers=graphql_headers,
    data=graphql_data,
)

print(f"\n  Response status: {resp.status_code}")
print(f"  Response body: {resp.text[:2000]}")

step("DONE!")
