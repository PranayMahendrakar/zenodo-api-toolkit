r"""Stage a Markdown paper on Zenodo as an UNPUBLISHED draft.

Every field that ends up on the permanent record is read from the paper's own YAML
front matter, defaulted conservatively, and echoed back in the confirmation block
before anything irreversible happens. Nothing about the record is decided here in
code. Renders the PDF if it is missing or stale, verifies every citation with
cite_check.py, then creates a Zenodo deposition and uploads the PDF.

It STOPS at the draft and prints the URL. It never publishes unless you both pass
--publish and type PUBLISH at the prompt:

    python publish_paper.py drafts/self-verification-gap.md --dry-run
    python publish_paper.py drafts/self-verification-gap.md
    python publish_paper.py drafts/self-verification-gap.md --orcid 0000-0002-1825-0097 \
        --affiliation "Advance Mechanical Services Pvt Ltd"

Front matter is parsed by md2pdf.py's reader, so the record and the rendered PDF
read the same file the same way: quoted values keep their contents, trailing
`# ...` comments are not part of the value, and paper_scaffold.py placeholder text
is recognised as placeholder text rather than shipped.

Front matter keys read (CLI flag in brackets overrides the file):

    title, author, date, license, keywords, ai_assistance
    upload_type       [--upload-type]     default "publication"
    access_right      [--access-right]    default "open"
    embargo_date      [--embargo-date]    required when access_right is embargoed
    publication_type  [--pub-type]        default "preprint" (claims nothing)
    journal_title     [--journal-title]   default absent
    journal_volume    [--journal-volume]  default absent
    journal_issue     [--journal-issue]   default absent
    journal_pages     [--journal-pages]   default absent
    copyright         [--copyright]       default: the author
    orcid             [--orcid]           default absent
    affiliation       [--affiliation]     default absent
    version           [--version]         default absent

Other flags:

    --dry-run           print the record block and stop. No token needed, no
                        render, no citation check, no network call of any kind.
    --skip-cite-check   upload even if citations fail (not recommended)
    --pdf <path>        use a specific PDF instead of the derived name
    --publish           arm the publish step (asks you to type PUBLISH)
    --yes               with --publish: do not ask. For unattended runs.
                        Every mechanical gate still applies - citations must
                        resolve, no placeholders, no duplicate title - but
                        nobody reads the paper before the DOI is minted.
    --force-duplicate   upload even though this title is already on the account
                        (normally refused - see the duplicate guard)

After a successful publish the minted DOI and record URL are written back into the
paper's front matter as `doi:` and `record_url:`, without disturbing the file's
line endings or its body. If either key is already there the confirmation block
prints it and warns: publishing again mints a second, unrelated DOI, it does not
supersede the existing record. Use zenodo_version.py for a corrected edition.
"""
import os
import re
import sys
import html
import time
import subprocess

import requests

BASE = os.environ.get("ZENODO_BASE", "https://zenodo.org")
HERE = os.path.dirname(os.path.abspath(__file__))

# One reader for the front matter, shared with the renderer. If these two ever
# disagree the PDF and the record disagree, which is how a wrong publication_type
# and a missing copyright line got onto a permanent record in the first place.
sys.path.insert(0, HERE)
import paper_defaults  # noqa: E402
from md2pdf import (fm_get as scalar, fm_block as block, fm_meta,   # noqa: E402
                    is_placeholder, yaml_scalar, copyright_text)

# Front-matter spelling -> the licence id Zenodo actually stores. Anything not
# on this list is passed through verbatim and flagged in the confirmation block;
# it is never quietly replaced with a different licence.
LICENSE_MAP = {
    "cc-by-4.0": "cc-by-4.0",
    "cc-by-sa-4.0": "cc-by-sa-4.0",
    "cc-by-nc-4.0": "cc-by-nc-4.0",
    "cc-by-nd-4.0": "cc-by-nd-4.0",
    "cc-by-nc-sa-4.0": "cc-by-nc-sa-4.0",
    "cc-by-nc-nd-4.0": "cc-by-nc-nd-4.0",
    "cc0-1.0": "cc-zero",
    "cc-zero": "cc-zero",
}

