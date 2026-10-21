"""Waptap (waptap.com): full catalogue of its videos. Runs on a US GitHub runner (Cloudflare 403s Indian IPs).

robots.txt: waptap.com allows everything; api.waptap.com is `Disallow: /`, so its /v1/media API is never used.
Listing = sitemap waptap.com/sitemap.xml -> xml/medias_*.xml (/feed/<slug>), each file asserted whole (</urlset>).
Fields = each /feed/<slug> page, whose React Query state holds the site's full item JSON (file, cover, size,
duration, width, height, hashtags, creator, likes, visits, comments, gender, created_at). raw = {"page_item": item,
"sitemap": the sitemap's video:video block}. A page with no item writes nothing and goes to errors for the repass; a 404 page, a site ad or a live-cam promo
goes to skipped.jsonl with its reason (Sink.skip).
Every fetch goes through RobotsFetcher, which refuses a disallowed URL.

  python crawl/sites/waptap.py [--limit N] [--gap S]
"""
import json, os, random, re, sys, time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Sink, Stop
from _png_robots import RobotsFetcher

SITE, BASE = "waptap", "https://waptap.com"
HDR = {"Accept": "text/html,application/xhtml+xml", "Referer": BASE + "/"}
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
GAP = float(sys.argv[sys.argv.index("--gap") + 1]) if "--gap" in sys.argv else 1.0


def norm(it):
    fd = it.get("file_data") or {}
    cr = it.get("creator") or {}
    tags = [h.get("hashtag") for h in it.get("hashtags") or [] if h.get("hashtag")]
    desc = (it.get("description") or "").strip() or None
    return dict(
        title=desc, description=desc, thumb=it.get("cover") or fd.get("cover_url"), file=it.get("file") or fd.get("url"),
        file_expires=False, expires_at=None, hls=None, duration=fd.get("duration") or it.get("length"),
        width=fd.get("width"), height=fd.get("height"), tags=tags or None,
        categories=[it["content_gender"]] if it.get("content_gender") else None,
        author=cr.get("username"), author_url=f"{BASE}/{cr['username']}" if cr.get("username") else None,
        posted=int(it["created_at"] / 1000) if it.get("created_at") else None,
        views=it.get("visit_count"), likes=it.get("like_count"),
    )


def rsc_items(html):
    """The /feed/<slug> page's item list, from the concatenated Next.js flight strings."""
    flight = "".join(json.loads('"' + s + '"') for s in re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html))
    i = flight.find('"items":[{"_id"')
    if i < 0:
        return []
    return json.JSONDecoder().raw_decode(flight, i + len('"items":'))[0]


