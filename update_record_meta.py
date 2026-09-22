r"""Edit the metadata of an ALREADY-PUBLISHED Zenodo record.

Metadata (unlike files) can be changed after publication, via the
edit -> update -> publish action cycle. This script does exactly that for one
record, starting from its current metadata so nothing is dropped, showing a full
diff, and requiring you to type APPLY.

    python update_record_meta.py 22030500 --pub-type article \
        --journal-title "Life of Research" --copyright "Pranay Mahendrakar"

    (add --apply to actually write; without it this is a dry run)

Options:
    --pub-type X          publication_type: article|report|workingpaper|preprint|
                          technicalnote|conferencepaper|thesis|other
    --journal-title X     journal_title (only meaningful with --pub-type article)
    --journal-volume X    --journal-issue X    --journal-pages X
    --copyright NAME      prepend a copyright line to the description
    --supersede DOI       mark this record as a duplicate of DOI: prefixes the
                          title with [DUPLICATE - see DOI], prepends a notice to
                          the description, and adds an isObsoletedBy relation.
                          Use when the same paper was published twice; published
                          records cannot be deleted, only annotated.
    --version X           set the version string
    --dry-run             explicit no-op (same as omitting --apply)
"""
import os
import re
import sys
import html
import json
import difflib

import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
VALID_TYPES = {"article", "report", "workingpaper", "preprint", "technicalnote",
               "conferencepaper", "thesis", "book", "section", "patent",
               "deliverable", "milestone", "proposal", "softwaredocumentation",
               "taxonomictreatment", "datamanagementplan", "annotationcollection",
               "other"}


def die(msg, code=2):
    print(msg)
    sys.exit(code)


