"""
Daily Instagram auto-poster: AI/tech news + innovative tech ideas.

Usage:
    python main.py --slot news            # post 1: AI / tech news
    python main.py --slot idea            # post 2: innovative tech idea
    python main.py --slot news --dry-run  # makes image + caption, does NOT post
    python main.py --refresh-token        # refresh Instagram token (run weekly)

UiPath Orchestrator triggers this script twice a day.
"""
import argparse
import subprocess
import json
import math
import os
import random
import re
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

VERSION = "2026-10-21-v25 (CTA + topic hashtags, Andrew natural voice, AI art backgrounds, cinematic motion Reels, Groq free backup, idea posts framed as concepts, paragraph formatting, auto-discovered fallback model, longer retries, optional Reels, extra random topic posts at random times, no-repeat captions, quota-aware Gemini retry + fallback model, two-step news writer, 8 themes)"
BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

OUT_DIR = BASE / "output"
OUT_DIR.mkdir(exist_ok=True)
POSTED_FILE = BASE / "posted.json"
TOKEN_FILE = BASE / "token.json"
LOG_FILE = BASE / "log.csv"

FEEDS = [
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    "https://blog.google/technology/ai/rss/",
    "https://openai.com/news/rss.xml",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
]

# Extra posts: random topic (robotics, electronics, sci-fi tech ...) at random times
EXTRA_FEEDS = [
    "https://hackaday.com/blog/feed/",
    "https://interestingengineering.com/feed",
    "https://spectrum.ieee.org/feeds/feed.rss",
    "https://newatlas.com/index.rss",
    "https://techcrunch.com/category/robotics/feed/",
    "https://www.sciencedaily.com/rss/matter_energy/robotics.xml",
]
# label -> (what the post is about, keywords used to find matching news)
TOPICS = {
    "ROBOTICS": ("robotics: robots, actuators, humanoids, drones, robot learning",
                 ["robot", "humanoid", "drone", "actuator", "exoskeleton", "autonomous"]),
    "ELECTRONICS": ("electronics: chips, sensors, circuits, microcontrollers, batteries, displays",
                    ["chip", "semiconductor", "sensor", "circuit", "battery", "transistor", "display", "arduino", "raspberry", "fpga", "pcb"]),
    "MECHATRONICS": ("mechatronics: where mechanics, electronics and control software meet (motors, servos, CNC, 3D printers, smart machines)",
                     ["motor", "servo", "cnc", "3d print", "3d-print", "machine", "mechanical", "automation", "gripper", "actuator"]),
    "SCI-FI TECH": ("real technology that science fiction imagined (e.g. flying cars, invisibility, brain interfaces, space elevators, holograms): the idea versus where the real science stands today",
                    ["hologram", "brain", "neural", "quantum", "fusion", "space", "invisib", "teleport", "cyborg", "bionic", "laser"]),
    "NEW INNOVATION": ("a recent or emerging innovation in engineering or science (materials, energy, health tech, wearables, transport)",
                       ["new", "breakthrough", "researchers", "invent", "prototype", "develop", "innovation"]),
    "FUTURE TECH": ("future technology: where a real technology could be in 10-20 years, grounded in current research",
                    ["future", "quantum", "fusion", "graphene", "solid-state", "energy", "space", "ai"]),
}
# UTC times of the extra-post triggers in the workflow (must match the cron lines)
EXTRA_TRIGGERS_UTC = [(4, 47), (6, 23), (8, 11), (10, 37), (13, 29), (15, 41)]

# LLM_PROVIDER: "gemini" (free tier, default) or "claude" (paid API)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
LANGUAGE = os.getenv("POST_LANGUAGE", "English")  # English / Marathi / Hinglish
GRAPH = "https://graph.instagram.com/v21.0"

# Growth: call-to-action line + fixed topic hashtags (added to every caption)
CTA = os.getenv("CTA") or "Follow @mindmeetsmachine6 for daily tech 🚀"
TOPIC_TAGS = {
    "AI NEWS": ["#AI", "#ArtificialIntelligence", "#TechNews"],
    "TECH IDEA OF THE DAY": ["#TechIdeas", "#Innovation", "#FutureTech"],
    "ROBOTICS": ["#Robotics", "#Robots", "#Automation"],
    "ELECTRONICS": ["#Electronics", "#Chips", "#Embedded"],
    "MECHATRONICS": ["#Mechatronics", "#Engineering", "#Automation"],
    "SCI-FI TECH": ["#SciFi", "#FutureTech", "#Science"],
    "NEW INNOVATION": ["#Innovation", "#Engineering", "#NewTech"],
    "FUTURE TECH": ["#FutureTech", "#Innovation", "#Technology"],
}


def build_hashtags(post, label, limit=14):
    """AI hashtags + fixed topic hashtags + brand tag, de-duplicated."""
    base = TOPIC_TAGS.get(label, ["#Tech", "#Innovation"])
    seen, out = set(), []
    for h in list(post.get("hashtags", [])) + base + ["#MindMeetsMachine"]:
        h = "#" + re.sub(r"[^\w]", "", str(h).lstrip("#"))
        if len(h) > 1 and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out[:limit]


# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def log(slot, status, detail=""):
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        if new:
            f.write("time,slot,status,detail\n")
        detail = str(detail).replace(",", ";").replace("\n", " ")[:300]
        f.write(f"{datetime.now().isoformat()},{slot},{status},{detail}\n")


# ---------------------------------------------------------------- news
def fetch_items(feeds=None, limit_per_feed=6):
    items = []
    for url in (feeds or FEEDS):
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"})
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for e in feed.entries[:limit_per_feed]:
                items.append(
                    {
                        "title": e.get("title", "").strip(),
                        "summary": re.sub("<[^>]+>", "", e.get("summary", ""))[:600],
                        "link": e.get("link", ""),
                        "ts": (time.mktime(e.published_parsed) if e.get("published_parsed") else
                               time.mktime(e.updated_parsed) if e.get("updated_parsed") else None),
                        "source": feed.feed.get("title", url),
                    }
                )
        except Exception as ex:
            print(f"feed failed {url}: {ex}", file=sys.stderr)
    return items


def load_state():
    """posted.json holds {"links": [...], "headlines": [...]} (older list format is converted)."""
    data = load_json(POSTED_FILE, {})
    if isinstance(data, list):
        data = {"links": data}
    data.setdefault("links", [])
    data.setdefault("headlines", [])
    data.setdefault("themes", [])
    return data


def pick_items(items, posted, n, max_age_days=21):
    posted_norm = {p.split("?")[0].rstrip("/") for p in posted}
    now = time.time()
    fresh = [i for i in items if i["link"] and i["link"].split("?")[0].rstrip("/") not in posted_norm
             and not (i.get("ts") and now - i["ts"] > max_age_days * 86400)]  # skip old articles
    return fresh[:n]


# ---------------------------------------------------------------- Claude
def build_prompt(slot, items, recent=None):
    news_block = "\n".join(
        f"{idx+1}. [{i['source']}] {i['title']} - {i['summary']} ({i['link']})"
        for idx, i in enumerate(items)
    )
    if slot == "news":
        task = (
            "Pick the SINGLE most interesting AI/technology news item from the list below "
            "and write an Instagram post about it. Use ONLY facts present in the list. "
            "Do not invent numbers, quotes or claims."
        )
    else:
        task = (
            "Using the trends in the list below as inspiration, write an Instagram post "
            "presenting ONE innovative, practical technology idea (a product, tool or use "
            "case someone could build or try). Make it clearly an idea, not a news claim. "
            "Write it as a concept ('Imagine...', 'What if...', 'This could...'), never as if the product already exists "
            "and never with 'our app/our bot/we built'. Do not name real companies as partners or users of the idea."
        )
    recent_block = ""
    if recent:
        recent_block = (
            "\nTopics already posted recently. Do NOT cover the same story, statement or event "
            "again, even if it comes from a different source or is worded differently. If every "
            "item repeats one of these, pick the item least similar to them:\n"
            + "\n".join(f"- {h}" for h in recent)
            + "\n"
        )
    return f"""{task}

Language of the post: {LANGUAGE}.
Audience: Indian tech-curious Instagram users. Tone: clear, energetic, no hype or clickbait.

Rules:
- Write everything in your own words. Never copy sentences or headlines from the list. Use at most one short quote (under 12 words).
- The headline and subheadline must not claim more than the source says. Keep the exact scope (who, what, how much). Never turn a narrow fact into a general one.
- Do not add conclusions, motives, predictions or opinions that are not in the source. Do not attribute a view to a group (e.g. "leadership", "experts") when the source names one person.
- Every hashtag must relate directly to this post's topic. No generic or location hashtags unless the story is about that place.
{recent_block}
Return ONLY valid JSON with these keys:
- "headline": max 60 characters, big text for the image
- "subheadline": max 110 characters, supporting line for the image
- "caption": 50-90 words: a hook line, then 2-3 short lines with DIFFERENT points, then one closing takeaway line. 1-2 emojis. Never repeat a sentence or idea.
- "hashtags": list of 6-9 relevant hashtags (each starting with #)
- "source_name": name of the source (news slot) or "" (idea slot)
- "source_link": the exact URL from the list (news slot) or "" (idea slot)

Items:
{news_block}
"""


def build_pick_prompt(items, recent=None):
    """Step 1 (news): choose a story and turn it into short neutral notes."""
    news_block = "\n".join(
        f"{idx+1}. [{i['source']}] {i['title']} - {i['summary']} ({i['link']})"
        for idx, i in enumerate(items)
    )
    recent_block = ""
    if recent:
        recent_block = (
            "\nTopics already posted recently. Do NOT pick the same story, statement or event again, "
            "even from a different source. If every item repeats one of these, pick the least similar:\n"
            + "\n".join(f"- {h}" for h in recent)
            + "\n"
        )
    return f"""You are a news editor. From the items below, pick the SINGLE most interesting technology or engineering story.
{recent_block}
Return ONLY valid JSON with these keys:
- "source_name": the publication name (e.g. "The Verge")
- "source_link": the exact URL of the chosen item from the list
- "facts": 3 to 5 short notes (max 12 words each) of what the story says

Rules for "facts":
- Telegraphic fragments in your own words. NEVER copy a sentence or a long phrase from the source.
- Only what the source says: who, what, numbers, dates. No opinions, no guesses, no background of your own.
- Keep the exact scope. Do not make a narrow fact sound general.
- A direct quote is allowed only if short (under 10 words) and inside quotation marks.

Items:
{news_block}
"""


def build_news_write_prompt(facts, source_name):
    """Step 2 (news): write the post from the notes only (the AI never sees the source sentences)."""
    fact_lines = "\n".join(f"- {f}" for f in facts)
    return f"""Write an Instagram post about the news story described by these notes (from {source_name}).
Use ONLY the facts in the notes. Do not invent numbers, quotes, names, motives or claims.

Language of the post: {LANGUAGE}.
Audience: Indian tech-curious Instagram users. Tone: clear, energetic, no hype or clickbait.

Rules:
- Start the caption with why this matters to the reader (a consequence or a question). Do not open with "X said ...".
- Then explain the facts in your own sentences. Use at most one short quote (under 12 words).
- The headline and subheadline must not claim more than the notes say. Keep the exact scope (who, what, how much).
- Do not add conclusions, predictions or opinions. Do not attribute a view to a group when the notes name one person.
- Every hashtag must relate directly to this topic. No generic or location hashtags.
- Never say the same fact or idea twice in the caption. Do not repeat a sentence, and do not end by restating the opening. Every line must add something new; the closing line is a short takeaway, not a summary.

Return ONLY valid JSON with these keys:
- "headline": max 60 characters, big text for the image
- "subheadline": max 110 characters, supporting line for the image
- "caption": 50-90 words: a hook line, then 2-3 short lines with DIFFERENT facts, then one closing takeaway line. 1-2 emojis
- "hashtags": list of 6-9 relevant hashtags (each starting with #)

Notes:
{fact_lines}
"""


