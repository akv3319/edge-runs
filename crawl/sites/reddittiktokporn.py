"""Reddit TikTok Porn (r/tiktokporn): every video post. Runs on the Dell (home line).

Reddit's own listing is closed to us: www.reddit.com shows an interactive reCAPTCHA ("Prove your humanity"),
old.reddit.com redirects NSFW to /login, .json is 403 (2026-10-21). We never solve a captcha or log in, so the
listing comes from Arctic Shift (arctic-shift.photon-reddit.com), a free public archive of Reddit's own post
objects: /api/posts/search?subreddit=tiktokporn&sort=asc&limit=100&after=<created_utc>, keyset by created_utc.
Every raw row is the archived Reddit post object, tagged {"source": "arctic-shift archive"}.

Video posts (Reddit-hosted video, redgifs, gfycat, imgur gifv/mp4, a video preview) become rows; other posts
(images, links, self posts) go to skipped.jsonl as "not a video post" (Sink.skip), counted per domain in progress.

  uv run --no-project --with curl_cffi python crawl/sites/reddittiktokporn.py [--limit N]
"""
import html, json, os, sys, time
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Sink, Stop
from _png_robots import RobotsFetcher  # arctic-shift robots.txt: Disallow: (empty), checked per URL anyway

SITE, SUB = "reddittiktokporn", "tiktokporn"
API = "https://arctic-shift.photon-reddit.com/api/posts/search"
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
VIDEO_DOMAINS = ("redgifs.com", "gfycat.com", "v.redd.it", "streamable.com", "imgur.com", "tiktok.com")


def video_of(p):
    """(file, hls, duration, width, height) of a video post, or None for a non-video post."""
    rv = ((p.get("secure_media") or p.get("media") or {}).get("reddit_video")
          or ((p.get("preview") or {}).get("reddit_video_preview")))
    url = p.get("url_overridden_by_dest") or p.get("url") or ""
    dom = p.get("domain") or ""
    if rv:
        return rv.get("fallback_url"), rv.get("hls_url"), rv.get("duration"), rv.get("width"), rv.get("height")
    if url.split("?")[0].endswith((".mp4", ".webm", ".gifv")):
        return url.replace(".gifv", ".mp4") if "imgur.com" in url else url, None, None, None, None
    if p.get("is_video") or p.get("post_hint") in ("hosted:video", "rich:video") or any(dom.endswith(d) for d in VIDEO_DOMAINS):
        return None, None, None, None, None  # a video, but no direct file in the post object (e.g. a redgifs watch page)
    return None


def thumb_of(p):
    imgs = (p.get("preview") or {}).get("images") or []
    if imgs and imgs[0].get("source", {}).get("url"):
        return html.unescape(imgs[0]["source"]["url"])
    oe = (p.get("secure_media") or p.get("media") or {}).get("oembed") or {}
    if oe.get("thumbnail_url"):
        return html.unescape(oe["thumbnail_url"])
    t = p.get("thumbnail") or ""
    return t if t.startswith("http") else None


def norm(p, v):
    file, hls, dur, w, h = v
    return dict(
        title=p.get("title"), description=None if (p.get("selftext") or "").strip() in ("", "[deleted]", "[removed]") else p["selftext"].strip(),
        thumb=thumb_of(p),
        file=html.unescape(file) if file else None, file_expires=False, expires_at=None,
        hls=html.unescape(hls) if hls else None, duration=dur, width=w, height=h,
        tags=[p["link_flair_text"]] if p.get("link_flair_text") else None, categories=[p.get("subreddit") or SUB],
        author=p.get("author"), author_url=f"https://www.reddit.com/user/{p['author']}" if p.get("author") not in (None, "[deleted]") else None,
        posted=int(p["created_utc"]) if p.get("created_utc") else None, views=None, likes=p.get("score"),
    )


