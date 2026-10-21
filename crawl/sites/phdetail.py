"""pornhub + PornHub Shorties per-video detail (pn-j): exact duration, views, likes/dislikes/rating, tags, categories,
pornstars, production, uploader type, orientation, language, quality, publish date, from the desktop watch page.
Runs on a US GitHub runner (Airtel blocks pornhub), one sitemap file per job.

Route: /view_video.php?viewkey=<vkey> (robots-allowed for *, ~1.3 MB, ~170 KB brotli on the wire), age cookies,
curl_cffi chrome, 1 req/s. Every URL goes through RobotsFetcher. Listing = the job's own sitemap file(s)
(sitemap_g_vids<N> or sitemap_g_shorties<N>, oldest first, stable by id). raw = the page's ld+json, its dataLayer
videodata, a few flashvars keys and the extracted lists; never the HTML. Rows go through Sink.item, so the minors and
non-consent rules now see tags, categories and pornstars. 5 unreachable in a row stops (no control url); any
403/429/challenge backs off 60-300 s and 10 in one run stop the job, so a block is never pushed through.

  python crawl/sites/phdetail.py --files 7-7 [--kind shorties] [--limit N]
Self-check (parser on saved pages, nothing fetched): python3 crawl/sites/phdetail.py --selfcheck <page.html>...
"""
import html, json, os, random, re, sys, time
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MINOR, NONCONSENT, Sink, Stop
from _png_robots import RobotsFetcher
from pornhub import BASE, arg_files, block

LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
KIND = "shorties" if "--kind" in sys.argv and sys.argv[sys.argv.index("--kind") + 1] == "shorties" else "vids"
FLASH_KEYS = ("video_duration", "isHD", "isVertical", "isVR", "video_unavailable", "video_unavailable_country", "image_url")
FILL = ("duration", "views", "likes", "rating", "tags", "categories", "pornstars", "production", "author", "posted", "height")
GONE = re.compile(r"video has been (?:removed|disabled|flagged)|removed at the request|page not found|"
                  r"video is unavailable|awaiting review|is private|has been deleted", re.I)


