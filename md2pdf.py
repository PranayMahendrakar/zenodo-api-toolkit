r"""Render a Markdown paper to PDF using PyMuPDF's Story engine (no pandoc/LaTeX).

    python md2pdf.py drafts/self-verification-gap.md drafts/paper.pdf

Strips YAML front matter, renders headings/paragraphs/lists/blockquotes/code,
and lays out A4 pages with a running footer.

Provenance is driven entirely by front matter -- nothing here is hardcoded.
Title block (each line only when its key is present):
    affiliation
    orcid
    journal_title (+ journal_volume/journal_issue/journal_pages), else
        publication_type rendered as a human label
End-of-document footer block:
    copyright (falling back to author) + year from date + license name
    ai_assistance disclosure, small italic
    doi, when the paper already has one (normally only on a v2 render)

A PDF published to Zenodo can never be replaced, so anything that still looks
like paper_scaffold.py placeholder text is reported on stderr as a WARNING
before it is baked in. Placeholders in the optional identity fields (orcid,
affiliation) are dropped; placeholders in copyright / title / author /
ai_assistance are rendered as written -- silently deleting a disclosure would
be worse than printing a rough one -- but they are always announced.
"""
import os
import re
import sys
import html as _html

import fitz
import markdown as md


def warn(msg):
    """Announce something that is about to become permanent. stderr, ASCII."""
    sys.stderr.write("WARNING: %s\n" % msg)


def split_front_matter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end].strip(), text[end + 4:].lstrip("\n")
    return "", text


# A block scalar header ("key: >") carries no value of its own.
BLOCK_HEADERS = (">", ">-", ">+", "|", "|-", "|+")


def yaml_scalar(raw):
    """The value of a one-line YAML scalar, as YAML itself would read it.

    This exists because a naive .strip() gets two things wrong on the front
    matter paper_scaffold.py emits:

      * a quoted value must keep everything inside the quotes and drop whatever
        follows the closing quote, so a title containing a hash survives;
      * an unquoted value ends at a trailing comment, so the scaffold line
        "publication_type: workingpaper   # conservative default for genre"
        reads as workingpaper and not as the comment as well.

    A bare block-scalar header reads as empty; fm_block picks up its body.
    """
    s = raw.strip()
    if not s:
        return ""
    if s[0] in ('"', "'"):
        quote = s[0]
        i = 1
        out = []
        while i < len(s):
            c = s[i]
            if quote == '"' and c == "\\" and i + 1 < len(s):
                out.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                break
            out.append(c)
            i += 1
        return "".join(out).strip()
    s = re.split(r"(?:^|\s)#", s, maxsplit=1)[0].strip()
    return "" if s in BLOCK_HEADERS else s


def fm_get(fm, key):
    """A top-level front-matter scalar, comment- and quote-aware."""
    m = re.search(r"^%s:[ \t]*(.*)$" % re.escape(key), fm, re.M)
    return yaml_scalar(m.group(1)) if m else ""


def fm_block(fm, key):
    """A folded/literal block scalar joined into one line.

    Accepts every YAML block header, which matters because paper_scaffold.py
    writes the ai_assistance disclosure as a folded block with a strip chomp
    indicator. Falls back to the plain scalar when the key is on one line.
    """
    m = re.search(r"^%s:[ \t]*[>|][-+]?[ \t]*\n((?:[ \t]+\S.*\n?)+)" % re.escape(key),
                  fm, re.M)
    if m:
        return " ".join(l.strip() for l in m.group(1).splitlines() if l.strip())
    return fm_get(fm, key)


# Text paper_scaffold.py writes into a fresh draft. None of it is a decision.
PLACEHOLDER_EXACT = frozenset([
    "lastname, firstname", "todo", "tbd", "institution", "<name>",
    "journal name here", "journal title here", "0000-0000-0000-0000",
])


def is_placeholder(val):
    """True when the value is still scaffold text rather than a choice."""
    low = val.strip().lower()
    if not low:
        return False
    if low in PLACEHOLDER_EXACT:
        return True
    if low.startswith("edit") or "edit:" in low or "edit this" in low:
        return True
    # An ORCID of all zeros and dashes is the scaffold's, not a person's.
    return len(low) > 1 and set(low) <= set("0-")


def fm_meta(fm, key):
    """Optional identity field: top level, or indented under an authors list.

    Scaffold placeholders are dropped, so an unedited skeleton renders no ORCID
    line rather than an ORCID of all zeros.
    """
    m = re.search(r"^[ \t]*%s:[ \t]*(.*)$" % re.escape(key), fm, re.M)
    if not m:
        return ""
    val = yaml_scalar(m.group(1))
    return "" if is_placeholder(val) else val