class Run:
    def __init__(self):
        self.sink = Sink(SITE)
        # 1 req/s drew 9 x 422 "slow down" in 40 calls (each page is ~550 KB of post objects); 4 s apart, none
        self.f = RobotsFetcher(self.sink, gap=3.0, control=API + f"?subreddit={SUB}&limit=1")
        self.status, self.years, self.vyears, self.skipped, self.ids, self.nofile, self.last = Counter(), Counter(), Counter(), Counter(), set(), 0, 0

    def tick(self, force=False, **kw):
        if force or time.time() - self.last > 30:
            self.last = time.time()
            self.sink.progress(listed=len(self.ids), posts_by_year=dict(self.years), videos_by_year=dict(self.vyears),
                               skip_counts={"not a video post": sum(self.skipped.values())},
                               non_video_by_domain=dict(self.skipped.most_common(25)), video_without_direct_file=self.nofile,
                               status=dict(self.status), **kw)

    def get(self, url):
        for _ in range(6):
            out, st, t = self.f.get(url, timeout=90)
            self.status[str(st)] += 1
            if st in (429, 422):  # 429 = their rate limit, 422 = "Timeout. Maybe slow down a bit": wait, slow down, retry
                self.f.gap = min(self.f.gap * 1.5, 10.0)
                time.sleep(60)
                continue
            return out, st, t
        return out, st, t

    def walk(self, after=0, max_pages=None):
        pages = 0
        while True:
            out, st, t = self.get(f"{API}?subreddit={SUB}&sort=asc&limit=100&after={after}")
            if out != "ok":
                self.sink.error(f"after:{after}", st)
                raise Stop(f"listing page after={after} answered {st}")
            rows = json.loads(t).get("data") or []
            for p in rows:
                try:
                    pid = p["id"]
                    yr = datetime.fromtimestamp(p["created_utc"], timezone.utc).year
                    if pid not in self.ids:
                        self.years[yr] += 1
                    self.ids.add(pid)
                    v = video_of(p)
                    if v is None:
                        self.skipped[p.get("domain") or "?"] += 1
                        self.sink.skip(pid, "not a video post")  # image, link or self post; its domain is counted in progress
                        continue
                    self.vyears[yr] += 1
                    if not v[0] and not v[1]:
                        self.nofile += 1
                    self.sink.item(pid, "https://www.reddit.com" + (p.get("permalink") or f"/comments/{pid}/"),
                                   {"source": "arctic-shift archive", "post": p}, **norm(p, v))
                except Exception as e:
                    self.sink.error(p.get("id"), repr(e))
            pages += 1
            self.tick(phase="walk", after=after, pages=pages)
            if len(rows) < 100 or (max_pages and pages >= max_pages) or (LIMIT and len(self.sink.done) >= LIMIT):
                return after
            nxt = int(rows[-1]["created_utc"])
            after = max(nxt - 1, after + 1)  # keyset on created_utc, one second back so a shared second is not lost; Sink dedups

    def declared(self):
        """The archive's own counts for the subreddit, per year (aggregate endpoint and its time series)."""
        d = {}
        # the aggregate endpoint timed out (422 "slow down") and loaded the server; the time series is the cheap count
        for k, u in [("time_series", f"https://arctic-shift.photon-reddit.com/api/time_series?key=r/{SUB}/posts/count&precision=year")]:
            out, st, t = self.get(u)
            try:
                d[k] = {datetime.fromtimestamp(int(r.get("date") or r.get("created_utc")) / (1000 if int(r.get("date") or r.get("created_utc")) > 1e11 else 1),
                                               timezone.utc).year: r.get("value", r.get("count")) for r in json.loads(t)["data"]}
            except Exception as e:
                d[k] = f"{st} {repr(e)[:80]} {t[:120]}"
        return d

    def main(self):
        self.decl = {}
        try:
            self.decl["start"] = self.declared()
            start = 1118000000  # 2005-06, before Reddit; after=0 answers 400. A restart re-walks (~1 page/s), so listing stays whole
            last = self.walk(start)
            if not LIMIT:
                self.walk(max(last - 86400, 0), max_pages=5)  # re-walk the head (last day) for late-archived posts
        except Stop as e:
            self.tick(force=True, declared=self.decl, stopped=str(e))
            raise
        self.decl["end"] = self.declared()
        self.sink.listed(sorted(self.ids))
        self.tick(force=True, declared=self.decl, note="done" if not LIMIT else f"limit {LIMIT}")
        print(open(os.path.join(self.sink.dir, "progress.json")).read())


if __name__ == "__main__":
    Run().main()
