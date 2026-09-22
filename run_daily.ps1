# Daily paper run. Invoked by Windows Task Scheduler.
# Silent: no window, no popup. Everything lands in runner.log / daily_log.md.
#
# NOTE: claude.exe exits 0 even when the API call fails (expired auth, rate
# limit, network). Exit code alone cannot be trusted, so the output is
# inspected for failure markers and the log records an explicit verdict.
#
# The verdict is a DOI, nothing else. publish_paper.py writes `doi:` and
# `record_url:` back into the draft's front matter once Zenodo accepts the
# record, so a draft carrying a new DOI is the only proof the paper is
# actually public. This script used to judge success by counting .md files in
# drafts/, which was wrong twice over: it counted the sec*_*.md working
# fragments as papers, and it could not tell "wrote a draft" from "published
# a paper". On 2026-08-27 the run stalled at the citation gate, wrote a draft,
# published nothing, and still reported OK and exit 0 - so the failure was
# invisible until the website was noticed to be a day stale.
$ErrorActionPreference = "Stop"
# The content tree: DAILY_RUN.md, topics.md, drafts/. Defaults to this
# script's own directory, which is right when the toolkit is checked out
# alongside the content; set ZENODO_PAPER_HOME when they live apart.
$proj = if ($env:ZENODO_PAPER_HOME) { $env:ZENODO_PAPER_HOME } else { $PSScriptRoot }
$log  = Join-Path $proj "runner.log"
Set-Location $proj

function Note($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format s), $msg
    Add-Content $log $line
    # Also to the console, so a manual run is not a silent black box for the
    # hours this takes. Harmless under Task Scheduler, which has no console.
    Write-Host $line
}

# Every DOI currently recorded in a draft's front matter.
function Get-PublishedDois {
    $dir = Join-Path $proj "drafts"
    $out = @()
    foreach ($f in @(Get-ChildItem $dir -Filter *.md -ErrorAction SilentlyContinue)) {
        $m = Select-String -Path $f.FullName -Pattern '^doi:\s*(\S+)' -List
        if ($m) { $out += $m.Matches[0].Groups[1].Value }
    }
    return $out
}

# Today's paper, if it is already out. Derived from the drafts themselves:
# publish_paper.py writes `doi:` into the front matter of the draft it just
# published, and that front matter carries the publication date - so the
# repository already knows whether today is done. No separate stamp file that
# could fall out of sync with reality.
function Get-TodaysPublishedDoi {
    $today = Get-Date -Format 'yyyy-MM-dd'
    foreach ($f in @(Get-ChildItem (Join-Path $proj "drafts") -Filter *.md -ErrorAction SilentlyContinue)) {
        $head = Get-Content $f.FullName -TotalCount 60 -ErrorAction SilentlyContinue
        if (-not $head) { continue }
        $text = $head -join "`n"
        if (($text -match "(?m)^date:\s*$today\b") -and ($text -match "(?m)^doi:\s*(\S+)")) {
            return $Matches[1]
        }
    }
    return $null
}

Note "----- run starting -----"

# A finished day is a no-op. On 2026-09-22 the 05:00 run published, then three
# more launches fired; each spent an hour re-deriving that the work was done,
# and the runner scored those correct holds as failures and retried them. Every
# one of those retries was a chance to mint a second permanent DOI. Cheapest
# and safest to answer the question before starting any work at all.
$alreadyOut = Get-TodaysPublishedDoi
if ($alreadyOut) {
    Note ("Today's paper is already published: {0}" -f $alreadyOut)
    Note ("        https://doi.org/{0}" -f $alreadyOut)
    Note "        Nothing to do - a second launch on a finished day is a no-op."
    Note "----- run finished -----"
    exit 0
}

if (-not $env:ZENODO_TOKEN) {
    Note "ABORT: ZENODO_TOKEN not set for this user. Run:"
    Note "       [Environment]::SetEnvironmentVariable('ZENODO_TOKEN','<tok>','User')"
    exit 1
}

$cli = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
if (-not (Test-Path $cli)) {
    $found = Get-Command claude -ErrorAction SilentlyContinue
    if ($found) { $cli = $found.Source } else { Note "ABORT: claude CLI not found"; exit 1 }
}

# Snapshot before the run, so a new DOI can be told from the existing ones.
$doisBefore   = Get-PublishedDois
$draftsBefore = @(Get-ChildItem (Join-Path $proj "drafts") -Filter *.md -ErrorAction SilentlyContinue).Count

$prompt = @'
Follow the instructions in DAILY_RUN.md exactly. You are running unattended.
Publish the paper as step 9 describes, using --publish --yes, but only if every
gate passes. Never pass --skip-cite-check or --force-duplicate. If you have real
doubt about the paper, stage it as a draft instead and say why in the log.
'@

# Headless claude starts with no file-write and no network permission. The
# pipeline needs both - it writes drafts and verifies citations online - so the
# allowed-tool set comes from claude_flags.py, shared with go.py / run_now.py.
$flagStr = & python (Join-Path $proj "claude_flags.py")
$flags = $flagStr.Trim() -split '\s+'