def _int(x):
    try:
        return int(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _iso_secs(d):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    return int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + int(m[3] or 0) if m and any(m.groups()) else None


def _links(block, rx):
    return [html.unescape(re.sub(r"<[^>]+>", "", t)).strip() for t in re.findall(rx, block or "", re.S)]


def parse(t):
    """Watch page text -> (norm, raw), or (None, reason) when the page carries no video."""
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', t, re.S)
    try:
        ld = json.loads(m[1]) if m else None
    except ValueError:
        ld = None
    if not isinstance(ld, dict) or ld.get("@type") != "VideoObject":
        g = GONE.search(t)
        title = re.search(r"<title>(.*?)</title>", t, re.S)
        return None, (f"gone: {g[0]}" if g else "no ld+json") + f"; title={(title[1].strip() if title else '')[:80]}"
    stats = {s.get("interactionType", "").rsplit("/", 1)[-1]: s.get("userInteractionCount") for s in ld.get("interactionStatistic") or []}
    m = re.search(r"'videodata'\s*:\s*\{(.*?)\}", t, re.S)
    vd = {k: html.unescape(v) for k, v in re.findall(r"'(\w+)'\s*:\s*'([^']*)'", m[1])} if m else {}
    m = re.search(r"var flashvars_(\d+) = (\{.*?\});\n", t, re.S)
    try:
        fv = json.loads(m[2]) if m else {}
    except ValueError:
        fv = {}
    flash = {k: fv[k] for k in FLASH_KEYS if k in fv}
    if m:
        flash["video_id"] = int(m[1])
    sizes = sorted({(d.get("height") or 0, d.get("width") or 0) for d in fv.get("mediaDefinitions") or [] if isinstance(d, dict) and d.get("height")})
    m = re.search(r'"currentUp":(\d+),"currentDown":(\d+)', t)
    up, down = (int(m[1]), int(m[2])) if m else (None, None)
    cats = _links(re.search(r'<div class="categoriesWrapper">(.*?)</div>', t, re.S)[1] if "categoriesWrapper" in t else "",
                  r'data-label="category"[^>]*>(.*?)</a>')
    tags = _links(re.search(r'<div class="tagsWrapper">(.*?)</div>', t, re.S)[1] if "tagsWrapper" in t else "",
                  r'data-label="tag"[^>]*>(.*?)</a>')
    ps_block = re.search(r'<div class="pornstarsWrapper[^"]*">(.*?)<div class="responseWrapper', t, re.S)
    stars = [{"name": html.unescape(re.sub(r"<[^>]+>", "", n)).strip(), "url": BASE + h, "id": _int(i)} for i, h, n in
             re.findall(r'data-id="(\d+)"\s+data-type="pornstar"[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', ps_block[1] if ps_block else "", re.S)]
    m = re.search(r'<div class="userInfo">\s*<div class="usernameWrap[^"]*" data-type="(\w+)"([^>]*)>(.*?)<div class="avatarPosition"', t, re.S)
    uploader = {}
    if m:
        a = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', m[3], re.S)
        uid, badge = re.search(r'data-\w*id="(\d+)"', m[2]), re.search(r'userBadges" data-title="([^"]+)"', m[3])
        uploader = {"type": m[1], "id": _int(uid[1]) if uid else None, "url": BASE + a[1] if a else None,
                    "name": html.unescape(re.sub(r"<[^>]+>", "", a[2] if a else m[3])).strip() or None, "badge": badge[1] if badge else None}
    try:
        posted = int(datetime.fromisoformat(ld["uploadDate"]).timestamp())
    except (KeyError, ValueError, TypeError):
        posted = None
    raw = {"ld": ld, "videodata": vd, "flash": flash, "tags": tags, "categories": cats, "pornstars": stars,
           "uploader": uploader, "votes": {"up": up, "down": down}, "sizes": sizes}
    norm = dict(
        title=html.unescape(ld.get("name") or "").strip() or None, duration=_int(fv.get("video_duration")) or _iso_secs(ld.get("duration")),
        width=sizes[-1][1] if sizes else None, height=sizes[-1][0] if sizes else None,
        tags=tags, categories=cats, pornstars=[s["name"] for s in stars],
        author=uploader.get("name") or ld.get("author"), author_url=uploader.get("url"), author_type=vd.get("video_uploader"),
        author_badge=uploader.get("badge"), posted=posted, views=_int(stats.get("WatchAction")),
        likes=up if up is not None else _int(stats.get("LikeAction")), dislikes=down,
        votes=(up + down) if up is not None else None, rating=round(100 * up / (up + down), 1) if up is not None and up + down else None,
        production=vd.get("video_production"), segment=vd.get("video_segment"), orientation=vd.get("video_orientation"),
        language=vd.get("language_spoken_in_video"), hd=vd.get("hd_video") == "Yes" if vd else None,
        vertical=str(fv.get("isVertical")) == "true" if fv else None, vr=bool(fv.get("isVR")) if fv else None,
        premium=vd.get("premium_video") == "Yes" if vd else None, video_id=flash.get("video_id"),
    )
    return norm, raw


class Run:
    def __init__(self):
        self.files = arg_files(None)
        if not self.files:
            raise SystemExit("--files A-B is required")
        self.sink = Sink("phdetail")
        self.f = RobotsFetcher(self.sink, gap=float(os.environ.get("PH_GAP", "1.0")))  # no control: 5 bad in a row stops
        for c, v in [("age_verified", "1"), ("accessAgeDisclaimerPH", "1"), ("platform", "pc")]:
            self.f.s.cookies.set(c, v, domain=".pornhub.com")
        self.wire, self.last = 0, None
        sget = self.f.s.get

        def metered(*a, **kw):  # common meters decompressed bytes; this adds what crossed the wire (brotli body + headers)
            r = self.last = sget(*a, **kw)
            self.wire += (getattr(r, "download_size", 0) or 0) + (getattr(r, "header_size", 0) or 0)
            return r
        self.f.s.get = metered
        self.status, self.fill, self.rows, self.refused, self.ids, self.parts = Counter(), Counter(), 0, 0, [], {}

    def get(self, url, **kw):
        self.last = None
        out, st, t = self.f.get(url, **kw)
        challenge = st == 200 and out == "unreachable"
        self.status["200-challenge" if challenge else str(st)] += 1
        if challenge or st in (403, 429):
            self.refused += 1
            self.save(phase=f"refused {st}, backing off")
            if self.refused >= 10:
                raise Stop(f"{self.refused} refusals (403/429/challenge) in this run: stopped, not pushed through")
            time.sleep(60 * min(self.refused, 5))
        return out, st, t

    def sitemap(self, n):
        url = f"{BASE}/sitemap_g_{KIND}{n}.xml"
        out, st, t = self.get(url, timeout=180)
        if out != "ok" or not t.rstrip().endswith("</urlset>"):
            raise Stop(f"{url}: {st}, not whole")
        blocks = re.findall(r"<url>(.*?)</url>", t, re.S)
        rows = {}
        for b in blocks:
            d = block(b)
            m = re.search(r"(?:viewkey=|/shorties/)([A-Za-z0-9]+)", d["loc"])
            if m:
                rows[m[1]] = d
        self.parts[str(n)] = len(blocks)
        if len(rows) != len(blocks):
            raise Stop(f"{url}: {len(blocks)} urls but {len(rows)} viewkeys parsed")  # a short list is never silent
        return rows

    def excluded_by_sitemap(self, k, sm):
        """The sitemap's own title + uploader under the two rules the Dell import applied: such a page is never fetched."""
        title = re.sub(r"\s*-\s*Porn Shortie\s*$", "", sm.get("title") or "")
        for rule, rx in (("minors", MINOR), ("nonconsent", NONCONSENT)):
            m = rx.search(f"{title} {sm.get('uploader') or ''}")
            if m:
                self.sink._append("excluded.jsonl", {"src": self.sink.site, "id": k, "rule": rule, "matched": m.group(0), "stage": "sitemap"})
                self.sink.done.add(k)
                return True
        return False

    def save(self, **extra):
        return self.sink.progress(listed=len(self.ids), kind=KIND, files=self.parts, status=dict(self.status),
                                  wire_compressed=self.wire, rows_this_run=self.rows, fill=dict(self.fill), **extra)

    def one(self, k):
        url = f"{BASE}/view_video.php?viewkey={k}"
        out, st, t = self.get(url, headers={"Accept": "text/html"}, timeout=60)
        final = str(getattr(self.last, "url", "") or url)
        if out == "absent":
            return self.sink.skip(k, f"absent upstream ({st})")
        if out != "ok":
            return self.sink.error(k, f"unreachable {st}")
        if k not in final:
            return self.sink.skip(k, f"redirected away ({final[:120]})")
        norm, raw = parse(t)
        if norm is None:
            return self.sink.skip(k, raw) if raw.startswith("gone") else self.sink.error(k, raw)
        self.rows += 1
        self.fill.update(f for f in FILL if norm.get(f) not in (None, [], ""))
        self.sink.item(k, url, raw, **norm)

    def live_check(self, n=3):
        """3 random rows of this part re-read from the live page: each stored tag/category/pornstar is on the page,
        duration and title equal, views within 5% (they move)."""
        p = os.path.join(self.sink.dir, "items.jsonl")
        rows = [json.loads(l) for l in open(p) if l.strip().endswith("}")] if os.path.exists(p) else []
        res = []
        for r in random.sample(rows, min(n, len(rows))):
            out, st, t = self.get(r["url"], headers={"Accept": "text/html"}, timeout=60)
            norm, _ = parse(t) if out == "ok" else (None, None)
            page = html.unescape(t or "")
            res.append({"id": r["id"], "status": st,
                        "tags_on_page": sum(f"<span>{x}</span>" in page for x in r.get("tags") or []), "tags": len(r.get("tags") or []),
                        "cats_on_page": sum(f">{x}</a>" in page for x in r.get("categories") or []), "cats": len(r.get("categories") or []),
                        "stars_on_page": sum(x in page for x in r.get("pornstars") or []), "stars": len(r.get("pornstars") or []),
                        "duration_eq": bool(norm) and norm["duration"] == r.get("duration"), "title_eq": bool(norm) and norm["title"] == r.get("title"),
                        "views_5pct": bool(norm) and bool(r.get("views")) and abs((norm["views"] or 0) - r["views"]) <= 0.05 * r["views"] + 5})
        return res

    def main(self):
        try:
            sm = {}
            for n in self.files:
                sm.update(self.sitemap(n))
            self.ids = list(sm)
            self.sink.listed(self.ids)
            self.save(phase="listed")
            todo = [k for k in self.ids if k not in self.sink.done and not self.excluded_by_sitemap(k, sm[k])]
            for i, k in enumerate(todo):
                if LIMIT and i >= LIMIT:
                    break
                try:
                    self.one(k)
                except Stop:
                    raise
                except Exception as e:  # per item, never per run
                    self.sink.error(k, repr(e))
                if i % 100 == 0:
                    self.save(phase=f"{i} of {len(todo)} this run")
            left = sum(k not in self.sink.done for k in self.ids)
            note = "done" if not left else f"{left} left (errors this run {self.sink.errors})"
            print(json.dumps(self.save(note=note if not LIMIT else f"limit {LIMIT}", left=left, live_check=self.live_check())))
        except (Stop, KeyboardInterrupt) as e:  # KeyboardInterrupt = crawl.yml's budget SIGINT; the next run resumes
            self.save(stopped=f"{type(e).__name__}: {e}")
            raise


def selfcheck(paths):
    for p in paths:
        norm, raw = parse(open(p, encoding="utf-8", errors="replace").read())
        assert norm, (p, raw)
        for f in ("duration", "views", "likes", "dislikes", "rating", "tags", "categories", "author", "posted", "height", "production"):
            assert norm[f] not in (None, [], ""), (p, f, norm[f])
        assert len(json.dumps(raw)) < 20000, (p, "raw stays small, no HTML")
        print(p, {k: norm[k] for k in ("title", "duration", "views", "likes", "dislikes", "rating", "height", "production",
                                        "author", "author_type", "pornstars", "posted")}, len(norm["tags"]), "tags", len(norm["categories"]), "cats")
    assert parse("<html><title>Page Not Found</title></html>")[0] is None
    assert parse('<script type="application/ld+json">{"@type": "VideoObject", "name": "x", "duration": "PT00H01M02S"}</script>')[0]["duration"] == 62
    assert _iso_secs("PT00H03M05S") == 185 and _iso_secs("") is None
    print("ok: phdetail self-check passed")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck(sys.argv[sys.argv.index("--selfcheck") + 1:])
    else:
        Run().main()
