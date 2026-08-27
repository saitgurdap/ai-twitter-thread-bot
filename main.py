"""
Automated GitHub + AI content X (Twitter) THREAD posting bot.
- Runs on a fixed schedule (default: 4 times/day, at peak hours for your
  target timezone).
- Picks a rotating category each run (trending repo, AI tool intro, open
  source SaaS alternatives, AI news, automation tools, GitHub 101 tips,
  useful skills/tools, trend-jacking, etc.).
- Uses the Gemini API (with Google Search grounding when useful) to write a
  THREAD (6-8 connected tweets): opens with a hook, continues with "cont. 👇"
  / "++", and closes with a "link in first reply" call to action.
- Attaches a preview image to the first tweet whenever possible.
- Posts the thread via the X API v2 (does NOT include a link, to keep costs
  low).
- Sends an instant push notification via ntfy.sh so you can manually add the
  source link as a reply to the last tweet.

LANGUAGE: Set OUTPUT_LANGUAGE below to control what language the bot writes
in. This only changes the language Gemini is instructed to write in — the
instructions in this file are in English regardless of that setting.
"""

import os
import io
import json
import random
import re
import sys
import textwrap
from datetime import date
from pathlib import Path

import requests
import tweepy
from google import genai
from google.genai import types as genai_types
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

STATE_FILE = Path(__file__).parent / "state.json"
TIPS_FILE = Path(__file__).parent / "github_tips.json"
AI_TOOLS_FILE = Path(__file__).parent / "ai_tools.json"
CONCEPT_TOPICS_FILE = Path(__file__).parent / "concept_topics.json"
HOWTO_TOPICS_FILE = Path(__file__).parent / "howto_topics.json"
COMPARISON_TOPICS_FILE = Path(__file__).parent / "comparison_topics.json"

# ---- Environment variables (come from GitHub Secrets) ----
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
X_API_KEY = os.environ["X_API_KEY"]
X_API_SECRET = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_SECRET = os.environ["X_ACCESS_SECRET"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]  # e.g. a hard-to-guess name like "yourname-xbot-8f2k1"
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN")  # optional, for X trend tracking
SCREENSHOT_API_KEY = os.environ.get("SCREENSHOT_API_KEY")  # optional, fallback when og:image is missing
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")

# The language the generated thread content is written in. Change this to
# "Turkish", "Spanish", "German", etc. to localize the bot's OUTPUT for your
# own audience — you don't need to touch anything else in this file.
OUTPUT_LANGUAGE = os.environ.get("OUTPUT_LANGUAGE", "English")

MIN_THREAD_LEN = 6
MAX_THREAD_LEN = 8
TWEET_CHAR_LIMIT = 260  # safety margin under X's 280 limit, while maximizing room for detail

# Rotation: a 14-slot cycle that interleaves GitHub-sourced categories (6) with
# fully independent, purely educational categories (8). No two GitHub
# categories ever appear back-to-back.
CATEGORIES = [
    "trending_repo",         # GitHub
    "concept_explainer",     # non-GitHub - explains a concept
    "ai_tool_intro",         # non-GitHub - tool spotlight
    "opensource_saas",       # GitHub
    "practical_howto",       # non-GitHub - practical guide
    "trend_take",            # non-GitHub - comments on an X trend through our niche's lens
    "game_dev_tool",         # GitHub - game dev + AI/open source crossover
    "ai_news",               # non-GitHub - current events
    "automation_tool",       # GitHub
    "comparison",            # non-GitHub - head-to-head comparison
    "beginner_github_tip",   # GitHub (but purely educational)
    "ai_tool_intro",         # non-GitHub - tool spotlight
    "useful_skill_tool",     # GitHub
    "practical_howto",       # non-GitHub - practical guide
]

# GitHub search API category -> topic query mapping (all except beginner_github_tip)
TOPIC_QUERIES = {
    "opensource_saas": "topic:saas+stars:%3E200",
    "automation_tool": "topic:automation+stars:%3E200",
    "useful_skill_tool": "topic:cli-tool+stars:%3E100",
    "productivity_tool": "topic:productivity+stars:%3E100",
    "game_dev_tool": "topic:game-development+stars:%3E300",
}

