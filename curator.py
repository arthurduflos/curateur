"""Curateur hebdo : récupère les nouvelles vidéos, les fait noter par Claude,
ajoute les meilleures à une playlist YouTube et écrit un rapport."""
import datetime as dt
import json
import os
import re
import time

import anthropic
import feedparser
import yaml
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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
    cids = [resolve_channel(yt, h, state) for h in cfg["channels"]]
    cids += list(state["learned"]["channels"])
    for cid in dict.fromkeys(cids):
        if not cid or cid in state["learned"]["blocked"]:
            continue
        feed = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
        for e in feed.entries:
            published = dt.datetime.fromisoformat(e.published.replace("Z", "+00:00"))
            if published >= since:
                ids.add(e.yt_videoid)
    return ids


def from_guests(yt, cfg, state, since):
    """Recherche par invité, tous podcasts confondus (100 unités de quota / nom)."""
    ids = set()
    for name in dict.fromkeys(cfg["guests"] + state["learned"]["guests"]):
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
                "channel_id": v["snippet"]["channelId"],
                "description": v["snippet"]["description"][:1200],
                "minutes": round(duration_min(v["contentDetails"]["duration"])),
            })
    return out


def feedback_text(state):
    L = state["learned"]
    fmt = lambda xs: "\n".join(f"- {x['title']} ({x['channel']})" for x in xs[-25:])
    parts = []
    if L["likes"]:
        parts.append("Vidéos récentes qu'il a aimées (👍) :\n" + fmt(L["likes"]))
    if L["dislikes"]:
        parts.append("Vidéos récentes qu'il n'a PAS aimées (👎) :\n" + fmt(L["dislikes"]))
    if L["seeds"]:
        parts.append("Vidéos qu'il a ajoutées lui-même pour découvrir de nouveaux sujets :\n" + fmt(L["seeds"]))
    return "\n\n".join(parts)


def score(videos, cfg, state):
    client = anthropic.Anthropic()
    fb = feedback_text(state)
    system = (
        "Tu es un curateur exigeant de podcasts et vidéos YouTube pour une personne précise.\n\n"
        f"PROFIL :\n{cfg['profile']}\n\n"
        + (f"SES RETOURS (prioritaires sur le profil en cas de doute) :\n{fb}\n\n" if fb else "")
        + f"GRILLE :\n{cfg['criteria']}\n\n"
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
RETRY_STATUS = (409, 429, 500, 503)


def call(request, tries=5):
    """Exécute une requête YouTube en réessayant sur les erreurs passagères (409 SERVICE_UNAVAILABLE…)."""
    for i in range(tries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in RETRY_STATUS and i < tries - 1:
                time.sleep(3 * 2 ** i)
                continue
            raise


def my_playlists(yt):
    out, token = [], None
    while True:
        res = call(yt.playlists().list(part="snippet,contentDetails", mine=True,
                                       maxResults=50, pageToken=token))
        out += res.get("items", [])
        token = res.get("nextPageToken")
        if not token:
            return out


def playlist_items(yt, playlist_id):
    out, token = [], None
    while True:
        res = call(yt.playlistItems().list(part="snippet", playlistId=playlist_id,
                                           maxResults=50, pageToken=token))
        out += res.get("items", [])
        token = res.get("nextPageToken")
        if not token:
            return out


def create_playlist(yt, cfg, title, description):
    res = call(yt.playlists().insert(part="snippet,status", body={
        "snippet": {"title": title, "description": description},
        "status": {"privacyStatus": cfg["playlist_privacy"]},
    }))
    print(f"Nouvelle playlist créée : {title}")
    time.sleep(10)  # YouTube a besoin de quelques secondes avant d'accepter des vidéos
    return res["id"]


def tidy_legacy_playlists(yt, state, playlists):
    """Anciennes playlists « Curation — Mois Année » : renomme celles qui ont des vidéos,
    supprime les doublons vides laissés par les runs plantés."""
    legacy = re.compile(r"^Curation — (\w+) (\d{4})$")
    known = set(state["playlists"].values())
    for p in playlists:
        m = legacy.match(p["snippet"]["title"])
        if not m:
            continue
        if p["contentDetails"]["itemCount"] == 0 and p["id"] not in known:
            call(yt.playlists().delete(id=p["id"]))
            print(f"Doublon vide supprimé : {p['snippet']['title']}")
            continue
        month = m.group(1)
        call(yt.playlists().update(part="snippet", body={
            "id": p["id"], "snippet": {"title": month, "description": p["snippet"].get("description", "")}}))
        p["snippet"]["title"] = month
        if month in MOIS:
            state["playlists"].setdefault(f"{m.group(2)}-{MOIS.index(month) + 1:02d}", p["id"])
        print(f"Playlist renommée : {month}")


def monthly_playlist(yt, cfg, state, playlists):
    """ID de la playlist du mois en cours (« Novembre »…), créée au besoin."""
    key = f"{NOW:%Y-%m}"
    if key in state["playlists"]:
        return state["playlists"][key]
    title = MOIS[NOW.month - 1]
    start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    for p in playlists:
        if p["snippet"]["title"] == title and p["snippet"]["publishedAt"] >= start[:19]:
            state["playlists"][key] = p["id"]
            return p["id"]
    pid = create_playlist(yt, cfg, title,
                          "Sélection automatique : interviews et contenus de fond notés par Claude.")
    state["playlists"][key] = pid
    save_state(state)
    return pid


def seed_playlist(yt, cfg, state, playlists):
    """Playlist « New » : on y enregistre depuis l'iPhone ce qu'on veut voir plus souvent."""
    name = cfg.get("seed_playlist", "New")
    if state.get("seed_playlist_id"):
        return state["seed_playlist_id"]
    for p in playlists:
        if p["snippet"]["title"] == name:
            state["seed_playlist_id"] = p["id"]
            return p["id"]
    state["seed_playlist_id"] = create_playlist(
        yt, cfg, name, "Ajoute ici des vidéos qui t'inspirent : le curateur suivra leur chaîne ou leur invité.")
    save_state(state)
    return state["seed_playlist_id"]


def add_to_playlist(yt, playlist_id, video_id):
    try:
        call(yt.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }}))
        time.sleep(1)
        return True
    except HttpError as e:
        print(f"⚠️  Ajout impossible ({video_id}) : {e}")
        return False


