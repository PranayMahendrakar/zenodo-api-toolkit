"""Throwaway self-audit for a draft: ASCII, word count, orphan/dangling refs.

    python audit_draft.py drafts/x.md

Splits the file at "## References", extracts (Author, year) markers from the body
and reference-list surnames, and reports refs never cited and in-text names with no
reference entry. Name matching is by first surname token, so it is a screen, not a
proof. Read-only.
"""
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

path = sys.argv[1]
raw = open(path, encoding="utf-8").read()

bad = [(i + 1, line) for i, line in enumerate(raw.splitlines())
       if any(ord(c) > 126 for c in line)]
print("non-ASCII lines: %d" % len(bad))
for ln, line in bad[:20]:
    print("  %d: %s" % (ln, "".join(c if ord(c) < 127 else "<U+%04X>" % ord(c)
                                    for c in line))[:160])

head, _, refs = raw.partition("\n## References")
body = head.split("## Abstract", 1)[-1]
prose = re.sub(r"(?m)^#.*$", "", body)
print("\nprose words (abstract to end of body): %d" % len(prose.split()))
print("reference entries: %d" % len([l for l in refs.splitlines()
                                     if re.match(r"^[A-Z][A-Za-z'-]+,", l)]))

# reference surnames
ref_names = []
for line in refs.splitlines():
    m = re.match(r"^([A-Z][A-Za-z'-]+),.*?\((\d{4})\)", line)
    if m:
        ref_names.append((m.group(1), m.group(2)))

# in-text markers: "Surname et al., 2026" / "Surname and Surname, 2026" /
# "Surname et al. (2026)" / "Surname (2026)"
cited = set()
# The body is hard-wrapped, so a citation can straddle a newline ("Penedo et\nal.
# (2024)"). Every pattern below assumes single spaces, so flatten first.
flat = re.sub(r"\s+", " ", body)
for m in re.finditer(r"\(([^()]{0,200}?\d{4}[^()]{0,60}?)\)", flat):
    for mm in re.finditer(r"([A-Z][A-Za-z'-]+)[^;()]*?(\d{4})", m.group(1)):
        cited.add((mm.group(1), mm.group(2)))
for mm in re.finditer(r"\b([A-Z][A-Za-z'-]+)(?: and [A-Z][A-Za-z'-]+)?"
                      r"(?:, [A-Z]\.,)? et al\.? \((\d{4})\)", flat):
    cited.add((mm.group(1), mm.group(2)))
for mm in re.finditer(r"\b([A-Z][A-Za-z'-]+)(?: and [A-Z][A-Za-z'-]+)? \((\d{4})\)", flat):
    cited.add((mm.group(1), mm.group(2)))

ref_set = set(ref_names)
print("\nORPHAN references (in list, no matching in-text surname+year):")
for n in sorted(ref_set):
    if n not in cited:
        print("  ", n)
print("\nDANGLING in-text markers (no matching reference entry):")
for n in sorted(cited):
    if n not in ref_set:
        print("  ", n)

print("\nhedge/booster counts:")
for w in ["we show", "our results", "we find", "we demonstrate", "our experiments",
          "clearly", "obviously", "significantly", "proves", "prove that",
          "suggests", "may ", "might ", "could ", "appears", "seems", "likely",
          "speculat"]:
    c = len(re.findall(r"(?i)\b%s" % re.escape(w), body))
    if c:
        print("  %-16s %d" % (w, c))