# Label printed on the auto-generated visual card for categories that have no
# source URL (and therefore no repo/tool preview image to fetch).
CATEGORY_LABELS = {
    "concept_explainer": "CONCEPT EXPLAINER",
    "practical_howto": "PRACTICAL GUIDE",
    "comparison": "COMPARISON",
    "ai_news": "AI NEWS",
    "beginner_github_tip": "GITHUB 101",
    "trend_take": "TREND TAKE",
}

CARD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _load_card_font(size):
    for path in CARD_FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def generate_title_card(title_text, label_text):
    """Generates a simple, branded visual card for categories that have no
    source URL. Needs no external service, entirely free."""
    width, height = 1200, 630
    img = Image.new("RGB", (width, height), color=(12, 16, 28))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, 10], fill=(90, 150, 255))

    label_font = _load_card_font(32)
    title_font = _load_card_font(56)
    footer_font = _load_card_font(28)

    draw.text((60, 70), label_text, font=label_font, fill=(120, 170, 255))

    wrapped = textwrap.fill(title_text, width=26)
    draw.multiline_text((60, 170), wrapped, font=title_font, fill=(245, 245, 250), spacing=18)

    draw.text((60, height - 80), "Automated AI content bot", font=footer_font, fill=(130, 140, 160))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# Projects that are direct alternatives to / bypass tools for X itself are
# strategically risky to promote on our own platform, so we filter them out.
BLOCKED_KEYWORDS = [
    "nitter", "twitter clone", "twitter alternative", "x alternative",
    "tweet scraper", "twitter scraper", "twitter-scraper", "x.com scraper",
    "twitter downloader", "twitter bypass", "x bypass", "twitter proxy",
]