def process_seeds(yt, cfg, state, seed_id, month_id):
    """Pour chaque vidéo de « New » : Claude décide quelle chaîne / quel invité suivre,
    puis la vidéo passe dans la playlist du mois."""
    items = playlist_items(yt, seed_id)
    if not items:
        return []
    vids = details(yt, [i["snippet"]["resourceId"]["videoId"] for i in items])
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=cfg["model"], max_tokens=2000,
        system=(
            "La personne a enregistré ces vidéos YouTube pour signaler qu'elle veut découvrir "
            "plus de contenus de ce genre. Pour chacune, décide ce qu'il faut suivre :\n"
            "- follow_channel : true si la chaîne est une émission/créateur de fond à suivre en entier, "
            "false si c'est une chaîne générique, un média généraliste ou une chaîne d'extraits ;\n"
            "- guest : le nom de la personne interviewée à suivre dans tous les podcasts, ou null.\n"
            "Réponds UNIQUEMENT par un tableau JSON : "
            '[{"id": "...", "follow_channel": true, "guest": "Nom" ou null}]'),
        messages=[{"role": "user", "content": json.dumps(vids, ensure_ascii=False)}],
    )
    text = re.sub(r"```(json)?", "", "".join(b.text for b in msg.content if b.type == "text")).strip()
    try:
        decisions = {d["id"]: d for d in json.loads(text)}
    except json.JSONDecodeError:
        print("⚠️  Réponse illisible pour la playlist New, on réessaiera au prochain run")
        return []

    L, done = state["learned"], []
    by_id = {v["id"]: v for v in vids}
    for it in items:
        vid = it["snippet"]["resourceId"]["videoId"]
        v, d = by_id.get(vid), decisions.get(vid)
        if not v or not d:
            continue
        if d.get("follow_channel"):
            L["channels"][v["channel_id"]] = v["channel"]
            L["blocked"].pop(v["channel_id"], None)
        guest = d.get("guest")
        if guest and guest not in cfg["guests"] and guest not in L["guests"]:
            L["guests"] = (L["guests"] + [guest])[-cfg.get("max_learned_guests", 25):]
        L["seeds"].append({"title": v["title"], "channel": v["channel"]})
        state["seen"][vid] = NOW.isoformat()
        if month_id:
            add_to_playlist(yt, month_id, vid)
        call(yt.playlistItems().delete(id=it["id"]))
        done.append(f"{v['title']} — {v['channel']}"
                    + (" → chaîne suivie" if d.get("follow_channel") else "")
                    + (f" → invité suivi : {guest}" if guest else ""))
    L["seeds"] = L["seeds"][-50:]
    return done