# A dropped connection used to cost the whole day: the session was invoked
# once, and "API Error: Connection lost mid-response" four minutes in meant no
# paper until tomorrow. It is safe to try again ONLY when the attempt produced
# nothing at all - no new DOI and no new draft. If a draft exists the next
# attempt would write a second paper on the next queue topic, and if a DOI was
# minted the paper is already public; in both cases retrying makes things
# worse, so both stop the loop.
#
# Auth failures are not retried. An expired token will not fix itself,
# and hammering it just delays the report.
# Patterns, not literal strings. These were literals once and it cost a run:
# on 2026-09-21 the CLI said "Failed to authenticate: OAuth session expired and
# could not be refreshed", the literal "OAuth access token has expired" did not
# match, and the script fell through to its generic verdict and blamed the
# topic queue for what was an expired login. Match the shape of the message,
# not one phrasing of it.
$TERMINAL_PATTERNS = @(
    'failed to authenticate',
    'authentication[_ ]error',
    'oauth.*(expired|refresh)',
    'please run /login',
    'invalid api key',
    'not (logged in|authenticated)',
    'session expired'
)
$TRANSIENT_PATTERNS = @(
    'api error',
    'connection lost',
    'rate[_ ]limit',
    'overloaded',
    'response stopped arriving',
    '(request|read|connection) timed out',
    '50[0234] '
)

$MAX_ATTEMPTS = 3
$RETRY_WAIT   = 300          # 5 minutes; these outages are usually brief

$out = ""
$failed = $false

for ($attempt = 1; $attempt -le $MAX_ATTEMPTS; $attempt++) {
    if ($attempt -gt 1) {
        Note ("retrying: attempt {0} of {1}" -f $attempt, $MAX_ATTEMPTS)
    }

    $out = ""
    try {
        # Tee to the console as it arrives rather than collecting silently:
        # Write-Host shows it now, the object still flows down the pipeline
        # into $out for the marker checks below.
        $out = & $cli -p $prompt @flags 2>&1 |
               ForEach-Object { Write-Host $_; $_ } |
               Out-String
    } catch {
        Note ("ERROR: invoking claude threw: {0}" -f $_)
        exit 1
    }

    Add-Content $log $out

    # claude.exe returns 0 on API failures, so look at what it actually said -
    # but only at how it ENDED. Matching these patterns against the whole
    # transcript was wrong: on 2026-09-22 a session that ran fine for 84
    # minutes and deliberately held mentioned a timeout in its own prose, the
    # runner read that as a live API failure and retried it twice. A session
    # killed by an error says so in its last breath; a session discussing one
    # is just talking.
    $tail = ($out -split "`r?`n" | Select-Object -Last 20) -join "`n"

    $terminal = $false
    $transient = $false
    foreach ($pat in $TERMINAL_PATTERNS) {
        if ($tail -match "(?i)$pat") {
            Note ("FAILED: authentication problem - not retrying, this needs you:")
            Note ("        {0}" -f $Matches[0])
            $terminal = $true
            break
        }
    }
    if (-not $terminal) {
        foreach ($pat in $TRANSIENT_PATTERNS) {
            if ($tail -match "(?i)$pat") {
                Note ("transient API problem: {0}" -f $Matches[0])
                $transient = $true
                break
            }
        }
    }

    if ($terminal) { $failed = $true; break }
    if (-not $transient) { break }          # ran clean; let the verdict judge it

    # Transient. Only worth another go if this attempt left nothing behind.
    $soFarDois   = Get-PublishedDois
    $soFarDrafts = @(Get-ChildItem (Join-Path $proj "drafts") -Filter *.md -ErrorAction SilentlyContinue).Count
    $soFarNew    = @($soFarDois | Where-Object { $doisBefore -notcontains $_ })

    if ($soFarNew.Count -gt 0) {
        Note "        ...but a DOI was minted, so the paper is out. Not retrying."
        break
    }
    if ($soFarDrafts -gt $draftsBefore) {
        Note "        ...but a draft was written. Not retrying - that would start a"
        Note "        second paper. Finish the existing draft by hand instead."
        break
    }
    if ($attempt -eq $MAX_ATTEMPTS) {
        Note ("FAILED: {0} attempts all hit a transient API error." -f $MAX_ATTEMPTS)
        $failed = $true
        break
    }

    Note ("        nothing was produced, so waiting {0}s and trying again" -f $RETRY_WAIT)
    Start-Sleep -Seconds $RETRY_WAIT
}

if ($failed) {
    Note "FAILED: no paper produced."
    Note "        If the message above is about authentication, re-login with:"
    Note "            claude auth login"
    Note "        then re-run. Otherwise: python setup_daily.py --check"
    exit 1
}

$doisAfter   = Get-PublishedDois
$draftsAfter = @(Get-ChildItem (Join-Path $proj "drafts") -Filter *.md -ErrorAction SilentlyContinue).Count
$newDois     = @($doisAfter | Where-Object { $doisBefore -notcontains $_ })

if ($newDois.Count -gt 0) {
    foreach ($d in $newDois) {
        Note ("OK: published {0} - https://doi.org/{1}" -f $d, $d)
    }
    Note "----- run finished -----"
    exit 0
}

# Nothing was published. Say which of the two ways it went wrong, and give the
# exact command to finish by hand - the draft is usually complete and only the
# last step is missing.
if ($draftsAfter -gt $draftsBefore) {
    $newest = Get-ChildItem (Join-Path $proj "drafts") -Filter *.md |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Note ("FAILED: a draft was written but no DOI was minted - {0}" -f $newest.Name)
    Note "        Either the run stopped early (check the tail of this log for"
    Note "        where it got to) or it deliberately staged the paper. Finish it with:"
    Note ("        python publish_paper.py drafts/{0} --publish --yes" -f $newest.Name)
} else {
    Note ("FAILED: no new draft and no DOI (drafts still {0})." -f $draftsAfter)
    Note "        The run produced nothing. Read the session output above first -"
    Note "        an auth or network error there is the usual cause. An empty or"
    Note "        unworkable topic queue is the other, and daily_log.md says which."
}

Note "----- run finished -----"
exit 1