# Human label for every publication_type Zenodo accepts under upload_type
# "publication".  The keys are the vocabulary; the values exist so the
# confirmation block can say what the code word actually means.
PUB_TYPES = [
    ("article", "journal article"),
    ("report", "report"),
    ("workingpaper", "working paper"),
    ("preprint", "preprint - not peer reviewed, claims no venue"),
    ("technicalnote", "technical note"),
    ("conferencepaper", "conference paper"),
    ("thesis", "thesis"),
    ("book", "book"),
    ("section", "book section / chapter"),
    ("patent", "patent"),
    ("deliverable", "project deliverable"),
    ("milestone", "project milestone"),
    ("proposal", "proposal"),
    ("softwaredocumentation", "software documentation"),
    ("taxonomictreatment", "taxonomic treatment"),
    ("datamanagementplan", "data management plan"),
    ("annotationcollection", "annotation collection"),
    ("other", "other"),
]
TYPE_LABEL = dict(PUB_TYPES)
VALID_TYPES = [t for t, _ in PUB_TYPES]

# Zenodo's upload_type vocabulary. Only "publication" carries publication_type.
UPLOAD_TYPES = ["publication", "poster", "presentation", "dataset", "image",
                "video", "software", "lesson", "physicalobject", "other"]

# Zenodo's access_right vocabulary. "open" is the conservative default here
# because an open record is the one a reader can actually check.
ACCESS_RIGHTS = ["open", "embargoed", "restricted", "closed"]

JOURNAL_FIELDS = [
    ("journal_title", "--journal-title"),
    ("journal_volume", "--journal-volume"),
    ("journal_issue", "--journal-issue"),
    ("journal_pages", "--journal-pages"),
]


def die(msg, code=2):
    print(msg)
    sys.exit(code)


def front_matter(text):
    if not text.startswith("---"):
        die("no YAML front matter found in the paper")
    end = text.find("\n---", 3)
    if end < 0:
        die("the YAML front matter in the paper is never closed by a '---' line")
    return text[3:end].strip(), text[end + 4:]


def listing(fm, key):
    m = re.search(r"^%s:\s*\n((?:\s*-\s*.*\n?)+)" % re.escape(key), fm, re.M)
    if not m:
        return []
    items = [yaml_scalar(re.sub(r"^\s*-\s*", "", l))
             for l in m.group(1).splitlines() if l.strip()]
    return [i for i in items if i]


def abstract_of(body):
    """The ## Abstract section as one line, with HTML comments removed.

    The comments matter: a draft generated by paper_scaffold.py carries a
    working note under every heading, and without this the note itself became
    the public description of the record.
    """
    m = re.search(r"(?ms)^##\s+Abstract\s*\n(.+?)(?=^##\s)", body)
    if not m:
        return ""
    text = re.sub(r"(?s)<!--.*?-->", " ", m.group(1))
    return " ".join(text.split())


def surname_first(name):
    """Citation form "Surname, Given", without reversing a name already in it.

    paper_scaffold.py writes the author key in citation form already (its own
    placeholder is "LASTNAME, Firstname"), and swapping on the last whitespace
    token turned "Doe, Jane" into "Jane, Doe" -- a wrong creator name on a
    permanent record.
    """
    name = " ".join(name.split())
    if "," in name:
        surname, _, given = name.partition(",")
        surname, given = surname.strip(), given.strip()
        return "%s, %s" % (surname, given) if given else surname
    parts = name.split()
    return "%s, %s" % (parts[-1], " ".join(parts[:-1])) if len(parts) > 1 else name


def write_front_matter(src, updates):
    """Set scalar keys in the paper's own front matter, in place.

    updates is a list of (key, value) pairs. An existing key is replaced, a new
    one is appended just before the closing '---'. The body is untouched, and so
    are the file's line endings: it is read and written with newline="" so no
    translation happens, and an inserted line uses whatever the front matter
    already uses. Returns True on success, False if the file no longer looks
    like it has front matter (in which case nothing is written).
    """
    with open(src, encoding="utf-8", newline="") as fh:
        text = fh.read()
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    head, tail = text[3:end], text[end:]
    # On a CRLF file the closing delimiter's CR is the last character of head.
    # Hold it aside so an appended line goes in front of it and the ending
    # survives intact.
    trail = ""
    if head.endswith("\r"):
        head, trail = head[:-1], "\r"
    nl = "\r\n" if "\r\n" in head else "\n"
    for key, value in updates:
        line = "%s: %s" % (key, value)
        # [^\r\n]* rather than .*$ because '.' matches CR: with .*$ replacing a
        # key on a CRLF line ate its CR and left a lone LF in a CRLF file.
        pat = re.compile(r"^%s:[^\r\n]*" % re.escape(key), re.M)
        if pat.search(head):
            head = pat.sub(lambda m: line, head, count=1)
        else:
            head = head + nl + line
    with open(src, "w", encoding="utf-8", newline="") as fh:
        fh.write("---" + head + trail + tail)
    return True


