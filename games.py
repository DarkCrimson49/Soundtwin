"""Game finder: work out which game a YouTube / TikTok gameplay video shows, and where to get it.

Identification reads what the video says about itself - the title, tags, hashtags and
description - and checks those names against real store catalogs.
It does not look at the picture."""
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

STOP = {
    "gameplay", "game", "games", "gaming", "gamer", "shorts", "short", "funny", "video", "videos", "live",
    "stream", "walkthrough", "part", "episode", "fyp", "foryou", "foryoupage", "viral", "trending", "clips",
    "clip", "moments", "memes", "meme", "best", "new", "update", "trailer", "play", "pc", "xbox", "ps5", "ps4",
    "playstation", "nintendo", "switch", "mobile", "android", "ios", "4k", "hd", "60fps", "full", "the", "a",
    "review", "reaction", "tutorial", "guide", "tips", "speedrun", "montage", "highlights", "edit", "story",
    "online", "multiplayer", "first", "one", "win", "fun", "love", "war", "hero", "youtube", "tiktok", "twitch",
}

# Big games that are not sold on Steam, with where they actually live.
KNOWN = {
    "Fortnite": [("Epic Games", "https://store.epicgames.com/p/fortnite")],
    "Minecraft": [("Minecraft.net", "https://www.minecraft.net/store/minecraft-java-bedrock-edition-pc")],
    "Roblox": [("Roblox", "https://www.roblox.com/download")],
    "Valorant": [("Riot Games", "https://playvalorant.com"), ("Epic Games", "https://store.epicgames.com/p/valorant")],
    "League of Legends": [("Riot Games", "https://www.leagueoflegends.com"),
                          ("Epic Games", "https://store.epicgames.com/p/league-of-legends")],
    "Genshin Impact": [("HoYoverse", "https://genshin.hoyoverse.com"),
                       ("Epic Games", "https://store.epicgames.com/p/genshin-impact")],
    "Honkai: Star Rail": [("HoYoverse", "https://hsr.hoyoverse.com"),
                          ("Epic Games", "https://store.epicgames.com/p/honkai-star-rail")],
    "Zenless Zone Zero": [("HoYoverse", "https://zenless.hoyoverse.com")],
    "Rocket League": [("Epic Games", "https://store.epicgames.com/p/rocket-league")],
    "Fall Guys": [("Epic Games", "https://store.epicgames.com/p/fall-guys")],
    "Brawl Stars": [("Supercell", "https://supercell.com/en/games/brawlstars/")],
    "Clash Royale": [("Supercell", "https://supercell.com/en/games/clashroyale/")],
    "Clash of Clans": [("Supercell", "https://supercell.com/en/games/clashofclans/")],
    "Escape from Tarkov": [("Battlestate Games", "https://www.escapefromtarkov.com")],
    "World of Warcraft": [("Battle.net", "https://worldofwarcraft.blizzard.com")],
    "Hearthstone": [("Battle.net", "https://hearthstone.blizzard.com")],
    "osu!": [("osu!", "https://osu.ppy.sh/home/download")],
    "Free Fire": [("Garena", "https://ff.garena.com")],
    "PUBG Mobile": [("PUBG Mobile", "https://www.pubgmobile.com")],
    "Mobile Legends": [("Moonton", "https://www.mobilelegends.com")],
    "Subway Surfers": [("SYBO", "https://subwaysurfers.com")],
    "Pokemon GO": [("Niantic", "https://pokemongolive.com")],
}
ALIASES = {"genshin": "Genshin Impact", "honkai star rail": "Honkai: Star Rail", "hsr": "Honkai: Star Rail",
           "zzz": "Zenless Zone Zero", "tarkov": "Escape from Tarkov", "mlbb": "Mobile Legends",
           "coc": "Clash of Clans", "wow": "World of Warcraft"}