def build_concept_prompt(topic_text, recent=None):
    recent_block = ""
    if recent:
        recent_block = (
            "\nTopics already posted recently. Do NOT repeat any of these or anything very similar:\n"
            + "\n".join(f"- {h}" for h in recent) + "\n"
        )
    return f"""Write an Instagram post that teaches ONE fascinating, specific thing about: {topic_text}.
Pick a real, well-established technology, component, principle or milestone. Explain how it works or why it matters.
Be concrete (one specific thing, not a broad overview).

Language of the post: {LANGUAGE}.
Audience: Indian tech-curious Instagram users, many are students and makers. Tone: clear, energetic, no hype or clickbait.

Rules:
- Only well-established facts. Do NOT invent statistics, dates, prices, company claims or study results. If unsure of a number, leave it out.
- Do not present speculation as fact. If you discuss the future, say it is a possibility.
- Never say the same fact or idea twice in the caption. Every line adds something new.
- Every hashtag must relate directly to this post's topic.
{recent_block}
Return ONLY valid JSON with these keys:
- "headline": max 60 characters, big text for the image
- "subheadline": max 110 characters, supporting line for the image
- "caption": 50-90 words: a hook line, then 2-3 short lines with DIFFERENT points, then one closing takeaway line. 1-2 emojis
- "hashtags": list of 6-9 relevant hashtags (each starting with #)
"""


_FALLBACKS = None


def _discover_fallbacks():
    """Ask Google which flash models this key lists (newest first). Each has its own capacity/quota."""
    global _FALLBACKS
    if _FALLBACKS is not None:
        return _FALLBACKS
    _FALLBACKS = []
    forced = os.getenv("GEMINI_FALLBACK_MODEL")
    if forced:
        _FALLBACKS = [forced]
        return _FALLBACKS
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                         headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=20)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        names = [n for n in names if "flash" in n and n != GEMINI_MODEL
                 and not re.search(r"image|tts|live|audio|embed|thinking|exp|robotics|computer", n)]

        def key(n):
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            return (-(float(v.group(1)) if v else 0), "lite" not in n, n)
        names.sort(key=key)
        _FALLBACKS = names[:4]
    except Exception as ex:
        print(f"could not list models: {ex}", file=sys.stderr)
    print(f"fallback models to try: {_FALLBACKS or 'none found'}", file=sys.stderr)
    return _FALLBACKS


def call_gemini(prompt):
    """Free tier: key from https://aistudio.google.com/apikey
    Plan: main model, then (when it is busy) other flash models, then main again with longer waits.
    - busy (500/502/503/504) or per-minute limit (429): moves on / waits and retries
    - model not available to this key (404) or its daily quota used up: dropped for this run."""
    dead, errors = set(), {}
    # (which model, seconds to wait before this try); "fb" = every fallback model once
    plan = [("main", 0), ("main", 20), ("fb", 0), ("main", 45), ("fb", 30), ("main", 90), ("fb", 60), ("main", 120)]
    if os.getenv("GROQ_API_KEY"):  # a free backup exists: do not spend minutes waiting on a busy Gemini
        plan = [("main", 0), ("fb", 0), ("main", 20), ("fb", 20)]
    last = ""
    for who, wait in plan:
        if who == "main":
            models = [GEMINI_MODEL]
        else:
            models = [m for m in _discover_fallbacks()]
        models = [m for m in models if m not in dead]
        if not models:
            continue
        if wait:
            print(f"Gemini busy/limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
        for model in models:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
                    },
                    timeout=60,
                )
            except requests.RequestException as ex:  # network hiccup
                errors[model] = last = f"network error [{model}]: {ex}"
                print(f"Gemini {model}: network error", file=sys.stderr)
                continue
            if r.ok:
                if model != GEMINI_MODEL:
                    print(f"(answered by fallback model {model})", file=sys.stderr)
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            errors[model] = last = f"Gemini API error {r.status_code} [{model}]: {r.text[:300]}"
            print(f"Gemini {model}: {r.status_code}", file=sys.stderr)
            if r.status_code in (400, 401, 403) and model == GEMINI_MODEL:
                raise RuntimeError(last)  # bad key / bad request on the main model: retrying will not help
            if r.status_code in (400, 401, 403, 404):
                dead.add(model)  # this fallback is not usable with this key
            elif r.status_code == 429 and re.search(r"PerDay|per day|daily", r.text, re.I):
                errors[model] = last = last + "  -> DAILY FREE QUOTA USED UP (resets about midday IST)"
                dead.add(model)
            elif r.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(last)
            m = re.search(r'retry in ([\d.]+)s|"retryDelay":\s*"(\d+)', r.text)
            if m and who == "main":  # Google told us how long to wait
                time.sleep(min(max(float(m.group(1) or m.group(2)), 5), 60))
    gemini_err = errors.get(GEMINI_MODEL) or last or "Gemini unavailable"
    if os.getenv("GROQ_API_KEY"):
        print("Gemini unavailable -> trying Groq (free backup)", file=sys.stderr)
        try:
            return call_groq(prompt)
        except Exception as ex:
            raise RuntimeError(f"{gemini_err} | Groq backup also failed: {ex}")
    raise RuntimeError(gemini_err)


def call_groq(prompt):
    """Free backup AI. Key from https://console.groq.com/keys (GROQ_API_KEY)."""
    headers = {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"}
    base = "https://api.groq.com/openai/v1"
    model = os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile"
    tried_list = False
    for attempt in range(4):
        try:
            r = requests.post(f"{base}/chat/completions", headers=headers, timeout=60, json={
                "model": model, "temperature": 0.8,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "Reply with one valid JSON object only."},
                             {"role": "user", "content": prompt}]})
        except requests.RequestException as ex:
            print(f"Groq network error: {ex}", file=sys.stderr)
            time.sleep(10)
            continue
        if r.ok:
            print(f"(answered by Groq {model})", file=sys.stderr)
            return r.json()["choices"][0]["message"]["content"]
        print(f"Groq {model}: {r.status_code} {r.text[:150]}", file=sys.stderr)
        if r.status_code in (400, 404) and not tried_list:  # model renamed/removed: pick another big model
            tried_list = True
            try:
                ids = [m["id"] for m in requests.get(f"{base}/models", headers=headers, timeout=20).json()["data"]]
                ids = [i for i in ids if re.search(r"llama|gpt-oss|qwen", i) and not re.search(r"guard|whisper|tts|vision|prompt", i)]
                ids.sort(key=lambda i: (not re.search(r"70b|120b|large|versatile", i), i))
                if ids:
                    model = ids[0]
                    continue
            except Exception as ex:
                print(f"Groq model list failed: {ex}", file=sys.stderr)
        if r.status_code == 429:
            time.sleep(20)
            continue
        if r.status_code >= 500:
            time.sleep(10)
            continue
        break
    raise RuntimeError(f"Groq failed (last model {model})")


def call_claude(prompt):
    from anthropic import Anthropic  # only needed if LLM_PROVIDER=claude

    client = Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=MODEL, max_tokens=1200, messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def _words(s):
    return re.findall(r"[a-z0-9']+", s.lower().replace("’", "'"))


def longest_shared_run(a, b):
    """Longest run of consecutive words that appears in both texts -> (count, phrase)."""
    A, B = _words(a), _words(b)
    best, end = 0, 0
    prev = [0] * (len(B) + 1)
    for i in range(1, len(A) + 1):
        cur = [0] * (len(B) + 1)
        for j in range(1, len(B) + 1):
            if A[i - 1] == B[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, end = cur[j], i
        prev = cur
    return best, " ".join(A[end - best:end])


# max words allowed to be copied in a row from the source (names and short quotes pass)
COPY_LIMITS = (("headline", 5), ("subheadline", 6), ("caption", 7))


def _strip_quotes(s):
    """Remove short quoted passages so a short quote ("...", up to 12 words) never counts as copying."""
    def repl(m):
        return " " if len(m.group(1).split()) <= 12 else m.group(0)
    return re.sub(r'["“]([^"”]{1,160})["”]', repl, s)


def too_close(post, items):
    """Returns (field, phrase) if the post copies wording from its source, else None."""
    src = next((i for i in items if i["link"] == post.get("source_link")), None)
    if not src:
        return None  # idea posts have no single source
    source_text = f"{src['title']} {src['summary']}"
    for field, limit in COPY_LIMITS:
        n, phrase = longest_shared_run(_strip_quotes(str(post.get(field, ""))), source_text)
        if n >= limit:
            return field, phrase
    return None


def _ask(prompt):
    raw = call_claude(prompt) if LLM_PROVIDER == "claude" else call_gemini(prompt)
    return json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip())


def _clean_source(name):
    """'AI | The Verge' -> 'The Verge';  'Ars Technica - Technology Lab' -> 'Ars Technica'"""
    name = (name or "").split(" | ")[-1]
    name = re.split(r"\s+[-–—]\s+", name)[0].strip()
    return name


def generate_news_post(items, recent=None):
    """Two steps so the writer never sees the source sentences:
    1) pick a story and turn it into short notes, 2) write the post from the notes only."""
    # ---- step 1: pick + notes
    feedback, pick = "", None
    for attempt in range(3):
        try:
            cand = _ask(build_pick_prompt(items, recent) + feedback)
            facts = [str(f).strip() for f in cand["facts"] if str(f).strip()]
            src = next((i for i in items if i["link"] == cand["source_link"]), None)
            if not facts or not src:
                raise ValueError("no facts or unknown source_link")
        except (ValueError, TypeError, KeyError) as ex:
            feedback = ("\n\nYour previous reply was invalid. Return ONLY the JSON object with source_name, "
                        "source_link (an exact URL from the list) and facts.")
            print(f"bad story pick, retrying ({ex})", file=sys.stderr)
            continue
        source_text = f"{src['title']} {src['summary']}"
        clean = [f for f in facts if longest_shared_run(_strip_quotes(f), source_text)[0] < 7]
        if len(clean) >= 2:  # drop copied notes instead of spending another AI call
            pick = (cand, src, clean)
            break
        pick = (cand, src, facts)  # fallback if the retries fail too
        print("notes copy the source, retrying", file=sys.stderr)
        feedback = ("\n\nYour notes copied long phrases from the source. Rewrite every note as a short "
                    "fragment with different words.")
    if pick is None:
        raise RuntimeError("AI did not pick a valid story after 3 tries")
    cand, src, facts = pick
    source_name = _clean_source(cand.get("source_name") or src["source"])

    # ---- step 2: write from notes only
    write_prompt = build_news_write_prompt(facts, source_name)
    feedback, data = "", None
    for attempt in range(3):
        try:
            data = _ask(write_prompt + feedback)
            for k in ("headline", "subheadline", "caption", "hashtags"):
                if k not in data:
                    raise ValueError(f"output missing {k}")
        except (ValueError, TypeError) as ex:
            feedback = "\n\nYour previous reply was not valid JSON with all required keys. Return ONLY the JSON object."
            print(f"bad AI output, retrying ({ex})", file=sys.stderr)
            data = None
            continue
        data["source_name"], data["source_link"] = source_name, src["link"]
        issue = too_close(data, items)
        if not issue:
            return data
        field, phrase = issue
        print(f"too close to source in {field}: '{phrase}' (try {attempt + 1}/3)", file=sys.stderr)
        feedback = (f"\n\nYour previous draft reused wording from the source in the {field}: \"{phrase}\". "
                    "Rewrite the whole post with different sentence structure and vocabulary.")
    if data is None:
        raise RuntimeError("AI did not return valid post JSON after 3 tries")
    print("WARNING: post wording still close to the source after 3 tries", file=sys.stderr)
    return data


