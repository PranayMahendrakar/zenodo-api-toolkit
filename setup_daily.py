r"""One-shot setup for the daily paper job. Run it from PowerShell:

    python setup_daily.py                  # check everything, then register 05:00 daily
    python setup_daily.py --check          # check only, change nothing
    python setup_daily.py --check --quick  # skip the slow capability probe
    python setup_daily.py --weekly Monday  # weekly instead of daily
    python setup_daily.py --at 06:30       # a different time
    python setup_daily.py --remove         # delete the scheduled task

It verifies the Python packages, the Claude CLI, your Zenodo token and the project
files, then registers a Windows Scheduled Task that runs run_daily.ps1 silently.

The job stages a Zenodo DRAFT. It never publishes - that stays your decision.
"""
import os
import re
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
TASK = "ZenodoDailyPaper"
OK, BAD, WARN = "  OK   ", "  FAIL ", "  WARN "


def ps(cmd):
    """Run one PowerShell command; return (returncode, stdout)."""
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip()


def check_packages(problems):
    for mod, pkg, why in (("requests", "requests", "Zenodo API"),
                          ("fitz", "pymupdf", "PDF rendering"),
                          ("markdown", "markdown", "Markdown to HTML")):
        try:
            __import__(mod)
            print(OK + "python package %-10s (%s)" % (mod, why))
        except ImportError:
            print(BAD + "python package %-10s MISSING" % mod)
            problems.append("pip install " + pkg)


def check_cli(problems):
    cli = os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe")
    if os.path.isfile(cli):
        print(OK + "claude CLI          %s" % cli)
        return
    rc, out = ps("(Get-Command claude -ErrorAction SilentlyContinue).Source")
    if rc == 0 and out:
        print(OK + "claude CLI          %s" % out)
    else:
        print(BAD + "claude CLI not found - the job cannot run without it")
        problems.append("install the Claude Code CLI")


def find_cli():
    cli = os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe")
    if os.path.isfile(cli):
        return cli
    rc, out = ps("(Get-Command claude -ErrorAction SilentlyContinue).Source")
    return out if (rc == 0 and out) else None


def auth_failure(text):
    low = text.lower()
    return ("authentication_error" in low or "401" in text
            or "re-authenticate" in low or "/login" in low)


def check_headless(problems):
    """The binary existing proves nothing. Actually invoke it."""
    cli = find_cli()
    if not cli:
        return
    try:
        p = subprocess.run([cli, "-p", "Reply with exactly: READY"],
                           capture_output=True, text=True, timeout=90, cwd=HERE)
    except subprocess.TimeoutExpired:
        print(BAD + "headless claude     no reply within 90s")
        print("         A healthy 'claude -p' answers in seconds. This silence is")
        print("         expired CLI auth: the token refresh retries for about five")
        print("         minutes before surfacing a 401, so it looks like a hang.")
        print("         Fix: run 'claude', accept the trust prompt, then type")
        print("         /login and complete sign-in in the browser. Then re-check.")
        problems.append("re-authenticate the Claude CLI: run 'claude', sign in, exit")
        return
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if "READY" in out and "error" not in out.lower():
        print(OK + "headless claude     responds to 'claude -p'")
    elif auth_failure(out):
        print(BAD + "headless claude     AUTH EXPIRED - the daily job cannot run")
        print("         run 'claude', then type /login and sign in. Then re-check.")
        problems.append("re-authenticate the Claude CLI (run: claude, then /login)")
    else:
        print(WARN + "headless claude     unexpected reply: %s" % out[:60])


def check_capabilities(problems, quick):
    """Answering 'READY' proves nothing about whether the pipeline can WORK.

    Headless claude starts sandboxed: no file writes, no network. The job needs
    both. This ran green for a whole day while the pipeline could not function,
    so the check now proves the capability instead of assuming it.
    """
    if quick:
        print(WARN + "tool permissions    skipped (--quick)")
        return
    cli = find_cli()
    if not cli:
        return
    try:
        import claude_flags
        flags = claude_flags.flags()
    except Exception as e:
        print(BAD + "tool permissions    cannot load claude_flags.py (%s)" % e)
        problems.append("restore claude_flags.py")
        return

    probe = os.path.join(HERE, "_capcheck.tmp")
    if os.path.exists(probe):
        os.remove(probe)
    prompt = ("Two things, then stop. (1) Write a file named _capcheck.tmp in the "
              "current directory containing exactly: CAP-OK. (2) Fetch "
              "http://export.arxiv.org/api/query?id_list=1706.03762 and reply with "
              "the paper title, prefixed by FETCH-OK:")
    try:
        p = subprocess.run([cli, "-p", prompt] + flags,
                           capture_output=True, text=True, timeout=420, cwd=HERE)
    except subprocess.TimeoutExpired:
        print(WARN + "tool permissions    probe timed out after 7 min")
        return
    out = ((p.stdout or "") + (p.stderr or ""))

    wrote = os.path.isfile(probe)
    if wrote:
        try:
            wrote = "CAP-OK" in open(probe, encoding="utf-8").read()
        except OSError:
            wrote = False
        try:
            os.remove(probe)
        except OSError:
            pass
    fetched = "Attention Is All You Need" in out

    if wrote and fetched:
        print(OK + "tool permissions    can write files and reach the network")
    else:
        if auth_failure(out):
            print(BAD + "tool permissions    auth failed during the probe")
            problems.append("re-authenticate the Claude CLI (run: claude, then /login)")
            return
        print(BAD + "tool permissions    write=%s  network=%s"
              % ("OK" if wrote else "BLOCKED", "OK" if fetched else "BLOCKED"))
        print("         Without both, the run cannot verify citations and will")
        print("         refuse to draft. Check ALLOWED_TOOLS in claude_flags.py.")
        problems.append("fix tool permissions in claude_flags.py")


