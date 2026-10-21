"""PornHub Shorties (www.pornhub.com/shorties): every shortie, route A. Runs on a US GitHub runner.
Listing = sitemap_g_shorties1..8 (113,293 on 2026-10-21); same engine and fields as pornhub.py.
The /shorties/get feed (5 per call, seeded random, empties past ~20k offsets) is a sample, not a listing.

  python crawl/sites/phshorties.py [--files 1-8] [--limit N]
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pornhub import Run

if __name__ == "__main__":
    Run(site="phshorties", family="shorties", files=range(1, 9)).main()
