r"""Render a Markdown paper to PDF as a standard two-column journal article,
using PyMuPDF's Story engine (no pandoc/LaTeX).

    python md2pdf.py drafts/self-verification-gap.md drafts/paper.pdf
    python md2pdf.py drafts/paper.md drafts/paper.pdf --doi 10.5281/zenodo.123

Page 1 carries a full-width title block - venue and date, title, author,
affiliation and ORCID, then the abstract and keywords between rules - with the
body in two justified columns below. Tables too wide for a column, code blocks
with lines longer than a column holds, and figures drawn wider than five
inches span both columns at the top of the next page, captions attached
(table* / figure* placement). The last page's columns are balanced. Every page
gets a number; from page 2 a running head carries the short title and the
journal. The PDF gets title/author/keyword metadata and a bookmark outline.

Captions: a figure's caption is its alt text, ![Figure 1. ...](fig.png). A
table's or algorithm's is a paragraph directly above or below it that begins
"Table N." / "Algorithm N." - it travels with the block.

Provenance is driven entirely by front matter -- nothing here is hardcoded.
Title block (each line only when its key is present):
    journal_title (+ journal_volume/journal_issue/journal_pages), else
        publication_type rendered as a human label; date; version if not 1.0;
        doi when the paper already has one
    affiliation, orcid, keywords
End-of-document block:
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
import collections
import io
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


# --------------------------------------------------------------------------
# Layout: a standard two-column journal page.
#
#   page 1   full-width title block - venue line, title, author, affiliation,
#            ORCID, then the abstract and keywords between two rules - and the
#            body in two columns below it;
#   body     two justified columns; a table too wide for one column, a code
#            block whose lines do not fit one, and every figure drawn wider
#            than five inches span both columns at the top of the next page
#            (the table* / figure* convention), with their captions;
#   last     the final page's columns are balanced;
#   every    page number at the foot; from page 2 a running head with the
#            short title and the journal.
#
# One engine, PyMuPDF's Story, so the layout is identical on the author's
# Windows machine and on the Linux CI runner, with the PDF base-14 fonts and
# no TeX installation.
# --------------------------------------------------------------------------

A4 = fitz.paper_rect("a4")
SIDE = 50                 # left and right margin
TOP = 62                  # leaves room for the running head above it
BOTTOM = 56               # leaves room for the page number below it
GUTTER = 16               # between the columns
BODY = fitz.Rect(SIDE, TOP, A4.width - SIDE, A4.height - BOTTOM)
COLW = (BODY.width - GUTTER) / 2
BLOCK_GAP = 12            # between a spanning block and what follows it
MIN_COLUMN = 40           # a sliver of column shorter than this is left empty
WIDE_FIGURE_IN = 5.0      # figures drawn wider than this span both columns
CODE_COLUMN_CHARS = 56    # longest code line that fits one column at 7.1pt
CODE_FLOAT_LINES = 12     # uncaptioned code longer than this may float
TABLE_CHAR_PT = 3.7       # mean width of one character of 7.4pt table text
TABLE_CELL_CHARS = 28     # a cell wider than this wraps rather than widens
KEEP_WITH_NEXT = 26       # a heading needs this much of its text below it
ORPHAN = 15               # a paragraph fragment this short is one line
MAX_PAGES = 400
HEAD_MAX = 0.6            # largest share of page 1 the title block may take
BALANCE_STEPS = 60        # 4pt steps tried when balancing the last page
RUNT = 0.25               # a last page filled less than this is squeezed away

# Line height and paragraph gap. "tight" is tried only when the normal setting
# leaves a last page holding a few lines - a runt page.
SPACING = {"normal": ("1.32", "5"), "tight": ("1.27", "4")}
_spacing = SPACING["normal"]

CSS_TEMPLATE = """
body { font-family: serif; font-size: 9.4pt; line-height: @LH@; margin: 0; }
p  { margin-top: 0; margin-bottom: @PM@pt; text-align: justify; }
h1 { font-size: 16pt; line-height: 1.2; text-align: center; margin: 0 0 7pt 0; }
h2 { font-size: 10.6pt; margin-top: 11pt; margin-bottom: 4pt; }
h3 { font-size: 9.6pt; font-style: italic; margin-top: 8pt; margin-bottom: 3pt; }
h4 { font-size: 9.4pt; font-style: italic; font-weight: normal;
     margin-top: 6pt; margin-bottom: 2pt; }
