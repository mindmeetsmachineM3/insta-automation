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
import math
import os
import random
import re
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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

# LLM_PROVIDER: "gemini" (free tier, default) or "claude" (paid API)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
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


def load_state():
    """posted.json holds {"links": [...], "headlines": [...]} (older list format is converted)."""
    data = load_json(POSTED_FILE, {})
    if isinstance(data, list):
        data = {"links": data}
    data.setdefault("links", [])
    data.setdefault("headlines", [])
    data.setdefault("themes", [])
    return data


def pick_items(items, posted, n):
    fresh = [i for i in items if i["link"] and i["link"] not in posted]
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
            "case someone could build or try). Make it clearly an idea, not a news claim."
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
- "caption": 60-120 words, with a hook in the first line, short paragraphs, 1-2 emojis
- "hashtags": list of 6-9 relevant hashtags (each starting with #)
- "source_name": name of the source (news slot) or "" (idea slot)
- "source_link": the exact URL from the list (news slot) or "" (idea slot)

Items:
{news_block}
"""


def call_gemini(prompt):
    """Free tier: key from https://aistudio.google.com/apikey
    Retries when Google is busy (429 / 500 / 502 / 503 / 504)."""
    waits = [15, 30, 60, 90]  # seconds to wait before retry 2, 3, 4, 5
    last = ""
    for attempt in range(len(waits) + 1):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
                },
                timeout=90,
            )
        except requests.RequestException as ex:  # network hiccup
            last = f"network error: {ex}"
        else:
            if r.ok:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            last = f"Gemini API error {r.status_code}: {r.text[:300]}"
            if r.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(last)  # real error (bad key, wrong model...): do not retry
        if attempt < len(waits):
            print(f"Gemini busy, retry {attempt + 1}/{len(waits)} in {waits[attempt]}s ({last[:80]})", file=sys.stderr)
            time.sleep(waits[attempt])
    raise RuntimeError(last)


def call_claude(prompt):
    from anthropic import Anthropic  # only needed if LLM_PROVIDER=claude

    client = Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=MODEL, max_tokens=1200, messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def generate_post(slot, items, recent=None):
    prompt = build_prompt(slot, items, recent)
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
    label = "AI NEWS" if slot == "news" else "TECH IDEA OF THE DAY"
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
    d.text((margin, H - 88), "AI-assisted content", font=load_font(28), fill=(175, 190, 235, 255))

    img.convert("RGB").save(path, "JPEG", quality=93, subsampling=0)


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
        state = load_state()
        items = pick_items(fetch_items(), state["links"], 8 if slot == "news" else 6)
        if not items:
            raise RuntimeError("no fresh items found in feeds")

        post = generate_post(slot, items, state["headlines"][-25:])

        caption = post["caption"].strip()
        if post.get("source_name") and slot == "news":
            caption += f"\n\nSource: {post['source_name']}"
        caption += "\n\n(AI-assisted post)\n\n" + " ".join(post["hashtags"])
        caption = caption[:2200]

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        img_path = OUT_DIR / f"{stamp}_{slot}.jpg"
        theme = pick_theme(state["themes"])
        make_image(slot, post, img_path, theme)
        (OUT_DIR / f"{stamp}_{slot}.txt").write_text(caption, encoding="utf-8")

        if args.dry_run:
            print("DRY RUN ok ->", img_path)
            print("THEME:", theme)
            print("HEADLINE:", post["headline"], "|", post["subheadline"])
            print(caption)
            return

        url = upload_image(img_path)
        media_id = publish(url, caption)

        if post.get("source_link"):
            state["links"].append(post["source_link"])
        else:
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
        sys.exit(1)  # non-zero exit lets UiPath send the alert email


if __name__ == "__main__":
    main()
