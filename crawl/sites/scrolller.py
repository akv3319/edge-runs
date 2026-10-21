"""Scrolller TikTok collection (scrolller.com/pd/tiktok-collection): only that collection, which is Scrolller's
mirror of r/tiktokporn (subredditId 1211562). Runs on a US GitHub runner (the page times out from India).

Listing: the subreddit's declared counts (itemCount / videoCount / pictureCount in the page state), then
getSubredditChildren (GraphQL POST api.scrolller.com/admin) walked to the end twice, sortBy NEW and TOP, filter
VIDEO, limit 100. The whole-site sitemaps carry no subreddit marker, so the proof is walk vs videoCount.
The listing item is the full payload (the post page adds only the same fields as JSON-LD).

  python crawl/sites/scrolller.py [--limit N]
"""
import json, os, re, sys, time
from html import unescape as html_unescape
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Sink, Stop, classify
from _png_robots import RobotsFetcher  # refuses any URL robots.txt disallows (scrolller.com and api.scrolller.com: Allow: /)

SITE, BASE, GQL, SUB = "scrolller", "https://scrolller.com", "https://api.scrolller.com/admin", 1211562
HDR = {"Accept": "*/*", "Content-Type": "application/json", "Referer": BASE + "/", "Origin": BASE}
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
Q = (" query SubredditChildrenQuery( $subredditId: Int! $iterator: String $filter: GalleryFilter $sortBy: GallerySortBy"
     " $limit: Int! $nsfw: NsfwFilter ) { getSubredditChildren( data: { subredditId: $subredditId, iterator: $iterator,"
     " filter: $filter, sortBy: $sortBy, limit: $limit, nsfw: $nsfw }, ) { iterator items { __typename id url title"
     " reddit_posted_by subredditId subredditTitle subredditUrl redditPath isNsfw hasAudio fullLengthSource gfycatSource"
     " redgifsSource ownerAvatar username displayName userType favoriteCount repostCount isPaid tags commentsCount"
     " commentsRepliesCount duration createdAt albumContent { mediaSources { url width height isOptimized type } }"
     " mediaSources { url width height isOptimized type } blurredMediaSources { url width height isOptimized type } } } } ")


def secs(iso):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", iso or "")
    return round(int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + float(m[3] or 0), 3) if m and iso != "PT" else None


def norm(it):
    src = it.get("mediaSources") or []
    vids = sorted((s for s in src if s.get("type") in ("MP4", "WEBM")), key=lambda s: (s["type"] == "MP4", s.get("width") or 0), reverse=True)
    imgs = sorted((s for s in src if s.get("type") in ("WEBP", "JPEG", "JPG", "PNG")), key=lambda s: s.get("width") or 0, reverse=True)
    v = vids[0] if vids else {}
    who = it.get("reddit_posted_by") or it.get("username")
    return dict(
        title=it.get("title"), description=None, thumb=imgs[0]["url"] if imgs else None, file=v.get("url"),
        file_expires=False, expires_at=None, hls=None, duration=secs(it.get("duration")),
        width=v.get("width"), height=v.get("height"), tags=it.get("tags") or None,
        categories=[it["subredditTitle"]] if it.get("subredditTitle") else None,
        author=who, author_url=f"https://www.reddit.com/user/{who}" if it.get("reddit_posted_by") else None,
        posted=int(datetime.fromisoformat(it["createdAt"].replace("Z", "+00:00")).timestamp()) if it.get("createdAt") else None,
        views=None, likes=it.get("favoriteCount"),
    )