ul, ol { margin-top: 0; margin-bottom: @PM@pt; }
li { margin-bottom: 2pt; text-align: justify; }
blockquote { margin: 3pt 8pt 6pt 8pt; font-style: italic; }
code { font-family: monospace; font-size: 8.2pt; }
pre { font-family: monospace; font-size: 7.1pt; line-height: 1.22;
      white-space: pre-wrap; margin: 2pt 0 8pt 0; padding: 4pt;
      border: 0.5pt solid #444; }
pre code { font-size: 7.1pt; }
table { border-collapse: collapse; width: 100%; font-size: 7.4pt;
        line-height: 1.22; margin: 0 0 6pt 0;
        border-top: 1pt solid #000; border-bottom: 1pt solid #000; }
th { text-align: left; vertical-align: bottom; padding: 2pt 3pt;
     border-bottom: 0.6pt solid #000; }
td { text-align: left; vertical-align: top; padding: 1.5pt 3pt; }
img { width: 100%; }
sup { font-size: 75%; }
.venue { font-size: 8pt; font-style: italic; text-align: center;
         margin-bottom: 9pt; }
.author { font-size: 11pt; text-align: center; margin-bottom: 2pt; }
.affil { font-size: 8.5pt; text-align: center; margin-bottom: 9pt; }
.rule { border-top: 0.8pt solid #000; margin: 0 0 6pt 0; }
.abstract p { font-size: 9pt; line-height: 1.3; margin-bottom: 4pt; }
.keywords { font-size: 8.5pt; margin: 2pt 0 7pt 0; text-align: left; }
.caption { font-size: 8pt; line-height: 1.25; text-align: justify;
           margin: 6pt 0 0 0; }
.tcaption { font-size: 8pt; line-height: 1.25; text-align: justify;
            margin: 0 0 4pt 0; }
.figure { margin: 0 0 8pt 0; }
.refs p { font-size: 7.8pt; line-height: 1.25; margin-bottom: 3pt;
          text-align: left; padding-left: 10pt; text-indent: -10pt; }
.endrule { border-top: 0.5pt solid #000; margin: 0 0 4pt 0; }
p.end { font-size: 7.6pt; line-height: 1.25; margin-bottom: 3pt; text-align: left; }
p.disclosure { font-style: italic; }
"""


def css():
    return CSS_TEMPLATE.replace("@LH@", _spacing[0]).replace("@PM@", _spacing[1])


MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


def long_date(value):
    """2026-09-26 -> 26 September 2026; anything else unchanged."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return value or ""
    return "%d %s %s" % (int(m.group(3)), MONTHS[int(m.group(2)) - 1], m.group(1))


def display_name(author):
    """'Mahendrakar, Pranay' -> 'Pranay Mahendrakar'. Other forms unchanged."""
    parts = [p.strip() for p in (author or "").split(",")]
    if len(parts) == 2 and all(parts):
        return "%s %s" % (parts[1], parts[0])
    return author or ""


def fm_list(fm, key):
    """A front-matter list, block (`- item` lines) or flow (`[a, b]`) style."""
    m = re.search(r"^%s:[ \t]*\[(.*)\][ \t]*$" % re.escape(key), fm, re.M)
    if m:
        return [yaml_scalar(x) for x in m.group(1).split(",") if yaml_scalar(x)]
    m = re.search(r"^%s:[ \t]*\n((?:[ \t]+-[^\n]*\n?)+)" % re.escape(key), fm, re.M)
    if m:
        return [yaml_scalar(l.strip()[1:]) for l in m.group(1).splitlines()
                if l.strip().startswith("-") and yaml_scalar(l.strip()[1:])]
    one = fm_get(fm, key)
    return [one] if one else []


def short_title(title, limit=90):
    """The running-head title: the part before a colon or a question mark
    (which it keeps), cut at a word if still too long."""
    title = title.strip()
    m = re.search(r"[:?]", title)
    head = title if not m else (title[:m.start()] + ("?" if m.group(0) == "?" else ""))
    head = head.strip()
    if len(head) <= limit:
        return head
    cut = head[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;") + " ..."


def png_width_inches(path):
    """Physical width a PNG was drawn at, from its pHYs chunk; None if unknown."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(4096)
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    i = data.find(b"pHYs")
    if i < 0:
        return None
    ppu = int.from_bytes(data[i + 4:i + 8], "big")
    unit = data[i + 12]
    if unit != 1 or not ppu:
        return None
    return width / (ppu * 0.0254)


def split_sections(body):
    """(abstract markdown or '', main markdown, references markdown)."""
    parts = re.split(r"(?m)^##\s+References\s*$", body, maxsplit=1)
    main = parts[0]
    refs = parts[1] if len(parts) > 1 else ""
    abstract = ""
    m = re.search(r"(?ms)^##\s+Abstract\s*\n(.*?)(?=^##\s)", main)
    if m:
        abstract = m.group(1).strip()
        main = main[:m.start()] + main[m.end():]
    return abstract, main, refs


def strip_title_block(main, title, author):
    """Drop the body's own H1 and a bare author line under it: the page's
    title block already carries both."""
    main = re.sub(r"(?m)^#\s+.*$", "", main, count=1)
    names = {n for n in (author, display_name(author)) if n}
    for name in names:
        main = re.sub(r"(?m)^\**%s\**\s*$" % re.escape(name), "", main, count=1)
    return main


# ---- markdown clean-up, before conversion ----------------------------------

FENCE_RE = re.compile(r"(?ms)^(```|~~~).*?^\1[ \t]*$")
SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}")


def _escape_stray_asterisks(par):
    """Backslash every '*' in a paragraph that cannot be emphasis.

    Mathematical stars - k*, pi_{k*,t}, FN*_u - were read as emphasis
    markers: pairs of them vanished and the text between turned italic,
    which printed a published formula wrong. A '*' preceded by a letter can
    only close emphasis; with nothing open it is a literal star.
    """
    out, stack, i, n = list(par), [], 0, len(par)
    escape = set()
    in_code = False
    while i < n:
        c = par[i]
        if c == "`":
            in_code = not in_code
        elif c == "*" and not in_code:
            if i + 1 < n and par[i + 1] == "*":         # strong: leave alone
                i += 2
                continue
            line_start = par.rfind("\n", 0, i) + 1
            if not par[line_start:i].strip() and i + 1 < n and par[i + 1] in " \t":
                i += 1                                   # a list marker
                continue
            prev = par[i - 1] if i else " "
            nxt = par[i + 1] if i + 1 < n else " "
            if prev == "\\":                                # already escaped
                i += 1
                continue
            opener = (prev.isspace() or prev in "([{\"'/-") and not nxt.isspace()
            closer = not prev.isspace()
            if closer and not (opener and not stack):
                if stack:
                    stack.pop()
                else:
                    escape.add(i)
            elif opener:
                stack.append(i)
        i += 1
    escape.update(stack)                                 # never closed
    for i in sorted(escape, reverse=True):
        out.insert(i, "\\")
    return "".join(out)


def _is_indented_code(par):
    """A paragraph whose every line is indented four spaces or a tab is an
    indented code block. Escaping a star in it printed the backslash."""
    lines = [l for l in par.split("\n") if l.strip()]
    return bool(lines) and all(l.startswith(("    ", "\t")) for l in lines)


def prepare_markdown(text):
    """Repairs to the prose before markdown sees it; code is left untouched."""
    parts, last = [], 0
    for m in FENCE_RE.finditer(text):
        parts.append((False, text[last:m.start()]))
        parts.append((True, m.group(0)))
        last = m.end()
    parts.append((False, text[last:]))
    out = []
    for is_code, chunk in parts:
        if is_code:
            out.append(chunk)
            continue
        # "machine-\nchecked" is one word the source happened to wrap; joined
        # with a space it printed as "machine- checked".
        chunk = re.sub(r"(?<=[A-Za-z])-\n(?![ ]{4}|\t)[ \t]*(?=[A-Za-z])", "-", chunk)
        # A table with a second header and separator under the first printed
        # the second as data rows and lost its extra columns.
        lines = chunk.split("\n")
        fixed, i = [], 0
        while i < len(lines):
            if (lines[i].lstrip().startswith("|") and i + 3 < len(lines)
                    and SEPARATOR_RE.match(lines[i + 1]) and SEPARATOR_RE.match(lines[i + 3])
                    and lines[i + 2].lstrip().startswith("|")):
                i += 2
                continue
            fixed.append(lines[i])
            i += 1
        chunk = "\n".join(fixed)
        chunk = "\n\n".join(p if _is_indented_code(p) else _escape_stray_asterisks(p)
                             for p in chunk.split("\n\n"))
        out.append(chunk)
    return "".join(out)


# ---- typography and structure, after conversion -----------------------------

def typeset(html):
    """Standard typography on prose text: spaced '--' as an en dash, '->' as
    an arrow, and numeric exponents (10^-8) as superscripts. Text inside
    <pre> and <code> is never touched."""
    out, depth = [], 0
    for piece in re.split(r"(<[^>]+>)", html):
        if piece.startswith("<"):
            tag = re.match(r"</?(\w+)", piece)
            if tag and tag.group(1).lower() in ("pre", "code"):
                depth += -1 if piece.startswith("</") else 1
            out.append(piece)
            continue
        if depth <= 0:
            piece = re.sub(r"(?<=\s)--(?=\s)", "&#8211;", piece)
            piece = re.sub(r"(?<=\s)-(?:>|&gt;)(?=\s)", "&#8594;", piece)
            piece = re.sub(r"(?:<|&lt;)=(?=\s*[0-9A-Za-z(-])", "&#8804;", piece)
            piece = re.sub(r"(?:>|&gt;)=(?=\s*[0-9A-Za-z(-])", "&#8805;", piece)
            # "n = 1,000" kept on one line
            piece = re.sub(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}) = (?=[0-9-])", r"\1&#160;=&#160;", piece)
            piece = re.sub(r"(?<=[0-9A-Za-z)])\^(-?[0-9]+)\b", r"<sup>\1</sup>", piece)
            # "3 x 10^-8": a multiplication sign, and never broken at the sign
            piece = re.sub(r"(?<=[0-9])\s+x\s+(?=10<sup>)", "&#160;&#215;&#160;", piece)
        out.append(piece)
    return "".join(out)


def number_blocks(html, prefix="e"):
    """Give every block element an id. Story reports the position of elements
    that carry one, which is how a heading or a lone first line stranded at
    the foot of a column is found and moved to the next."""
    counter = iter(range(1, 10 ** 7))
    return re.sub(r"<(p|li|h[2-4]|pre|table|blockquote)(?=[\s>])(?![^>]*\bid=)",
                  lambda m: '<%s id="%s%d"' % (m.group(1), prefix, next(counter)), html)


# A caption names its object and then stops: "Table 1." or "Algorithm 2:".
# "Table 1 collects the values..." is a sentence about the table, and stays
# in the text.
CAPTION_RE = re.compile(r"^\s*(Table|Algorithm|Listing|Figure|Fig\.)\s+(\d+)\s*[.:]", re.I)


def _plain(html):
    return _html.unescape(re.sub(r"<[^>]+>", "", html))


def _table_is_wide(table_html):
    """Does the table need the full page width? Judged by the width it would
    naturally take - short cells, even many of them, fit a column."""
    rows = re.findall(r"(?s)<tr>(.*?)</tr>", table_html)
    widths = []
    for row in rows:
        cells = re.findall(r"(?s)<t[hd][^>]*>(.*?)</t[hd]>", row)
        for j, cell in enumerate(cells):
            longest = max((len(w) for w in _plain(cell).split()), default=0)
            need = max(min(len(_plain(cell)), TABLE_CELL_CHARS), longest)
            if j >= len(widths):
                widths.append(need)
            else:
                widths[j] = max(widths[j], need)
    natural = sum(w * TABLE_CHAR_PT + 6 for w in widths)
    return natural > COLW


def _code_lines(pre_html):
    return _plain(pre_html).splitlines()


def _bold_label(caption_html):
    """'Figure 1. What it shows' -> '<b>Figure 1.</b> What it shows', unless
    the caption is already styled as a whole."""
    if re.match(r"\s*<(strong|b|em|i)>", caption_html):
        return caption_html
    return re.sub(r"^(\s*)((?:Table|Algorithm|Listing|Figure|Fig\.)\s+\d+\s*[.:])",
                  r"\1<b>\2</b>", caption_html, count=1)


def _label(caption_html):
    m = CAPTION_RE.match(_plain(caption_html))
    if not m:
        return None
    kind = "Figure" if m.group(1).lower().startswith("fig") else m.group(1).capitalize()
    return kind, m.group(2)


def extract_spans(html_main, asset_dir):
    """Pull the blocks too wide for a column out of the flow.

    Returns (flow html with an anchor where each block goes, [block html]).
    Captions travel with their block: a "Table 1." / "Algorithm 1."
    paragraph directly above or below it, the first lines of a code block
    that name it, or - for a table titled only by its section heading - a
    caption taken from that heading. A figure's caption is its alt text.
    """
    tokens = re.split(r"(?s)(<table>.*?</table>|<pre>.*?</pre>|<p>\s*<img [^>]*/?>\s*</p>)",
                      html_main)
    out = []
    spans = []
    labels = []

    def caption_from(text, before):
        paras = list(re.finditer(r"(?s)<p>(.*?)</p>", text))
        if not paras:
            return text, ""
        p = paras[-1] if before else paras[0]
        edge = text[p.end():] if before else text[:p.start()]
        if edge.strip() or not CAPTION_RE.match(_plain(p.group(1))) \
                or _plain(p.group(1)).lower().startswith("fig"):
            return text, ""
        return text[:p.start()] + text[p.end():], p.group(1)

    def caption_from_heading(flow_so_far, kind):
        """'10.1 Table 1: measured values...' as the table's only title."""
        heads = list(re.finditer(r"(?s)<h[2-4]>(.*?)</h[2-4]>", flow_so_far))
        if not heads:
            return ""
        h = heads[-1]
        between = flow_so_far[h.end():]
        if "span-" in between or "<table" in between:
            return ""
        m = re.search(r"\b(%s\s+\d+)\s*[:.]\s*(.*)$" % kind, _plain(h.group(1)))
        if not m:
            return ""
        rest = m.group(2).strip()
        return _html.escape("%s. %s" % (m.group(1), rest[:1].upper() + rest[1:]))

    def anchor(k):
        return '<div id="span-%d"></div>' % k

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("<table>"):
            if not _table_is_wide(tok):
                # Stays in the column; a caption written below it still
                # belongs above it.
                _, above = caption_from(out[-1] if out else "", before=True)
                if not above and i + 1 < len(tokens):
                    tokens[i + 1], below = caption_from(tokens[i + 1], before=False)
                    if below:
                        out.append('<p class="tcaption">%s</p>' % _bold_label(below))
                out.append(tok)
                i += 1
                continue
            prev = out.pop() if out else ""
            prev, cap = caption_from(prev, before=True)
            out.append(prev)
            if not cap and i + 1 < len(tokens):
                tokens[i + 1], cap = caption_from(tokens[i + 1], before=False)
            if not cap:
                cap = caption_from_heading("".join(out), "Table")
            out.append(anchor(len(spans)))
            spans.append(('<p class="tcaption">%s</p>' % _bold_label(cap) if cap else "") + tok)
            labels.append(_label(cap) if cap else None)
        elif tok.startswith("<pre>"):
            lines = _code_lines(tok)
            # The block's own opening lines may name it: "Algorithm 1. ...".
            own_cap = ""
            if lines and CAPTION_RE.match(lines[0]):
                # The caption is the first line, plus the lines after it only
                # when a blank line closes them off within the first few - a
                # block with no blank line is caption line then algorithm.
                blank = next((j for j, l in enumerate(lines[:5]) if not l.strip()), None)
                cap_lines = [l.strip() for l in lines[:blank]] if blank else [lines[0].strip()]
                own_cap = _html.escape(" ".join(cap_lines))
            prev = out[-1] if out else ""
            _, prev_cap = caption_from(prev, before=True)
            captioned = bool(own_cap or prev_cap)
            wide = max((len(l) for l in lines), default=0) > CODE_COLUMN_CHARS
            # Floated only when it is a captioned algorithm or a long listing.
            # An equation set as code belongs to its sentence and stays there,
            # wrapping in the column if it must.
            if not wide or not (captioned or len(lines) > CODE_FLOAT_LINES):
                out.append(tok)
                i += 1
                continue
            if prev_cap:
                out.pop()
                prev, cap = caption_from(prev, before=True)
                out.append(prev)
                block = '<p class="tcaption">%s</p>%s' % (_bold_label(cap), tok)
            elif own_cap:
                body = "\n".join(lines[len(cap_lines):]).lstrip("\n")
                block = '<p class="tcaption">%s</p><pre><code>%s</code></pre>' % (
                    _bold_label(own_cap), _html.escape(body))
                cap = own_cap
            else:
                block, cap = tok, ""
            out.append(anchor(len(spans)))
            spans.append(block)
            labels.append(_label(cap) if cap else None)
        elif tok.startswith("<p>") and "<img " in tok:
            src = re.search(r'src="([^"]+)"', tok)
            alt = re.search(r'alt="([^"]*)"', tok)
            caption = alt.group(1) if alt else ""
            src = src.group(1) if src else ""
            block = ('<div class="figure"><img src="%s"/>%s</div>'
                     % (src, '<p class="caption">%s</p>' % _bold_label(caption)
                        if caption else ""))
            inches = png_width_inches(os.path.join(asset_dir or ".", _html.unescape(src)))
            if inches is None or inches > WIDE_FIGURE_IN:
                out.append(anchor(len(spans)))
                spans.append(block)
                labels.append(_label(caption) if caption else None)
            else:
                out.append(block)
        else:
            out.append(tok)
        i += 1
    flow = "".join(out)
    flow = anchor_at_first_citation(flow, labels)
    flow = anchor_via_other_blocks(flow, labels, spans)
    return flow, spans


OTHER_PAPER_RE = re.compile(
    r"(et al\.?|\d{4}[a-z]?|\b(?:their|its|his|her|whose|paper's|source's|"
    r"study's|authors')|'s)\s*[,;]?\s*$", re.I)


def anchor_at_first_citation(flow, labels):
    """Move each spanning block's anchor to the place the text first cites it.

    A block spans the top of the page after its anchor. Anchored where the
    draft happened to put it - often a "numbers in one place" section near
    the end - Figure 1 printed nine pages after the text first sent the
    reader to its panels. The anchor sits inline, at the words "Figure 1",
    so a citation at a column foot is not pushed a page further by the rest
    of its paragraph, and blocks cited in one sentence keep their order.

    Figures: the first citation anywhere. Tables and algorithms: the first
    citation within their own section. These papers cite other papers'
    tables constantly - "Their Table 1", "(Choi et al., 2025, Table 1)",
    "the homogeneous vote in Table 1 scored 94.00" - and no rule on the
    words can tell those from this paper's own; anchored to them, a
    consolidating table printed eleven pages before its section. The
    sentence that introduces a table sits in its section.
    """
    events = []                                  # (position, order, text, skip)
    for k, label in enumerate(labels):
        if not label:
            continue
        kind, num = label
        div = '<div id="span-%d"></div>' % k
        here = flow.find(div)
        if here < 0:
            continue
        start = 0 if kind == "Figure" else max(0, flow.rfind("<h2", 0, here))
        pat = re.compile(r"\b%s\s+%s(?![0-9])" % (
            "(?:Figure|Fig\\.)" if kind == "Figure" else kind, num))
        for m in pat.finditer(flow, start, here):
            if flow.rfind("<", 0, m.start()) > flow.rfind(">", 0, m.start()):
                continue                                # inside a tag
            after = _plain(flow[m.end():m.end() + 30])
            before = _plain(flow[max(0, m.start() - 60):m.start()])
            if re.match(r"\s*(of|in)\s", after) or OTHER_PAPER_RE.search(before):
                continue
            # Just past the label and any panel letter: "Figure 1a".
            end = m.end() + len(re.match(r"[a-z]?", flow[m.end():]).group(0))
            events.append((end, m.start(), '<a id="span-%d"></a>' % k, 0))
            events.append((here, 0, "", len(div)))       # drop the old anchor
            break
    if not events:
        return flow
    out, cur = [], 0
    for pos, _order, text, skip in sorted(events):
        out.append(flow[cur:pos])
        out.append(text)
        cur = pos + skip
    out.append(flow[cur:])
    return "".join(out)


def anchor_via_other_blocks(flow, labels, spans):
    """A block cited only from inside another spanning block - "Fig. 1a" in
    a cell of Table 1 - is anchored right after that block. Left where the
    draft put it, after the conclusion, Figure 1 printed inside the
    reference list, four pages from the only place that sent a reader to it.
    """
    for k, label in enumerate(labels):
        div = '<div id="span-%d"></div>' % k
        if not label or div not in flow:
            continue                         # already anchored at a citation
        kind, num = label
        pat = re.compile(r"\b%s\s+%s(?![0-9])" % (
            "(?:Figure|Fig\\.)" if kind == "Figure" else kind, num))
        for j, block in enumerate(spans):
            if j == k or not pat.search(_plain(block)):
                continue
            host = re.search(r'<(?:a|div) id="span-%d"></(?:a|div)>' % j, flow)
            if host and host.start() < flow.index(div):
                flow = flow.replace(div, "", 1)
                # Inline, like the host's own anchor: a block-level div in
                # the middle of a heading forced a line break there.
                inline = '<a id="span-%d"></a>' % k
                flow = flow[:host.end()] + inline + flow[host.end():]
            break
    return flow


def drop_empty_sections(flow):
    """Remove a heading whose section has nothing left in it - "## Figure"
    around an image that now spans the top of a page. Its heading alone
    printed as an empty section."""
    heads = list(re.finditer(r"<h([2-4])[^>]*>.*?</h\1>", flow))
    for idx in range(len(heads) - 1, -1, -1):
        h = heads[idx]
        rest = flow[h.end():]
        m = re.match(r'((?:\s|<div id="span-\d+"></div>)*)(?:<h([2-4])[^>]*>|$)', rest)
        if m and (m.group(2) is None or int(m.group(2)) <= int(h.group(1))):
            flow = flow[:h.start()] + flow[h.end():]
    return flow


def build_parts(src, asset_dir=None):
    """The page's content as HTML: the title block, the two-column flow (body
    and references), the spanning blocks, the end matter, and metadata."""
    fm, body = split_front_matter(src)
    title = fm_get(fm, "title") or "Untitled"
    author = fm_get(fm, "author")
    date = fm_get(fm, "date")

    for key, val in (("title", title), ("author", author)):
        if val and is_placeholder(val):
            warn("%s is still scaffold text (%r) and is going into the PDF."
                 % (key, val))

    abstract, main, refs = split_sections(prepare_markdown(body))
    main = strip_title_block(main, title, author)

    conv = md.Markdown(extensions=["extra", "sane_lists"])

    def to_html(text):
        conv.reset()
        return conv.convert(text) if text.strip() else ""

    # ---- title block --------------------------------------------------
    venue = venue_line(fm)
    bits = [b for b in (venue, ("Published " + long_date(date)) if date else "") if b]
    version = fm_get(fm, "version")
    if version and version not in ("1", "1.0"):
        bits.append("Version %s" % version)
    doi = fm_get(fm, "doi")
    if doi:
        bits.append("DOI %s" % doi)
    head = ""
    if bits:
        head += '<p class="venue">%s</p>' % " &#183; ".join(_html.escape(b) for b in bits)
    head += "<h1>%s</h1>" % _html.escape(title)
    if author:
        head += '<p class="author">%s</p>' % _html.escape(display_name(author))
    affil = [x for x in (fm_meta(fm, "affiliation"),
                         ("ORCID " + fm_meta(fm, "orcid")) if fm_meta(fm, "orcid") else "")
             if x]
    if affil:
        head += '<p class="affil">%s</p>' % " &#183; ".join(_html.escape(a) for a in affil)
    head += '<div class="rule"></div>'
    if abstract:
        ab = to_html(abstract)
        ab = re.sub(r"^<p>", "<p><b>Abstract.</b> ", ab, count=1)
        head += '<div class="abstract">%s</div>' % ab
    keywords = fm_list(fm, "keywords")
    if keywords:
        head += '<p class="keywords"><b>Keywords:</b> %s</p>' % _html.escape("; ".join(keywords))
    if abstract or keywords:
        head += '<div class="rule"></div>'

    # ---- the flow, the spanning blocks, the end matter -----------------
    flow, spans = extract_spans(to_html(main), asset_dir)
    if refs.strip():
        flow += '<h2>References</h2><div class="refs">%s</div>' % to_html(refs)
    flow = drop_empty_sections(flow)
    # The first heading's space-before would start the left column lower
    # than the right one under the title block.
    flow = re.sub(r"^(\s*)<h([2-4])>", r'\1<h\2 style="margin-top:0">', flow, count=1)
    ending = end_matter(fm, author)
    if ending:
        spans.append(ending)                  # placed after the flow ends

    meta = {"title": title, "author": display_name(author), "venue": fm_get(fm, "journal_title"),
            "keywords": "; ".join(keywords), "short": short_title(title),
            "license": copyright_text(fm_get(fm, "copyright") or author, date,
                                      fm_get(fm, "license")),
            "end_span": len(spans) - 1 if ending else None}
    return (typeset(head), number_blocks(typeset(flow)),
            [typeset(s) for s in spans], meta)


def demote_abstract(head, flow):
    """Move the abstract and keywords from the title block into the flow."""
    m = re.search(r'(?s)<div class="abstract">.*?</div>(?:<p class="keywords">.*?</p>)?'
                  r'(?:<div class="rule"></div>)?', head)
    if not m:
        return head, flow
    moved = m.group(0).replace('<div class="rule"></div>', "")
    return (head[:m.start()] + head[m.end():],
            number_blocks(moved + '<div class="rule"></div>', prefix="a") + flow)


def end_matter(fm, author):
    """build_footer's copyright / disclosure / DOI lines under a rule, set
    across the page after the last column: one block, never split, never
    left as a stray DOI line alone on a page of its own."""
    footer = build_footer(fm, author)
    inner = re.sub(r'^<div class="footer">|</div>$', "", footer)
    if not inner:
        return ""
    inner = inner.replace('<p class="copyright">(c) ', '<p class="copyright">&#169; ')
    inner = inner.replace('<p class="', '<p class="end ')
    return '<div class="endrule"></div>' + inner


def _doc(inner):
    return "<html><head><style>%s</style></head><body>%s</body></html>" % (css(), inner)


def _story(inner, archive):
    return (fitz.Story(html=_doc(inner), archive=archive) if archive is not None
            else fitz.Story(html=_doc(inner)))


def _height(inner, width, archive):
    """Height the HTML occupies at this width."""
    st = _story(inner, archive)
    more, filled = st.place(fitz.Rect(0, 0, width, 50000))
    if more:
        raise RuntimeError("a block is taller than 50000pt")
    # Two points of slack: a rect exactly the measured height can come up a
    # fraction short and drop a table's last row.
    return fitz.Rect(filled).height + 2


def place_column(st, rect):
    """Place the flow into one column, keeping headings with their text.

    Returns (more, rect actually used, element events). When the column
    would end on a heading (or a heading with less than two lines under
    it), or on the lone first line of a paragraph, the column is cut just
    above it so that it starts the next column instead. Nothing is drawn;
    the caller draws.
    """
    def events():
        ev = []
        st.element_positions(lambda p: ev.append(
            (p.id or "", p.heading, fitz.Rect(p.rect))) if p.open_close & 1 else None)
        return ev

    more, _ = st.place(rect)
    ev = events()
    if not more or not ev:
        return more, rect, ev
    blocks = sorted([e for e in ev if (e[0] and not e[0].startswith("span-")) or e[1]],
                    key=lambda e: (e[2].y0, -e[2].height))
    # Nested elements (a list item and its paragraph) start together; keep
    # the outermost of each start.
    starts = []
    for e in blocks:
        if starts and abs(e[2].y0 - starts[-1][2].y0) < 0.5:
            continue
        starts.append(e)
    if not starts:
        return more, rect, ev
    cut = None
    last = starts[-1]
    if last[1]:
        cut = len(starts) - 1                           # ends on a heading
    elif (len(starts) >= 2 and starts[-2][1]
          and last[2].height < KEEP_WITH_NEXT and last[2].y0 - starts[-2][2].y1 < 10):
        cut = len(starts) - 2                           # heading, one line
    elif last[2].height < ORPHAN and last[2].y1 > rect.y1 - ORPHAN:
        cut = len(starts) - 1                           # a lone first line
    if cut is None:
        return more, rect, ev
    while cut > 0 and starts[cut - 1][1] and starts[cut][2].y0 - starts[cut - 1][2].y1 < 10:
        cut -= 1                                        # stacked headings go too
    y = starts[cut][2].y0 - 1
    if y - rect.y0 < 0.4 * rect.height:
        return more, rect, ev                           # would empty the column
    shorter = fitz.Rect(rect.x0, rect.y0, rect.x1, y)
    more, _ = st.place(shorter)
    return more, shorter, events()


def plan_pages(head_h, flow_html, spans, span_heights, archive, end_span=None):
    """Decide every rectangle on every page, without drawing anything.

    Returns a list of pages; each page is a list of (kind, index, rect) with
    kind in {"head", "flow", "span"}. Drawing then replays the same sequence
    with fresh stories, so what is drawn is exactly what was planned.
    """
    flow = _story(flow_html, archive)
    flow_rects = []                 # every flow placement so far, in order
    pages = []
    pending = []                    # spans whose anchor has been passed
    started = {}                    # span index -> planning story (split spans)
    placed_spans = set()
    flow_done = False

    def place_spans(items, y, page_top):
        """Put pending spans at y, in order, while they fit. A span taller than
        a whole page starts at the top of an empty page and continues."""
        while pending:
            k = pending[0]
            h = span_heights[k]
            room = BODY.y1 - y
            if k not in started and h <= room:
                items.append(("span", k, fitz.Rect(BODY.x0, y, BODY.x1, y + h)))
                y += h + BLOCK_GAP
                pending.pop(0)
                placed_spans.add(k)
                continue
            if y == page_top:       # too tall for any page: split it
                st = started.setdefault(k, _story(spans[k], archive))
                rect = fitz.Rect(BODY.x0, y, BODY.x1, BODY.y1)
                more, filled = st.place(rect)
                st.draw(None)
                if more:
                    items.append(("span", k, rect))
                    return BODY.y1
                # The last piece gets the height it actually used, so the rect
                # on record does not claim the flow set below it.
                end = min(BODY.y1, fitz.Rect(filled).y1 + 2)
                items.append(("span", k, fitz.Rect(BODY.x0, y, BODY.x1, end)))
                pending.pop(0)
                placed_spans.add(k)
                y = end + BLOCK_GAP
                continue
            break
        return y

    while not flow_done or pending:
        if len(pages) >= MAX_PAGES:
            raise RuntimeError("runaway pagination")
        items = []
        y = BODY.y0
        if not pages:
            items.append(("head", 0, fitz.Rect(BODY.x0, y, BODY.x1, y + head_h)))
            y += head_h + 4
        page_top = y
        y = place_spans(items, y, page_top)

        if not flow_done and BODY.y1 - y >= MIN_COLUMN:
            cols = [fitz.Rect(BODY.x0, y, BODY.x0 + COLW, BODY.y1),
                    fitz.Rect(BODY.x1 - COLW, y, BODY.x1, BODY.y1)]
            before = len(flow_rects)
            used = []
            for col in cols:
                more, col, ev = place_column(flow, col)
                for pid, _h, _r in ev:
                    if pid.startswith("span-"):
                        k = int(pid[5:])
                        if k not in placed_spans and k not in pending:
                            pending.append(k)
                # A story only moves past what was placed once it is drawn;
                # drawing to no device advances it without output.
                flow.draw(None)
                flow_rects.append(col)
                used.append(col)
                if not more:
                    flow_done = True
                    break
            if flow_done:
                if end_span is not None and end_span not in pending:
                    pending.append(end_span)
                used = balance(flow_html, archive, flow_rects[:before], y, used)
                flow_rects[before:] = used
                items.extend(("flow", 0, r) for r in used)
                # What is left - blocks anchored on this last page, and the
                # end matter - goes below its columns when it fits.
                y = max(r.y1 for r in used) + BLOCK_GAP
                y = place_spans(items, y, None)
            else:
                items.extend(("flow", 0, r) for r in used)
        elif flow_done and end_span is not None and end_span not in pending \
                and end_span not in placed_spans:
            pending.append(end_span)
        pages.append(items)
    return pages


def ink_bottom(page):
    """Lowest point anything is drawn on a page: text, images, rules."""
    ys = [b[3] for b in page.get_text("blocks")]
    ys += [info["bbox"][3] for info in page.get_image_info()]
    ys += [d["rect"].y1 for d in page.get_drawings()]
    return max(ys) if ys else 0.0


def balance(flow_html, archive, before, y0, fallback):
    """The last page's column rects, as short as the remaining text allows.

    A story cannot be rewound to a midpoint, so each trial replays every
    earlier placement from the start - deterministic, and cheap next to
    everything else a paper costs.
    """
    def replay():
        st = _story(flow_html, archive)
        for r in before:
            st.place(r)
            st.draw(None)
        return st

    def drawn(h):
        """Lay the rest of the flow into two columns of height h on a scratch
        page. Returns (left over, words, images, lowest ink, rects used)."""
        st = replay()
        buf = io.BytesIO()
        writer = fitz.DocumentWriter(buf)
        dev = writer.begin_page(fitz.Rect(0, 0, A4.width, 60000))
        used = []
        more = True
        for rect in (fitz.Rect(BODY.x0, y0, BODY.x0 + COLW, y0 + h),
                     fitz.Rect(BODY.x1 - COLW, y0, BODY.x1, y0 + h)):
            if not used:
                more, rect, _ = place_column(st, rect)   # keep headings with text
            else:
                more, _ = st.place(rect)
            st.draw(dev)
            used.append(rect)
            if not more:
                break
        writer.end_page()
        writer.close()
        page = fitz.open("pdf", buf.getvalue())[0]
        words = collections.Counter(w[4] for w in page.get_text("words"))
        return more, words, len(page.get_image_info()), ink_bottom(page), used

    # Everything that is left, laid out with no height limit: what a fit has
    # to reproduce, word for word.
    _, want_words, want_images, total, _ = drawn(59000 - y0)

    def fits(h):
        # A fit is judged by what was actually drawn. "Nothing left over" is
        # not enough: Story can report the last blocks of a document as
        # placed while they run past the column, and draw() clips them away -
        # which cut the AI disclosure off five papers. Its filled rect is no
        # judge either: it counts the margin above the next heading.
        more, words, images, bottom, used = drawn(h)
        ok = (not more and words == want_words and images == want_images
              and bottom <= y0 + h + 1)
        return ok, used

    # Fit is not monotonic in h - one line more in the left column can make
    # Story drop the last block - so a bisection can step over every good
    # height. Walk up from half the text instead: a balanced page is found
    # within a few steps, and every h returned was tested and fitted.
    full = BODY.y1 - y0
    h = max(0.0, (total - y0) / 2 - 6)
    for _ in range(BALANCE_STEPS):
        if h >= full:
            break
        ok, used = fits(h)
        if ok:
            return used
        h += 4
    ok, used = fits(full)
    # Nothing verified: keep the columns the planner laid, and let verify()
    # judge the finished PDF - it refuses one that lost text.
    return used if ok else fallback


def with_doi(src, doi):
    """The paper with `doi:` set in its front matter - in memory only."""
    if not doi or not src.startswith("---"):
        return src
    end = src.find("\n---", 3)
    if end == -1:
        return src
    fm = re.sub(r"(?m)^doi:.*\n?", "", src[:end + 1])
    return fm + "doi: %s\n" % doi + src[end + 1:]


LAST_PLAN = None           # the last render's plan, for tools that check it


def _is_runt(pages):
    """Does the last page hold only a few lines?"""
    last = pages[-1]
    if len(pages) < 2 or any(kind == "head" for kind, _k, _r in last):
        return False
    extent = max((r.y1 for _kind, _k, r in last), default=BODY.y0) - BODY.y0
    return extent < RUNT * BODY.height


def _layout(src, asset_dir, archive):
    """Build the content and plan every page at the current spacing."""
    head, flow_html, spans, meta = build_parts(src, asset_dir)
    head_h = _height(head, BODY.width, archive) + 1
    if head_h > HEAD_MAX * BODY.height:
        # An abstract this long would push the columns off the first page.
        # Standard two-column practice has the abstract open the first
        # column instead, so move it (and the keywords) there.
        head, flow_html = demote_abstract(head, flow_html)
        head_h = _height(head, BODY.width, archive) + 1
    span_heights = [_height(s, BODY.width, archive) for s in spans]
    pages = plan_pages(head_h, flow_html, spans, span_heights, archive,
                       end_span=meta["end_span"])
    return head, flow_html, spans, meta, pages


def render(src, out_path, asset_dir=None, doi=None):
    """Lay the paper out and write the PDF. Returns the page count.

    doi: print this DOI in the title block and end matter even though the
    front matter has none yet - publish_paper passes the DOI Zenodo has
    reserved for the deposition, so the uploaded PDF carries its own DOI.
    """
    global _spacing
    src = with_doi(src, doi)
    # Without an Archive, Story cannot resolve a relative <img src> and drops
    # the tag silently - a figure cited in the text and missing from the PDF.
    # Rooting one at the paper's own directory makes ![caption](fig.png) work.
    archive = None
    if asset_dir and os.path.isdir(asset_dir):
        try:
            archive = fitz.Archive(asset_dir)
        except Exception as exc:
            sys.stderr.write("WARNING: figures not embedded (%s)\n" % exc)

    try:
        _spacing = SPACING["normal"]
        layout = _layout(src, asset_dir, archive)
        if _is_runt(layout[4]):
            # A last page holding a few lines - often only the end matter -
            # reads as a production error. Set the paper a little tighter and
            # keep that only if it takes the page away.
            _spacing = SPACING["tight"]
            tight = _layout(src, asset_dir, archive)
            if len(tight[4]) < len(layout[4]):
                layout = tight
            else:
                _spacing = SPACING["normal"]
        head, flow_html, spans, meta, pages = layout
        global LAST_PLAN
        LAST_PLAN = (pages, head, flow_html, spans)      # for diagnostics
        _draw(out_path, pages, head, flow_html, spans, archive, meta)
        verify(out_path, pages, head, flow_html, spans, archive)
    finally:
        _spacing = SPACING["normal"]
    return len(pages)


def _draw(out_path, pages, head, flow_html, spans, archive, meta):
    stories = {"head": _story(head, archive), "flow": _story(flow_html, archive)}
    span_stories = {}
    headings = []
    last_more = {}
    writer = fitz.DocumentWriter(out_path)
    for pno, items in enumerate(pages):
        dev = writer.begin_page(A4)
        for kind, k, rect in items:
            if kind == "span":
                st = span_stories.setdefault(k, _story(spans[k], archive))
            else:
                st = stories[kind]
            more, _ = st.place(rect)
            if kind == "flow":
                page_no = pno + 1

                def seen(pos):
                    if pos.heading in (2, 3) and pos.open_close & 1 and pos.text:
                        headings.append((pos.heading - 1, pos.text.strip(), page_no,
                                         pos.rect[1]))
                st.element_positions(seen)
            last_more[kind if kind != "span" else ("span", k)] = more
            st.draw(dev)
        writer.end_page()
    writer.close()
    # A PDF on Zenodo is permanent. If any story still has content after its
    # last planned rect, text would be missing from it: refuse, loudly.
    unfinished = [k for k, more in last_more.items() if more]
    if unfinished:
        raise RuntimeError("layout lost content from %s - refusing to write an "
                           "incomplete paper" % unfinished)
    finish(out_path, meta, headings)


def _unbounded_words(inner, width, archive):
    """The words a block contains, laid out at this width with no height limit."""
    st = _story(inner, archive)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    dev = writer.begin_page(fitz.Rect(0, 0, width + 100, 60000))
    st.place(fitz.Rect(0, 0, width, 59000))
    st.draw(dev)
    writer.end_page()
    writer.close()
    page = fitz.open("pdf", buf.getvalue())[0]
    return collections.Counter(w[4] for w in page.get_text("words"))


def verify(out_path, pages, head, flow_html, spans, archive):
    """Prove the PDF holds every word of the paper, each where it belongs.

    Every word drawn on every page is assigned to the rect it sits in; each
    region's words must equal the same content laid out with no height limit.
    A word missing - clipped at a column foot, dropped from a table's last
    row, lost off the end of the flow - fails the render instead of shipping
    a permanent PDF with a hole in it. Each of those happened while this
    layout was being built; none is visible to a reader skimming the PDF.
    """
    with open(out_path, "rb") as fh:
        doc = fitz.open("pdf", fh.read())
    got = collections.defaultdict(collections.Counter)
    stray = collections.Counter()
    for pno, items in enumerate(pages):
        for w in doc[pno].get_text("words"):
            c = fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
            for kind, k, rect in items:
                if fitz.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1).contains(c):
                    got["flow" if kind == "flow" else (kind, k)][w[4]] += 1
                    break
            else:
                stray[w[4]] += 1
    want = {("head", 0): _unbounded_words(head, BODY.width, archive),
            "flow": _unbounded_words(flow_html, COLW, archive)}
    for k, block in enumerate(spans):
        want[("span", k)] = _unbounded_words(block, BODY.width, archive)
    problems = []
    for key, words in want.items():
        missing = words - got[key]
        if missing:
            problems.append("%s: %d word(s) missing, e.g. %s"
                            % (key, sum(missing.values()),
                               ", ".join(list(missing)[:6])))
    if problems:
        raise RuntimeError("the layout lost text - refusing to write an incomplete "
                           "paper:\n  " + "\n  ".join(problems))


FURNITURE_CSS = "* { font-family: serif; margin: 0; }"


def furniture(page, rect, html):
    """A line of page furniture, set by the same engine and font as the body."""
    page.insert_htmlbox(rect, html, css=FURNITURE_CSS)


def finish(out_path, meta, headings):
    """Running heads, page numbers, the first page's licence line, document
    metadata and a bookmark outline - the furniture of a standard paper -
    then a compressed, garbage-collected save. The Story writer stores images
    uncompressed: one figure made a 0.5 MB paper 10 MB."""
    # Read into memory, so the file on disk is never held open while it is
    # rewritten (Windows refuses to replace a file that is open).
    with open(out_path, "rb") as fh:
        doc = fitz.open("pdf", fh.read())
    foot = fitz.Rect(BODY.x0, A4.height - 40, BODY.x1, A4.height - 26)
    for i, page in enumerate(doc, start=1):
        furniture(page, foot, '<p style="text-align:center;font-size:8.5pt">%d</p>' % i)
        if i == 1:
            if meta["license"]:
                licence = re.sub(r"^\(c\)", "\u00a9", meta["license"])
                furniture(page, fitz.Rect(BODY.x0, foot.y0, BODY.x0 + 220, foot.y1),
                          '<p style="font-size:7pt">%s</p>' % _html.escape(licence))
            continue
        head = fitz.Rect(BODY.x0, TOP - 26, BODY.x1, TOP - 12)
        furniture(page, fitz.Rect(head.x0, head.y0, head.x1 - 90, head.y1),
                  '<p style="font-size:7.5pt;font-style:italic">%s</p>'
                  % _html.escape(meta["short"]))
        if meta["venue"]:
            furniture(page, fitz.Rect(head.x1 - 140, head.y0, head.x1, head.y1),
                      '<p style="font-size:7.5pt;font-style:italic;text-align:right">%s</p>'
                      % _html.escape(meta["venue"]))
        page.draw_line((BODY.x0, head.y1 + 1), (BODY.x1, head.y1 + 1), width=0.4)
    doc.set_metadata({"title": meta["title"], "author": meta["author"],
                      "subject": meta["venue"] or "", "keywords": meta["keywords"],
                      "creator": "md2pdf (two-column)", "producer": "PyMuPDF"})
    toc = [[lvl, text, pno, {"kind": fitz.LINK_GOTO, "page": pno - 1,
                             "to": fitz.Point(0, top)}]
           for lvl, text, pno, top in headings]
    # set_toc insists the outline starts at level 1 and never skips a level.
    fixed, last = [], 0
    for item in toc:
        item[0] = min(item[0], last + 1)
        fixed.append(item)
        last = item[0]
    if fixed:
        try:
            doc.set_toc(fixed)
        except Exception:                       # an outline is a courtesy
            doc.set_toc([item[:3] for item in fixed])
    data = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
    doc.close()
    with open(out_path, "wb") as fh:
        fh.write(data)


def main():
    argv = sys.argv[1:]
    doi = None
    if "--doi" in argv:
        i = argv.index("--doi")
        if i + 1 >= len(argv):
            sys.exit("--doi needs a value")
        doi = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) < 2:
        sys.exit(__doc__)
    src_path, out_path = argv[0], argv[1]
    src = open(src_path, encoding="utf-8").read()
    pages = render(src, out_path, asset_dir=os.path.dirname(os.path.abspath(src_path)),
                   doi=doi)
    size = os.path.getsize(out_path)
    print("wrote %s  (%d pages, %.2f MB)" % (out_path, pages, size / 1e6))


if __name__ == "__main__":
    main()