def get(url, extra=None, timeout=15):
    req = urllib.request.Request(url, headers={**UA, **(extra or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def nrm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def spaced(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "


# ---------------------------------------------------------------- stores

def country():
    """Two-letter country from the Windows region setting, so prices show in the local currency."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(85)
        ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85)
        cc = buf.value.split("-")[-1].lower()
        return cc if len(cc) == 2 else "us"
    except Exception:
        return "us"


CC = country()


def steam_search(term):
    try:
        q = urllib.parse.urlencode({"term": term, "l": "english", "cc": CC})
        data = json.loads(get("https://store.steampowered.com/api/storesearch/?" + q))
        return [i for i in data.get("items") or [] if i.get("type") == "app"]
    except Exception:
        return []


def gog_search(term, currency="USD"):
    try:
        q = urllib.parse.urlencode({"limit": 5, "query": "like:" + term, "order": "desc:score",
                                    "productType": "in:game", "countryCode": CC.upper(),
                                    "currencyCode": currency})
        data = json.loads(get("https://catalog.gog.com/v1/catalog?" + q))
        return data.get("products") or []
    except Exception:
        return []


def money(price):
    if not price or price.get("final") is None:
        return None
    if price["final"] == 0:
        return "Free"
    return f"{price['final'] / 100:.2f} {price.get('currency', '')}".strip()


def stores(name, steam_item=None):
    """Real listings first (with price where the store gives one), then search links."""
    out, want, q = [], nrm(name), urllib.parse.quote(name)
    steam_items = steam_search(name)
    item = steam_item or next((i for i in steam_items if nrm(i["name"]) == want), None)
    # ask GOG for the same currency Steam is showing, so prices are comparable
    currency = next((i["price"]["currency"] for i in [item, *steam_items] if i and i.get("price")), "USD")
    gog_items = gog_search(name, currency)
    if item:
        out.append({"store": "Steam", "url": f"https://store.steampowered.com/app/{item['id']}",
                    "price": money(item.get("price")), "kind": "listing"})
    else:
        out.append({"store": "Steam", "url": f"https://store.steampowered.com/search/?term={q}", "kind": "search"})

    known = KNOWN.get(name) or next((v for k, v in KNOWN.items() if nrm(k) == want), [])
    for store, url in known:
        out.append({"store": store, "url": url, "kind": "listing"})
    if not any(s["store"] == "Epic Games" for s in out):
        out.append({"store": "Epic Games", "url": f"https://store.epicgames.com/en-US/browse?q={q}&sortBy=relevancy",
                    "kind": "search"})

    g = next((p for p in gog_items if nrm(p.get("title")) == want), None)
    if g and (g.get("storeLink") or g.get("slug")):
        url = g.get("storeLink") or f"https://www.gog.com/en/game/{g['slug']}"
        price = ((g.get("price") or {}).get("final") or "").strip() or None
        out.append({"store": "GOG", "url": url, "price": price, "kind": "listing"})

    out += [
        {"store": "Xbox", "url": f"https://www.xbox.com/en-US/Search/Results?q={q}", "kind": "search"},
        {"store": "PlayStation", "url": f"https://store.playstation.com/search/{q}", "kind": "search"},
        {"store": "Nintendo", "url": f"https://www.nintendo.com/us/search/#q={q}&cat=gme", "kind": "search"},
        {"store": "Google Play", "url": f"https://play.google.com/store/search?q={q}&c=apps", "kind": "search"},
        {"store": "App Store", "url": f"https://www.apple.com/us/search/{q}?src=serp", "kind": "search"},
    ]
    return out, item


# ---------------------------------------------------------------- identification

def tiktok_page_info(url):
    """Fallback for TikTok links the downloader can't read: take the caption from the page itself."""
    html = get(url).decode("utf-8", "ignore")
    m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    item = (json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
            .get("itemInfo", {}).get("itemStruct", {})) if m else {}
    if not item.get("desc") and not item.get("textExtra"):
        raise RuntimeError("Couldn't read that TikTok. Check the link opens a single public video.")
    return {
        "title": item.get("desc") or "",
        "description": item.get("desc") or "",
        "tags": [t.get("hashtagName") for t in item.get("textExtra") or [] if t.get("hashtagName")],
        "uploader": (item.get("author") or {}).get("uniqueId"),
        "thumbnail": (item.get("video") or {}).get("cover"),
        "extractor_key": "TikTok",
    }


def video_info(url):
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        if "tiktok.com" in url.lower():
            return tiktok_page_info(url)
        raise


def identify(url, say):
    say("Reading the video's details")
    info = video_info(url)
    title = info.get("title") or ""
    desc = (info.get("description") or "")[:600]
    tags = [t for t in (info.get("tags") or []) if t][:25]
    hashtags = re.findall(r"#(\w+)", f"{title} {desc}")[:20]
    site = info.get("extractor_key") or ""
    video = {"title": title or desc[:80], "by": info.get("uploader") or info.get("channel"),
             "thumb": info.get("thumbnail"), "site": site}

    t_sp, d_sp = spaced(title), spaced(desc)
    tagset = {nrm(t) for t in tags} | {nrm(h) for h in hashtags}
    by = nrm(video["by"] or "")
    segs = [s.strip() for s in re.split(r"[|\[\]()–—/]| - |: ", title) if s.strip()]
    # places a game name usually sits: the start of the title or of a section of it
    starts = segs + [s.strip() for s in re.split(r"[|\[\]()–—/]| - ", title) if s.strip()]
    cands = {}

    def consider(name, item=None, base_bonus=0):
        n, sp = nrm(name), spaced(name)
        if len(n) < 3 or n in STOP or n.isdigit():
            return
        if sp in t_sp:
            score, why = 50, "its name is in the video title"
        elif n in tagset:
            score, why = 42, "its name is in the video's tags"
        elif len(n) >= 6 and any(n in t for t in tagset):
            score, why = 30, "its name is part of a tag on the video"
        elif len(n) >= 5 and sp in d_sp:
            score, why = 22, "its name is in the video description"
        elif len(n) >= 5 and n == by:
            score, why = 34, "the video was posted by the game's own channel"
        else:
            return
        if sp in t_sp and n in tagset:
            score, why = 65, "its name is in both the title and the tags"
        elif sp in t_sp and not any(spaced(s).startswith(sp) for s in starts):
            score = 32  # a phrase buried mid-title is often just ordinary words
        score += min(len(n), 20) + base_bonus
        if n not in cands or cands[n]["score"] < score:
            cands[n] = {"name": name, "score": score, "why": why, "item": item or (cands.get(n) or {}).get("item")}

    for name in KNOWN:
        consider(name, base_bonus=5)
    for alias, name in ALIASES.items():
        if spaced(alias) in t_sp or nrm(alias) in tagset:
            consider_n = nrm(name)
            if consider_n not in cands:
                cands[consider_n] = {"name": name, "score": 45, "why": f'the video is tagged "{alias}"', "item": None}

    say("Checking names against store catalogs")
    words = [w for w in re.split(r"[^\w'&!:.-]+", title) if w]
    grams = [" ".join(words[i:i + k]) for k in (4, 3, 2, 1) for i in range(len(words) - k + 1)]
    terms, seen = [], set()
    for term in tags + hashtags + segs + grams + ([video["by"]] if video["by"] else []):
        n = nrm(term)
        if len(n) < 3 or n in STOP or n in seen or len(term) > 60:
            continue
        seen.add(n)
        terms.append(term)
    with ThreadPoolExecutor(6) as pool:
        for items in pool.map(steam_search, terms[:30]):
            for it in items[:6]:
                consider(it["name"], it)

    if not cands:
        return {"video": video, "game": None}

    ranked = sorted(cands.values(), key=lambda c: c["score"], reverse=True)
    best = ranked[0]
    say(f"Looking up where to get {best['name']}")
    links, item = stores(best["name"], best["item"])
    conf = "high" if best["score"] >= 70 else "medium" if best["score"] >= 50 else "low"
    image = f"https://cdn.akamai.steamstatic.com/steam/apps/{item['id']}/header.jpg" if item else None
    others = []
    for c in ranked[1:]:
        if nrm(best["name"]) in nrm(c["name"]) or nrm(c["name"]) in nrm(best["name"]):
            continue  # editions / DLC of the same game
        if c["score"] < 45:
            continue
        others.append({"name": c["name"], "why": c["why"],
                       "url": f"https://store.steampowered.com/app/{c['item']['id']}" if c["item"]
                       else "https://store.steampowered.com/search/?term=" + urllib.parse.quote(c["name"])})
        if len(others) == 3:
            break
    return {"video": video,
            "game": {"name": best["name"], "why": best["why"], "confidence": conf, "image": image},
            "stores": links, "others": others}