def show(label, value, note=""):
    print("  %-18s: %s%s" % (label, value, note))


def resolve(path):
    """Accept a path relative to the CWD or to the project directory.

    These scripts are usually invoked by absolute path from some other folder,
    so "drafts/x.md" should still find D:/.../Zenodo API Research/drafts/x.md.
    Returns the resolved path, or None.
    """
    if os.path.isfile(path):
        return path
    alt = os.path.join(HERE, path)
    return alt if os.path.isfile(alt) else None


def main():
    args = sys.argv[1:]
    if not args:
        die(__doc__)
    src = resolve(args[0])
    if not src:
        die("file not found: %s\n"
            "  (tried it as given, and relative to %s)" % (args[0], HERE))

    def opt(flag, default=""):
        if flag not in args:
            return default
        i = args.index(flag) + 1
        if i >= len(args):
            die("%s needs a value" % flag)
        return args[i].strip()

    # A mistyped flag must not silently fall through to a default on a
    # permanent record, so reject anything not on these lists.
    value_flags = {"--pdf", "--orcid", "--affiliation", "--pub-type",
                   "--journal-title", "--journal-volume", "--journal-issue",
                   "--journal-pages", "--copyright", "--version",
                   "--upload-type", "--access-right", "--embargo-date"}
    bool_flags = {"--skip-cite-check", "--publish", "--dry-run",
                  "--yes", "--force-duplicate"}
    i = 1
    while i < len(args):
        if args[i] in value_flags:
            i += 2
        elif args[i] in bool_flags:
            i += 1
        elif args[i].startswith("-"):
            die("unknown option %r\n\n%s" % (args[i], __doc__))
        else:
            die("unexpected argument %r. One paper per run, and every other\n"
                "argument must be a flag; a value that lost its flag would\n"
                "otherwise be dropped silently.\n\n%s" % (args[i], __doc__))

    dry_run = "--dry-run" in args
    if dry_run and "--publish" in args:
        die("--dry-run and --publish are contradictory; pick one")

    # A dry run touches nothing, so it must not demand a credential either.
    token = os.environ.get("ZENODO_TOKEN")
    if not token and not dry_run:
        die('ZENODO_TOKEN is not set.  PowerShell:  $env:ZENODO_TOKEN="..."')

    text = open(src, encoding="utf-8").read()
    fm, body = front_matter(text)
    title = scalar(fm, "title")
    author = scalar(fm, "author")
    keywords = listing(fm, "keywords")
    disclosure = block(fm, "ai_assistance")
    abstract = abstract_of(body)
    if not (title and author and abstract):
        die("front matter is missing title, author, or the paper has no ## Abstract")

    # ---- every remaining permanent-record field: front matter, then CLI ----
    # Nothing below is decided by this program. It is declared, defaulted, and
    # printed back to you before the DOI is minted.
    fm_lic = scalar(fm, "license")
    if not fm_lic:
        die("no license: in the front matter. The licence is permanent public\n"
            "metadata; declare it rather than letting this script pick one.\n"
            "Accepted values:\n  %s" % "\n  ".join(sorted(LICENSE_MAP)))
    lic = LICENSE_MAP.get(fm_lic.lower(), fm_lic.lower())
    lic_recognised = fm_lic.lower() in LICENSE_MAP

    upload_type = opt("--upload-type") or scalar(fm, "upload_type") or "publication"
    if upload_type not in UPLOAD_TYPES:
        die("invalid upload_type %r. Valid values are:\n  %s"
            % (upload_type, "\n  ".join(UPLOAD_TYPES)))
    upload_type_defaulted = not (opt("--upload-type") or scalar(fm, "upload_type"))

    access_right = opt("--access-right") or scalar(fm, "access_right") or "open"
    if access_right not in ACCESS_RIGHTS:
        die("invalid access_right %r. Valid values are:\n  %s"
            % (access_right, "\n  ".join(ACCESS_RIGHTS)))
    access_right_defaulted = not (opt("--access-right") or scalar(fm, "access_right"))
    embargo_date = opt("--embargo-date") or scalar(fm, "embargo_date")
    if access_right == "embargoed" and not embargo_date:
        die("access_right is 'embargoed' but no embargo_date is set. Set\n"
            "embargo_date: YYYY-MM-DD in the front matter or pass --embargo-date.")

    fm_pub_type = scalar(fm, "publication_type")
    std_pub_type = paper_defaults.get("publication_type")
    pub_type = opt("--pub-type") or fm_pub_type or std_pub_type or "preprint"
    if pub_type not in TYPE_LABEL:
        die("invalid publication_type %r.\nValid values are:\n  %s"
            % (pub_type, "\n  ".join("%-22s %s" % (t, l) for t, l in PUB_TYPES)))
    pub_type_defaulted = not (opt("--pub-type") or fm_pub_type)
    pub_type_from_std = pub_type_defaulted and bool(std_pub_type)

    journal = {}
    for key, flag in JOURNAL_FIELDS:
        v = opt(flag) or scalar(fm, key) or paper_defaults.get(key)
        if v:
            journal[key] = v

    holder = opt("--copyright") or scalar(fm, "copyright") or author
    holder_defaulted = not (opt("--copyright") or scalar(fm, "copyright"))
    version = opt("--version") or scalar(fm, "version")
    orcid = opt("--orcid") or fm_meta(fm, "orcid")
    affiliation = opt("--affiliation") or fm_meta(fm, "affiliation")
    pub_date = scalar(fm, "date")
    prior_doi = scalar(fm, "doi")
    prior_url = scalar(fm, "record_url")

    # Scaffold text is not a decision. None of it may reach a permanent record.
    stale = [(name, val) for name, val in
             (("title", title), ("author", author), ("copyright holder", holder),
              ("ai_assistance", disclosure), ("orcid", orcid),
              ("affiliation", affiliation), ("journal_title",
                                             journal.get("journal_title", "")))
             if val and is_placeholder(val)]
    stale += [("keywords", k) for k in keywords if is_placeholder(k)]
    if stale:
        die("these fields still contain paper_scaffold.py placeholder text, and\n"
            "every one of them would land on the permanent record:\n\n%s\n\n"
            "Edit them in %s before publishing."
            % ("\n".join("  %-18s: %s" % (n, v) for n, v in stale), src))

    pdf = opt("--pdf") or os.path.splitext(src)[0] + ".pdf"
    if not dry_run:
        if (not os.path.isfile(pdf)) or os.path.getmtime(pdf) < os.path.getmtime(src):
            print("rendering PDF (missing or older than the markdown) ...")
            subprocess.check_call([sys.executable,
                                   os.path.join(HERE, "md2pdf.py"), src, pdf])

        if "--skip-cite-check" not in args:
            print("verifying citations ...")
            rc = subprocess.call([sys.executable, os.path.join(HERE, "cite_check.py"),
                                  src, "--delay", "3.0", "--timeout", "45"])
            if rc != 0:
                die("cite_check failed (exit %d). Fix the citations, or pass "
                    "--skip-cite-check to override." % rc)

    creator = {"name": surname_first(author)}
    if orcid:
        creator["orcid"] = orcid
    if affiliation:
        creator["affiliation"] = affiliation

    description = "<p>%s</p>" % html.escape(abstract)
    if disclosure:
        description += "<p><em>%s</em></p>" % html.escape(disclosure)

    # Copyright line, prepended so it is the first thing on the record. Built by
    # md2pdf.copyright_text, which is the same call the renderer makes, so the
    # sentence in the frozen PDF and the sentence on the record are identical.
    copyright_line = copyright_text(holder, pub_date or time.strftime("%Y"), fm_lic)
    if "Licensed under" in description:
        print("note: a copyright line is already present; not adding a second")
        copyright_line = ""
    else:
        description = ("<p><strong>%s</strong></p>%s"
                       % (html.escape(copyright_line), description))

    meta = {"metadata": {
        "upload_type": upload_type,
        "title": title,
        "description": description,
        "creators": [creator],
        "access_right": access_right,
        "license": lic,
        "keywords": keywords,
        "publication_date": pub_date or None,
        "version": version or None,
    }}
    if upload_type == "publication":
        meta["metadata"]["publication_type"] = pub_type
    if access_right == "embargoed":
        meta["metadata"]["embargo_date"] = embargo_date
    meta["metadata"].update(journal)
    meta["metadata"] = {k: v for k, v in meta["metadata"].items() if v}

    def record_block():
        """Print EVERY field that will land on the permanent record.

        Read this before typing PUBLISH. Anything wrong here is wrong forever
        for the file, and needs an edit cycle for the metadata.
        """
        warnings = []
        show("host", BASE)
        show("source", src)
        show("title", title)
        show("author", creator["name"])
        show("orcid", creator.get("orcid") or "(none)")
        show("affiliation", creator.get("affiliation") or "(none)")
        show("upload_type", upload_type,
             "  (DEFAULT - not declared)" if upload_type_defaulted else "")
        if upload_type != "publication":
            warnings.append("upload_type is '%s', so Zenodo ignores publication_type\n"
                            "           and the journal_* fields entirely." % upload_type)
        if pub_type_from_std:
            show("publication_type",
                 "%s  (%s)  [paper_defaults.py]" % (pub_type, TYPE_LABEL[pub_type]))
        elif pub_type_defaulted:
            show("publication_type",
                 "%s (DEFAULT - not declared in front matter)" % pub_type)
            warnings.append("publication_type was not declared in the front matter of\n"
                            "           %s\n"
                            "           so it defaulted to 'preprint' (%s).\n"
                            "           If this paper is anything else, set publication_type:\n"
                            "           in the front matter or pass --pub-type."
                            % (src, TYPE_LABEL["preprint"]))
        else:
            show("publication_type", "%s  (%s)" % (pub_type, TYPE_LABEL[pub_type]))
        for key, flag in JOURNAL_FIELDS:
            show(key, journal.get(key) or "(none)")
        if pub_type == "article" and not journal.get("journal_title"):
            warnings.append("publication_type is 'article' but journal_title is empty.\n"
                            "           Indexers will read that as an incomplete journal\n"
                            "           reference. Set journal_title, or use a type that\n"
                            "           does not claim a venue.")
        if journal and pub_type != "article":
            warnings.append("journal_* fields are set but publication_type is '%s'.\n"
                            "           Zenodo only surfaces those fields for 'article'."
                            % pub_type)
        show("access_right", access_right,
             "  (DEFAULT - not declared)" if access_right_defaulted else "")
        if access_right == "embargoed":
            show("embargo_date", embargo_date)
        show("license", "%s   [front matter: %s]" % (lic, fm_lic))
        if not lic_recognised:
            warnings.append("license %r is not one this script recognises, so it is\n"
                            "           being sent to Zenodo verbatim as %r rather than\n"
                            "           being replaced by a different licence. If Zenodo\n"
                            "           does not know that id it will reject the metadata."
                            % (fm_lic, lic))
        show("copyright holder", holder,
             "  (DEFAULT - taken from author)" if holder_defaulted else "")
        show("copyright line", copyright_line or "(already in the description)")
        show("version", version or "(none)")
        show("publication_date", pub_date or "(none - Zenodo will use today)")
        show("keywords", ", ".join(keywords) if keywords else "(none)")
        # The description is permanent public text, so show what it actually is.
        show("abstract", "%d chars, starts: %s"
             % (len(abstract), (abstract[:70] + "...") if abstract else "(none)"))
        if disclosure:
            show("ai_assistance", "%d chars, starts: %s"
                 % (len(disclosure), disclosure[:70] + "..."))
        else:
            show("ai_assistance", "(none - no AI-assistance disclosure on the record)")
            warnings.append("there is no ai_assistance disclosure. If an assistant was\n"
                            "           used, say so: the record cannot be un-published.")
        if os.path.isfile(pdf):
            # A dry run does not re-render, so the file on disk may not be the
            # file a real run would upload. Say so rather than quoting a size
            # that would change.
            stale = (dry_run
                     and os.path.getmtime(pdf) < os.path.getmtime(src))
            show("file", "%s  (%.2f MB, %d bytes)%s"
                 % (os.path.basename(pdf), os.path.getsize(pdf) / 1e6,
                    os.path.getsize(pdf),
                    "  STALE - older than the markdown, a real run re-renders it"
                    if stale else ""))
        else:
            show("file", "%s  (not rendered yet)" % os.path.basename(pdf))
        # Publishing writes doi/record_url back into the paper. Finding them
        # already there means this paper has been published once.
        if prior_doi or prior_url:
            show("EXISTING doi", prior_doi or "(none)")
            show("EXISTING record", prior_url or "(none)")
            warnings.append("this paper's front matter already names a published record:\n"
                            "             %s\n"
                            "             %s\n"
                            "           Publishing here mints a SECOND, unrelated DOI. It does\n"
                            "           NOT supersede that record, and the two will compete in\n"
                            "           search results forever. For a corrected edition make a\n"
                            "           new version of the existing record instead:\n"
                            "             python zenodo_version.py --new <record id> <pdf> --version 2.0"
                            % (prior_doi or "(no doi)", prior_url or "(no record_url)"))
        for w in warnings:
            print("\n  WARNING: %s" % w)

    print("")
    print("-" * 68)
    print("RECORD FIELDS")
    print("-" * 68)
    record_block()
    print("-" * 68)

    if dry_run:
        print("")
        print("DRY RUN. Nothing was rendered, no citations were checked, no")
        print("network request was made, and no draft exists. Re-run without")
        print("--dry-run to stage a draft on %s." % BASE)
        return

    s = requests.Session()
    s.headers.update({"Authorization": "Bearer %s" % token})

    # Refuse to upload a paper that is already on the account.
    #
    # This exists because it happened: the same paper was published twice, 33
    # seconds apart, producing two permanent DOIs that cannot be withdrawn. A
    # guard against duplicate DRAFTS would not have caught it - the first copy
    # was already published by the time the second was created. So the check
    # is against every deposition, published or not.
    #
    # --force-duplicate overrides, for the rare case of a genuinely separate
    # work with a colliding title. Use zenodo_version.py for a revision.
    if "--force-duplicate" not in args:
        norm = " ".join(title.lower().split())

        # The listing has to run to completion for the comparison below to mean
        # anything. It used to `break` on a bad response and carry on against
        # whatever had been collected - so on 2026-09-09, when Zenodo returned
        # 504 site-wide, the guard compared this title against an empty list and
        # passed. A gate that could not run is a gate that failed, not one that
        # passed, and this particular gate is the only thing standing between a
        # retry and a second permanent DOI.
        complete, reason = False, ""
        existing, page = [], 1
        try:
            while page <= 20:
                rr = s.get("%s/api/deposit/depositions" % BASE,
                           params={"size": 100, "page": page}, timeout=45)
                if not rr.ok:
                    reason = "HTTP %s from the deposition listing" % rr.status_code
                    break
                batch = rr.json()
                if not isinstance(batch, list):
                    reason = "unexpected payload from the deposition listing"
                    break
                if not batch:
                    complete = True
                    break
                existing.extend(batch)
                if len(batch) < 100:
                    complete = True
                    break
                page += 1
            else:
                reason = "page cap reached with more depositions still to read"
        except Exception as e:
            reason = "%s: %s" % (type(e).__name__, e)

        if not complete:
            print("\nREFUSING TO UPLOAD - the duplicate-title check could not run.")
            print("  %s" % reason)
            print()
            print("  Nothing was uploaded. This is not a failure of the paper: the")
            print("  check that would catch a second permanent DOI for it could not")
            print("  reach Zenodo, and an unverified gate is treated as a failed one.")
            print()
            print("  Wait for Zenodo to recover and re-run this command. If it is")
            print("  urgent, confirm by hand at %s/me/uploads that no" % BASE)
            print("  deposition carries this title, then:")
            print("    ... --force-duplicate")
            sys.exit(4)

        clash = [d for d in existing
                 if " ".join((d.get("title") or "").lower().split()) == norm]
        if clash:
            print("\nREFUSING TO UPLOAD - this title is already on your account:")
            for d in clash:
                state = d.get("state")
                print("  [%s] %s%s" % (d.get("id"), state,
                                       "  doi:" + d["doi"] if d.get("doi") else ""))
                print("        %s" % (d.get("title") or "")[:64])
            pub = [d for d in clash if d.get("state") == "done"]
            print()
            if pub:
                print("  Publishing again would mint ANOTHER permanent DOI for the")
                print("  same paper. Zenodo records cannot be withdrawn.")
                print("  For a revision:  python zenodo_version.py --new %s <pdf>"
                      % pub[0].get("id"))
            else:
                print("  An unsubmitted draft already exists. Finish or delete it:")
                print("    python delete_draft.py %s" % clash[0].get("id"))
            print("\n  Override only if this is a genuinely different work:")
            print("    ... --force-duplicate")
            sys.exit(3)

    r = s.post("%s/api/deposit/depositions" % BASE, json={})
    if not r.ok:
        die("could not create deposition: %s %s" % (r.status_code, r.text[:300]))
    dep = r.json()
    dep_id, bucket = dep["id"], dep["links"]["bucket"]
    print("\ndeposition %s created" % dep_id)

    with open(pdf, "rb") as fh:
        r = s.put("%s/%s" % (bucket, os.path.basename(pdf)), data=fh)
    if not r.ok:
        die("upload failed: %s %s" % (r.status_code, r.text[:300]))
    print("uploaded %s bytes, checksum %s" % (r.json()["size"], r.json()["checksum"]))

    r = s.put("%s/api/deposit/depositions/%s" % (BASE, dep_id), json=meta)
    if not r.ok:
        die("metadata rejected: %s %s" % (r.status_code, r.text[:400]))
    print("metadata attached")

    if "--publish" not in args:
        print("")
        print("-" * 68)
        print("DRAFT staged. NOT published. No DOI has been minted.")
        print("  review : %s/deposit/%s" % (BASE, dep_id))
        print("  discard: python delete_draft.py %s" % dep_id)
        print("")
        print("To publish, re-run the same command with --publish, or use the")
        print("Publish button on that page. Publishing is IRREVERSIBLE.")
        print("-" * 68)
        return

    print("")
    print("=" * 68)
    print("ABOUT TO PUBLISH ON %s" % BASE)
    print("=" * 68)
    print("  Everything below becomes a permanent public record. Read it.")
    print("")
    record_block()
    show("draft", "%s/deposit/%s" % (BASE, dep_id))
    print("")
    print("  This mints a PERMANENT DataCite DOI.")
    print("  The record can never be deleted.")
    print("  The file can never be changed - only superseded by a new version.")
    print("  Metadata can still be edited after publication.")
    print("")
    if "--yes" in args:
        # Unattended publish. Everything that can be checked mechanically has
        # already been checked to get here: citations resolve and match their
        # titles, no placeholder text survives, no deposition on this account
        # carries this title, and every permanent field is printed above.
        # What is NOT checked is whether the paper is worth publishing - no
        # gate can do that.
        print("  --yes given: publishing without asking.")
    else:
        answer = input("  type PUBLISH to proceed (anything else aborts): ")
        if answer.strip() != "PUBLISH":
            print("")
            print("aborted. The draft is intact at %s/deposit/%s" % (BASE, dep_id))
            print("discard it with: python delete_draft.py %s" % dep_id)
            return

    r = s.post("%s/api/deposit/depositions/%s/actions/publish" % (BASE, dep_id))
    if not r.ok:
        die("publish failed: %s %s -- the draft is intact at %s/deposit/%s"
            % (r.status_code, r.text[:400], BASE, dep_id))
    rec = r.json()
    doi = rec.get("doi") or ""
    rec_url = rec["links"].get("record_html", "%s/records/%s" % (BASE, dep_id))
    print("")
    print("-" * 68)
    print("PUBLISHED.")
    print("  DOI    : %s" % doi)
    print("  record : %s" % rec_url)

    # Record where the paper went, in the paper itself, so the source file is
    # self-describing and a future v2 render can print its own DOI.
    try:
        ok = write_front_matter(src, [("doi", doi), ("record_url", rec_url)])
    except Exception as exc:                    # never lose the DOI to an IO error
        ok = False
        print("  note   : could not write the DOI back into %s (%s)" % (src, exc))
    if ok:
        print("  wrote  : doi and record_url into the front matter of %s" % src)
    print("-" * 68)


if __name__ == "__main__":
    main()
