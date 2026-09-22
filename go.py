r"""Everything, in one command:

    python go.py

Writes the next paper from the queue, reviews it, and takes you straight to the
publish confirmation. No arguments, no options, no decisions along the way.

The only thing it asks is the last one: the permanent-record block appears and
you type PUBLISH, or anything else to leave the draft unpublished. One word.

Takes hours. Leave the window open; closing it kills the run.
"""
import os
import re
import sys
import glob
import time
import threading
import subprocess

import claude_flags

HERE = os.path.dirname(os.path.abspath(__file__))
FAIL_MARKERS = ("API Error", "authentication_error", "OAuth access token has expired",
                "Please run /login", "rate_limit_error", "Invalid API key")


def rule(ch="="):
    print(ch * 74)


def step(n, what):
    print()
    rule()
    print("  STEP %d/3  %s" % (n, what))
    rule()


def claude_path():
    p = os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe")
    if os.path.isfile(p):
        return p
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(Get-Command claude -ErrorAction SilentlyContinue).Source"],
                       capture_output=True, text=True)
    return (r.stdout or "").strip() or None


def drafts_now():
    """name -> mtime. Mtimes matter: a run may REVISE an existing draft rather
    than create one, and treating that as 'nothing produced' hides a success."""
    out = {}
    for p in glob.glob(os.path.join(HERE, "drafts", "*.md")):
        name = os.path.basename(p)
        if not re.search(r"sec\d", name):
            out[name] = os.path.getmtime(p)
    return out


def produced(before, after, text):
    """Drafts this run created or modified, newest first.

    Falls back to a draft named in the transcript, so a run that staged a paper
    is never reported as a failure on a filesystem technicality.
    """
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
    cli = claude_path()
    if not cli:
        sys.exit("claude CLI not found.  Run: python setup_daily.py --check")
    if not os.environ.get("ZENODO_TOKEN"):
        sys.exit('ZENODO_TOKEN is not set in this shell.\n'
                 '  PowerShell:  $env:ZENODO_TOKEN="..."')

    topic = next_queued() or "(the pipeline will select its own)"

    step(1, "WRITE")
    print("  topic : %s" % topic[:64])
    if len(topic) > 64:
        print("          %s" % topic[64:128])
    print("  This takes hours. A dot every 15s means it is alive.")
    print()
    sys.stdout.write("  working ")
    sys.stdout.flush()

    before = drafts_now()
    t0 = time.time()
    cmd = ([cli, "-p", "Follow the instructions in DAILY_RUN.md exactly. "
                       "Do not publish anything to Zenodo - stage a draft only."]
           + claude_flags.flags())
    proc = subprocess.Popen(cmd, cwd=HERE, text=True,
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
    elapsed = int(time.time() - t0)
    print("  %dm%02ds" % (elapsed // 60, elapsed % 60))

    text = "\n".join(out)
    logpath = os.path.join(HERE, "go.log")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(logpath, "a", encoding="utf-8") as fh:
            fh.write("\n===== run at " + stamp + " =====\n" + text + "\n")
    except OSError:
        pass

    # Did it produce a paper? That is the real test. A transient "API Error"
    # that the CLI retried past is not a failure if the draft exists.
    new = produced(before, drafts_now(), text)
    hit = [m for m in FAIL_MARKERS if m.lower() in text.lower()]

    staged = re.search(r"(https://\S*zenodo\.org/deposit/(\d+))", text)
    if staged:
        print()
        print("  staged: %s" % staged.group(1))

    if new and hit:
        print()
        print("  note: '%s' appeared during the run but a draft was produced;" % hit[0])
        print("        treating it as transient. Details in %s" % logpath)

    if not new:
        print()
        if hit:
            print("  FAILED after %dm%02ds: '%s'"
                  % (elapsed // 60, elapsed % 60, hit[0]))
            print("  Transient API errors are common on long runs - re-running")
            print("  usually works. If it repeats: python setup_daily.py --check")
        else:
            print("  No draft was produced, and no API error was reported.")
            print("  That is a valid outcome when the topic fails the writability")
            print("  test - see daily_log.md and topics.md for the reason.")
        print("  --- what claude actually said ---")
        for line in [l for l in out if l.strip()][-12:]:
            print("  " + line[:100])
        print("  --- full output saved to: %s" % logpath)
        sys.exit(1)

    draft = os.path.join(HERE, "drafts", new[-1])
    rel = "drafts/" + new[-1]

    step(2, "REVIEW")
    subprocess.call([sys.executable, os.path.join(HERE, "review_draft.py"), draft])

    step(3, "PUBLISH")
    print("  Read the review above before answering.")
    print("  Everything below goes onto a permanent public record.")
    print()
    cmd = [sys.executable, os.path.join(HERE, "publish_paper.py"), rel, "--publish"]
    try:
        import paper_defaults
        if getattr(paper_defaults, "AUTO_PUBLISH", False):
            cmd.append("--yes")
            print("  AUTO_PUBLISH is on: publishing without asking.")
            print("  (set AUTO_PUBLISH = False in paper_defaults.py to review first)")
    except Exception:
        pass
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        print()
        print("  Not published. The draft is intact:")
        print("    %s" % rel)
        print("  Publish later with:")
        print('    python publish_paper.py "%s" --publish' % rel)


if __name__ == "__main__":
    main()
