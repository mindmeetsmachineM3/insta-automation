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
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

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
    "https://www.anthropic.com/news/rss.xml",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
]

# LLM_PROVIDER: "gemini" (free tier, default) or "claude" (paid API)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
LANGUAGE = os.getenv("POST_LANGUAGE", "English")  # English / Marathi / Hinglish
GRAPH = "https://graph.instagram.com/v21.0"


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
def fetch_items(limit_per_feed=6):
    items = []
    for url in FEEDS:
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
                        "source": feed.feed.get("title", url),
                    }
                )
        except Exception as ex:
            print(f"feed failed {url}: {ex}", file=sys.stderr)
    return items


def pick_items(items, posted, n):
    fresh = [i for i in items if i["link"] and i["link"] not in posted]
    return fresh[:n]


# ---------------------------------------------------------------- Claude
def build_prompt(slot, items):
    news_block = "\n".join(
        f"{idx+1}. [{i['source']}] {i['title']} - {i['summary']} ({i['link']})"
        for idx, i in enumerate(items)
    )
    if slot == "news":
        task = (
            "Pick the SINGLE most interesting AI/technology news item from the list below "
            "and write an Instagram post about it. Use ONLY facts present in the list. "
            "Do not invent numbers, quotes or claims. Write everything in your own words: never copy sentences or headlines from the list, and use at most one short quote (under 12 words). The headline and subheadline must not claim more than the source says: keep the exact scope (who, what, how much) and never turn a narrow fact into a general one."
        )
    else:
        task = (
            "Using the trends in the list below as inspiration, write an Instagram post "
            "presenting ONE innovative, practical technology idea (a product, tool or use "
            "case someone could build or try). Make it clearly an idea, not a news claim."
        )
    return f"""{task}

Language of the post: {LANGUAGE}.
Audience: Indian tech-curious Instagram users. Tone: clear, energetic, no hype or clickbait.

Return ONLY valid JSON with these keys:
- "headline": max 60 characters, big text for the image
- "subheadline": max 110 characters, supporting line for the image
- "caption": 60-120 words, with a hook in the first line, short paragraphs, 1-2 emojis
- "hashtags": list of 8-10 relevant hashtags (each starting with #)
- "source_name": name of the source (news slot) or "" (idea slot)
- "source_link": the exact URL from the list (news slot) or "" (idea slot)

Items:
{news_block}
"""


def call_gemini(prompt):
    """Free tier: key from https://aistudio.google.com/apikey"""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
        },
        timeout=90,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:300]}")
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def call_claude(prompt):
    from anthropic import Anthropic  # only needed if LLM_PROVIDER=claude

    client = Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=MODEL, max_tokens=1200, messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def generate_post(slot, items):
    prompt = build_prompt(slot, items)
    text = (call_claude(prompt) if LLM_PROVIDER == "claude" else call_gemini(prompt)).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    data = json.loads(text)
    for k in ("headline", "subheadline", "caption", "hashtags"):
        if k not in data:
            raise ValueError(f"Claude output missing {k}")
    return data


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


def make_image(slot, post, path):
    W, H = 1080, 1350
    top, bottom = ((14, 30, 80), (90, 30, 140)) if slot == "news" else ((10, 70, 80), (20, 20, 60))
    img = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        px.line(
            [(0, y), (W, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    d = ImageDraw.Draw(img)
    margin = 90

    label = "AI NEWS" if slot == "news" else "TECH IDEA OF THE DAY"
    lf = load_font(44)
    tw = d.textbbox((0, 0), label, font=lf)[2]
    d.rounded_rectangle([margin, 110, margin + tw + 40, 180], 20, fill=(255, 255, 255))
    d.text((margin + 20, 118), label, font=lf, fill=(20, 20, 60))

    y = 330
    hf = load_font(88)
    for line in wrap_to_width(d, post["headline"], hf, W - 2 * margin):
        d.text((margin, y), line, font=hf, fill=(255, 255, 255))
        y += 110
    y += 40
    sf = load_font(44)
    for line in wrap_to_width(d, post["subheadline"], sf, W - 2 * margin):
        d.text((margin, y), line, font=sf, fill=(210, 220, 255))
        y += 62

    handle = os.getenv("IG_HANDLE", "")
    d.text((margin, H - 130), handle, font=load_font(40), fill=(255, 255, 255))
    d.text((margin, H - 80), "AI-assisted content", font=load_font(30), fill=(180, 190, 230))
    img.save(path, "PNG")


# ---------------------------------------------------------------- hosting
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


def publish(image_url, caption):
    token = get_token()
    ig_id = os.environ["IG_USER_ID"]
    r = requests.post(
        f"{GRAPH}/{ig_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": token},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"create container failed: {r.text}")
    creation_id = r.json()["id"]

    for _ in range(20):  # wait until container is ready
        s = requests.get(
            f"{GRAPH}/{creation_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        ).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"container error: {s}")
        time.sleep(3)

    r = requests.post(
        f"{GRAPH}/{ig_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"publish failed: {r.text}")
    return r.json()["id"]


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["news", "idea"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-token", action="store_true")
    args = ap.parse_args()

    if args.refresh_token:
        refresh_token()
        return
    if not args.slot:
        ap.error("--slot is required")

    slot = args.slot
    try:
        posted = load_json(POSTED_FILE, [])
        items = pick_items(fetch_items(), posted, 8 if slot == "news" else 6)
        if not items:
            raise RuntimeError("no fresh items found in feeds")

        post = generate_post(slot, items)

        caption = post["caption"].strip()
        if post.get("source_name") and slot == "news":
            caption += f"\n\nSource: {post['source_name']}"
        caption += "\n\n(AI-assisted post)\n\n" + " ".join(post["hashtags"])
        caption = caption[:2200]

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        img_path = OUT_DIR / f"{stamp}_{slot}.png"
        make_image(slot, post, img_path)
        (OUT_DIR / f"{stamp}_{slot}.txt").write_text(caption, encoding="utf-8")

        if args.dry_run:
            print("DRY RUN ok ->", img_path)
            print(caption)
            return

        url = upload_image(img_path)
        media_id = publish(url, caption)

        if post.get("source_link"):
            posted.append(post["source_link"])
        else:
            posted.extend(i["link"] for i in items[:1])
        save_json(POSTED_FILE, posted[-300:])
        log(slot, "OK", media_id)
        print("POSTED", slot, media_id)

    except Exception as ex:
        log(slot, "FAIL", ex)
        print("ERROR:", ex, file=sys.stderr)
        sys.exit(1)  # non-zero exit lets UiPath send the alert email


if __name__ == "__main__":
    main()