# Zenodo publication_type values, as a reader-facing label.
PUB_TYPE_LABELS = {
    "article": "Article",
    "report": "Report",
    "workingpaper": "Working paper",
    "preprint": "Preprint",
    "technicalnote": "Technical note",
    "conferencepaper": "Conference paper",
    "thesis": "Thesis",
    "book": "Book",
    "section": "Book section",
    "patent": "Patent",
    "deliverable": "Project deliverable",
    "milestone": "Project milestone",
    "proposal": "Proposal",
    "softwaredocumentation": "Software documentation",
    "taxonomictreatment": "Taxonomic treatment",
    "datamanagementplan": "Data management plan",
    "annotationcollection": "Annotation collection",
    "other": "Other",
}

LICENSE_NAMES = {
    "cc-by-4.0": "CC BY 4.0",
    "cc-by-sa-4.0": "CC BY-SA 4.0",
    "cc-by-nc-4.0": "CC BY-NC 4.0",
    "cc-by-nd-4.0": "CC BY-ND 4.0",
    "cc-by-nc-sa-4.0": "CC BY-NC-SA 4.0",
    "cc-by-nc-nd-4.0": "CC BY-NC-ND 4.0",
    "cc0-1.0": "CC0 1.0",
    "cc-zero": "CC0 1.0",
    "mit": "the MIT License",
    "apache-2.0": "the Apache License 2.0",
}


def venue_line(fm):
    """The publication venue, or an empty string when front matter names none."""
    journal = fm_get(fm, "journal_title")
    if journal and not is_placeholder(journal):
        bits = [journal]
        for key, label in (("journal_volume", "vol. "),
                           ("journal_issue", "no. "),
                           ("journal_pages", "pp. ")):
            val = fm_get(fm, key)
            if val and not is_placeholder(val):
                bits.append(label + val)
        return ", ".join(bits)
    ptype = fm_get(fm, "publication_type").lower()
    if not ptype:
        return ""
    if ptype not in PUB_TYPE_LABELS:
        warn("publication_type %r is not one of Zenodo's values; printing it "
             "as-is on a page that can never be replaced." % ptype)
        return ptype.capitalize()
    return PUB_TYPE_LABELS[ptype]


def license_name(license_id):
    """Reader-facing name for a licence id. Unknown ids are never replaced."""
    if not license_id:
        return ""
    name = LICENSE_NAMES.get(license_id.lower())
    if name is None:
        warn("license %r is not a name this renderer knows; printing it "
             "verbatim in the copyright line." % license_id)
        return license_id
    return name


def copyright_text(holder, date, license_id):
    """The one copyright sentence, shared by the PDF and the Zenodo record.

    publish_paper.py calls this too, so the line in the frozen PDF and the line
    at the top of the record description are produced by the same code from the
    same front matter and cannot drift apart.
    """
    if not holder:
        return ""
    m = re.search(r"(\d{4})", date or "")
    line = "(c) %s%s." % (m.group(1) + " " if m else "", holder.rstrip("."))
    if license_id:
        line += " Licensed under %s." % license_name(license_id)
    return line


def build_footer(fm, author):
    """Copyright / disclosure / DOI block for the very end of the document."""
    out = []

    holder = fm_get(fm, "copyright") or author
    if holder:
        if is_placeholder(holder):
            warn("the copyright holder is still scaffold text (%r). It is being "
                 "written into the PDF, and a published PDF can never be "
                 "replaced." % holder)
        if not fm_get(fm, "license"):
            warn("no license key in the front matter, so the copyright line "
                 "states no licence terms.")
        line = copyright_text(holder, fm_get(fm, "date"), fm_get(fm, "license"))
        out.append('<p class="copyright">%s</p>' % _html.escape(line))
    else:
        warn("no copyright holder: the front matter has neither a copyright key "
             "nor an author, so the PDF gets no copyright line at all.")

    disclosure = fm_block(fm, "ai_assistance")
    if disclosure:
        if is_placeholder(disclosure):
            warn("the ai_assistance disclosure is still scaffold text; it is "
                 "being written into the PDF exactly as written.")
        out.append('<p class="disclosure">%s</p>' % _html.escape(disclosure))

    doi = fm_get(fm, "doi")
    if doi:
        out.append('<p class="doi">DOI: %s</p>' % _html.escape(doi))

    if not out:
        return ""
    return '<div class="footer">%s</div>' % "".join(out)


