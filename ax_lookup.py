"""Scratch helper for the daily run: resolve arXiv IDs and run arXiv/Crossref
searches, printing exact titles, authors, dates and abstracts.

Not part of the pipeline. Used to verify citations before drafting, per
DAILY_RUN.md step 4 ("Never write a citation from memory").
"""
import sys, time, json, textwrap
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET

# Windows console defaults to cp1252 and dies on accented author names.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ATOM = "{http://www.w3.org/2005/Atom}"
UA = {"User-Agent": "daily-paper-run/1.0 (citation verification)"}


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def _entries(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall(ATOM + "entry"):
        def t(tag):
            n = e.find(ATOM + tag)
            return (n.text or "").strip() if n is not None else ""
        aid = t("id").rsplit("/", 1)[-1]
        authors = [a.find(ATOM + "name").text.strip()
                   for a in e.findall(ATOM + "author")]

        def arx(tag):
            n = e.find("{http://arxiv.org/schemas/atom}" + tag)
            return " ".join((n.text or "").split()) if n is not None else ""

        out.append({
            "id": aid,
            "title": " ".join(t("title").split()),
            "authors": authors,
            "published": t("published")[:10],
            "updated": t("updated")[:10],
            "summary": " ".join(t("summary").split()),
            "journal_ref": arx("journal_ref"),
            "comment": arx("comment"),
        })
    return out


def show(e, abslen=700):
    print("=" * 78)
    print("arXiv:%s   (v1 %s, latest %s)" % (e["id"], e["published"], e["updated"]))
    print("TITLE : %s" % e["title"])
    print("AUTH  : %s  [n=%d]" % (", ".join(e["authors"][:12]), len(e["authors"])))
    if e.get("journal_ref"):
        print("JREF  : %s" % e["journal_ref"])
    if e.get("comment"):
        print("NOTE  : %s" % e["comment"][:200])
    print("ABS   : %s" % textwrap.fill(e["summary"][:abslen], 76,
                                       subsequent_indent="        "))
    print()


def by_id(ids, abslen=700):
    url = ("http://export.arxiv.org/api/query?id_list=%s&max_results=%d"
           % (",".join(ids), len(ids)))
    got = _entries(_get(url))
    found = {e["id"].split("v")[0]: e for e in got}
    for want in ids:
        e = found.get(want)
        if e is None:
            print("=" * 78)
            print("arXiv:%s   *** NOT FOUND ***" % want)
            print()
        else:
            show(e, abslen)


def search(query, n=8, abslen=420):
    url = ("http://export.arxiv.org/api/query?search_query=%s"
           "&start=0&max_results=%d&sortBy=relevance"
           % (urllib.parse.quote(query), n))
    for e in _entries(_get(url)):
        show(e, abslen)


def crossref(query, n=5):
    url = ("https://api.crossref.org/works?query.bibliographic=%s&rows=%d"
           % (urllib.parse.quote(query), n))
    data = json.loads(_get(url))
    for it in data["message"]["items"]:
        auth = ", ".join("%s %s" % (a.get("given", ""), a.get("family", ""))
                         for a in it.get("author", [])[:10])
        date = it.get("issued", {}).get("date-parts", [[None]])[0]
        print("=" * 78)
        print("DOI   : %s" % it.get("DOI"))
        print("TITLE : %s" % (it.get("title") or [""])[0])
        print("AUTH  : %s" % auth)
        print("VENUE : %s (%s)" % ((it.get("container-title") or [""])[0], date))
        print()


def _pop_abslen(argv, default):
    """Pull an optional `--abslen N` out of argv, returning (argv, abslen)."""
    if "--abslen" in argv:
        i = argv.index("--abslen")
        return argv[:i] + argv[i + 2:], int(argv[i + 1])
    return argv, default


if __name__ == "__main__":
    mode = sys.argv[1]
    rest = sys.argv[2:]
    if mode == "id":
        rest, n = _pop_abslen(rest, 700)
        by_id(rest, abslen=n)
    elif mode == "s":
        rest, n = _pop_abslen(rest, 420)
        search(" ".join(rest), abslen=n)
    elif mode == "cr":
        crossref(" ".join(rest))
    else:
        print(__doc__)