def is_blocked(full_name, description):
    text = f"{full_name} {description}".lower()
    return any(kw in text for kw in BLOCKED_KEYWORDS)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"rotation_index": 0, "posted_ids": [], "tip_index": 0, "recent_news_topics": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def fetch_trending_repo(posted):
    """Scrapes github.com/trending and returns the first repo not already posted."""
    resp = requests.get(
        "https://github.com/trending?since=daily",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    soup = BeautifulSoup(resp.text, "html.parser")
    articles = soup.select("article.Box-row")
    for art in articles:
        h2 = art.select_one("h2 a")
        if not h2:
            continue
        full_name = h2["href"].strip("/")
        if full_name in posted:
            continue
        desc_tag = art.select_one("p")
        description = desc_tag.get_text(strip=True) if desc_tag else ""
        if is_blocked(full_name, description):
            continue
        return {
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
            "description": description,
        }
    return None


def fetch_topic_repo(category, posted):
    """Fetches a popular, not-yet-posted repo for a given topic via the GitHub Search API."""
    query = TOPIC_QUERIES[category]
    resp = requests.get(
        f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=15",
        headers={"Accept": "application/vnd.github+json"},
        timeout=20,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    random.shuffle(items)
    for item in items:
        full_name = item["full_name"]
        description = item.get("description") or ""
        if full_name in posted:
            continue
        if is_blocked(full_name, description):
            continue
        return {
            "full_name": full_name,
            "url": item["html_url"],
            "description": description,
        }
    return None


def fetch_ai_tool(posted):
    """Picks a not-yet-posted tool from the curated AI tools list."""
    tools = json.loads(AI_TOOLS_FILE.read_text())
    random.shuffle(tools)
    for tool in tools:
        key = f"tool::{tool['name']}"
        if key in posted:
            continue
        return tool, key
    return None, None


def get_next_tip(state):
    tips = json.loads(TIPS_FILE.read_text())
    idx = state["tip_index"] % len(tips)
    state["tip_index"] = idx + 1
    return tips[idx]


def get_next_from_list(state, file_path, state_key):
    """Returns the next topic from a given JSON topic list in sequence, advancing state."""
    topics = json.loads(file_path.read_text())
    idx = state.get(state_key, 0) % len(topics)
    state[state_key] = idx + 1
    return topics[idx]


HOOK_DEVICES = [
    ("curiosity_gap", "Open with a curiosity gap — don't give the answer right away (e.g. 'Most people don't know this, and it's costing them hours.')"),
    ("striking_stat", "Open with a concrete, striking number or statistic (e.g. '90% of users never touch this feature.')"),
    ("pain_point", "Open by naming a familiar pain point directly, empathetically, without asking a question."),
    ("bold_claim", "Open with an unexpected, mildly contrarian or bold claim (e.g. 'Most people are doing this wrong.')"),
    ("personal_observation", "Open in a confessional/observational tone (e.g. 'I made this exact mistake until I noticed this.')"),
    ("comparison", "Open with a striking before/after or A-vs-B comparison (e.g. 'One takes hours. The other takes two minutes.')"),
]


def get_next_hook_device(state):
    idx = state.get("hook_device_index", 0) % len(HOOK_DEVICES)
    state["hook_device_index"] = idx + 1
    return HOOK_DEVICES[idx]


def build_thread_prompt(category, repo, tip, recent_topics, hook_device, recent_hooks, x_trends):
    has_link = repo is not None
    cta_line = (
        "End the last tweet with '🔗 Link and details in the first reply 👇'."
        if has_link
        else "End the last tweet with a strong closing line that makes the reader think or act."
    )
    hook_name, hook_instruction = hook_device
    recent_hooks_line = (
        f"You've already used these opening lines before — do NOT match their style: "
        f"{' | '.join(recent_hooks[-6:])}"
        if recent_hooks
        else ""
    )
    base_rules = (
        "You are an expert viral content writer producing an X (Twitter) THREAD "
        f"(in {OUTPUT_LANGUAGE}). Your goal is not to skim the surface — it's to give genuinely "
        "SUBSTANTIAL, DETAILED, VALUABLE information. The reader should feel like they actually "
        "learned something by the end. "
        f"Write a thread of {MIN_THREAD_LEN}-{MAX_THREAD_LEN} tweets total. "
        "Return ONLY a valid JSON array, with no other explanation, markdown, or code fences. "
        'The format must be exactly: ["tweet text 1", "tweet text 2", ...]\n\n'
        "Depth rules:\n"
        "- Follow this structure: (1) hook/opening, (2) what the problem/need is, "
        "(3-4) how the solution works and its concrete features (you may use short bullet "
        "points starting with '- ' or '• ', fitting 2-4 per tweet), "
        "(5-6) the concrete benefit of these features / who it's for, a usage example or a "
        "comparison with alternatives if relevant, (final) summary + closing.\n"
        "- Fill each tweet up to the character limit, don't leave it half-empty; avoid generic "
        "one-liner tweets — every tweet should carry real information.\n"
        "- Use concrete numbers, feature names, real examples; avoid empty generalities like "
        "'very useful' or 'a great tool' — back every claim with a detail.\n\n"
        "Other rules:\n"
        f"- Each tweet must be at most {TWEET_CHAR_LIMIT} characters.\n"
        f"- For the FIRST TWEET, use this specific hook technique: {hook_instruction} "
        "Absolutely avoid cliché openers (like 'Hey everyone'); craft your own sentence specific "
        f"to the topic. {recent_hooks_line}\n"
        "- Keep curiosity alive throughout the thread: each tweet should answer the previous "
        "curiosity gap while opening a new one. But do NOT repeat the exact same transition "
        "pattern in every tweet — vary it (sometimes a question, sometimes a striking statement, "
        "sometimes a half-finished thought that continues).\n"
        "- Every tweet except the LAST must end with 'cont. 👇' or '++' so it's clear the thread continues.\n"
        f"- {cta_line}\n"
        "- Use emoji sparingly (0-2 per tweet), avoid hype/ad-like language, keep a warm but "
        "informative tone, use at most 1-2 hashtags.\n"
        "- NEVER write an actual link/URL — links will be added separately by hand.\n"
        f"- Write the ENTIRE thread in {OUTPUT_LANGUAGE}.\n"
    )

    if category == "trending_repo":
        topic_line = (
            f"Cover the project trending on GitHub today called '{repo['full_name']}'. "
            f"Its description: '{repo['description']}'. Go deep throughout the thread on what it "
            "does, who/where it can be used, and its benefits."
        )
    elif category in ("opensource_saas", "automation_tool", "useful_skill_tool", "productivity_tool", "game_dev_tool"):
        category_hint = {
            "opensource_saas": "an open-source alternative to a paid SaaS product",
            "automation_tool": "a workflow/automation tool",
            "useful_skill_tool": "a CLI/tool that makes developers' lives easier",
            "productivity_tool": "a tool that boosts productivity",
            "game_dev_tool": "an open-source tool/library that makes game developers' lives easier",
        }[category]
        topic_line = (
            f"'{repo['full_name']}' is {category_hint}. "
            f"Its description: '{repo['description']}'. Go deep throughout the thread on what it "
            "does, when/where it can be used, and its benefits."
        )
    elif category == "beginner_github_tip":
        topic_line = (
            f"Explain this topic to people who are new to GitHub: '{tip}'. "
            "Use a simple, unintimidating tone, step by step, written for someone who has never used it."
        )
    elif category == "ai_tool_intro":
        topic_line = (
            f"Introduce the AI tool '{repo['name']}'. Hint: {repo['hint']}. Search the web if needed "
            "for this tool's most current features. Cover throughout the thread what it does, how to "
            "use it effectively, who it's useful for, and a practical usage example. "
            "IMPORTANT: This must read as an unbiased spotlight, not an ad — somewhere in the thread "
            "you MUST mention an honest limitation, drawback, or a 'this might not be right for you if...' "
            "note. Do not write purely flattering copy; the reader should feel this account is genuinely "
            "unbiased."
        )
    elif category == "ai_news":
        recent = ", ".join(recent_topics[-8:]) if recent_topics else "none"
        topic_line = (
            "Research a REAL, CURRENT AI news item or development from the last 24-48 hours that's "
            f"actually trending online right now. I've already covered these topics, do NOT repeat them: {recent}. "
            "Summarize what you find in your own words, and explain throughout the thread why it "
            "matters and its likely impact. Make the news topic clear in the first tweet."
        )
    elif category == "concept_explainer":
        topic_line = (
            f"Explain this AI/tech concept in a way anyone can understand: '{tip}'. Search the web if "
            "needed for an accurate, current definition and example. Avoid technical jargon, use an "
            "everyday analogy or a concrete example. Make sure to state, by the end of the thread, why "
            "knowing this concept actually benefits the reader (where it's useful to them)."
        )
    elif category == "practical_howto":
        topic_line = (
            f"Write a step-by-step, actionable guide thread on this: '{tip}'. Search the web if needed "
            "for current tool/method info. The reader should be able to go apply this IMMEDIATELY after "
            "finishing the thread — concrete steps, clear on which tool/how, no vague advice."
        )
    elif category == "comparison":
        topic_line = (
            f"Do this comparison: '{tip}'. Search the web if needed for current feature/pricing info. "
            "Don't take sides — concretely state the scenario where each option wins (e.g. 'A makes more "
            "sense for X, B for Y'). End the thread with a clear decision criterion the reader can apply "
            "to their own situation."
        )
    elif category == "trend_take":
        trends_str = ", ".join(x_trends) if x_trends else ""
        topic_line = (
            f"Here's what's currently trending on X: {trends_str}. "
            "Pick one that's related to TECHNOLOGY, SOFTWARE, GAMING, DIGITAL CULTURE, or CURRENT EVENTS "
            "(if none directly fit those, pick the most broadly interesting one). Bridge your chosen "
            "trend to our niche (AI, GitHub, automation, software tools) in a CREATIVE but NATURAL way "
            "— for example, if the trend is 'gaming' or a specific game, cover useful open-source GitHub "
            "tools for that genre or AI tools used in game development; if the trend is a tech "
            "company/product, focus on its AI/software angle. Search the web if needed for current info "
            "on the trend. Goal: ride the moment to give the reader something REAL and USABLE — don't "
            "just restate the news, add value from our domain. It should feel natural and interesting, "
            "never forced."
        )
    else:
        raise ValueError(category)

    trend_line = ""
    if x_trends and category != "trend_take":
        trend_line = (
            f"\n\nExtra context: Currently trending on X: {', '.join(x_trends)}. "
            "If one of these connects NATURALLY and MEANINGFULLY to the topic at hand, weave it in "
            "somewhere in the thread (especially in the hook) to tie into the current conversation. "
            "Don't force a connection; if none of them are relevant, ignore the trend list entirely and "
            "proceed normally."
        )

    return base_rules + "\n" + topic_line + trend_line


def _call_gemini(prompt, use_search):
    client = genai.Client(api_key=GEMINI_API_KEY)
    config_kwargs = {}
    if use_search:
        grounding_tool = genai_types.Tool(google_search=genai_types.GoogleSearch())
        config_kwargs["tools"] = [grounding_tool]
    config = genai_types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=config)
    raw = response.text.strip()
    raw = re.sub(r"^```(json)?", "", raw.strip()).strip()
    raw = re.sub(r"```$", "", raw.strip()).strip()
    try:
        tweets = json.loads(raw)
        if not isinstance(tweets, list) or not tweets:
            raise ValueError("Empty or invalid list")
        return tweets
    except Exception:
        return None


def generate_thread(prompt, use_search=False):
    tweets = _call_gemini(prompt, use_search)
    # If the result is too short (JSON error or the model ignored the instruction), retry once
    if tweets is None or len(tweets) < MIN_THREAD_LEN - 2:
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT REMINDER: Your response must be ONLY a valid JSON array, with at least "
            f"{MIN_THREAD_LEN} elements. Do not add any other text."
        )
        retried = _call_gemini(retry_prompt, use_search)
        if retried:
            tweets = retried

    if tweets is None:
        # If both attempts failed, use a one-element fallback instead of crashing
        tweets = ["Something went wrong generating this content, this post was skipped."]

    cleaned = []
    for t in tweets:
        t = str(t).strip()
        if len(t) > 275:
            t = t[:272].rsplit(" ", 1)[0] + "..."
        cleaned.append(t)
    return cleaned[:MAX_THREAD_LEN]