def generate_post(slot, items, recent=None):
    if slot == "news":
        return generate_news_post(items, recent)
    base_prompt = build_prompt(slot, items, recent)
    feedback, data = "", None
    for attempt in range(3):
        raw = call_claude(base_prompt + feedback) if LLM_PROVIDER == "claude" else call_gemini(base_prompt + feedback)
        text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            data = json.loads(text)
            for k in ("headline", "subheadline", "caption", "hashtags"):
                if k not in data:
                    raise ValueError(f"output missing {k}")
        except (ValueError, TypeError) as ex:
            feedback = "\n\nYour previous reply was not valid JSON with all required keys. Return ONLY the JSON object."
            print(f"bad AI output, retrying ({ex})", file=sys.stderr)
            data = None
            continue
        issue = too_close(data, items)
        if not issue:
            return data
        field, phrase = issue
        print(f"too close to source in {field}: '{phrase}' (try {attempt + 1}/3)", file=sys.stderr)
        feedback = (
            f"\n\nYour previous draft copied wording from the source in the {field}: \"{phrase}\". "
            "Rewrite the whole post from scratch with a different sentence structure and different "
            "vocabulary. Start the caption with why this matters to the reader (a consequence or a "
            "question), not with who said what. Keep every fact accurate. Do not reuse that phrase."
        )
    if data is None:
        raise RuntimeError("AI did not return valid post JSON after 3 tries")
    print("WARNING: post wording still close to the source after 3 tries", file=sys.stderr)
    return data


def generate_concept_post(topic_text, recent=None):
    prompt = build_concept_prompt(topic_text, recent)
    feedback = ""
    for attempt in range(3):
        try:
            data = _ask(prompt + feedback)
            for k in ("headline", "subheadline", "caption", "hashtags"):
                if k not in data:
                    raise ValueError(f"output missing {k}")
            data["source_name"], data["source_link"] = "", ""
            return data
        except (ValueError, TypeError) as ex:
            feedback = "\n\nYour previous reply was not valid JSON with all required keys. Return ONLY the JSON object."
            print(f"bad AI output, retrying ({ex})", file=sys.stderr)
    raise RuntimeError("AI did not return valid post JSON after 3 tries")


def format_caption(text):
    """If the AI wrote one dense paragraph, split it: hook / body lines / closing takeaway."""
    text = text.strip()
    if "\n" in text:
        return text
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z\W])", text)
    if len(sents) < 4:
        return text
    body = sents[1:-1]
    mid = (len(body) + 1) // 2
    parts = [sents[0], " ".join(body[:mid]), " ".join(body[mid:]) if body[mid:] else "", sents[-1]]
    return "\n\n".join(p for p in parts if p)


def extra_gate():
    """Scheduled 'extra' runs: post only some of the triggers so times look random.
    Returns True if this run should post."""
    per_day = int(os.getenv("EXTRA_PER_DAY") or 2)
    ist = timedelta(hours=5, minutes=30)
    today = (datetime.now() + ist).date()
    done = 0
    try:
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split(",", 3)
            if len(parts) >= 3 and parts[1] == "extra" and parts[2] == "OK":
                if (datetime.fromisoformat(parts[0]) + ist).date() == today:
                    done += 1
    except Exception:
        pass
    need = per_day - done
    now_min = datetime.now().hour * 60 + datetime.now().minute
    remaining = max(1, sum(1 for h, m in EXTRA_TRIGGERS_UTC if h * 60 + m >= now_min - 50))
    p = 1.0 if need >= remaining else max(0.0, need / remaining)
    go = need > 0 and random.random() < p
    print(f"extra gate: done today={done}, target={per_day}, triggers left={remaining}, p={p:.2f} -> {'POST' if go else 'skip'}", file=sys.stderr)
    return go


# ---------------------------------------------------------------- image
def load_font(size, bold=True):
    candidates = [
        os.getenv("FONT_PATH", ""),  # set to NotoSansDevanagari-Bold.ttf for Marathi
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def wrap_to_width(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _gradient(W, H, top, bot):
    img = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        px.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return img.convert("RGBA")


def _blob(W, H, cx, cy, r, color, alpha, k=4):
    """Soft glowing circle, drawn small and scaled up (fast)."""
    w, h = W // k, H // k
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse([(cx - r) / k, (cy - r) / k, (cx + r) / k, (cy + r) / k], fill=alpha)
    mask = mask.filter(ImageFilter.GaussianBlur(r / k * 0.6))
    layer = Image.new("RGBA", (w, h), color + (0,))
    layer.putalpha(mask)
    return layer.resize((W, H), Image.LANCZOS)


def _stars(W, H, rng, n=260, sparkles=8):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(n):
        x, y = rng.randrange(W), rng.randrange(H)
        s = rng.choice([1, 1, 1, 2, 2, 3])
        d.ellipse([x, y, x + s, y + s], fill=(255, 255, 255, rng.randrange(60, 200)))
    placed = 0
    while placed < sparkles:  # sparkle stars, kept away from the text area
        x, y = rng.randrange(60, W - 60), rng.randrange(60, H - 60)
        if 300 < y < 1050 and x < 1000:
            continue
        L = rng.randrange(14, 36)
        d.line([(x - L, y), (x + L, y)], fill=(255, 255, 255, 170), width=2)
        d.line([(x, y - L), (x, y + L)], fill=(255, 255, 255, 170), width=2)
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 255, 255, 255))
        placed += 1
    return layer.filter(ImageFilter.GaussianBlur(0.6))


