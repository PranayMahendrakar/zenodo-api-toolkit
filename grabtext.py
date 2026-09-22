"""Throwaway helper: fetch a URL and print sentences matching a regex.

    python grabtext.py https://arxiv.org/html/2608.11434 "9.24|13 positions"

Strips HTML tags crudely, then prints every sentence that matches, so a number
can be read in the source's own words rather than through a summariser.
Read-only: GET requests only.
"""
import re
import sys
import html

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


def main(url, pattern, width=400):
    r = requests.get(url, timeout=90,
                     headers={"User-Agent": "Mozilla/5.0 (citation-check)"})
    r.raise_for_status()
    text = r.text
    text = re.sub(r"(?is)<(script|style|math)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\n ]+", " ", text)
    rx = re.compile(pattern, re.I)
    seen = set()
    for m in rx.finditer(text):
        a = max(0, m.start() - width // 2)
        b = min(len(text), m.end() + width // 2)
        chunk = text[a:b].strip()
        if chunk in seen:
            continue
        seen.add(chunk)
        print("...", chunk, "...")
        print("-" * 70)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