def get_preview_image_bytes(url):
    """First tries the site's/repo's own preview image (og:image / GitHub's social
    card). If not found, falls back to a live screenshot via the SnapRender API
    (used at a volume well under the free quota)."""
    try:
        if "github.com/" in url:
            owner_repo = url.split("github.com/", 1)[1].strip("/")
            img_url = f"https://opengraph.githubassets.com/1/{owner_repo}"
        else:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            tag = soup.find("meta", property="og:image")
            img_url = tag["content"] if tag and tag.get("content") else None
        if img_url:
            img_resp = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if img_resp.status_code == 200 and img_resp.content:
                return img_resp.content
    except Exception as e:
        print("Could not fetch og:image:", e)

    # Fallback: live screenshot (only if SCREENSHOT_API_KEY is set)
    if SCREENSHOT_API_KEY:
        try:
            shot_resp = requests.get(
                "https://app.snap-render.com/v1/screenshot",
                params={
                    "url": url,
                    "format": "jpeg",
                    "width": 1200,
                    "height": 630,
                    "block_ads": "true",
                    "block_cookie_banners": "true",
                },
                headers={"X-API-Key": SCREENSHOT_API_KEY},
                timeout=30,
            )
            if shot_resp.status_code == 200 and shot_resp.content:
                return shot_resp.content
        except Exception as e:
            print("Could not get image from Screenshot API:", e)
    return None


