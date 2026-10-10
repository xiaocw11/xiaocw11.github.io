#!/usr/bin/env python3
"""Refresh citation counts from the public Google Scholar profile.

Run daily by .github/workflows/update-citations.yml. It
  1. writes data/citations.json, and
  2. writes the numbers straight into index.html (total citations, h-index,
     date, and the per-paper badges in Selected Publications), so they are
     plain HTML that search engines and AI crawlers can read without
     running JavaScript.

Uses only the standard library. If Scholar can't be reached or returns
something unexpected (for example a CAPTCHA page), nothing is changed, so
the website keeps showing the last good numbers.

  python3 scripts/update_citations.py                # fetch, then render
  python3 scripts/update_citations.py --render-only  # re-render from data/citations.json
"""
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser

USER = "Juoqtj8AAAAJ"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "data", "citations.json")
INDEX = os.path.join(ROOT, "index.html")
PAGE = "https://scholar.google.com/citations?user={user}&hl=en&cstart={start}&pagesize=100"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MIN_BADGE = 20  # papers with fewer citations get no badge


def norm(title):
    """Normalize a title for matching: lowercase alphanumerics, single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(title).lower()).strip()


class ProfileParser(HTMLParser):
    """Collects (title, citations) rows and the summary stats table."""

    def __init__(self):
        super().__init__()
        self.rows, self.stats = [], []
        self._field = None
        self._buf = ""
        self._title = None

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "") or ""
        if tag == "a" and "gsc_a_at" in cls:
            self._field, self._buf = "title", ""
        elif tag == "a" and "gsc_a_ac" in cls:
            self._field, self._buf = "cites", ""
        elif tag == "td" and "gsc_rsb_std" in cls:
            self._field, self._buf = "stat", ""

    def handle_data(self, data):
        if self._field:
            self._buf += data

    def handle_endtag(self, tag):
        if self._field == "title" and tag == "a":
            self._title = self._buf.strip()
        elif self._field == "cites" and tag == "a":
            n = self._buf.strip()
            self.rows.append((self._title, int(n) if n.isdigit() else 0))
        elif self._field == "stat" and tag == "td":
            self.stats.append(self._buf.strip())
        else:
            return
        self._field = None


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def scrape():
    """Return fresh citation data, or None if Scholar couldn't be read."""
    rows, stats = [], []
    for start in range(0, 1000, 100):
        try:
            page = fetch(PAGE.format(user=USER, start=start))
        except Exception as e:  # network error, 429, etc.
            print(f"fetch failed at cstart={start}: {e}; keeping existing data")
            return None
        p = ProfileParser()
        p.feed(page)
        if start == 0:
            stats = p.stats
        rows += p.rows
        if len(p.rows) < 100:
            break
        time.sleep(3)

    # Sanity checks: a blocked/CAPTCHA page has no rows or no stats.
    if len(rows) < 50 or len(stats) < 4 or not stats[0].isdigit():
        print(f"unexpected page content (rows={len(rows)}, stats={stats}); keeping existing data")
        return None

    papers = {}
    for title, n in rows:
        key = norm(title or "")
        if key:
            papers[key] = max(n, papers.get(key, 0))
    return {
        "source": f"https://scholar.google.com/citations?user={USER}",
        "updated": datetime.date.today().isoformat(),
        "total": int(stats[0]),
        "hindex": int(stats[2]),
        "papers": dict(sorted(papers.items(), key=lambda kv: -kv[1])),
    }


def fmt(n):
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k" if n >= 1000 else str(n)


def lookup(papers, key):
    """Exact title match, else the best prefix match (titles often get shortened)."""
    if key in papers:
        return papers[key]
    best = None
    for k, n in papers.items():
        if len(k) >= 25 and len(key) >= 25 and (k.startswith(key) or key.startswith(k)):
            best = n if best is None else max(best, n)
    return best


def render(data):
    """Write the numbers into index.html as static HTML."""
    with open(INDEX, encoding="utf-8") as f:
        s = f.read()
    orig = s

    def put(attr, value, text):
        return re.sub(r"(" + attr + r">)[^<]*(<)", lambda m: m.group(1) + value + m.group(2), text)

    s = put('id="cite-total"', f'{data["total"]:,}', s)
    s = put('id="cite-h"', str(data["hindex"]), s)
    s = put('id="cite-date"', data["updated"], s)
    s = put('data-cite="total-floor"', f'{data["total"] // 1000 * 1000:,}', s)
    s = put('data-cite="total"', f'{data["total"]:,}', s)
    s = put('data-cite="hindex"', str(data["hindex"]), s)

    count = 0
    a = s.find('<section id="publications">')
    b = s.find("</section>", a)
    if a >= 0 and b >= 0:
        papers = data["papers"]

        def badge(m):
            nonlocal count
            li = re.sub(r' <span class="badge cite">[^<]*</span>', "", m.group(0))
            title = re.search(r'<div class="pub-title">(.*?)</div>', li, re.S)
            venue = re.search(r'<div class="pub-venue">(.*?)</div>', li, re.S)
            if not title or not venue:
                return li
            gs = re.search(r'data-gs="([^"]+)"', li)
            text = re.sub(r"<[^>]+>", "", re.sub(r'<span class="tag">.*?</span>', "", title.group(1)))
            n = lookup(papers, norm(gs.group(1) if gs else text))
            marks = list(re.finditer(r'<span class="(?:venue|badge award)">[^<]*</span>', venue.group(1)))
            if n is None or n < MIN_BADGE or not marks:
                return li
            count += 1
            pos = venue.start(1) + marks[-1].end()
            return li[:pos] + f' <span class="badge cite">{fmt(n)} citations</span>' + li[pos:]

        s = s[:a] + re.sub(r"<li\b[^>]*>.*?</li>", badge, s[a:b], flags=re.S) + s[b:]

    if s != orig:
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write(s)
    print(f"index.html: {count} citation badges, total {data['total']:,}, h-index {data['hindex']}")


def main():
    if "--render-only" in sys.argv:
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = scrape()
        if data is None:
            return 0
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"citations.json: {data['total']} citations, h-index {data['hindex']}, {len(data['papers'])} papers")
    render(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
