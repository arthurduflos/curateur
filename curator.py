"""Curateur hebdo : récupère les nouvelles vidéos, les fait noter par Claude,
ajoute les meilleures à une playlist YouTube et écrit un rapport."""
import datetime as dt
import json
import os
import re

import anthropic
import feedparser
import yaml
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

STATE_FILE = "state.json"
REPORT_FILE = "report.md"
NOW = dt.datetime.now(dt.timezone.utc)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen": {}, "channel_ids": {}}


def save_state(state):
    cutoff = (NOW - dt.timedelta(days=120)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)


def youtube_client():
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def resolve_channel(yt, handle, state):
    if handle in state["channel_ids"]:
        return state["channel_ids"][handle]
    res = yt.channels().list(part="id", forHandle=handle.lstrip("@")).execute()
    items = res.get("items", [])
    if not items:
        print(f"⚠️  Chaîne introuvable : {handle}")
        return None
    state["channel_ids"][handle] = items[0]["id"]
    return items[0]["id"]


def from_channels(yt, cfg, state, since):
    """Flux RSS : gratuit en quota."""
    ids = set()
    for handle in cfg["channels"]:
        cid = resolve_channel(yt, handle, state)
        if not cid:
            continue
        feed = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
        for e in feed.entries:
            published = dt.datetime.fromisoformat(e.published.replace("Z", "+00:00"))
            if published >= since:
                ids.add(e.yt_videoid)
    return ids


def from_guests(yt, cfg, since):
    """Recherche par invité, tous podcasts confondus (100 unités de quota / nom)."""
    ids = set()
    for name in cfg["guests"]:
        res = yt.search().list(
            part="id", q=f'"{name}"', type="video", videoDuration="long",
            publishedAfter=since.isoformat().replace("+00:00", "Z"),
            maxResults=10, order="relevance",
        ).execute()
        ids.update(i["id"]["videoId"] for i in res.get("items", []))
    return ids


def duration_min(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 60 + mi + s / 60


def details(yt, ids):
    out, ids = [], list(ids)
    for i in range(0, len(ids), 50):
        res = yt.videos().list(part="snippet,contentDetails", id=",".join(ids[i:i + 50])).execute()
        for v in res.get("items", []):
            out.append({
                "id": v["id"],
                "title": v["snippet"]["title"],
                "channel": v["snippet"]["channelTitle"],
                "description": v["snippet"]["description"][:1200],
                "minutes": round(duration_min(v["contentDetails"]["duration"])),
            })
    return out


def score(videos, cfg):
    client = anthropic.Anthropic()
    system = (
        "Tu es un curateur exigeant de podcasts et vidéos YouTube pour une personne précise.\n\n"
        f"PROFIL :\n{cfg['profile']}\n\nGRILLE :\n{cfg['criteria']}\n\n"
        "Réponds UNIQUEMENT par un tableau JSON, sans texte ni balises autour : "
        '[{"id": "...", "score": 0-10, "raison": "une phrase en français"}]'
    )
    results = []
    for i in range(0, len(videos), 10):
        batch = videos[i:i + 10]
        msg = client.messages.create(
            model=cfg["model"], max_tokens=3000, system=system,
            messages=[{"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        text = re.sub(r"```(json)?", "", text).strip()
        try:
            results += json.loads(text)
        except json.JSONDecodeError:
            print(f"⚠️  Réponse illisible pour le lot {i // 10}, ignoré")
    return {r["id"]: r for r in results}


MOIS = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet",
        "Août", "Septembre", "Octobre", "Novembre", "Décembre"]


def monthly_playlist(yt, cfg, state):
    """Retourne l'ID de la playlist du mois en cours, en la créant au besoin."""
    key = f"{NOW:%Y-%m}"
    state.setdefault("playlists", {})
    if key in state["playlists"]:
        return state["playlists"][key]
    title = f"{cfg['playlist_prefix']} — {MOIS[NOW.month - 1]} {NOW.year}"
    res = yt.playlists().insert(part="snippet,status", body={
        "snippet": {"title": title,
                    "description": "Sélection automatique : interviews et contenus de fond notés par Claude."},
        "status": {"privacyStatus": cfg["playlist_privacy"]},
    }).execute()
    state["playlists"][key] = res["id"]
    print(f"Nouvelle playlist créée : {title}")
    return res["id"]


def add_to_playlist(yt, playlist_id, video_id):
    yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": playlist_id,
        "resourceId": {"kind": "youtube#video", "videoId": video_id},
    }}).execute()


def main():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    state = load_state()
    yt = youtube_client()
    since = NOW - dt.timedelta(days=cfg["lookback_days"])

    ids = from_channels(yt, cfg, state, since) | from_guests(yt, cfg, since)
    ids -= set(state["seen"])
    videos = [v for v in details(yt, ids) if v["minutes"] >= cfg["min_duration_min"]]
    print(f"{len(videos)} nouvelles vidéos à évaluer")

    scores = score(videos, cfg) if videos else {}
    picks, rejects = [], []
    for v in videos:
        s = scores.get(v["id"])
        state["seen"][v["id"]] = NOW.isoformat()
        if not s:
            continue
        v.update(score=s["score"], raison=s["raison"])
        (picks if s["score"] >= cfg["score_threshold"] else rejects).append(v)

    picks.sort(key=lambda v: -v["score"])
    if picks:
        playlist_id = monthly_playlist(yt, cfg, state)
        for v in picks:
            add_to_playlist(yt, playlist_id, v["id"])

    lines = [f"# Sélection du {NOW:%d/%m/%Y}\n", f"{len(picks)} retenues sur {len(videos)} évaluées\n"]
    for v in picks:
        lines.append(f"- **{v['score']}/10** [{v['title']}](https://youtu.be/{v['id']}) "
                     f"— {v['channel']}, {v['minutes']} min  \n  {v['raison']}")
    lines.append("\n<details><summary>Écartées</summary>\n")
    for v in sorted(rejects, key=lambda v: -v["score"]):
        lines.append(f"- {v['score']}/10 {v['title']} — {v['channel']} : {v['raison']}")
    lines.append("\n</details>")
    report = "\n".join(lines)

    with open(REPORT_FILE, "w") as f:
        f.write(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report)
    save_state(state)
    print(report)


if __name__ == "__main__":
    main()