def post_thread_to_x(tweets, image_bytes=None):
    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET,
    )

    media_id = None
    if image_bytes:
        try:
            auth = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET)
            api_v1 = tweepy.API(auth)
            media = api_v1.media_upload(filename="preview.jpg", file=io.BytesIO(image_bytes))
            media_id = media.media_id
        except Exception as e:
            print("Could not upload image, continuing without it:", e)

    tweet_urls = []
    previous_id = None
    for i, text in enumerate(tweets):
        kwargs = {"text": text}
        if previous_id:
            kwargs["in_reply_to_tweet_id"] = previous_id
        if i == 0 and media_id:
            kwargs["media_ids"] = [media_id]
        result = client.create_tweet(**kwargs)
        tweet_id = result.data["id"]
        tweet_urls.append(f"https://x.com/i/web/status/{tweet_id}")
        previous_id = tweet_id
    return tweet_urls


# WOEID (Where On Earth ID) for the X Trends API. Defaults to worldwide (1);
# change to your target region's WOEID for localized trends (e.g. Turkey =
# 23424969, United States = 23424977).
TRENDS_WOEID = int(os.environ.get("TRENDS_WOEID", "1"))


def fetch_x_trends():
    """Fetches what's currently trending on X for TRENDS_WOEID.
    Returns an empty list silently if X_BEARER_TOKEN isn't set (the system
    still works fine without it)."""
    if not X_BEARER_TOKEN:
        return []
    try:
        resp = requests.get(
            f"https://api.x.com/2/trends/by/woeid/{TRENDS_WOEID}",
            headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
            timeout=15,
        )
        if resp.status_code != 200:
            print("Could not fetch X trends:", resp.status_code, resp.text[:200])
            return []
        data = resp.json().get("data", [])
        return [item.get("trend_name", "") for item in data if item.get("trend_name")][:15]
    except Exception as e:
        print("Error fetching X trends:", e)
        return []


