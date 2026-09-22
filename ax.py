"""Throwaway helper: print verbatim arXiv metadata for a list of ids.

    python ax.py 2504.13837 2310.01798

Prints id, title, authors, published/updated dates, categories, comment, doi,
journal_ref and the full abstract, exactly as arXiv returns them. Read-only.
"""
import sys
import time
import xml.etree.ElementTree as ET

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

NS = {"a": "http://www.w3.org/2005/Atom",
      "x": "http://arxiv.org/schemas/atom"}


def show(ids):
    r = requests.get("https://export.arxiv.org/api/query",
                     params={"id_list": ",".join(ids), "max_results": len(ids)},
                     timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    entries = root.findall("a:entry", NS)
    print("=== %d id(s) requested, %d entry(ies) returned ===" % (len(ids), len(entries)))
    for e in entries:
        def t(tag, ns="a"):
            n = e.find("%s:%s" % (ns, tag), NS)
            return (n.text or "").strip() if n is not None else ""
        print("-" * 70)
        print("ID       :", t("id").rsplit("/", 1)[-1])
        print("TITLE    :", " ".join(t("title").split()))
        print("AUTHORS  :", "; ".join(
            " ".join((a.find("a:name", NS).text or "").split())
            for a in e.findall("a:author", NS)))
        print("PUBLISHED:", t("published"), " UPDATED:", t("updated"))
        print("PRIMARY  :", (e.find("x:primary_category", NS).get("term")
                             if e.find("x:primary_category", NS) is not None else ""))
        print("COMMENT  :", " ".join(t("comment", "x").split()))
        print("JOURNAL  :", " ".join(t("journal_ref", "x").split()))
        print("DOI      :", t("doi", "x"))
        print("ABSTRACT :", " ".join(t("summary").split()))
    print("-" * 70)


def search(query, n=12):
    r = requests.get("https://export.arxiv.org/api/query",
                     params={"search_query": query, "max_results": n,
                             "sortBy": "relevance"}, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    for e in root.findall("a:entry", NS):
        def t(tag, ns="a"):
            n_ = e.find("%s:%s" % (ns, tag), NS)
            return (n_.text or "").strip() if n_ is not None else ""
        print("%-14s %s" % (t("id").rsplit("/", 1)[-1],
                            " ".join(t("title").split())))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "-s":
        search(" ".join(args[1:]))
    else:
        for i in range(0, len(args), 6):
            show(args[i:i + 6])
            if i + 6 < len(args):
                time.sleep(3.0)
