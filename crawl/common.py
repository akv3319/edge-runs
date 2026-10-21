"""Shared crawl base for pn-video: one sink per site, resumable append-only output, polite fetches.

Every site crawler imports this. Output lives on the Dell OUTSIDE the synced tree:
    ~/pn-crawl/<site>/listing.jsonl   the source's own listing (one id per line), for reconcile
    ~/pn-crawl/<site>/items.jsonl     one normalised record per video, appended + flushed as it completes
    ~/pn-crawl/<site>/raw.jsonl       the source's own payload per id, verbatim (the no-re-crawl guard)
    ~/pn-crawl/<site>/excluded.jsonl  one line per id dropped by a named rule (minors), nothing else kept
    ~/pn-crawl/<site>/errors.jsonl    unreachable ids, for the low-concurrency repass
    ~/pn-crawl/<site>/progress.json   listed / crawled / errors / wire bytes / rate, rewritten each batch
Rules baked in: three outcomes (ok / absent / unreachable), a challenge page is unreachable not empty,
stop after 5 unreachable in a row unless the control URL still answers, resume skips ids already written.
Self-check: python3 common.py
"""
import gzip, json, os, re, time

ROOT = os.path.expanduser(os.environ.get("PN_CRAWL_ROOT", "~/pn-crawl"))
# Markers of a real challenge PAGE (HTML), not words: FikFap post 1748291's caption "This isn't just a moment"
# made a 200 JSON feed page read as a challenge and stopped the job 8 times (2026-10-21).
CHALLENGE = re.compile(r"<title>\s*(?:just a moment|attention required|prove your humanity)|cf-chl|challenge-platform|"
                       r"checking your browser before|/cdn-cgi/challenge|recaptcha/api|hcaptcha\.com|challenges\.cloudflare\.com", re.I)
# Hard line: anything that reads as a minor is never stored past one exclusion line. Over-matching only
# drops adult content (the safe direction). "teen" is the 18-19 adult category and is not matched.
MINOR = re.compile(r"\b(?:under\s?age\w*|under\s?18|minors?|child\w*|kids?|pre-?teens?|jail\s?bait|pedo\w*|"
                   r"loli\w*|shota\w*|toddler\w*|cp|(?:1[0-7]|[1-9])\s?(?:yo|y/o|y\.o\.|years?\s?old))\b"
                   r"|\b(?:little|lil|tiny)[\s_-]?(?:girl|boy)", re.I)  # no trailing \b: handles like "littlegirlbyrio"

# Second hard line (2026-10-21): markers of a recording made or shared without consent. We cannot verify
# consent, so these are dropped like minors, one exclusion line kept (reversible by id). Staged genres
# that use the same words lose rows too: the safe direction.
NONCONSENT = re.compile(r"\b(?:hidden[\s_-]?cams?|spy[\s_-]?cams?|creep[\s_-]?shots?|revenge[\s_-]?porn|"
                        r"non[\s_-]?consens\w*|up[\s_-]?skirts?|down[\s_-]?blouse)\b", re.I)
# Not excluded, flagged on the row for serving to filter: "voyeur" is a mainstream staged genre; "leaked"
# is paywalled content shared without the creator's OK (a copyright matter). "leaking" is not a leak.
FLAGS = {"voyeur": re.compile(r"\bvoyeur\w*", re.I), "leaked": re.compile(r"\bleak(?:ed|s)?\b", re.I)}


class Stop(Exception):
    pass