def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        die(__doc__)
    rec_id = args[0]
    if not rec_id.isdigit():
        die("first argument must be a numeric record id")

    def opt(flag):
        return args[args.index(flag) + 1] if flag in args else None

    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        die('ZENODO_TOKEN is not set.  PowerShell:  $env:ZENODO_TOKEN="..."')

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer %s" % token})

    r = s.get("%s/api/deposit/depositions/%s" % (BASE, rec_id))
    if not r.ok:
        die("cannot read deposition %s: %s %s" % (rec_id, r.status_code, r.text[:300]))
    dep = r.json()
    if dep.get("state") != "done":
        die("deposition %s is not published (state=%s). Use the normal draft flow."
            % (rec_id, dep.get("state")))

    before = dep["metadata"]
    after = json.loads(json.dumps(before))          # deep copy
    changes = []

    pt = opt("--pub-type")
    if pt:
        if pt not in VALID_TYPES:
            die("invalid --pub-type %r. Valid: %s" % (pt, ", ".join(sorted(VALID_TYPES))))
        after["publication_type"] = pt
        changes.append("publication_type -> %s" % pt)

    for flag, key in (("--journal-title", "journal_title"),
                      ("--journal-volume", "journal_volume"),
                      ("--journal-issue", "journal_issue"),
                      ("--journal-pages", "journal_pages"),
                      ("--version", "version")):
        v = opt(flag)
        if v:
            after[key] = v
            changes.append("%s -> %s" % (key, v))

    # --supersede: mark this record as the duplicate it is, and point at the
    # canonical one. A published Zenodo record cannot be deleted, so this is
    # the only honest remedy - the record stays, but it stops competing with
    # the real one for citations, and anyone who lands on it is told where to go.
    canonical = opt("--supersede")
    if canonical:
        canon_doi = canonical.strip()
        if not canon_doi.startswith("10."):
            die("--supersede takes the canonical DOI, e.g. 10.5281/zenodo.22044420")
        if canon_doi == after.get("doi"):
            die("--supersede points at this record's own DOI")

        rels = [r for r in (after.get("related_identifiers") or [])
                if not (r.get("identifier") == canon_doi
                        and r.get("relation") == "isObsoletedBy")]
        rels.append({"identifier": canon_doi,
                     "relation": "isObsoletedBy",
                     "scheme": "doi"})
        after["related_identifiers"] = rels
        changes.append("related_identifiers += isObsoletedBy %s" % canon_doi)

        notice = ("DUPLICATE RECORD. This deposit was uploaded twice by mistake. "
                  "The canonical version of this work is %s. Please cite that DOI "
                  "instead of this one. The file and text are identical; this "
                  "record is retained only because published Zenodo records "
                  "cannot be withdrawn." % ("https://doi.org/" + canon_doi))
        desc = after.get("description", "")
        if "DUPLICATE RECORD" not in desc:
            after["description"] = ("<p><strong>%s</strong></p>%s"
                                    % (html.escape(notice), desc))
            changes.append("description: prepended the duplicate notice")

        title = after.get("title", "")
        if not title.startswith("[DUPLICATE"):
            after["title"] = "[DUPLICATE - see %s] %s" % (canon_doi, title)
            changes.append("title: prefixed with the duplicate marker")

    holder = opt("--copyright")
    if holder:
        year = (after.get("publication_date") or "")[:4] or "2026"
        line = "(c) %s %s. Licensed under CC BY 4.0." % (year, holder)
        desc = after.get("description", "")
        if "Licensed under CC BY 4.0" in desc:
            print("note: a copyright line is already present; not adding a second")
        else:
            after["description"] = "<p><strong>%s</strong></p>%s" % (html.escape(line), desc)
            changes.append("description: prepended %s" % line)

    if not changes:
        die("no changes requested. See --help.", 0)

    if after.get("publication_type") == "article" and not after.get("journal_title"):
        print("warning: publication_type 'article' without a journal_title reads as an\n"
              "         incomplete journal reference to indexers.")

    print("record  : %s" % rec_id)
    print("title   : %s" % (before.get("title") or "")[:72])
    print("state   : %s (published)" % dep.get("state"))
    print("\nCHANGES")
    for c in changes:
        print("  * %s" % c)

    b = json.dumps(before, indent=2, sort_keys=True).splitlines()
    a = json.dumps(after, indent=2, sort_keys=True).splitlines()
    diff = [l for l in difflib.unified_diff(b, a, "before", "after", lineterm="", n=1)]
    print("\nDIFF")
    for l in diff[:60]:
        print("  " + (l[:160]))
    if len(diff) > 60:
        print("  ... %d more diff lines" % (len(diff) - 60))

    if "--apply" not in args:
        print("\nDRY RUN. Nothing was written. Re-run with --apply to make these changes.")
        return

    print("\nThis reopens the published record, replaces its metadata, and")
    print("re-publishes it. The DOI and the file are unaffected.")
    answer = input("type APPLY to proceed (anything else aborts): ")
    if answer.strip() != "APPLY":
        print("aborted. Nothing was written.")
        return

    r = s.post("%s/api/deposit/depositions/%s/actions/edit" % (BASE, rec_id))
    if not r.ok and r.status_code != 400:      # 400 = already in edit mode
        die("could not open record for editing: %s %s" % (r.status_code, r.text[:300]))

    r = s.put("%s/api/deposit/depositions/%s" % (BASE, rec_id),
              json={"metadata": after})
    if not r.ok:
        s.post("%s/api/deposit/depositions/%s/actions/discard" % (BASE, rec_id))
        die("metadata rejected (%s), edits discarded, record unchanged:\n%s"
            % (r.status_code, r.text[:400]))

    r = s.post("%s/api/deposit/depositions/%s/actions/publish" % (BASE, rec_id))
    if not r.ok:
        s.post("%s/api/deposit/depositions/%s/actions/discard" % (BASE, rec_id))
        die("re-publish failed (%s), edits discarded, record unchanged:\n%s"
            % (r.status_code, r.text[:400]))

    print("\nUPDATED. %s/records/%s" % (BASE, rec_id))
    chk = s.get("%s/api/records/%s" % (BASE, rec_id))
    if chk.ok:
        rt = chk.json()["metadata"].get("resource_type", {})
        print("  resource_type now: %s" % rt.get("title"))


if __name__ == "__main__":
    main()