CSS = """
body { font-family: serif; font-size: 10pt; line-height: 1.45; }
h1 { font-size: 17pt; margin-top: 0; margin-bottom: 4pt; }
h2 { font-size: 12.5pt; margin-top: 13pt; margin-bottom: 4pt; }
h3 { font-size: 11pt; margin-top: 10pt; margin-bottom: 3pt; }
p  { margin-top: 0; margin-bottom: 6pt; text-align: justify; }
li { margin-bottom: 3pt; }
blockquote { margin-left: 14pt; margin-right: 10pt; font-style: italic; }
code { font-family: monospace; font-size: 9pt; }
pre  { font-family: monospace; font-size: 8.5pt; }
.meta { font-size: 9.5pt; margin-bottom: 2pt; }
.venue { font-size: 9.5pt; font-style: italic; margin-bottom: 2pt; }
.refs p { font-size: 8.8pt; margin-bottom: 3.5pt; text-align: left; }
.footer { margin-top: 18pt; }
.footer p { font-size: 8.5pt; margin-bottom: 3pt; text-align: left; }
.footer p.disclosure { font-size: 8pt; font-style: italic; }
"""


def build_html(src):
    fm, body = split_front_matter(src)
    title = fm_get(fm, "title") or "Untitled"
    author = fm_get(fm, "author")
    date = fm_get(fm, "date")

    for key, val in (("title", title), ("author", author)):
        if val and is_placeholder(val):
            warn("%s is still scaffold text (%r) and is going into the PDF."
                 % (key, val))

    # Split references so they can be typeset smaller.
    parts = re.split(r"(?m)^##\s+References\s*$", body, maxsplit=1)
    main = parts[0]
    refs = parts[1] if len(parts) > 1 else ""

    # Drop a duplicate H1 in the body; the header block supplies it. If the
    # author line follows it as a bare paragraph, drop that too.
    main = re.sub(r"(?m)^#\s+.*$", "", main, count=1)
    if author:
        main = re.sub(r"(?m)^\**%s\**\s*$" % re.escape(author), "", main, count=1)

    conv = md.Markdown(extensions=["extra", "sane_lists"])
    main_html = conv.convert(main)
    conv.reset()
    refs_html = conv.convert(refs) if refs.strip() else ""

    head = "<h1>%s</h1>" % _html.escape(title)
    if author:
        head += '<p class="meta"><b>%s</b></p>' % _html.escape(author)

    affiliation = fm_meta(fm, "affiliation")
    if affiliation:
        head += '<p class="meta">%s</p>' % _html.escape(affiliation)

    orcid = fm_meta(fm, "orcid")
    if orcid:
        head += '<p class="meta">ORCID: %s</p>' % _html.escape(orcid)

    venue = venue_line(fm)
    if venue:
        head += '<p class="venue">%s</p>' % _html.escape(venue)

    if date:
        head += '<p class="meta">%s</p>' % _html.escape(date)

    if refs_html:
        refs_html = '<h2>References</h2><div class="refs">%s</div>' % refs_html

    return "<html><head><style>%s</style></head><body>%s%s%s%s</body></html>" % (
        CSS, head, main_html, refs_html, build_footer(fm, author))


def render(html_str, out_path, asset_dir=None):
    page_w, page_h = fitz.paper_size("a4")
    margin = 56
    where = fitz.Rect(margin, margin, page_w - margin, page_h - margin - 18)

    # Without an Archive, Story cannot resolve a relative <img src> and drops
    # the tag silently - a figure cited in the text and missing from the PDF.
    # Rooting one at the paper's own directory makes ![caption](fig.png) work.
    archive = None
    if asset_dir and os.path.isdir(asset_dir):
        try:
            archive = fitz.Archive(asset_dir)
        except Exception as exc:
            sys.stderr.write("WARNING: figures not embedded (%s)\n" % exc)
    story = (fitz.Story(html=html_str, user_css=None, archive=archive)
             if archive is not None
             else fitz.Story(html=html_str, user_css=None))
    writer = fitz.DocumentWriter(out_path)
    n = 0
    more = 1
    while more:
        n += 1
        dev = writer.begin_page(fitz.Rect(0, 0, page_w, page_h))
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        if n > 400:
            raise RuntimeError("runaway pagination")
    writer.close()

    # Stamp page numbers.
    doc = fitz.open(out_path)
    for i, page in enumerate(doc, start=1):
        page.insert_text((page_w / 2 - 12, page_h - 34),
                         "%d" % i, fontname="helv", fontsize=8.5)
    doc.saveIncr()
    doc.close()
    return n


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    src_path, out_path = sys.argv[1], sys.argv[2]
    src = open(src_path, encoding="utf-8").read()
    pages = render(build_html(src), out_path,
                   asset_dir=os.path.dirname(os.path.abspath(src_path)))
    size = os.path.getsize(out_path)
    print("wrote %s  (%d pages, %.2f MB)" % (out_path, pages, size / 1e6))


if __name__ == "__main__":
    main()