class Sink:
    def __init__(self, site):
        self.dir = os.path.join(ROOT, site)
        os.makedirs(self.dir, exist_ok=True)
        self.site, self.t0, self.bytes = site, time.time(), 0
        self.done = self._ids("items.jsonl") | self._ids("excluded.jsonl") | self._ids("skipped.jsonl")
        self.errors = 0

    def _ids(self, name):
        p = os.path.join(self.dir, name)
        if not os.path.exists(p):
            return set()
        out = set()
        for line in open(p):
            try:
                out.add(str(json.loads(line)["id"]))
            except (ValueError, KeyError):
                pass  # a torn last line from a crash; the id is re-fetched
        return out

    def _append(self, name, row):
        with open(os.path.join(self.dir, name), "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()

    def listed(self, ids):
        """Write the source's own listing once (overwrite), so reconcile compares by the same key."""
        ids = list(dict.fromkeys(str(i) for i in ids))
        with open(os.path.join(self.dir, "listing.jsonl"), "w") as f:
            for i in ids:
                f.write(json.dumps({"id": i}) + "\n")
        return ids

    def item(self, vid, url, raw, **norm):
        """raw = the source's payload for this id, verbatim; norm = title/thumb/file/duration/tags/... .
        Returns False when the minors rule dropped it (only an exclusion line is written)."""
        vid = str(vid)
        if vid in self.done:
            return True
        text = " ".join(str(norm.get(k) or "") for k in ("title", "description", "tags", "categories", "author"))
        for rule, rx in (("minors", MINOR), ("nonconsent", NONCONSENT)):
            m = rx.search(text)
            if m:
                self._append("excluded.jsonl", {"src": self.site, "id": vid, "rule": rule, "matched": m.group(0)})
                self.done.add(vid)
                return False
        flags = [k for k, rx in FLAGS.items() if rx.search(text)]
        if flags:
            norm["flags"] = flags
        self._append("raw.jsonl", {"id": vid, "raw": raw})
        self._append("items.jsonl", {"src": self.site, "id": vid, "url": url, "t": int(time.time()), **norm})
        self.done.add(vid)
        return True

    def skip(self, vid, reason):
        """A listed id that is deliberately not a row (not a video, sfw, ...): kept by id so reconcile can name it."""
        vid = str(vid)
        if vid not in self.done:
            self._append("skipped.jsonl", {"id": vid, "reason": reason})
            self.done.add(vid)

    def error(self, vid, why):
        self.errors += 1
        self._append("errors.jsonl", {"id": str(vid), "why": str(why)[:200], "t": int(time.time())})

    def progress(self, listed=None, **extra):
        el = max(time.time() - self.t0, 1)
        p = {"site": self.site, "listed": listed, "crawled": len(self.done), "errors": self.errors,
             "wire_bytes": self.bytes, "per_min": round(len(self.done) / el * 60, 1), "updated": int(time.time()), **extra}
        tmp = os.path.join(self.dir, "progress.json.tmp")
        json.dump(p, open(tmp, "w"))
        os.replace(tmp, os.path.join(self.dir, "progress.json"))
        return p


def robots_rules(text):
    """Allow/Disallow rules of the user-agent * group(s), per RFC 9309."""
    rules, group, ua_line = [], False, False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            group = (v == "*") if not ua_line else (group or v == "*")
            ua_line = True
        elif k in ("allow", "disallow"):
            ua_line = False
            if group and v:
                rules.append((k == "allow", v))
    return rules


def robots_allows(rules, path):
    """Longest matching rule wins, Allow on a tie (RFC 9309; Python's robotparser takes the first match)."""
    best = (-1, True)
    for allow, pat in rules:
        rx = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\$", "$")
        if re.match(rx, path) and (len(pat) > best[0] or (len(pat) == best[0] and allow)):
            best = (len(pat), allow)
    return best[1]


def classify(status, body):
    if status in (404, 410):
        return "absent"
    if status != 200:
        return "unreachable"
    head = (body or "")[:4000]
    if not head.lstrip()[:1] in ("{", "[") and CHALLENGE.search(head):  # a JSON body is never a challenge page
        return "unreachable"
    return "ok"


class Fetcher:
    """curl_cffi Chrome-impersonating session, one request per `gap` seconds per instance, byte-metered."""

    def __init__(self, sink, gap=1.0, control=None, headers=None, impersonate="chrome", doh=None):
        """doh: a DNS-over-HTTPS URL. The home line (Airtel) sinks xvideos/xnxx at DNS only; with
        doh="https://cloudflare-dns.com/dns-query" both load from the Dell (measured 2026-10-21)."""
        from curl_cffi import requests as cr, CurlOpt  # imported here so the self-check runs without it
        self.s = cr.Session(impersonate=impersonate, headers=headers or {},
                            curl_options={CurlOpt.DOH_URL: doh} if doh else None)
        self.sink, self.gap, self.control, self.last, self.bad = sink, gap, control, 0.0, 0
        self.headers = {}

    def get(self, url, **kw):
        wait = self.gap - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        try:
            r = self.s.get(url, timeout=kw.pop("timeout", 30), **kw)
            body, status = r.content, r.status_code
        except Exception as e:  # timeout, reset, DNS: unreachable, never absent
            body, status = b"", f"exc:{type(e).__name__}"
        self.sink.bytes += len(body)
        self.headers = getattr(r, "headers", {}) if isinstance(status, int) else {}  # e.g. X-WP-Total
        if url.endswith(".gz") and body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        text = body.decode("utf-8", "replace")
        out = classify(status, text) if isinstance(status, int) else "unreachable"
        self.bad = self.bad + 1 if out == "unreachable" else 0
        if self.bad >= 5:
            self._stop_or_continue(status)
        return out, status, text

    def _stop_or_continue(self, status):
        self.bad = 0
        if not self.control:
            raise Stop(f"5 unreachable in a row (last {status}), no control url")
        time.sleep(30)
        r = self.s.get(self.control, timeout=30)
        if classify(r.status_code, r.text) != "ok":
            raise Stop(f"5 unreachable in a row (last {status}) and the control url failed too: blocked or line down")


def sitemap_urls(fetch, url, match=None, limit_children=None):
    """Yield every <loc> under a sitemap or sitemap index (recursing children). match filters child sitemaps."""
    out, status, text = fetch.get(url)
    if out != "ok":
        raise Stop(f"sitemap {url}: {status}")
    kids = re.findall(r"<sitemap>\s*<loc>\s*([^<\s]+)", text)
    if kids:
        kids = [k for k in kids if not match or re.search(match, k)]
        for k in kids[:limit_children]:
            yield from sitemap_urls(fetch, k.replace("&amp;", "&"))
    else:
        yield from (u.replace("&amp;", "&") for u in re.findall(r"<url>\s*<loc>\s*([^<\s]+)", text))


if __name__ == "__main__":
    import tempfile
    os.environ["PN_CRAWL_ROOT"] = ROOT = tempfile.mkdtemp()
    assert classify(200, "<html>ok</html>") == "ok"
    assert classify(200, "<title>Just a moment...</title>") == "unreachable", "challenge page is not content"
    assert classify(200, '[{"postId":1748291,"label":"This is not just a moment; captcha"}]') == "ok", "JSON is data"
    assert classify(200, "<html><title>Just a moment of bliss | site</title>") == "unreachable"
    assert classify(200, "<html><title>Hot clip</title><p>just a moment</p>") == "ok", "a word in content is not a wall"
    assert classify(404, "") == "absent" and classify(429, "") == "unreachable" and classify(503, "") == "unreachable"
    s = Sink("selfcheck")
    assert s.item(1, "u", {"a": 1}, title="t") and s.item(1, "u", {"a": 1}, title="t")
    assert s.item(9, "u", {"a": 1}, title="petite 17yo") is False, "minors rule drops it"
    assert s.item(8, "u", {}, title="teen 19 solo", tags=["amateur"]), "18-19 'teen' is adult, kept"
    assert MINOR.search("schoolkid") is None and MINOR.search("Kids") and MINOR.search("16 years old")
    assert open(os.path.join(s.dir, "excluded.jsonl")).read().count("minors") == 1
    assert s.item(7, "u", {}, title="x", tags=["Hidden Cam"]) is False, "nonconsent rule drops it"
    assert s.item(6, "u", {}, title="creampie leaking", tags=["voyeur"]) and '"flags": ["voyeur"]' in open(os.path.join(s.dir, "items.jsonl")).read()
    assert FLAGS["leaked"].search("OnlyFans leaked") and not FLAGS["leaked"].search("leaking")
    assert MINOR.search("littlegirlbyrio") and MINOR.search("Lil boy") and not MINOR.search("little goth girl")
    with open(os.path.join(s.dir, "items.jsonl"), "a") as f:
        f.write('{"id": "2", "torn')
    assert {"1", "8", "9"} <= Sink("selfcheck").done, "resume skips written and excluded ids, survives a torn line"
    assert s.listed([3, 3, 4]) == ["3", "4"]
    s.skip(5, "not a video"); s.skip(5, "not a video")
    assert "5" in Sink("selfcheck").done and open(os.path.join(s.dir, "skipped.jsonl")).read().count('"5"') == 1
    kg = robots_rules("User-agent: *\nAllow: /\nDisallow: /api\nDisallow: /feed/\nUser-agent: GPTBot\nDisallow: /")
    assert not robots_allows(kg, "/api/feed/x") and robots_allows(kg, "/v/Qkrl") and not robots_allows(kg, "/feed/a")
    assert robots_allows(robots_rules("User-agent: *\nDisallow: /a\nAllow: /a/b"), "/a/b/c"), "longest match wins"
    print("ok: common.py self-check passed")
