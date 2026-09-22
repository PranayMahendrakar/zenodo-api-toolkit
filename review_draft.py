r"""One-screen review of a staged draft, so deciding takes a minute.

    python review_draft.py                 # newest draft in drafts/
    python review_draft.py drafts/x.md
    python review_draft.py --no-cite       # skip the citation check (faster)

Shows what you need to decide publish-or-delete: the abstract, the shape of the
paper, citation status, the run's own honest assessment, and the two commands.
"""
import os
import re
import sys
import glob
import time
import threading
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def rule(ch="-"):
    print(ch * 74)


def front_matter(t):
    if not t.startswith("---"):
        return "", t
    e = t.find("\n---", 3)
    return t[3:e].strip(), t[e + 4:]


def val(fm, k):
    m = re.search(r"^%s:\s*(.+)$" % re.escape(k), fm, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def wrap(s, w=72, indent="  "):
    out, line = [], indent
    for word in s.split():
        if len(line) + len(word) + 1 > w:
            out.append(line)
            line = indent + word
        else:
            line = (line + " " + word) if line.strip() else indent + word
    if line.strip():
        out.append(line)
    return "\n".join(out)


def main():
    args = sys.argv[1:]
    VALUE_FLAGS = {"--delay"}
    named, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in VALUE_FLAGS:
            skip = True
            continue
        if not a.startswith("--"):
            named.append(a)
    if named:
        path = named[0]
    else:
        cands = [p for p in glob.glob(os.path.join(HERE, "drafts", "*.md"))
                 if not re.search(r"sec\d", os.path.basename(p))]
        if not cands:
            sys.exit("no drafts found in drafts/")
        path = max(cands, key=os.path.getmtime)
    if not os.path.isfile(path):
        alt = os.path.join(HERE, path)
        if os.path.isfile(alt):
            path = alt
        else:
            print("not found: %s" % path)
            sys.exit("  (tried it as given, and relative to %s)" % HERE)

    text = open(path, encoding="utf-8").read()
    fm, body = front_matter(text)
    parts = re.split(r"(?m)^##\s+References\s*$", body, maxsplit=1)
    main_body, refs = parts[0], (parts[1] if len(parts) > 1 else "")

    rule("=")
    print("  %s" % (val(fm, "title") or os.path.basename(path))[:70])
    rule("=")
    print("  file    : %s" % os.path.relpath(path, HERE))
    print("  genre   : %s   type: %s" % (val(fm, "genre") or "?",
                                         val(fm, "publication_type") or "(default)"))
    print("  author  : %s" % (val(fm, "author") or "(missing)"))
    print("  words   : %s body, %s references"
          % (len(main_body.split()),
             len([l for l in refs.splitlines() if l.strip()])))

    m = re.search(r"(?ms)^##\s+Abstract\s*\n(.+?)(?=^##\s)", body)
    if m:
        print()
        rule()
        print("  ABSTRACT")
        rule()
        print(wrap(" ".join(m.group(1).split())))

    secs = re.findall(r"(?m)^##\s+(?!Abstract|References)(.+)$", main_body)
    if secs:
        print()
        rule()
        print("  SECTIONS")
        rule()
        for s in secs:
            print("   %s" % s.strip()[:68])

    log = os.path.join(HERE, "daily_log.md")
    if os.path.isfile(log):
        slug = os.path.splitext(os.path.basename(path))[0]
        entries = [b for b in open(log, encoding="utf-8").read().split("\n\n")
                   if slug in b]
        if entries:
            print()
            rule()
            print("  THE RUN'S OWN ASSESSMENT")
            rule()
            print(wrap(" ".join(entries[-1].split())))

    if "--no-cite" not in args:
        print()
        rule()
        print("  CITATIONS")
        rule()
        n = len(set(re.findall(r"10\.\d{4,9}/\S+", text))) +             len(set(re.findall(r"(?i)arxiv[:/]\s*(\d{4}\.\d{4,5})", text)))
        delay = float(args[args.index("--delay") + 1]) if "--delay" in args else 2.5
        est = int(n * delay)
        print("  ~%d identifiers to verify against Crossref and arXiv" % n)
        print("  paced at %.1fs per call - estimate %d:%02d. --no-cite skips it."
              % (delay, est // 60, est % 60))
        sys.stdout.write("  working ")
        sys.stdout.flush()

        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "cite_check.py"), path,
             "--delay", str(delay), "--timeout", "45"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        out = []
        done = threading.Event()

        def collect():
            for line in proc.stdout:
                out.append(line.rstrip())
            done.set()

        threading.Thread(target=collect, daemon=True).start()
        t0 = time.time()
        while not done.wait(5):
            sys.stdout.write(".")
            sys.stdout.flush()
        proc.wait()
        print(" %ds" % int(time.time() - t0))
        print()

        for line in out:
            if re.match(r"\s*(SUMMARY|PASS|FAIL)", line) or "extracted" in line:
                print("  " + line[:70])
        bad = [l for l in out if re.match(r"\s*\|\s*(NOT-FOUND|MISMATCH)", l)]
        for l in bad[:6]:
            print("  " + l[:70])
        print()
        print("  gate: %s" % ("PASS - citations check out"
                              if proc.returncode == 0 else "FAIL - do not publish"))

    rel = os.path.relpath(path, HERE).replace("\\", "/")
    print()
    rule("=")
    print("  PUBLISH  (permanent, irreversible):")
    print('    python publish_paper.py "%s" --publish' % rel)
    print()
    print("  DISCARD the staged Zenodo draft:")
    print("    python delete_draft.py <draft_id>      # id is in daily_log.md")
    rule("=")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print()
        print("  interrupted. Use --no-cite to skip the citation check.")
        sys.exit(130)
