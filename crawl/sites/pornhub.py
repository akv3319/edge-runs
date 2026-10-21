"""pornhub (www.pornhub.com): full catalogue from its own video sitemaps. Runs on a US GitHub runner (Airtel sinks
and resets pornhub). Also the engine for PornHub Shorties (phshorties.py).

Listing and fields = www.pornhub.com/sitemaps.xml -> sitemap_g_vids1..294 (15,000 viewkeys per file) or
sitemap_g_shorties1..8. Every file must end in </urlset> and is counted. Per video: title, thumbnail (signed to
2125), publication date, uploader + URL, country restriction; raw = the parsed sitemap block.
robots.txt (user-agent *) disallows the richer bulk routes, so they are not used: /shorties/analyticsData?...&token=
and /shorties/get?token= (`*token=*`), /webmasters/video_by_id?id= (`*/video*id=*`). Every fetch goes through
RobotsFetcher, which refuses a disallowed URL. Exact duration, views, tags: only the per-video watch page (allowed,
~1.2 MB each), out of this pass.

  python crawl/sites/pornhub.py --files 1-50 [--limit N]      (1-based sitemap numbers, inclusive)
"""
import html, json, os, random, re, sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Sink, Stop
from _png_robots import RobotsFetcher

BASE = "https://www.pornhub.com"
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None


def arg_files(default):
    if "--files" not in sys.argv:
        return default
    a, _, b = sys.argv[sys.argv.index("--files") + 1].partition("-")
    return range(int(a), int(b or a) + 1)


def block(b):
    d = dict(re.findall(r"<video:([a-z_]+)>([^<]*)</video:\1>", b))
    d = {k: html.unescape(v) for k, v in d.items()}
    d["loc"] = re.search(r"<loc>([^<]+)</loc>", b).group(1)
    m = re.search(r'<video:uploader info="([^"]*)">([^<]*)</video:uploader>', b)
    if m:
        d["uploader"], d["uploader_info"] = html.unescape(m[2]), m[1]
    m = re.search(r'<video:restriction relationship="([^"]*)">([^<]*)</video:restriction>', b)
    if m:
        d["restriction"] = {"relationship": m[1], "countries": m[2].split()}
    m = re.search(r"<lastmod>([^<]+)</lastmod>", b)
    if m:
        d["lastmod"] = m[1]
    return d


def norm(sm, shorties):
    title = (sm.get("title") or "").strip()
    if shorties:
        title = re.sub(r"\s*-\s*Porn Shortie\s*$", "", title)
    try:
        posted = int(datetime.fromisoformat(sm["publication_date"]).timestamp())
    except (KeyError, ValueError):
        posted = None
    return dict(
        title=title or None, description=None, thumb=sm.get("thumbnail_loc"), file=None, file_expires=True,
        expires_at=None, hls=None, duration=None, width=None, height=None, tags=None, categories=None,
        author=sm.get("uploader"), author_url=sm.get("uploader_info") or None, posted=posted, views=None, likes=None,
    )


class Run:
    def __init__(self, site="pornhub", family="vids", files=range(1, 295)):
        self.site, self.family, self.files, self.shorties = site, family, arg_files(files), family == "shorties"
        self.sink = Sink(site)
        self.f = RobotsFetcher(self.sink, gap=1.0, control=BASE + "/robots.txt")
        for c, v in [("age_verified", "1"), ("accessAgeDisclaimerPH", "1"), ("platform", "pc")]:
            self.f.s.cookies.set(c, v, domain=".pornhub.com")
        self.status, self.parts, self.ids = Counter(), {}, []

    def get(self, url, **kw):
        out, st, t = self.f.get(url, **kw)
        self.status[str(st)] += 1
        return out, st, t

    def sitemap(self, n):
        url = f"{BASE}/sitemap_g_{self.family}{n}.xml"
        out, st, t = self.get(url, timeout=180)
        if out != "ok" or not t.rstrip().endswith("</urlset>"):
            raise Stop(f"{url}: {st}, not whole")  # never let a short file shrink the listing
        blocks = re.findall(r"<url>(.*?)</url>", t, re.S)
        rows = {}
        for b in blocks:
            d = block(b)
            m = re.search(r"(?:viewkey=|/shorties/)([A-Za-z0-9]+)", d["loc"])
            if m:
                rows[m[1]] = d
        self.parts[str(n)] = len(blocks)
        return rows

    def write(self, rows):
        for k, sm in rows.items():
            if LIMIT and len(self.sink.done) >= LIMIT:
                return
            try:
                url = f"{BASE}/shorties/{k}" if self.shorties else f"{BASE}/view_video.php?viewkey={k}"
                self.sink.item(k, url, {"sitemap": sm}, **norm(sm, self.shorties))
            except Exception as e:
                self.sink.error(k, repr(e))

    def live_check(self, n=3):
        """3 random rows against their live (robots-allowed) watch page: title and uploader in the page."""
        rows = [json.loads(l) for l in open(os.path.join(self.sink.dir, "items.jsonl")) if l.strip().endswith("}")]
        res = []
        for r in random.sample(rows, min(n, len(rows))):
            out, st, t = self.get(r["url"], headers={"Accept": "text/html"}, timeout=60)
            page = html.unescape(t or "")
            res.append({"id": r["id"], "status": st, "title_in_page": bool(r.get("title")) and r["title"][:40] in page,
                        "author_in_page": bool(r.get("author")) and r["author"] in page})
        return res

    def main(self):
        try:
            for n in self.files:
                rows = self.sitemap(n)
                self.ids += list(rows)
                self.write(rows)
                self.sink.progress(listed=len(self.ids), files=self.parts, status=dict(self.status), phase=f"file {n} done")
                if LIMIT and len(self.sink.done) >= LIMIT:
                    break
            self.parts["live_check"] = self.live_check()
        except Stop as e:
            self.sink.progress(listed=len(self.ids), files=self.parts, status=dict(self.status), stopped=str(e))
            raise
        self.sink.listed(self.ids)
        print(json.dumps(self.sink.progress(listed=len(set(self.ids)), files=self.parts, status=dict(self.status),
                                            robots={h: len(r) for h, r in self.f.rules.items()},
                                            note="done" if not LIMIT else f"limit {LIMIT}")))


if __name__ == "__main__":
    Run().main()
