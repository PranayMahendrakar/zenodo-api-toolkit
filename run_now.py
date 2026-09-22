r"""Write the next paper NOW, instead of waiting for the 05:00 job.

    python run_now.py                     # next topic from the queue
    python run_now.py --topic "Some specific question you want written"
    python run_now.py --no-review         # generate only, skip the review screen
    python run_now.py --dry-run           # show what it would do, run nothing

Same pipeline the scheduled job uses: it reads DAILY_RUN.md, verifies every
citation, drafts, audits, gates on cite_check, and stages a Zenodo DRAFT.

It does NOT publish. When it finishes it prints the exact publish command for
the draft it just produced, so publishing stays one deliberate step.

Expect this to take a long while - a full paper is hours of work, not minutes.
The terminal will show a dot every 15 seconds so you can tell it is alive.
Leave it running; closing the window kills it.
"""
import os
import re
import sys
import time
import glob
import threading
import subprocess

import claude_flags

HERE = os.path.dirname(os.path.abspath(__file__))
FAIL_MARKERS = ("API Error", "authentication_error", "OAuth access token has expired",
                "Please run /login", "rate_limit_error", "Invalid API key")


def rule(ch="-"):
    print(ch * 74)


def claude_path():
    p = os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe")
    if os.path.isfile(p):
        return p
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-Command claude -ErrorAction SilentlyContinue).Source"],
                           capture_output=True, text=True)
        return (r.stdout or "").strip() or None
    except Exception:
        return None


def drafts_now():
    """name -> mtime; a revised draft counts as produced, not as nothing."""
    out = {}
    for p in glob.glob(os.path.join(HERE, "drafts", "*.md")):
        name = os.path.basename(p)
        if not re.search(r"sec\d", name):
            out[name] = os.path.getmtime(p)
    return out


def produced(before, after, text):
    touched = [n for n, m in after.items()
               if n not in before or m > before[n] + 1]
    if touched:
        return sorted(touched, key=lambda n: after[n])
    for name in re.findall(r"drafts[/\\]([A-Za-z0-9_.-]+\.md)", text):
        if name in after:
            return [name]
    return []


def next_queued():
    try:
        text = open(os.path.join(HERE, "topics.md"), encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"^- \[ \] (.+)$", text, re.M)
    return m.group(1).strip() if m else None


def main():
    args = sys.argv[1:]
    topic = args[args.index("--topic") + 1] if "--topic" in args else None

    cli = claude_path()
    if not cli:
        sys.exit("claude CLI not found. Run: python setup_daily.py --check")
    if not os.environ.get("ZENODO_TOKEN"):
        sys.exit('ZENODO_TOKEN is not set in this shell.\n'
                 '  PowerShell:  $env:ZENODO_TOKEN="..."')

    target = topic or next_queued()
    if not target:
        print("The queue is empty, so the pipeline will select its own topic.")
        target = "(self-selected)"

    print()
    rule("=")
    print("  Writing the next paper now")
    rule("=")
    print("  topic  : %s" % target[:66])
    if len(target) > 66:
        print("           %s" % target[66:132])
    print("  output : a Zenodo DRAFT. Nothing is published.")
    print("  time   : hours, not minutes. Leave this window open.")
    rule("=")

    if "--dry-run" in args:
        print("\ndry run - nothing was started.")
        return

    if topic:
        prompt = (
            "Follow the instructions in DAILY_RUN.md exactly, with one change: "
            "instead of taking the next topic from topics.md, write about this "
            "topic:\n\n  %s\n\n"
            "Apply the same writability test from step 3 - if it needs experiments "
            "you cannot run, apply the reframe or stop and say why. "
            "Do not publish anything to Zenodo. Stage a draft only." % topic)
    else:
        prompt = ("Follow the instructions in DAILY_RUN.md exactly. "
                  "Do not publish anything to Zenodo under any circumstances - "
                  "stage a draft only.")

    before = drafts_now()
    t0 = time.time()
    sys.stdout.write("\nworking ")
    sys.stdout.flush()

    proc = subprocess.Popen([cli, "-p", prompt] + claude_flags.flags(),
                            cwd=HERE, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, done = [], threading.Event()

    def collect():
        for line in proc.stdout:
            out.append(line.rstrip())
        done.set()

    threading.Thread(target=collect, daemon=True).start()
    while not done.wait(15):
        sys.stdout.write(".")
        sys.stdout.flush()
    proc.wait()
    mins = int(time.time() - t0) // 60

    text = "\n".join(out)
    print(" %dm%02ds" % (mins, int(time.time() - t0) % 60))

    logpath = os.path.join(HERE, "run_now.log")
    try:
        with open(logpath, "a", encoding="utf-8") as fh:
            fh.write("\n===== run at " + time.strftime("%Y-%m-%d %H:%M:%S")
                     + " =====\n" + text + "\n")
    except OSError:
        pass

    # A draft on disk is the real test. A transient "API Error" the CLI
    # retried past is not a failure if the paper actually got written.
    new = produced(before, drafts_now(), text)
    hit = [m for m in FAIL_MARKERS if m.lower() in text.lower()]

    if new and hit:
        print()
        print("  note: '%s' appeared but a draft was produced; treating it"
              % hit[0])
        print("        as transient. Details in %s" % logpath)

    if not new:
        print()
        rule()
        if hit:
            print("  FAILED: '%s'" % hit[0])
            print("  Transient API errors are common on long runs - re-running")
            print("  usually works. If it repeats: python setup_daily.py --check")
        else:
            print("  No draft was produced, and no API error was reported.")
            print("  That is a valid outcome if the topic failed the writability")
            print("  test - check daily_log.md and topics.md for the reason.")
        rule()
        for line in [l for l in out if l.strip()][-10:]:
            print("  " + line[:100])
        print("  full output: %s" % logpath)
        sys.exit(1)

    draft = new[-1]
    rel = "drafts/" + draft
    print()
    rule("=")
    print("  DRAFT READY: %s" % rel)
    rule("=")

    if "--no-review" not in args:
        print()
        subprocess.call([sys.executable, os.path.join(HERE, "review_draft.py"),
                         os.path.join(HERE, "drafts", draft)])
    else:
        print()
        print("  review it:")
        print('    python review_draft.py "%s"' % rel)
        print()
        print("  publish it (permanent, irreversible):")
        print('    python publish_paper.py "%s" --publish' % rel)


if __name__ == "__main__":
    main()
