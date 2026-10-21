"""pn-g: a Fetcher that never fetches a URL its host's robots.txt (user-agent *, RFC 9309 longest match) disallows.
robots.txt is read once per host through the same session; 404 = allow all, unreachable = Stop (RFC 9309: assume
complete disallow). Self-check: python3 crawl/sites/_png_robots.py"""
import os, sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import Fetcher, Stop, robots_allows, robots_rules


class RobotsFetcher(Fetcher):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rules = {}

    def allowed(self, url):
        u = urlsplit(url)
        host = f"{u.scheme}://{u.netloc}"
        if host not in self.rules:
            out, st, t = Fetcher.get(self, host + "/robots.txt")
            if out == "absent":
                self.rules[host] = []
            elif out == "ok":
                self.rules[host] = robots_rules(t)
            else:
                raise Stop(f"{host}/robots.txt unreachable ({st}): treated as disallow-all")
        return u.path == "/robots.txt" or robots_allows(self.rules[host], u.path + ("?" + u.query if u.query else ""))

    def get(self, url, **kw):
        if not self.allowed(url):
            raise Stop(f"robots.txt disallows {url}")
        return super().get(url, **kw)


if __name__ == "__main__":
    f = RobotsFetcher.__new__(RobotsFetcher)
    f.rules = {"https://www.pornhub.com": robots_rules("User-agent: *\nDisallow: *token=*\nDisallow: */video*id=*\nAllow: /model/*/videos*"),
               "https://api.waptap.com": robots_rules("User-agent: *\nDisallow: /\nAllow: /v1/media/share/")}
    assert f.allowed("https://www.pornhub.com/sitemap_g_vids1.xml") and f.allowed("https://www.pornhub.com/view_video.php?viewkey=ab1")
    assert not f.allowed("https://www.pornhub.com/shorties/analyticsData?vkeys[]=a&token=b")
    assert not f.allowed("https://www.pornhub.com/webmasters/video_by_id?id=x") and f.allowed("https://www.pornhub.com/robots.txt")
    assert not f.allowed("https://api.waptap.com/v1/media?type=new") and f.allowed("https://api.waptap.com/v1/media/share/x")
    print("ok: _png_robots self-check passed")