class Run:
    def __init__(self):
        self.sink = Sink(SITE)
        self.f = RobotsFetcher(self.sink, gap=1.0, control=BASE + "/robots.txt", headers=HDR)
        self.status, self.walks, self.ids, self.other, self.novideo, self.declared, self.last = Counter(), {}, set(), set(), set(), {}, 0

    def tick(self, force=False, **kw):
        if force or time.time() - self.last > 30:
            self.last = time.time()
            self.sink.progress(listed=len(self.ids), declared=self.declared, walks=self.walks, status=dict(self.status),
                               other_subreddit=len(self.other), no_video_source=len(self.novideo), **kw)

    def post(self, url, body):
        """Fetcher.get's rules (gap, wire bytes, 3 outcomes) for the one POST route; Fetcher has no POST."""
        f = self.f
        if not f.allowed(url):
            raise Stop(f"robots.txt disallows {url}")
        wait = f.gap - (time.time() - f.last)
        if wait > 0:
            time.sleep(wait)
        f.last = time.time()
        try:
            r = f.s.post(url, data=body, timeout=30)
            text, st = r.text, r.status_code
            self.sink.bytes += len(r.content)
        except Exception as e:
            text, st = "", f"exc:{type(e).__name__}"
        out = classify(st, text) if isinstance(st, int) else "unreachable"
        f.bad = f.bad + 1 if out == "unreachable" else 0
        if f.bad >= 5:
            f._stop_or_continue(st)
        return out, st, text

    def gq(self, var):
        out, st, t = self.post(GQL, json.dumps({"query": Q, "variables": var, "authorization": None}))
        self.status[str(st)] += 1
        if out != "ok":
            return None, st
        d = json.loads(t)
        if d.get("errors"):
            return None, str(d["errors"])[:200]
        return d["data"]["getSubredditChildren"], st

    def walk(self, sort, max_pages=None):
        it, n, seen = None, 0, set()
        while True:
            var = {"subredditId": SUB, "sortBy": sort, "limit": 100, "nsfw": "NSFW", "filter": "VIDEO"}
            if it:
                var["iterator"] = it
            g, st = self.gq(var)
            if g is None:
                self.sink.error(f"walk:{sort}:{it}", st)
                g, st = self.gq(var)  # one retry, then stop this walk
                if g is None:
                    self.walks[sort + ":broken_at"] = it
                    break
            for x in g.get("items") or []:
                try:
                    if x.get("subredditId") != SUB:
                        self.other.add(x["id"])
                        continue
                    seen.add(str(x["id"]))
                    self.ids.add(str(x["id"]))
                    nv = norm(x)
                    if not nv["file"]:
                        self.novideo.add(x["id"])
                    self.sink.item(x["id"], BASE + x["url"], x, **nv)
                except Exception as e:
                    self.sink.error(x.get("id"), repr(e))
            n += 1
            if not max_pages:
                self.walks[sort] = len(seen)
            self.tick(phase=f"walk {sort}", pages=n)
            it = g.get("iterator")
            if not it or not g.get("items") or (max_pages and n >= max_pages) or (LIMIT and len(self.sink.done) >= LIMIT):
                break
        return seen

    def live_check(self, n=3):
        """3 random stored rows against their live post page (JSON-LD VideoObject + the file name in the page)."""
        import random
        rows = [json.loads(l) for l in open(os.path.join(self.sink.dir, "items.jsonl")) if l.strip().endswith("}")]
        res = []
        for r in random.sample(rows, min(n, len(rows))):
            out, st, t = self.f.get(r["url"], headers={"Accept": "text/html"})
            m = re.search(r'"@type":"VideoObject".*?"duration":"([^"]*)","uploadDate":"([^"]*)"', t or "")
            stem = (r.get("file") or "").rsplit("/", 1)[-1].split(".")[0].replace("-mobile", "")
            res.append({"id": r["id"], "status": st, "title_in_page": (r.get("title") or "")[:40] in html_unescape(t or ""),
                        "file_stem_in_page": bool(stem) and stem in (t or ""),
                        "duration": [r.get("duration"), secs(m[1]) if m else None],
                        "posted": [r.get("posted"), int(datetime.fromisoformat(m[2].replace("Z", "+00:00")).timestamp()) if m else None]})
        return res

    def pictures(self):
        """The collection's pictures: listed and skipped by id, so listed = rows + excluded + skipped."""
        g, st = self.gq({"subredditId": SUB, "sortBy": "NEW", "limit": 100, "nsfw": "NSFW", "filter": "PICTURE"})
        n = 0
        while g:
            for x in g.get("items") or []:
                if x.get("subredditId") == SUB:
                    self.ids.add(str(x["id"]))
                    self.sink.skip(x["id"], "picture, not a video")
                    n += 1
            if not g.get("iterator") or not g.get("items"):
                break
            g, st = self.gq({"subredditId": SUB, "sortBy": "NEW", "limit": 100, "nsfw": "NSFW", "filter": "PICTURE", "iterator": g["iterator"]})
        self.walks["PICTURE"] = n

    def main(self):
        try:
            out, st, t = self.f.get(BASE + "/pd/tiktok-collection", headers={"Accept": "text/html"})
            m = re.search(r'\\?"itemCount\\?":(\d+),\\?"videoCount\\?":(\d+),\\?"pictureCount\\?":(\d+)', t or "")
            if m:
                self.declared = {"itemCount": int(m[1]), "videoCount": int(m[2]), "pictureCount": int(m[3])}
            self.walk("NEW")
            self.pictures()
            if not LIMIT:
                self.walk("TOP")
                self.walks["NEW_head"] = len(self.walk("NEW", max_pages=2))  # re-walk the head for posts added during the run
            self.walks["live_check"] = self.live_check()
        except Stop as e:
            self.sink.progress(listed=len(self.ids), stopped=str(e))
            raise
        self.sink.listed(sorted(self.ids, key=int))
        print(json.dumps(self.sink.progress(listed=len(self.ids), declared=self.declared, walks=self.walks, status=dict(self.status),
                                            other_subreddit=len(self.other), no_video_source=sorted(self.novideo),
                                            note="done" if not LIMIT else f"limit {LIMIT}")))


if __name__ == "__main__":
    Run().main()