class Run:
    def __init__(self):
        self.sink = Sink(SITE)
        self.f = RobotsFetcher(self.sink, gap=GAP, control=BASE + "/robots.txt", headers=HDR)
        self.status, self.smap, self.parts, self.ads, self.absent, self.last = Counter(), {}, {}, set(), set(), 0

    def get(self, url, **kw):
        out, st, t = self.f.get(url, **kw)
        self.status[str(st)] += 1
        return out, st, t

    def tick(self, force=False, **kw):
        if force or time.time() - self.last > 30:
            self.last = time.time()
            self.sink.progress(listed=len(self.smap), partitions=self.parts, status=dict(self.status),
                               skip_counts={"ads/live/non-video": len(self.ads), "removed upstream (feed page 404)": len(self.absent)},
                               gap=self.f.gap, **kw)

    def sitemap(self):
        out, st, t = self.get(BASE + "/sitemap.xml")
        if out != "ok":
            raise Stop(f"sitemap index {st}")
        for kid in re.findall(r"<loc>\s*([^<\s]+medias_\d+\.xml)", t):
            out, st, x = self.get(kid, timeout=90)
            if out != "ok" or not x.rstrip().endswith("</urlset>"):
                raise Stop(f"sitemap child {kid} {st} not whole")  # never let a short child shrink the list
            blocks = re.findall(r"<url>(.*?)</url>", x, re.S)
            self.parts["sitemap:" + kid.rsplit("/", 1)[1]] = len(blocks)
            for b in blocks:
                slug = re.search(r"/feed/([A-Za-z0-9_-]+)</loc>", b).group(1)
                self.smap[slug] = dict(re.findall(r"<video:([a-z_]+)>([^<]*)</video:\1>", b))

    def page(self, slug):
        out, st, t = self.get(f"{BASE}/feed/{slug}")
        if out != "ok":
            if out == "absent":
                self.absent.add(slug)
                self.sink.skip(slug, "removed upstream (feed page 404)")
            else:
                self.sink.error(slug, st)
            return
        items = [it for it in rsc_items(t) if it.get("slug") == slug]
        if not items:
            self.sink.error(slug, "no item in page state")
            return
        it = items[0]
        if it.get("is_advertisement") or it.get("is_livestream"):
            self.ads.add(slug)
            self.sink.skip(slug, "site ad or live-cam promo")
            return
        if (it.get("file_data") or {}).get("type_label") not in (None, "video"):
            self.ads.add(slug)
            self.sink.skip(slug, "not a video")
            return
        self.sink.item(slug, f"{BASE}/feed/{slug}", {"page_item": it, "sitemap": self.smap.get(slug)}, **norm(it))

    def pages(self, slugs, phase):
        for n, slug in enumerate(slugs):
            if LIMIT and len(self.sink.done) >= LIMIT:
                return
            if slug in self.sink.done or slug in self.ads or slug in self.absent:
                continue
            try:
                self.page(slug)
            except Stop:
                raise
            except Exception as e:
                self.sink.error(slug, repr(e))
            self.tick(phase=phase, done=n + 1, of=len(slugs))

    def live_check(self, n=3):
        rows = [json.loads(l) for l in open(os.path.join(self.sink.dir, "items.jsonl")) if l.strip().endswith("}")]
        res = []
        for r in random.sample(rows, min(n, len(rows))):
            out, st, t = self.get(r["url"])
            live = [it for it in rsc_items(t) if it.get("slug") == r["id"]] if out == "ok" else []
            ln = norm(live[0]) if live else {}
            sm = self.smap.get(r["id"]) or {}
            res.append({"id": r["id"], "status": st, "diff_vs_live_page": {k: [r.get(k), ln.get(k)] for k in
                        ("title", "file", "thumb", "duration", "width", "height", "author", "posted", "tags") if r.get(k) != ln.get(k)},
                        "file_eq_sitemap": r.get("file") == sm.get("content_loc")})
        return res

    def main(self):
        try:
            self.sitemap()
            self.tick(force=True, phase="sitemap")
            self.pages(list(self.smap), "pages")
            errs = os.path.join(self.sink.dir, "errors.jsonl")
            if os.path.exists(errs) and not LIMIT:  # repass errors at a lower rate
                self.f.gap = max(2.0, self.f.gap)
                self.pages(list(dict.fromkeys(json.loads(l)["id"] for l in open(errs))), "repass")
                self.f.gap = GAP
            if not LIMIT:  # re-walk the head: re-read the newest sitemap file for slugs added during the run
                before = set(self.smap)
                self.sitemap()
                self.parts["new_during_run"] = len(set(self.smap) - before)
                self.pages([s for s in self.smap if s not in before], "head")
            self.parts["live_check"] = self.live_check()
        except Stop as e:
            self.tick(force=True, stopped=str(e))
            raise
        listed = self.sink.listed(list(self.smap))
        print(json.dumps(self.sink.progress(listed=len(listed), partitions=self.parts, status=dict(self.status),
                                            ads_or_live=sorted(self.ads), page_absent=sorted(self.absent),
                                            note="done" if not LIMIT else f"limit {LIMIT}")))


if __name__ == "__main__":
    Run().main()