def collect_ratings(yt, cfg, state):
    """Lit tes 👍 / 👎 sur les vidéos ajoutées par le curateur et bloque les chaînes rejetées."""
    L = state["learned"]
    cutoff = (NOW - dt.timedelta(days=90)).isoformat()
    pending = {k: v for k, v in state["added"].items() if "rating" not in v and v["date"] >= cutoff}
    ids, news = list(pending), []
    for i in range(0, len(ids), 50):
        res = call(yt.videos().getRating(id=",".join(ids[i:i + 50])))
        for r in res.get("items", []):
            rating = r["rating"]
            if rating not in ("like", "dislike"):
                continue
            v = state["added"][r["videoId"]]
            v["rating"] = rating
            entry = {"title": v["title"], "channel": v["channel"]}
            (L["likes"] if rating == "like" else L["dislikes"]).append(entry)
            news.append(("👍 " if rating == "like" else "👎 ") + v["title"])
    L["likes"], L["dislikes"] = L["likes"][-50:], L["dislikes"][-50:]

    # Chaîne bloquée dès N 👎 sans aucun 👍
    stats = {}
    for v in state["added"].values():
        if "rating" in v:
            s = stats.setdefault(v["channel_id"], [0, 0, v["channel"]])
            s[0 if v["rating"] == "like" else 1] += 1
    for cid, (likes, dislikes, name) in stats.items():
        if dislikes >= cfg.get("block_after_dislikes", 2) and likes == 0 and cid not in L["blocked"]:
            L["blocked"][cid] = name
            L["channels"].pop(cid, None)
            news.append(f"⛔ Chaîne écartée : {name}")
    # on oublie les vidéos de plus de 6 mois
    old = (NOW - dt.timedelta(days=180)).isoformat()
    state["added"] = {k: v for k, v in state["added"].items() if v["date"] >= old}
    return news


def main():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    state = load_state()
    state.setdefault("playlists", {})
    state.setdefault("added", {})
    state.setdefault("learned", {})
    for k, default in (("channels", {}), ("guests", []), ("blocked", {}),
                       ("likes", []), ("dislikes", []), ("seeds", [])):
        state["learned"].setdefault(k, default)
    yt = youtube_client()
    since = NOW - dt.timedelta(days=cfg["lookback_days"])
    log = []

    try:
        playlists = my_playlists(yt)
        tidy_legacy_playlists(yt, state, playlists)
        month_id = monthly_playlist(yt, cfg, state, playlists)

        # 1. Tes retours : 👍/👎 et playlist « New »
        log += collect_ratings(yt, cfg, state)
        log += ["🌱 " + s for s in process_seeds(yt, cfg, state, seed_playlist(yt, cfg, state, playlists), month_id)]
        save_state(state)

        # 2. Nouvelles vidéos
        ids = from_channels(yt, cfg, state, since) | from_guests(yt, cfg, state, since)
        ids -= set(state["seen"])
        videos = [v for v in details(yt, ids)
                  if v["minutes"] >= cfg["min_duration_min"] and v["channel_id"] not in state["learned"]["blocked"]]
        print(f"{len(videos)} nouvelles vidéos à évaluer")

        scores = score(videos, cfg, state) if videos else {}
        picks, rejects = [], []
        for v in videos:
            s = scores.get(v["id"])
            state["seen"][v["id"]] = NOW.isoformat()
            if not s:
                continue
            v.update(score=s["score"], raison=s["raison"])
            (picks if s["score"] >= cfg["score_threshold"] else rejects).append(v)

        picks.sort(key=lambda v: -v["score"])
        for v in picks:
            if add_to_playlist(yt, month_id, v["id"]):
                state["added"][v["id"]] = {"title": v["title"], "channel": v["channel"],
                                           "channel_id": v["channel_id"], "date": NOW.isoformat()}
    finally:
        save_state(state)

    lines = [f"# Sélection du {NOW:%d/%m/%Y}\n", f"{len(picks)} retenues sur {len(videos)} évaluées\n"]
    if log:
        lines.append("## Tes retours pris en compte\n")
        lines += [f"- {x}" for x in log]
        lines.append("")
    lines.append("## Retenues\n")
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
    print(report)


if __name__ == "__main__":
    main()