def check_files(problems):
    need = ["run_daily.ps1", "DAILY_RUN.md", "topics.md", "publish_paper.py",
            "cite_check.py", "md2pdf.py", "review_draft.py", "claude_flags.py",
            "go.py"]
    missing = [f for f in need if not os.path.isfile(os.path.join(HERE, f))]
    if missing:
        print(BAD + "missing project files: %s" % ", ".join(missing))
        problems.append("restore: " + ", ".join(missing))
    else:
        print(OK + "project files       all %d present" % len(need))


def check_queue():
    path = os.path.join(HERE, "topics.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return
    n = len(re.findall(r"^- \[ \] ", text, re.M))
    print((OK if n else WARN) + "topic queue         %d unchecked topic(s)" % n)
    if not n:
        print("         an empty queue means the job selects its own topic")


def check_token(problems):
    rc, tok = ps("[Environment]::GetEnvironmentVariable('ZENODO_TOKEN','User')")
    if not tok:
        print(BAD + "ZENODO_TOKEN not set at User scope")
        print("         a scheduled task cannot see a session variable. Run:")
        print("         [Environment]::SetEnvironmentVariable("
              "\"ZENODO_TOKEN\",\"<token>\",\"User\")")
        problems.append("set ZENODO_TOKEN at User scope")
        return
    print(OK + "ZENODO_TOKEN        set at User scope (%d chars)" % len(tok))
    try:
        import requests
    except ImportError:
        return
    base = os.environ.get("ZENODO_BASE", "https://zenodo.org")
    try:
        r = requests.get(base + "/api/deposit/depositions",
                         headers={"Authorization": "Bearer " + tok},
                         params={"size": 1}, timeout=30)
    except Exception as e:
        print(WARN + "could not reach Zenodo to test the token (%s)" % type(e).__name__)
        return
    if r.status_code == 200:
        print(OK + "token works         authenticated against %s" % base)
    elif r.status_code in (401, 403):
        print(BAD + "token rejected (%d) - wrong token or missing scopes" % r.status_code)
        problems.append("create a token with deposit:write and deposit:actions")
    else:
        print(WARN + "token check returned HTTP %d" % r.status_code)


def task_exists():
    """The task's ACTUAL next run time, not its trigger StartBoundary.

    StartBoundary is when the schedule was defined and never moves, so it
    showed a date in the past while the job was running fine.
    """
    rc, out = ps("if (Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue)"
                 " { (Get-ScheduledTaskInfo -TaskName '%s').NextRunTime }" % (TASK, TASK))
    return out


def register(at, weekly):
    runner = os.path.join(HERE, "run_daily.ps1")
    if weekly:
        trigger = ("New-ScheduledTaskTrigger -Weekly -DaysOfWeek %s -At %s"
                   % (weekly, at))
        schedule = "every %s at %s" % (weekly, at)
    else:
        trigger = "New-ScheduledTaskTrigger -Daily -At %s" % at
        schedule = "daily at %s" % at

    print("\nabout to register scheduled task '%s'" % TASK)
    print("  runs   : %s" % runner)
    print("  when   : %s" % schedule)
    print("  window : hidden, no popup")
    print("  action : stages a Zenodo DRAFT. It will NOT publish.")
    if input("\nproceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("cancelled. nothing was changed.")
        return

    action = ("New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
              "'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%s\"'"
              % runner)
    cmd = ("$a = %s; $t = %s; "
           "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable; "
           "Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t "
           "-Settings $s -Description "
           "'Drafts and stages a research paper; never publishes' -Force"
           % (action, trigger, TASK))
    rc, out = ps(cmd)
    if rc != 0:
        print("\nregistration failed:\n%s" % out)
        print("\nIf that is a permissions error, run PowerShell as Administrator.")
        return

    print("\nregistered. next run: %s" % (task_exists()[:16] or schedule))
    print("\nEach morning after it runs:")
    print("    python review_draft.py")
    print("    python publish_paper.py drafts/<name>.md --publish   # or discard")
    print("\nlogs: runner.log (the run) and daily_log.md (what it produced)")


def main():
    args = sys.argv[1:]
    at = args[args.index("--at") + 1] if "--at" in args else "05:00"
    weekly = args[args.index("--weekly") + 1] if "--weekly" in args else None
    if not re.match(r"^\d{1,2}:\d{2}$", at):
        sys.exit("--at must look like 05:00")

    print("\nDaily paper job - setup")
    print("=" * 60)

    if "--remove" in args:
        rc, out = ps("Unregister-ScheduledTask -TaskName '%s' -Confirm:$false" % TASK)
        print("removed." if rc == 0 else "not registered, or removal failed:\n" + out)
        return

    problems = []
    check_packages(problems)
    check_cli(problems)
    check_headless(problems)
    check_capabilities(problems, "--quick" in args)
    check_files(problems)
    check_queue()
    check_token(problems)

    when = task_exists()
    print((OK + "task registered     next run %s" % when[:16]) if when
          else (WARN + "task not registered yet"))
    print("=" * 60)

    if problems:
        print("\nFix these first:")
        for p in problems:
            print("  - %s" % p)
        print("\nnot registering the task until they are fixed.")
        return

    if "--check" in args:
        print("\nall checks passed."
              + ("" if when else " Run without --check to register."))
        return

    register(at, weekly)


if __name__ == "__main__":
    main()
