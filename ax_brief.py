"""Scratch: terse arXiv search - id, date, title, author count, 1-line gist.

Used only during the daily run's step-4 literature sweep to keep the search
output small. Full verification still goes through ax_lookup.py.
"""
import sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ATOM = "{http://www.w3.org/2005/Atom}"
UA = {"User-Agent": "daily-paper-run/1.0 (citation verification)"}


def _get(url, timeout=45):
    """Fetch bytes.

    Through requests, not urllib: export.arxiv.org answers urllib with HTTP
    406 regardless of headers, while requests gets 200 for the same URL.
    """
    resp = requests.get(url, headers=UA, timeout=timeout)
    resp.raise_for_status()
    return resp.content

def entries(q, n):
    url = ("http://export.arxiv.org/api/query?search_query=%s"
           "&start=0&max_results=%d&sortBy=relevance"
           % (urllib.parse.quote(q), n))
    root = ET.fromstring(_get(url, 45))
    for e in root.findall(ATOM + "entry"):
        def t(tag):
            node = e.find(ATOM + tag)
            return " ".join((node.text or "").split()) if node is not None else ""
        aid = t("id").rsplit("/", 1)[-1]
        na = len(e.findall(ATOM + "author"))
        print("%-12s %s  n=%-2d %s" % (aid, t("published")[:7], na, t("title")))
        print("      %s" % t("summary")[:260])


if __name__ == "__main__":
    n = 8
    for q in sys.argv[1:]:
        print("### QUERY: %s" % q)
        entries(q, n)
        print()
        time.sleep(3)