def _streaks(W, H, rng, color):
    """Anime-style diagonal light streaks."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(18):
        x = rng.randrange(int(W * 0.35), W + 300)
        y = rng.randrange(-100, int(H * 0.55))
        L = rng.randrange(180, 540)
        ang = math.radians(rng.uniform(28, 38))
        d.line(
            [(x, y), (x - L * math.cos(ang), y + L * math.sin(ang))],
            fill=color + (rng.randrange(35, 100),),
            width=rng.choice([1, 2, 2, 3]),
        )
    return layer.filter(ImageFilter.GaussianBlur(0.8))


def _orb(W, H, cx, cy, r, c_edge, c_core, ring_color):
    """Glowing planet with a tilted ring."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 40
    for i in range(steps):
        t = i / steps
        rr = int(r * (1 - t))
        col = tuple(int(c_edge[j] + (c_core[j] - c_edge[j]) * t) for j in range(3))
        ox, oy = int(r * 0.28 * t), -int(r * 0.28 * t)
        d.ellipse([cx - rr + ox, cy - rr + oy, cx + rr + ox, cy + rr + oy], fill=col + (235,))
    ring = Image.new("RGBA", (int(r * 4), int(r * 4)), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([r * 0.1, r * 1.45, r * 3.9, r * 2.55], outline=ring_color + (230,), width=5)
    ring = ring.rotate(22, resample=Image.BICUBIC)
    layer.paste(ring, (int(cx - 2 * r), int(cy - 2 * r)), ring)
    return layer


def _circuits(W, H, rng, color):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(6):
        x, y = W, rng.randrange(430, 1000)
        pts = [(x, y)]
        for _ in range(2):
            x -= rng.randrange(40, 100)
            pts.append((x, y))
            y += rng.choice([-1, 1]) * rng.randrange(30, 110)
            pts.append((x, y))
        d.line(pts, fill=color + (110,), width=3)
        d.ellipse([x - 7, y - 7, x + 7, y + 7], outline=color + (200,), width=3)
    return layer


def _grid(W, H, color, horizon, alpha=95):
    """Synthwave-style perspective floor grid that fades out toward the horizon."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    vx = W / 2
    for i in range(-16, 17):
        d.line([(vx + i * 20, horizon), (vx + i * 170, H)], fill=color + (alpha,), width=2)
    y, step = horizon, 9.0
    while y < H:
        d.line([(0, y), (W, y)], fill=color + (alpha,), width=2)
        step *= 1.27
        y += step
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    for yy in range(horizon, H):
        md.line([(0, yy), (W, yy)], fill=int(255 * ((yy - horizon) / (H - horizon)) ** 0.7))
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).line([(0, horizon), (W, horizon)], fill=color + (230,), width=3)
    glow = glow.filter(ImageFilter.GaussianBlur(5))
    return Image.alpha_composite(layer, glow)


def _skyline(W, H, rng, base_y, fill, h_min, h_max, win_colors, lit=0.28, edge=None):
    """Row of city buildings with lit windows."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x = -20
    while x < W:
        bw = rng.randrange(44, 120)
        top = base_y - rng.randrange(h_min, h_max)
        d.rectangle([x, top, x + bw, H], fill=fill + (255,))
        if edge and rng.random() < 0.5:
            d.line([(x, top), (x + bw, top)], fill=edge + (220,), width=3)
        for wy in range(top + 14, base_y - 8, 22):
            for wx in range(x + 8, x + bw - 10, 18):
                if rng.random() < lit:
                    d.rectangle([wx, wy, wx + 7, wy + 11], fill=rng.choice(win_colors) + (rng.randrange(120, 230),))
        x += bw + rng.randrange(0, 6)
    return layer


def _sun(W, H, cx, cy, r, c_top, c_bot):
    """Retro-anime sun with horizontal slits."""
    size = 2 * r
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / (size - 1)
        gd.line([(0, y), (size, y)], fill=tuple(int(c_top[i] + (c_bot[i] - c_top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([0, 0, size - 1, size - 1], fill=255)
    for k in range(8):
        y = int(size * 0.52 + k * size * 0.06)
        md.rectangle([0, y, size, y + 3 + k * 2], fill=0)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.paste(grad, (cx - r, cy - r), mask)
    return layer


def _rain(W, H, rng, color, n=170):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(n):
        x, y, L = rng.randrange(-100, W), rng.randrange(-50, H), rng.randrange(25, 70)
        d.line([(x, y), (x - L * 0.25, y + L)], fill=color + (rng.randrange(25, 80),), width=1)
    return layer


def _matrix(W, H, rng, color, head=(215, 255, 235)):
    """Digital rain."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = load_font(24)
    chars = "01ABCDEF<>/{}=+*#%$&?"
    colw, rowh = 26, 28
    for cx in range(10, W, colw):
        if rng.random() < 0.22:
            continue
        n = rng.randrange(8, 34)
        y0 = rng.randrange(-5, H // rowh)
        for j in range(n):
            y = (y0 + j) * rowh
            if y > H:
                break
            t = j / max(1, n - 1)
            if j == n - 1:
                d.text((cx, y), rng.choice(chars), font=f, fill=head + (235,))
            else:
                d.text((cx, y), rng.choice(chars), font=f, fill=color + (int(30 + 170 * t * t),))
    return layer


def _hologram(W, H, rng, color, color2):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = W // 2, int(H * 0.60)
    for r in range(110, 900, 95):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color + (110 if r % 190 < 95 else 60,), width=2)
    for deg in range(0, 360, 5):  # tick ring
        a = math.radians(deg)
        r0, r1 = 470, 490 if deg % 15 else 505
        d.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)), (cx + r1 * math.cos(a), cy + r1 * math.sin(a))],
               fill=color + (140,), width=2)
    d.line([(cx, 0), (cx, H)], fill=color + (45,), width=1)
    d.line([(0, cy), (W, cy)], fill=color + (45,), width=1)
    ang = rng.randrange(0, 360)
    for i in range(45):  # radar sweep
        d.pieslice([cx - 900, cy - 900, cx + 900, cy + 900], ang - (i + 1) * 0.8, ang - i * 0.8,
                   fill=color + (int(80 * (1 - i / 45)),))
    for gx in range(60, W, 90):
        for gy in range(60, H, 90):
            d.line([(gx - 5, gy), (gx + 5, gy)], fill=color + (55,), width=1)
            d.line([(gx, gy - 5), (gx, gy + 5)], fill=color + (55,), width=1)
    for i in range(14):  # data bars
        h = rng.randrange(20, 120)
        d.rectangle([90 + i * 20, 1140 - h, 102 + i * 20, 1140], fill=color2 + (120,))
    return layer


def _aurora(W, H, rng, cols):
    k = 4
    w, h = W // k, H // k
    total = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for ci, col in enumerate(cols):
        rib = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(rib)
        base = rng.uniform(0.14, 0.40) * h
        amp, f, ph = rng.uniform(18, 40), rng.uniform(0.012, 0.025), rng.uniform(0, 6.28)
        L = rng.randrange(90, 150)
        for x in range(w):
            top = base + amp * math.sin(x * f + ph) + 9 * math.sin(x * f * 3.1 + ph * 2)
            mod = 0.6 + 0.4 * math.sin(x * 0.15 + ci)
            for j in range(L):
                d.point((x, int(top + j)), fill=col + (int(150 * (1 - j / L) ** 1.6 * mod),))
        total.alpha_composite(rib.filter(ImageFilter.GaussianBlur(2.2)))
    return total.resize((W, H), Image.LANCZOS)


def _mountains(W, H, rng, base_y, fill, rough=70, n=14):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    step = W / n
    pts = [(0, H)] + [(i * step, base_y - rng.randrange(0, rough * 2)) for i in range(n + 1)] + [(W, H)]
    ImageDraw.Draw(layer).polygon(pts, fill=fill + (255,))
    return layer


def _halftone(W, H, color, alpha, origin, maxd=1000, step=24):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for y in range(0, H, step):
        for x in range(0, W, step):
            dist = math.hypot(x - origin[0], y - origin[1]) / maxd
            r = step * 0.5 * max(0.0, 1 - dist) ** 1.1
            if r > 1:
                d.ellipse([x - r, y - r, x + r, y + r], fill=color + (alpha,))
    return layer


def _burst(W, H, rng, cx, cy, colors):
    """Manga-style radial speed lines."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(120):
        a, w = rng.uniform(0, 2 * math.pi), rng.uniform(0.004, 0.02)
        r0, r1 = rng.randrange(110, 380), 1900
        d.polygon(
            [(cx + r0 * math.cos(a), cy + r0 * math.sin(a)),
             (cx + r1 * math.cos(a - w), cy + r1 * math.sin(a - w)),
             (cx + r1 * math.cos(a + w), cy + r1 * math.sin(a + w))],
            fill=rng.choice(colors) + (rng.randrange(40, 115),),
        )
    return layer


def _pcb(W, H, rng, color, color2):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    dirs = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
    for _ in range(48):
        x, y = rng.randrange(0, W // 30) * 30, rng.randrange(0, H // 30) * 30
        pts = [(x, y)]
        dx, dy = rng.choice(dirs)
        for _ in range(rng.randrange(3, 7)):
            L = rng.randrange(2, 7) * 30
            x, y = x + dx * L, y + dy * L
            pts.append((x, y))
            dx, dy = rng.choice(dirs)
        col = color if rng.random() < 0.7 else color2
        d.line(pts, fill=col + (rng.randrange(70, 150),), width=3, joint="curve")
        d.ellipse([x - 8, y - 8, x + 8, y + 8], outline=col + (210,), width=3)
        d.ellipse([pts[0][0] - 4, pts[0][1] - 4, pts[0][0] + 4, pts[0][1] + 4], fill=col + (220,))
    for _ in range(5):  # chips
        x, y = rng.randrange(0, W - 120), rng.randrange(0, H - 120)
        w, h = rng.randrange(60, 120), rng.randrange(40, 90)
        d.rectangle([x, y, x + w, y + h], outline=color + (150,), width=3)
        for p in range(x + 10, x + w, 16):
            d.line([(p, y), (p, y - 12)], fill=color + (150,), width=2)
            d.line([(p, y + h), (p, y + h + 12)], fill=color + (150,), width=2)
    return layer


def _scanlines(W, H, alpha=26):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for y in range(0, H, 4):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    return layer


def _vignette(W, H):
    k = 8
    w, h = W // k, H // k
    m = Image.new("L", (w, h))
    px = m.load()
    for y in range(h):
        for x in range(w):
            dx, dy = (x / w - 0.5) * 2, (y / h - 0.5) * 2
            dist = math.sqrt(dx * dx * 0.8 + dy * dy)
            px[x, y] = int(max(0.0, min(1.0, (dist - 0.65) / 0.8)) * 190)
    m = m.resize((W, H), Image.BILINEAR)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.putalpha(m)
    return layer


def _hud(W, H, color):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    m, L = 48, 64
    for x, y, sx, sy in [(m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1)]:
        d.line([(x, y), (x + sx * L, y)], fill=color + (220,), width=4)
        d.line([(x, y), (x, y + sy * L)], fill=color + (220,), width=4)
    for i in range(5):  # little segmented status bar, top right
        x0 = W - m - 30 - i * 22
        d.rectangle([x0 - 14, m + 22, x0, m + 30], fill=color + (150 if i < 3 else 60,))
    return layer


# ------------------------------------------------------------ themes
# Each theme returns: img (RGBA background), A/B/C neon colors, and optional
# panel (dark text-panel opacity) and scan (scanline opacity).
def theme_synthwave(W, H, rng):
    A, B, C = rng.choice([((0, 229, 255), (255, 46, 151), (110, 70, 255)),
                          ((0, 255, 190), (255, 120, 70), (170, 90, 255))])
    img = _gradient(W, H, (5, 9, 34), (40, 10, 72))
    img.alpha_composite(_blob(W, H, int(W * rng.uniform(0.75, 0.95)), int(H * rng.uniform(0.10, 0.22)), 400, A, 150))
    img.alpha_composite(_blob(W, H, int(W * rng.uniform(0.0, 0.15)), int(H * rng.uniform(0.65, 0.80)), 430, B, 120))
    img.alpha_composite(_blob(W, H, W // 2, int(H * rng.uniform(0.40, 0.55)), 520, C, 80))
    img.alpha_composite(_stars(W, H, rng))
    img.alpha_composite(_streaks(W, H, rng, A))
    img.alpha_composite(_blob(W, H, 880, 235, 190, B, 170))
    img.alpha_composite(_orb(W, H, 880, 235, 98, C, (255, 255, 255), A))
    img.alpha_composite(_circuits(W, H, rng, A))
    img.alpha_composite(_grid(W, H, B, 1010))
    return {"img": img, "A": A, "B": B, "C": C}


def theme_cyber_city(W, H, rng):
    A, B, C = (0, 235, 255), (255, 50, 170), (150, 80, 255)
    img = _gradient(W, H, (8, 4, 38), (96, 18, 96))
    img.alpha_composite(_blob(W, H, int(W * 0.76), 250, 330, (255, 190, 235), 150))
    img.alpha_composite(_stars(W, H, rng, n=120, sparkles=4))
    d = ImageDraw.Draw(img, "RGBA")
    d.ellipse([W * 0.76 - 120, 130, W * 0.76 + 120, 370], fill=(255, 215, 240, 235))
    img.alpha_composite(_rain(W, H, rng, A))
    win = [A, B, (255, 225, 120)]
    img.alpha_composite(_skyline(W, H, rng, 1200, (34, 16, 76), 180, 420, win, lit=0.22))
    img.alpha_composite(_skyline(W, H, rng, 1250, (12, 8, 34), 90, 250, win, lit=0.30, edge=B))
    img.alpha_composite(_blob(W, H, W // 2, 1250, 480, B, 90))
    return {"img": img, "A": A, "B": B, "C": C, "panel": 135}


def theme_sunset(W, H, rng):
    A, B, C = (255, 205, 80), (255, 70, 150), (140, 70, 255)
    horizon = 1110
    sky = _gradient(W, horizon, (20, 6, 56), (255, 100, 120))
    ground = _gradient(W, H - horizon, (30, 8, 60), (12, 4, 30))
    img = Image.new("RGBA", (W, H))
    img.paste(sky, (0, 0))
    img.paste(ground, (0, horizon))
    img.alpha_composite(_stars(W, H, rng, n=150, sparkles=3))
    img.alpha_composite(_blob(W, H, W // 2, horizon - 100, 460, (255, 130, 90), 130))
    img.alpha_composite(_sun(W, H, W // 2, horizon, 225, (255, 235, 90), (255, 55, 150)))
    img.alpha_composite(_mountains(W, H, rng, horizon, (26, 8, 58), rough=60))
    cover = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cover.paste(ground, (0, horizon))
    img.alpha_composite(cover)
    img.alpha_composite(_grid(W, H, (255, 90, 200), horizon, alpha=110))
    return {"img": img, "A": A, "B": B, "C": C, "panel": 150}


def theme_matrix(W, H, rng):
    A, B, C = (0, 255, 120), (130, 255, 210), (0, 170, 100)
    img = _gradient(W, H, (0, 10, 6), (0, 26, 16))
    img.alpha_composite(_blob(W, H, int(W * rng.uniform(0.2, 0.8)), int(H * 0.3), 420, C, 90))
    img.alpha_composite(_matrix(W, H, rng, A))
    img.alpha_composite(_blob(W, H, W // 2, int(H * 0.9), 450, A, 60))
    return {"img": img, "A": A, "B": B, "C": C, "panel": 175, "scan": 40}


def theme_hologram(W, H, rng):
    A, B, C = (90, 210, 255), (170, 130, 255), (0, 120, 255)
    img = _gradient(W, H, (2, 12, 40), (6, 34, 80))
    img.alpha_composite(_blob(W, H, W // 2, int(H * 0.6), 560, C, 110))
    img.alpha_composite(_stars(W, H, rng, n=90, sparkles=3))
    img.alpha_composite(_hologram(W, H, rng, A, B))
    beam_y = rng.randrange(150, 1100)
    beam = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(beam).rectangle([0, beam_y, W, beam_y + 4], fill=A + (200,))
    img.alpha_composite(beam.filter(ImageFilter.GaussianBlur(6)))
    return {"img": img, "A": A, "B": B, "C": C, "panel": 140}


def theme_aurora(W, H, rng):
    A, B, C = (70, 255, 175), (160, 100, 255), (50, 170, 255)
    img = _gradient(W, H, (3, 6, 26), (10, 24, 46))
    img.alpha_composite(_stars(W, H, rng, n=380, sparkles=6))
    cols = [A, B, C]
    rng.shuffle(cols)
    img.alpha_composite(_aurora(W, H, rng, cols))
    img.alpha_composite(_mountains(W, H, rng, 1210, (6, 9, 22), rough=80, n=12))
    img.alpha_composite(_mountains(W, H, rng, 1270, (3, 5, 14), rough=60, n=10))
    return {"img": img, "A": A, "B": B, "C": C, "panel": 115}


def theme_manga(W, H, rng):
    A, B, C = (255, 220, 40), (255, 60, 130), (90, 70, 255)
    img = _gradient(W, H, (22, 8, 70), (60, 10, 90))
    img.alpha_composite(_blob(W, H, int(W * 0.78), int(H * 0.24), 420, B, 150))
    img.alpha_composite(_burst(W, H, rng, int(W * 0.78), int(H * 0.24), [B, A, (255, 255, 255), (0, 220, 255)]))
    img.alpha_composite(_halftone(W, H, C, 150, (0, H), maxd=1100))
    img.alpha_composite(_halftone(W, H, B, 110, (W, 0), maxd=700, step=22))
    return {"img": img, "A": A, "B": B, "C": C, "panel": 155}


def theme_circuit(W, H, rng):
    A, B, C = (255, 175, 50), (255, 90, 60), (80, 120, 255)
    img = _gradient(W, H, (5, 8, 22), (16, 10, 40))
    img.alpha_composite(_blob(W, H, int(W * 0.8), int(H * 0.8), 460, C, 90))
    img.alpha_composite(_blob(W, H, int(W * 0.15), int(H * 0.2), 380, B, 80))
    img.alpha_composite(_pcb(W, H, rng, A, B))
    return {"img": img, "A": A, "B": B, "C": C, "panel": 165}


THEMES = {
    "synthwave": theme_synthwave,
    "cyber_city": theme_cyber_city,
    "sunset": theme_sunset,
    "matrix": theme_matrix,
    "hologram": theme_hologram,
    "aurora": theme_aurora,
    "manga": theme_manga,
    "circuit": theme_circuit,
}


def pick_theme(history):
    """Random theme, never one of the last 3 used."""
    forced = os.getenv("THEME", "").strip()
    if forced in THEMES:
        return forced
    options = [t for t in THEMES if t not in history[-3:]] or list(THEMES)
    return random.choice(options)


# ------------------------------------------------------------ text
def _fit(meas, text, max_w, max_lines, start, min_size):
    """Shrink the font until the text fits in max_lines."""
    size = start
    while True:
        f = load_font(size)
        lines = wrap_to_width(meas, text, f, max_w)
        if len(lines) <= max_lines or size <= min_size:
            return f, lines, size
        size -= 4


def _glow_text(base, items, font, glow_color, radius=10):
    """items = [(x, y, text)]. Draws a neon glow, then crisp white text on top."""
    W, H = base.size
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for x, y, t in items:
        gd.text((x, y), t, font=font, fill=glow_color + (255,))
    glow = glow.filter(ImageFilter.GaussianBlur(radius))
    base.alpha_composite(glow)
    d = ImageDraw.Draw(base, "RGBA")
    for x, y, t in items:
        d.text((x, y), t, font=font, fill=(255, 255, 255, 255))


def make_image(slot, post, path, theme=None):
    """Sci-fi / anime card (1080x1350). The theme changes with every post."""
    W, H = 1080, 1350
    theme = theme if theme in THEMES else "synthwave"
    rng = random.Random(random.getrandbits(64))
    margin = 90

    # ---- measure text first (the dark panel is sized to it)
    meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    hf, hlines, hsize = _fit(meas, post["headline"], W - 2 * margin - 30, 4, 96, 60)
    h_line = int(hsize * 1.18)
    y0 = 330
    y_h_end = y0 + len(hlines) * h_line
    sf, slines, ssize = _fit(meas, post["subheadline"], W - 2 * margin - 10, 3, 46, 32)
    s_line = int(ssize * 1.4)
    y_sub = y_h_end + 36
    y_end = y_sub + len(slines) * s_line

    # ---- themed background
    bg = THEMES[theme](W, H, rng)
    img, A, B = bg["img"], bg["A"], bg["B"]

    # dark soft panel behind the text for readability
    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(
        [margin - 45, 295, W - margin + 45, y_end + 50], 44, fill=(5, 7, 28, bg.get("panel", 120))
    )
    img.alpha_composite(panel.filter(ImageFilter.GaussianBlur(22)))

    img.alpha_composite(_scanlines(W, H, bg.get("scan", 26)))
    img.alpha_composite(_vignette(W, H))
    img.alpha_composite(_hud(W, H, A))

    # ---- label pill
    label = post.get("label") or ("AI NEWS" if slot == "news" else "TECH IDEA OF THE DAY")
    lf = load_font(42)
    tw = meas.textbbox((0, 0), label, font=lf)[2]
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle([margin, 110, margin + tw + 60, 182], 36, fill=(0, 0, 0, 130), outline=A + (255,), width=3)
    _glow_text(img, [(margin + 30, 120, label)], lf, A, radius=8)

    # ---- headline with neon accent bar
    bar = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    bar_top, bar_bot = y0 + 8, y_h_end - 10
    for y in range(bar_top, bar_bot):
        t = (y - bar_top) / max(1, bar_bot - bar_top)
        col = tuple(int(B[j] + (A[j] - B[j]) * t) for j in range(3))
        bd.line([(margin - 34, y), (margin - 26, y)], fill=col + (255,))
    img.alpha_composite(bar.filter(ImageFilter.GaussianBlur(5)))
    img.alpha_composite(bar)
    _glow_text(img, [(margin, y0 + i * h_line, ln) for i, ln in enumerate(hlines)], hf, A, radius=12)

    # ---- subheadline
    d = ImageDraw.Draw(img, "RGBA")
    for i, ln in enumerate(slines):
        d.text((margin, y_sub + i * s_line), ln, font=sf, fill=(220, 228, 255, 255))

    # ---- footer
    d.text((margin, H - 140), os.getenv("IG_HANDLE", ""), font=load_font(40), fill=(255, 255, 255, 255))

    img.convert("RGB").save(path, "JPEG", quality=93, subsampling=0)


# ---------------------------------------------------------------- reel with voice
VOICES = {  # (edge-tts voices, gTTS lang, gTTS tld). Override with the VOICE variable.
    "english": (["en-US-AndrewMultilingualNeural"], "en", "co.in"),
    "hinglish": (["hi-IN-SwaraNeural", "hi-IN-MadhurNeural"], "hi", "co.in"),
    "hindi": (["hi-IN-SwaraNeural", "hi-IN-MadhurNeural"], "hi", "co.in"),
    "marathi": (["mr-IN-AarohiNeural", "mr-IN-ManoharNeural"], "mr", "co.in"),
}


def _speech_text(post):
    t = post.get("headline", "").strip().rstrip(".") + ". " + post.get("caption", "")
    t = re.sub(r"https?://\S+|#\w+", " ", t)
    t = re.sub(r"[^\w\s.,!?'\u0900-\u097F-]", " ", t)  # drop emojis and symbols
    return re.sub(r"\s+", " ", t).strip()


def make_voice(text, out_path):
    """Free TTS: edge-tts (tries the voices in order), gTTS as backup. Returns True if a voice file was made."""
    voices, gl, tld = VOICES.get(LANGUAGE.lower(), VOICES["english"])
    forced = os.getenv("VOICE")
    order = [forced] if forced else voices
    try:
        import asyncio
        import edge_tts
        for voice in order:
            try:
                asyncio.run(asyncio.wait_for(
                    edge_tts.Communicate(text, voice, rate="+0%").save(str(out_path)), 90))
                if Path(out_path).stat().st_size > 2000:
                    print(f"(voice: {voice})", file=sys.stderr)
                    return True
            except Exception as ex:
                print(f"edge-tts {voice} failed: {ex}", file=sys.stderr)
    except Exception as ex:
        print(f"edge-tts unavailable: {ex}", file=sys.stderr)
    try:
        from gtts import gTTS
        gTTS(text, lang=gl, tld=tld).save(str(out_path))
        return Path(out_path).stat().st_size > 2000
    except Exception as ex:
        print(f"gTTS failed: {ex}", file=sys.stderr)
    return False


def _duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def _subtitle_png(text, path):
    f = load_font(58)
    meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap_to_width(meas, text, f, 940)[:3]
    h = 76 * len(lines) + 40
    im = Image.new("RGBA", (1000, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, 999, h - 1], 36, fill=(0, 0, 0, 150))
    for i, ln in enumerate(lines):
        w = meas.textbbox((0, 0), ln, font=f)[2]
        d.text(((1000 - w) // 2, 20 + i * 76), ln, font=f, fill=(255, 255, 255, 255),
               stroke_width=3, stroke_fill=(0, 0, 0, 255))
    im.save(path)


def make_reel_voice(img_path, out_path, post):
    """Image card + spoken voiceover + word-timed subtitles + soft generated background music."""
    text = _speech_text(post)
    voice = Path(out_path).with_suffix(".voice.mp3")
    if not make_voice(text, voice):
        raise RuntimeError("no text-to-speech available")
    lead, tail = 0.8, 1.6
    vdur = _duration(voice)
    total = min(lead + vdur + tail, 85)

    # subtitle chunks (~6 words), timed by their share of the speech length
    words = text.split()
    chunks = [" ".join(words[i:i + 6]) for i in range(0, len(words), 6)]
    weights = [max(1, len(c)) for c in chunks]
    tw, t, subs = sum(weights), lead, []
    for c, wgt in zip(chunks, weights):
        dur = vdur * wgt / tw
        p = Path(out_path).with_suffix(f".sub{len(subs)}.png")
        _subtitle_png(c, p)
        subs.append((p, t, t + dur))
        t += dur

    fg = "scale=w='trunc(1080*(1+0.05*t/%d)/2)*2':h=-2:eval=frame" % int(total)
    fc = [
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=40:6,eq=brightness=-0.18[bg]",
        f"[0:v]{fg}[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2-80,fade=t=in:st=0:d=0.5[v0]",
    ]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30", "-i", str(img_path),
           "-i", str(voice),
           "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*110*t)+0.35*sin(2*PI*164.8*t)+0.3*sin(2*PI*220*t)+0.2*sin(2*PI*277.2*t)':s=44100:d={total:.2f}"]
    last = "v0"
    for i, (p, a, b) in enumerate(subs):
        cmd += ["-i", str(p)]
        fc.append(f"[{last}][{3 + i}:v]overlay=(W-w)/2:1640:enable='between(t,{a:.2f},{b:.2f})'[s{i}]")
        last = f"s{i}"
    fc.append(f"[{last}]format=yuv420p[v]")
    fc.append(f"[1:a]adelay={int(lead * 1000)}|{int(lead * 1000)},volume=1.6[va]")
    fc.append(f"[2:a]lowpass=f=700,tremolo=f=0.25:d=0.4,volume=0.10,afade=t=in:d=2,afade=t=out:st={total - 2:.2f}:d=2[ma]")
    fc.append("[va][ma]amix=inputs=2:duration=longest:dropout_transition=0,volume=2[a]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-r", "30",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", str(out_path)]
    try:
        subprocess.run(cmd, check=True)
    finally:
        for p, _, _ in subs:
            p.unlink(missing_ok=True)
        voice.unlink(missing_ok=True)


# ---------------------------------------------------------------- AI art (cinematic background)
# Same look as big space/science pages: a dramatic AI-generated picture, big headline at the bottom.
# Free generators are tried in order; if none works the post falls back to stock video / the Pillow themes.
ART_STYLE = ("photorealistic cinematic still, ultra realistic, highly detailed textures, volumetric lighting, "
             "dramatic contrast, shot on 35mm anamorphic lens, 8k, vertical composition, "
             "no text, no letters, no logos, no watermark")
ART_LOOKS = [  # one is picked at random per post so the pictures do not all look alike
    "deep space backdrop, blue and violet nebula glow", "golden hour rim light, orange and teal colour grade",
    "dark industrial environment, cold blue neon accents", "bright lens flare, white and cyan highlights",
    "misty atmosphere, red and magenta light", "night scene, glowing particles in the air",
    "dramatic low angle, hard shadows", "wide establishing shot, enormous scale, tiny details",
    "extreme close-up macro, shallow depth of field", "top-down view, symmetrical composition",
]
LAST_ART = {}
ART_FALLBACK_SUBJECT = {
    "ROBOTICS": "a sleek advanced humanoid robot in a dark futuristic laboratory, glowing blue eyes",
    "ELECTRONICS": "a glowing microchip city, macro view of a circuit board with neon light trails",
    "MECHATRONICS": "a giant robotic arm assembling machinery in a dark factory, sparks and neon light",
    "SCI-FI TECH": "a futuristic spacecraft flying near a black hole, accretion disk glowing orange",
    "NEW INNOVATION": "a glowing futuristic prototype machine on a dark stage, light beams",
    "FUTURE TECH": "a futuristic megacity at night with flying vehicles and neon holograms",
    "AI NEWS": "a glowing artificial brain made of light and circuits floating in dark space",
    "TECH IDEA OF THE DAY": "a futuristic invention glowing in a dark workshop, holographic blueprints",
}


def build_art_prompt(post, label, recent=None):
    """Ask the LLM for one vivid image prompt (no real people, brands or text); fall back to a template."""
    subject = ART_FALLBACK_SUBJECT.get(label, "a futuristic technology scene, neon glow, dark background")
    try:
        q = ("Write ONE image-generation prompt (max 45 words) for a dramatic, cinematic, photorealistic digital "
             "artwork that visually represents this social media post. Show a striking scene or object (spacecraft, robot, "
             "planet, machine, circuit, black hole, futuristic city...). No real people, no celebrities, no brand names, "
             "no logos, no text or letters in the image.\n"
             f"Headline: {post.get('headline', '')}\nContext: {post.get('caption', '')[:300]}\n"
             + ("Do NOT repeat the subject or composition of these recent pictures: " + " | ".join(recent[-8:]) + "\n" if recent else "")
             + "Be specific and original: a different subject, camera angle and mood every time.\n"
             'Return JSON only: {"prompt": "..."}')
        p = _ask(q).get("prompt", "").strip()
        if 15 < len(p) < 500:
            subject = p
    except Exception as ex:
        print(f"art prompt: using template ({str(ex)[:80]})", file=sys.stderr)
    LAST_ART["subject"] = subject
    return f"{subject}, {random.choice(ART_LOOKS)}, {ART_STYLE}"


def _valid_image(path):
    try:
        with Image.open(path) as im:
            im.load()
            return im.size[0] >= 512 and Path(path).stat().st_size > 30000
    except Exception:
        return False


def _art_pollinations(prompt, dest):
    from urllib.parse import quote
    for model in ("flux", "turbo"):
        url = (f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=1080&height=1920&model={model}"
               f"&nologo=true&enhance=false&seed={random.randrange(10**6)}")
        try:
            r = requests.get(url, timeout=150)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                Path(dest).write_bytes(r.content)
                if _valid_image(dest):
                    return True
            else:
                print(f"pollinations {model}: HTTP {r.status_code}", file=sys.stderr)
        except Exception as ex:
            print(f"pollinations {model}: {str(ex)[:100]}", file=sys.stderr)
    return False


def _art_huggingface(prompt, dest):
    """Optional: free Hugging Face token (HF_TOKEN1) -> FLUX.1-schnell, very realistic."""
    tok = os.getenv("HF_TOKEN1", "").strip()
    if not tok:
        return False
    for model in ("black-forest-labs/FLUX.1-schnell", "stabilityai/stable-diffusion-xl-base-1.0"):
        try:
            r = requests.post(f"https://router.huggingface.co/hf-inference/models/{model}",
                              headers={"Authorization": f"Bearer {tok}"}, timeout=150,
                              json={"inputs": prompt, "parameters": {"width": 768, "height": 1344}})
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                Path(dest).write_bytes(r.content)
                if _valid_image(dest):
                    return True
            else:
                print(f"huggingface {model}: HTTP {r.status_code} {r.text[:80]}", file=sys.stderr)
        except Exception as ex:
            print(f"huggingface {model}: {str(ex)[:100]}", file=sys.stderr)
    return False


def _art_gemini(prompt, dest):
    """Gemini image models (works only if the key's plan allows image output)."""
    import base64
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        return False
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key, "pageSize": 200}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "image" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])
                 and "imagen" not in m["name"]]
    except Exception as ex:
        print(f"gemini image list: {ex}", file=sys.stderr)
        return False
    for name in names[:3]:
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent",
                              params={"key": key}, timeout=120,
                              json={"contents": [{"parts": [{"text": prompt + ". Vertical 9:16 image."}]}],
                                    "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}})
            if r.status_code != 200:
                print(f"gemini image {name}: HTTP {r.status_code}", file=sys.stderr)
                continue
            for part in r.json()["candidates"][0]["content"]["parts"]:
                data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
                if data:
                    Path(dest).write_bytes(base64.b64decode(data))
                    if _valid_image(dest):
                        return True
        except Exception as ex:
            print(f"gemini image {name}: {str(ex)[:100]}", file=sys.stderr)
    return False


def generate_art(post, label, dest, recent=None):
    """Returns True when a portrait AI picture was saved to dest (resized to 1080x1920)."""
    if (os.getenv("ART") or "on").lower() == "off":
        return False
    prompt = build_art_prompt(post, label, recent)
    print("ART prompt:", prompt[:160])
    for fn in (_art_huggingface, _art_pollinations, _art_gemini):
        try:
            if fn(prompt, dest):
                im = Image.open(dest).convert("RGB")
                if fn is _art_pollinations:   # the free service stamps a small logo in the bottom corner: cut it off
                    im = im.crop((0, 0, im.size[0], int(im.size[1] * 0.93)))
                w, h = im.size
                scale = max(1080 / w, 1920 / h)
                im = im.resize((int(w * scale + 1), int(h * scale + 1)), Image.LANCZOS)
                l, t = (im.size[0] - 1080) // 2, (im.size[1] - 1920) // 2
                im.crop((l, t, l + 1080, t + 1920)).save(dest, "JPEG", quality=94)
                return True
        except Exception as ex:
            print(f"{fn.__name__}: {ex}", file=sys.stderr)
    return False


ACCENTS = [(0, 229, 255), (255, 196, 0), (255, 95, 70), (130, 255, 170), (200, 120, 255)]


def make_art_image(post, art_path, path, label):
    """Feed post (1080x1350): the AI picture full-bleed, big headline at the bottom, label badge, handle."""
    W, H = 1080, 1350
    art = Image.open(art_path).convert("RGB")
    top = (art.size[1] - H) // 2 - 80
    img = art.crop((0, max(0, top), W, max(0, top) + H)).convert("RGBA")
    accent = random.choice(ACCENTS)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for y in range(H):
        a = int(235 * min(1, max(0, (y - 520) / 700)) ** 1.4) if y > 520 else 0
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    img.alpha_composite(shade)
    meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f, lines, size = _fit(meas, post["headline"], 940, 4, 84, 52)
    lh = int(size * 1.16)
    words = post["headline"].split()
    cut = len(words) - max(2, int(len(words) * 0.4)) if len(words) > 3 else len(words)
    d = ImageDraw.Draw(img, "RGBA")
    y = H - 110 - lh * len(lines)
    idx = 0
    for i, ln in enumerate(lines):
        x = 70
        for w in ln.split():
            col = (255, 255, 255, 255) if idx < cut else accent + (255,)
            d.text((x, y + i * lh), w, font=f, fill=col, stroke_width=5, stroke_fill=(0, 0, 0, 255))
            x += meas.textbbox((0, 0), w + " ", font=f)[2]
            idx += 1
    lf = load_font(36)
    tw = meas.textbbox((0, 0), label, font=lf)[2]
    d.rounded_rectangle([60, 56, 60 + tw + 52, 124], 34, fill=(0, 0, 0, 150), outline=accent + (255,), width=3)
    d.text((86, 66), label, font=lf, fill=(255, 255, 255, 255))
    d.text((70, H - 78), os.getenv("IG_HANDLE", ""), font=load_font(34), fill=(255, 255, 255, 220),
           stroke_width=2, stroke_fill=(0, 0, 0, 200))
    img.convert("RGB").save(path, "JPEG", quality=93, subsampling=0)


# ---------------------------------------------------------------- cinematic reel (real motion)
# Look: full-screen moving background, big bold headline in the lower third (last words in the accent
# colour), small label badge, word-timed subtitles, voice-over and soft music.
# Background = free Pexels stock video when PEXELS_API_KEY is set (licence: free to use, no credit needed),
# otherwise the animated sci-fi theme (drifting camera, moving particles, film grain, light pulse).
VIDEO_QUERIES = {
    "ROBOTICS": ["humanoid robot", "robot arm", "robotics technology", "futuristic robot"],
    "ELECTRONICS": ["circuit board", "microchip", "electronics technology", "printed circuit board macro"],
    "MECHATRONICS": ["robotic arm factory", "automation machine", "gears machinery", "3d printer"],
    "SCI-FI TECH": ["galaxy", "nebula", "futuristic city night", "hologram", "space"],
    "NEW INNOVATION": ["futuristic technology", "laboratory science", "hologram interface", "innovation technology"],
    "FUTURE TECH": ["futuristic city", "technology abstract", "digital particles", "neon lights"],
    "AI NEWS": ["artificial intelligence", "neural network", "data center", "digital code"],
    "TECH IDEA OF THE DAY": ["futuristic technology", "innovation", "hologram", "digital interface"],
}


CREDITS = {}  # credit line for the background clip (Pexels asks for one)


def _download(url, dest):
    with requests.get(url, stream=True, timeout=120) as dl:
        dl.raise_for_status()
        with open(dest, "wb") as fh:
            for part in dl.iter_content(1 << 20):
                fh.write(part)
    return Path(dest).stat().st_size > 50000


def _from_pexels(queries, dest, used):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key:
        return None, None
    for q in queries:
        params = {"query": q, "orientation": "portrait", "size": "medium", "per_page": 20}
        r = requests.get("https://api.pexels.com/v1/videos/search", headers={"Authorization": key},
                         params=params, timeout=30)
        if r.status_code == 404:  # older path, in case the new one is not available
            r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                             params=params, timeout=30)
        r.raise_for_status()
        vids = [v for v in r.json().get("videos", []) if f"px{v['id']}" not in used and v.get("duration", 0) >= 6]
        random.shuffle(vids)
        for v in vids:
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"
                     and 700 <= (f.get("width") or 0) <= 1500]
            if files:
                files.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
                if _download(files[0]["link"], dest):
                    CREDITS["video"] = f"Video: {(v.get('user') or {}).get('name', 'Pexels creator')} on Pexels"
                    return Path(dest), f"px{v['id']}"
    return None, None


def _from_pixabay(queries, dest, used):
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key:
        return None, None
    for q in queries:
        r = requests.get("https://pixabay.com/api/videos/", params={"key": key, "q": q, "per_page": 30,
                         "safesearch": "true"}, timeout=30)
        r.raise_for_status()
        hits = [h for h in r.json().get("hits", []) if f"pb{h['id']}" not in used and h.get("duration", 0) >= 6]
        random.shuffle(hits)
        for h in hits:
            for size in ("medium", "small"):  # "large" is 4K: too heavy
                v = (h.get("videos") or {}).get(size) or {}
                if v.get("url") and 0 < v.get("size", 0) < 45_000_000 and _download(v["url"], dest):
                    CREDITS["video"] = f"Video: {h.get('user', 'Pixabay creator')} on Pixabay"
                    return Path(dest), f"pb{h['id']}"
    return None, None


def _from_nasa(queries, dest, used):
    """NASA Image and Video Library: public domain, no key needed. Space-related labels only."""
    for q in queries:
        r = requests.get("https://images-api.nasa.gov/search", params={"q": q, "media_type": "video"}, timeout=30)
        r.raise_for_status()
        items = r.json().get("collection", {}).get("items", [])
        random.shuffle(items)
        for it in items[:12]:
            nid = (it.get("data") or [{}])[0].get("nasa_id", "")
            if not nid or f"na{nid}" in used:
                continue
            files = requests.get(it["href"], timeout=30).json()
            mp4 = [u for u in files if u.lower().endswith(".mp4")]
            pick = next((u for u in mp4 if "~medium" in u), None) or next((u for u in mp4 if "~small" in u), None)
            if pick and _download(pick.replace("http://", "https://"), dest):
                return Path(dest), f"na{nid}"
    return None, None


def _stock_video(label, dest, used):
    """Free stock/archive clip for the background. Returns (path, id) or (None, None)."""
    queries = list(VIDEO_QUERIES.get(label) or ["futuristic technology", "technology abstract"])
    random.shuffle(queries)
    queries = queries[:3]
    providers = [_from_pexels, _from_pixabay]
    if label in ("SCI-FI TECH", "FUTURE TECH"):
        providers.append(_from_nasa)
    for fn in providers:
        try:
            path, vid = fn(queries, dest, used)
            if path:
                return path, vid
        except Exception as ex:
            print(f"{fn.__name__}: {ex}", file=sys.stderr)
    return None, None


def _particle_layer(path, seed):
    """1080x3840 transparent strip of drifting particles; the top and bottom halves are identical so it loops."""
    rng = random.Random(seed)
    half = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    d = ImageDraw.Draw(half)
    for _ in range(170):
        x, y = rng.randrange(1080), rng.randrange(1920)
        r = rng.choice([1, 1, 2, 2, 3, 4])
        a = rng.randrange(70, 230)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    half = half.filter(ImageFilter.GaussianBlur(0.8))
    full = Image.new("RGBA", (1080, 3840), (0, 0, 0, 0))
    full.paste(half, (0, 0))
    full.paste(half, (0, 1920))
    full.save(path)


def _shade_png(path):
    """Dark gradient over the lower part of the frame so the headline is always readable."""
    im = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(1920):
        a = 0
        if y > 760:
            a = int(225 * min(1, (y - 760) / 760) ** 1.5)
        if y < 380:
            a = max(a, int(110 * (1 - y / 380)))
        d.line([(0, y), (1080, y)], fill=(0, 0, 0, a))
    im.save(path)


def _headline_png(text, accent, path, bottom=1330):
    """Bold headline, left aligned; the last ~40% of the words use the accent colour. Returns the block's top y."""
    meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f, lines, size = _fit(meas, text, 880, 5, 88, 54)
    lh = int(size * 1.16)
    words = text.split()
    cut = len(words) - max(2, int(len(words) * 0.4)) if len(words) > 3 else len(words)
    h = lh * len(lines) + 30
    im = Image.new("RGBA", (1080, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    idx = 0
    for i, ln in enumerate(lines):
        x = 70
        for w in ln.split():
            col = (255, 255, 255, 255) if idx < cut else tuple(accent) + (255,)
            d.text((x, 10 + i * lh), w, font=f, fill=col, stroke_width=5, stroke_fill=(0, 0, 0, 255))
            x += meas.textbbox((0, 0), w + " ", font=f)[2]
            idx += 1
    im.save(path)
    return bottom - h, h


def _badge_png(label, handle, accent, path):
    f, f2 = load_font(42), load_font(36)
    meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    tw = meas.textbbox((0, 0), label, font=f)[2]
    im = Image.new("RGBA", (1080, 150), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([70, 6, 70 + tw + 56, 76], 35, fill=(0, 0, 0, 150), outline=tuple(accent) + (255,), width=3)
    d.text((98, 14), label, font=f, fill=(255, 255, 255, 255))
    if handle:
        d.text((74, 92), handle, font=f2, fill=(255, 255, 255, 215), stroke_width=2, stroke_fill=(0, 0, 0, 200))
    im.save(path)


def _reel_sub_png(text, path):
    f = load_font(50)
    meas = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap_to_width(meas, text, f, 840)[:2]
    h = 64 * len(lines) + 30
    im = Image.new("RGBA", (1000, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, 999, h - 1], 30, fill=(0, 0, 0, 120))
    for i, ln in enumerate(lines):
        w = meas.textbbox((0, 0), ln, font=f)[2]
        d.text(((1000 - w) // 2, 14 + i * 64), ln, font=f, fill=(255, 255, 255, 255),
               stroke_width=3, stroke_fill=(0, 0, 0, 255))
    im.save(path)


def _short_speech(post, max_words=70):
    """Headline + caption, cut at a sentence end so the Reel stays short (better retention)."""
    t = _speech_text(post)
    w = t.split()
    if len(w) <= max_words:
        return t
    cut = " ".join(w[:max_words])
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > 120 else cut


def make_reel_cinematic(post, out_path, theme, state=None, label=None, art_path=None):
    """Motion-graphics Reel (1080x1920). Returns the stock-video id used, or '' for the animated theme."""
    work = Path(out_path).with_suffix("")
    text = _short_speech(post)
    voice = Path(str(work) + ".voice.mp3")
    have_voice = make_voice(text, voice)
    lead, tail = 0.8, 1.6
    vdur = _duration(voice) if have_voice else 0
    total = min(lead + (vdur if have_voice else 12) + tail, 60)
    label = label or post.get("label") or "TECH"
    used = (state or {}).get("videos", [])

    temps = []

    def tmp(name):
        p = Path(str(work) + name)
        temps.append(p)
        return p

    clip, clip_id = (None, None) if art_path else _stock_video(label, tmp(".clip.mp4"), used)
    rng = random.Random(random.getrandbits(64))
    bg = THEMES.get(theme, theme_synthwave)(1080, 1350, rng)
    accent = bg["A"]
    if art_path:   # AI picture: slow push-in + drift, drifting particles, light pulse, film grain
        card = tmp(".art.png")
        Image.open(art_path).convert("RGB").resize((1296, 2304), Image.LANCZOS).save(card)
        parts = tmp(".parts.png")
        _particle_layer(parts, random.getrandbits(32))
        inputs = ["-loop", "1", "-framerate", "30", "-i", str(card)]
        bg_chain = (f"[0:v]scale=w='trunc(1080*(1+0.17*t/{total:.1f})/2)*2':h=-2:eval=frame,"
                    "crop=1080:1920:x='(in_w-1080)/2+28*sin(t*0.55)':y='(in_h-1920)/2+18*sin(t*0.4)',fps=30[b0];"
                    "[b0][PARTS:v]overlay=x=0:y='-mod(t*55,1920)'[b1];"
                    "[b1]eq=brightness='0.035*sin(t*1.5)':contrast=1.06:saturation=1.12:eval=frame[bgv]")
        extra_in, n_extra = ["-loop", "1", "-framerate", "30", "-i", str(parts)], 1
    elif clip:
        inputs = ["-stream_loop", "-1", "-i", str(clip)]
        bg_chain = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,"
                    "eq=brightness=-0.10:saturation=1.15[bgv]")
        extra_in, n_extra = [], 0
    else:
        card = tmp(".bg.png")
        bg["img"].convert("RGB").resize((1536, 1920), Image.LANCZOS).save(card)
        parts = tmp(".parts.png")
        _particle_layer(parts, random.getrandbits(32))
        inputs = ["-loop", "1", "-framerate", "30", "-i", str(card)]
        bg_chain = ("[0:v]crop=1080:1920:x='(in_w-1080)/2*(1+sin(t*0.45))':y=0,fps=30[b0];"
                    f"[b0][PARTS:v]overlay=x=0:y='-mod(t*70,1920)'[b1];"
                    "[b1]eq=brightness='0.04*sin(t*1.7)':eval=frame[bgv]")
        extra_in, n_extra = ["-loop", "1", "-framerate", "30", "-i", str(parts)], 1

    shade, head, badge = tmp(".shade.png"), tmp(".head.png"), tmp(".badge.png")
    _shade_png(shade)
    htop, _ = _headline_png(post["headline"], accent, head)
    _badge_png(label, os.getenv("IG_HANDLE", ""), accent, badge)

    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs                      # 0 background
    n = 1
    if n_extra:
        cmd += extra_in                                                         # 1 particles
        n += 1
    ivoice = n
    if have_voice:
        cmd += ["-i", str(voice)]
        n += 1
    imusic = n
    cmd += ["-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*110*t)+0.35*sin(2*PI*164.8*t)+0.3*sin(2*PI*220*t)+0.2*sin(2*PI*277.2*t)':s=44100:d={total:.2f}"]
    n += 1
    for p in (shade, head, badge):
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{total:.2f}", "-i", str(p)]
    ishade, ihead, ibadge = n, n + 1, n + 2
    n += 3

    fc = [bg_chain.replace("PARTS", "1") if n_extra else bg_chain,
          "[bgv]noise=alls=7:allf=t,vignette=PI/5[bgn]",
          f"[bgn][{ishade}:v]overlay=0:0[l1]",
          f"[{ihead}:v]format=rgba,fade=t=in:st=0.35:d=0.7:alpha=1[hd]",
          f"[l1][hd]overlay=x=0:y='{htop}+50*(1-min(max(t-0.35,0)/0.7,1))'[l2]",
          f"[{ibadge}:v]format=rgba,fade=t=in:st=0.1:d=0.5:alpha=1[bd]",
          "[l2][bd]overlay=x=0:y=270[l3]"]
    last = "l3"
    if have_voice:   # word-timed subtitles under the headline
        words = text.split()
        chunks = [" ".join(words[i:i + 5]) for i in range(0, len(words), 5)]
        weights = [max(1, len(c)) for c in chunks]
        tw, t = sum(weights), lead
        for k, (c, wg) in enumerate(zip(chunks, weights)):
            dur = vdur * wg / tw
            p = tmp(f".sub{k}.png")
            _reel_sub_png(c, p)
            cmd += ["-i", str(p)]
            fc.append(f"[{last}][{n}:v]overlay=(W-w)/2:1385:enable='between(t,{t:.2f},{t + dur:.2f})'[s{k}]")
            last = f"s{k}"
            n += 1
            t += dur
    fc.append(f"[{last}]fade=t=in:st=0:d=0.4,fade=t=out:st={total - 0.6:.2f}:d=0.6,format=yuv420p[v]")
    music = f"[{imusic}:a]lowpass=f=700,tremolo=f=0.25:d=0.4,volume={0.10 if have_voice else 0.35},afade=t=in:d=2,afade=t=out:st={total - 2:.2f}:d=2"
    if have_voice:
        fc.append(f"[{ivoice}:a]adelay={int(lead * 1000)}|{int(lead * 1000)},volume=1.6[va]")
        fc.append(music + "[ma]")
        fc.append("[va][ma]amix=inputs=2:duration=longest:dropout_transition=0,volume=2[a]")
    else:
        fc.append(music + "[a]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", "-r", "30",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", str(out_path)]
    try:
        subprocess.run(cmd, check=True)
    finally:
        for p in temps + [voice]:
            Path(p).unlink(missing_ok=True)
    return clip_id or ""


# ---------------------------------------------------------------- hosting
def make_reel(img_path, out_path, seconds=8):
    """Image -> 1080x1920 MP4: blurred background, card slowly zooming in, silent audio track."""
    fg = "scale=w='trunc(1080*(1+0.05*t/%d)/2)*2':h=-2:eval=frame" % seconds
    fc = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=40:6,eq=brightness=-0.18[bg];"
        f"[0:v]{fg}[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,fade=t=in:st=0:d=0.5,format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30", "-i", str(img_path),
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-filter_complex", fc,
           "-map", "[v]", "-map", "1:a", "-t", str(seconds), "-r", "30",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "96k", "-shortest", str(out_path)]
    subprocess.run(cmd, check=True)


def upload_video(path):
    cloud = os.environ["CLOUDINARY_CLOUD_NAME"]
    preset = os.environ["CLOUDINARY_UPLOAD_PRESET"]
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud}/video/upload",
            data={"upload_preset": preset},
            files={"file": f},
            timeout=180,
        )
    if not r.ok:
        raise RuntimeError(f"video upload failed: {r.text[:300]}")
    return r.json()["secure_url"]


def upload_image(path):
    """Instagram needs a public image URL. Cloudinary free tier is used."""
    cloud = os.environ["CLOUDINARY_CLOUD_NAME"]
    preset = os.environ["CLOUDINARY_UPLOAD_PRESET"]  # must be an UNSIGNED preset
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud}/image/upload",
            data={"upload_preset": preset},
            files={"file": f},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()["secure_url"]


# ---------------------------------------------------------------- instagram
def get_token():
    data = load_json(TOKEN_FILE, {})
    return data.get("access_token") or os.environ["IG_ACCESS_TOKEN"]


def diagnose():
    """Safe checks (never prints the token): is the token valid, and how much of the publish quota is used?"""
    token, ig_id = get_token(), os.environ["IG_USER_ID"]
    for label, path, fields in [
        ("account", "me", "user_id,username,account_type,media_count"),
        ("publish quota", f"{ig_id}/content_publishing_limit", "quota_usage,config"),
        ("recent media", f"{ig_id}/media", "id,timestamp,media_type"),
    ]:
        r = requests.get(f"{GRAPH}/{path}", params={"fields": fields, "limit": 5, "access_token": token}, timeout=30)
        print(f"--- {label}: HTTP {r.status_code}")
        print(r.text[:700])


def refresh_token():
    token = get_token()
    r = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    save_json(TOKEN_FILE, {"access_token": j["access_token"], "refreshed": datetime.now().isoformat()})
    # GitHub Actions: write the new token to a file so the workflow can store it as a secret
    out = os.getenv("NEW_TOKEN_FILE")
    if out:
        Path(out).write_text(j["access_token"], encoding="utf-8")
    print("Token refreshed, expires in days:", j.get("expires_in", 0) // 86400)


def publish(media_url, caption, reel=False):
    token = get_token()
    ig_id = os.environ["IG_USER_ID"]
    if reel:
        payload = {"media_type": "REELS", "video_url": media_url, "share_to_feed": "true"}
    else:
        payload = {"image_url": media_url}
    r = requests.post(
        f"{GRAPH}/{ig_id}/media",
        data={**payload, "caption": caption, "access_token": token},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"create container failed: {r.text}")
    creation_id = r.json()["id"]

    for _ in range(60 if reel else 20):  # wait until container is ready (videos take longer)
        s = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        ).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"container error: {s}")
        time.sleep(5 if reel else 3)

    r = requests.post(
        f"{GRAPH}/{ig_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"publish failed: {r.text}")
    return r.json()["id"]


# ---------------------------------------------------------------- duplicate guard
_STOP = set("this that with from have been will your they their about which would could there what when where while into "
            "than then them more most also just like only over each such very much many some other these those because "
            "being does doing make makes made using used uses based after before between under again further both same "
            "ever new can how why its are was were has had not but for and the you our out all any may one two".split())


def _tokens(text):
    w = re.findall(r"[a-z0-9]+", text.lower().replace("-", " "))
    return {x for x in w if x not in _STOP and (len(x) >= 4 or any(c.isdigit() for c in x))}


def find_duplicate(post, past_texts):
    """Same story posted before (even from another source)? Returns (score, shared words) or None."""
    mine = _tokens(f"{post['headline']} {post['subheadline']} {post['caption']}")
    for old in past_texts:
        theirs = _tokens(old)
        shared = mine & theirs
        if not shared or min(len(mine), len(theirs)) < 6:
            continue
        score = len(shared) / min(len(mine), len(theirs))
        numbers = [x for x in shared if any(c.isdigit() for c in x)]
        if (score >= 0.25 and len(shared) >= 4) or (len(numbers) >= 2 and len(shared) >= 3):
            return score, ", ".join(sorted(shared)[:8])
    return None


def ig_recent_captions(limit=25):
    """Last captions really published on the account (best effort; empty list on any problem)."""
    try:
        r = requests.get(f"{GRAPH}/{os.environ['IG_USER_ID']}/media",
                         params={"fields": "caption", "limit": limit, "access_token": get_token()}, timeout=30)
        return [m.get("caption", "") for m in r.json().get("data", []) if m.get("caption")]
    except Exception as ex:
        print(f"could not read recent Instagram posts: {ex}", file=sys.stderr)
        return []


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["news", "idea", "extra"])
    ap.add_argument("--gate", action="store_true", help="extra slot: post only at random (scheduled runs)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reel", action="store_true", help="post as a Reel (video) instead of an image")
    ap.add_argument("--refresh-token", action="store_true")
    ap.add_argument("--diagnose", action="store_true", help="check the Instagram token and publish quota")
    args = ap.parse_args()
    print(f"main.py version {VERSION}", file=sys.stderr)

    if args.diagnose:
        diagnose()
        return
    if args.refresh_token:
        refresh_token()
        return
    if not args.slot:
        ap.error("--slot is required")

    slot = args.slot
    stage = "ai"
    if slot == "extra" and args.gate and not extra_gate():
        return
    try:
        state = load_state()
        ig_texts = ig_recent_captions()  # what is really on Instagram (does not depend on the Git state)
        recent = state["headlines"][-25:] + [t.split("\n")[0][:140] for t in ig_texts[:10]]
        items, label, pool_all = [], None, []
        if slot == "extra":
            label = random.choice(list(TOPICS))
            topic_text, kws = TOPICS[label]
            print("EXTRA topic:", label, file=sys.stderr)
            if random.random() < 0.6:  # 60% real recent news, 40% explainer
                pool_all = pick_items(fetch_items(EXTRA_FEEDS, 8), state["links"], 60)
                pool_all = [i for i in pool_all if any(k in (i["title"] + " " + i["summary"]).lower() for k in kws)][:8]
        else:
            pool_all = pick_items(fetch_items(), state["links"], 8 if slot == "news" else 6)
            if not pool_all:
                raise RuntimeError("no fresh items found in feeds")

        post = None
        for attempt in range(3):
            if slot == "extra" and len(pool_all) < 2:
                items = []
                post = generate_concept_post(topic_text, recent)
            elif slot == "extra":
                items = pool_all
                post = generate_news_post(items, recent)
            else:
                items = pool_all
                post = generate_post(slot, items, recent)
            if label:
                post["label"] = label
            dup = find_duplicate(post, state["headlines"] + ig_texts)
            if not dup:
                break
            print(f"DUPLICATE topic (overlap {dup[0]:.2f}: {dup[1]}); trying another ({attempt + 1}/3)", file=sys.stderr)
            if post.get("source_link"):
                state["links"].append(post["source_link"])  # never pick this story again
                pool_all = [i for i in pool_all if i["link"] != post["source_link"]]
            recent = recent + [f"{post['headline']} - {post['subheadline']}"]
            post = None
            if slot != "extra" and not pool_all:
                break
        if post is None:
            print("SKIP: every candidate story was already posted.", file=sys.stderr)
            log(slot, "SKIP", "all candidates duplicate")
            return

        label_txt = post.get("label") or ("AI NEWS" if slot == "news" else "TECH IDEA OF THE DAY")
        caption = format_caption(post["caption"])
        if post.get("source_name"):
            caption += f"\n\nSource: {post['source_name']}"
        caption += f"\n\n{CTA}"
        caption += "\n\n" + " ".join(build_hashtags(post, label_txt))
        caption = caption[:2200]

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        img_path = OUT_DIR / f"{stamp}_{slot}.jpg"
        theme = pick_theme(state["themes"])
        art_path = OUT_DIR / f"{stamp}_{slot}_art.jpg"
        has_art = generate_art(post, label_txt, art_path, state.get("arts", []))
        if has_art and LAST_ART.get("subject"):
            state.setdefault("arts", []).append(LAST_ART["subject"][:140])
            state["arts"] = state["arts"][-20:]
        if has_art:
            make_art_image(post, art_path, img_path, label_txt)
        else:
            print("ART: no AI picture available, using the themed card", file=sys.stderr)
            make_image(slot, post, img_path, theme)
        (OUT_DIR / f"{stamp}_{slot}.txt").write_text(caption, encoding="utf-8")

        mode = (os.getenv("REELS") or "off").lower()
        as_reel = args.reel or mode == "all" or (mode == "extra" and slot == "extra")
        vid_path = None
        if as_reel:
            vid_path = OUT_DIR / f"{stamp}_{slot}.mp4"
            try:
                try:
                    vid_id = make_reel_cinematic(post, vid_path, theme, state, label_txt,
                                                 art_path if has_art else None)
                    if CREDITS.get("video") and len(caption) + len(CREDITS["video"]) < 2150:
                        caption = caption.replace("\n\n#", f"\n{CREDITS['video']}\n\n#", 1)
                        (OUT_DIR / f"{stamp}_{slot}.txt").write_text(caption, encoding="utf-8")
                    if vid_id:
                        state.setdefault("videos", []).append(vid_id)
                        state["videos"] = state["videos"][-60:]
                except Exception as vex:  # motion reel failed: simple image-based Reel
                    print(f"WARNING: cinematic reel failed ({vex}); using the simple Reel", file=sys.stderr)
                    try:
                        make_reel_voice(img_path, vid_path, post)
                    except Exception as vex2:
                        print(f"WARNING: voiceover failed ({vex2}); making a silent Reel", file=sys.stderr)
                        make_reel(img_path, vid_path)
            except Exception as ex:  # no ffmpeg etc.: post the normal image instead of failing
                print(f"WARNING: could not make the Reel ({ex}); posting the image instead", file=sys.stderr)
                as_reel, vid_path = False, None

        if args.dry_run:
            print("DRY RUN ok ->", vid_path or img_path)
            print("THEME:", theme)
            print("HEADLINE:", post["headline"], "|", post["subheadline"])
            print(caption)
            return

        stage = "instagram"  # from here a failure is an Instagram/hosting problem, not an AI problem
        if as_reel:
            media_id = publish(upload_video(vid_path), caption, reel=True)
        else:
            media_id = publish(upload_image(img_path), caption)

        if post.get("source_link"):
            state["links"].append(post["source_link"])
        elif items:
            state["links"].extend(i["link"] for i in items[:1])
        state["headlines"].append(f"{post['headline']} - {post['subheadline']}")
        state["themes"].append(theme)
        state["themes"] = state["themes"][-20:]
        state["links"] = state["links"][-300:]
        state["headlines"] = state["headlines"][-40:]
        save_json(POSTED_FILE, state)
        log(slot, "OK", media_id)
        print("POSTED", slot, media_id)

    except Exception as ex:
        log(slot, "FAIL", ex)
        print("ERROR:", ex, file=sys.stderr)
        if stage == "instagram":
            print("Instagram/hosting rejected the post: not retrying (retrying cannot fix this).", file=sys.stderr)
            sys.exit(3)
        sys.exit(1)  # non-zero exit lets UiPath send the alert email


if __name__ == "__main__":
    main()
