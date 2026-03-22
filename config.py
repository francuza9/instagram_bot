# Bot behavior settings — edit these to customize how the bot operates.
# Credentials and secrets stay in .env; this file is only for tunable behavior.

# Reply to every message (False) or only when @mentioned (True)
REPLY_ONLY_WHEN_MENTIONED = False

# Chance of replying with a voice message instead of text (0.0 to 1.0)
VOICE_CHANCE = 0.25

# Language code for text-to-speech (e.g. "en", "sr", "de")
TTS_LANGUAGE = "en"

# Polling interval range in seconds (randomized between min and max)
POLL_MIN = 10
POLL_MAX = 15

# How many recent messages to send as context to the LLM
CONTEXT_MESSAGES = 5

# Delay before sending a reply in seconds (randomized, looks more human)
REPLY_DELAY_MIN = 1
REPLY_DELAY_MAX = 3

# Minimum seconds between replies to avoid spamming
REPLY_COOLDOWN = 15

# React to reels shared in the DM thread (requires GEMINI_API_KEY)
# Options: "FULL" — download full video, upload to Gemini (slow but sees everything)
#          "LITE" — use thumbnail image + caption, much faster (recommended)
#          "NONE" — disabled, bot ignores reels entirely
REACT_TO_REELS = "LITE"