def get_cached_trends(state):
    """Fetches trend data only once per day, reusing the cache for the rest of
    the day's runs — keeps costs under control."""
    today = date.today().isoformat()
    cache = state.get("x_trends_cache", {})
    if cache.get("date") == today:
        return cache.get("trends", [])
    trends = fetch_x_trends()
    state["x_trends_cache"] = {"date": today, "trends": trends}
    return trends


def notify(title, message, click_url=None):
    headers = {"Title": title.encode("utf-8")}
    if click_url:
        headers["Click"] = click_url
    requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=headers, timeout=10)


def main():
    state = load_state()
    state.setdefault("posted_ids", state.get("posted_repos", []))
    state.setdefault("recent_news_topics", [])
    state.setdefault("recent_hooks", [])
    state.setdefault("tip_index", 0)
    state.setdefault("hook_device_index", 0)
    state.pop("posted_repos", None)

    x_trends = get_cached_trends(state)  # fetched once/day, cached for the rest of the day's runs

    category = CATEGORIES[state["rotation_index"] % len(CATEGORIES)]
    state["rotation_index"] += 1

    # If there's no trend data (X_BEARER_TOKEN not set, or fetch failed), fall
    # back to practical_howto so this run isn't wasted.
    if category == "trend_take" and not x_trends:
        category = "practical_howto"

    repo = None
    tip = None
    dedupe_key = None
    use_search = False

    if category == "trending_repo":
        repo = fetch_trending_repo(state["posted_ids"])
        dedupe_key = repo["full_name"] if repo else None
    elif category == "beginner_github_tip":
        tip = get_next_tip(state)
    elif category == "ai_tool_intro":
        repo, dedupe_key = fetch_ai_tool(state["posted_ids"])
        use_search = True
    elif category == "ai_news":
        use_search = True
    elif category == "trend_take":
        use_search = True
    elif category == "concept_explainer":
        tip = get_next_from_list(state, CONCEPT_TOPICS_FILE, "concept_index")
        use_search = True
    elif category == "practical_howto":
        tip = get_next_from_list(state, HOWTO_TOPICS_FILE, "howto_index")
        use_search = True
    elif category == "comparison":
        tip = get_next_from_list(state, COMPARISON_TOPICS_FILE, "comparison_index")
        use_search = True
    else:
        repo = fetch_topic_repo(category, state["posted_ids"])
        dedupe_key = repo["full_name"] if repo else None

    NO_REPO_CATEGORIES = (
        "beginner_github_tip", "ai_news", "concept_explainer", "practical_howto",
        "comparison", "trend_take",
    )
    if category not in NO_REPO_CATEGORIES and repo is None:
        print(f"[{category}] no suitable new content found, skipping this run.")
        save_state(state)
        return

    hook_device = get_next_hook_device(state)
    prompt = build_thread_prompt(
        category, repo, tip, state["recent_news_topics"], hook_device, state["recent_hooks"], x_trends
    )
    tweets = generate_thread(prompt, use_search=use_search)

    if repo:
        image_bytes = get_preview_image_bytes(repo["url"])
    else:
        card_label = CATEGORY_LABELS.get(category, "CONTENT")
        card_title = tip if tip else tweets[0][:90]
        image_bytes = generate_title_card(card_title, card_label)
    tweet_urls = post_thread_to_x(tweets, image_bytes=image_bytes)

    if dedupe_key:
        state["posted_ids"].append(dedupe_key)
        state["posted_ids"] = state["posted_ids"][-1000:]
    if category == "ai_news":
        state["recent_news_topics"].append(tweets[0][:60])
        state["recent_news_topics"] = state["recent_news_topics"][-15:]
    state["recent_hooks"].append(tweets[0][:80])
    state["recent_hooks"] = state["recent_hooks"][-8:]

    save_state(state)

    click_target = repo["url"] if repo else tweet_urls[-1]
    extra_line = f"\nSource: {repo['url']}" if repo else ""
    preview = "\n\n---\n\n".join(tweets)
    notify(
        title=f"New thread posted ({category}, {len(tweets)} tweets)",
        message=f"{preview}\n\nLast tweet: {tweet_urls[-1]}{extra_line}",
        click_url=click_target,
    )
    print(f"Thread complete ({len(tweets)} tweets):", tweet_urls[-1])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        raise
