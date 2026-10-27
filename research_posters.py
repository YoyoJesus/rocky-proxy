"""Group the CS department's Choose Ohio First student posters by research lab.

The poster pages name advisors, not labs, so each poster is matched to the lab whose director advised it. Works on
text already extracted by knowledge_base.extract_html (markdown links kept), so it has no fetching of its own.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

LABS_URL = "https://www.kent.edu/cs/research-labs"
INDEX_URL = "https://www.kent.edu/cs/global-circ/creative-poster-projects"
TITLE = "Kent State CS research lab student posters (Choose Ohio First poster conferences)"
MAX_PAGES = 30

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
# Matched in raw HTML: some pages link sub-pages from headings or tabs that text extraction drops.
POSTER_LINK = re.compile(r"(?:https://www\.kent\.edu)?(/cs/global-circ/(20\d\d)-[\w-]*poster[\w-]*)")
DIRECTOR = re.compile(r"Dr\.\s+([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)*)")
FILE = re.compile(r"^(.+?\.(?:pdf|pptx?|docx?))\s*\([\d.]+\s*[KMG]?B\)$", re.IGNORECASE)
INITIAL = re.compile(r"\b[A-Z]\.\s")
ADVISOR = re.compile(r"\b(?:Advisors?:|Supervised by)")
BOILERPLATE = re.compile(
    r",?\s*\d{4} COF (?:Regional Symposium|Research Showcase Conference)(?:,\s*[A-Z][a-z]+(?: \d{4})?)?"
    r"|\s*\[\[[a-z]+\]\([^)]*\)\]"
)
# The 2025+ pages identify posters only by file name, which starts with the advisor. Three lab directors are named
# Kim, so their file-name prefixes are spelled out here; everyone else is matched by surname.
FILE_PREFIXES = {"jhkim": "Jong-Hoon Kim", "jykim": "Jungyoon Kim", "jy": "Jungyoon Kim", "kkim": "Kwangtaek Kim"}


@dataclass
class Lab:
    name: str
    directors: list[str]
    site: str | None
    posters: list[tuple[int, str]] = field(default_factory=list)


def _key(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _name_key(text: str) -> str:
    """Letters only, without middle initials, so "Javed I. Khan" matches "Dr. Javed Khan"."""
    return _key(INITIAL.sub("", text))


def parse_labs(text: str) -> list[Lab]:
    labs = []
    for line in text.splitlines():
        if not line.startswith("- ") or " - " not in line:
            continue
        left, right = line[2:].rsplit(" - ", 1)
        directors = DIRECTOR.findall(right)
        if not directors:
            continue
        site = LINK.search(left)
        labs.append(Lab(LINK.sub(r"\1", left).strip(), directors, site.group(2) if site else None))
    return labs


def poster_links(html: str) -> list[tuple[str, int]]:
    return list(dict.fromkeys(("https://www.kent.edu" + path, int(year)) for path, year in POSTER_LINK.findall(html)))


def parse_posters(text: str) -> tuple[list[str], list[tuple[str | None, str]]]:
    """Return (items with an advisor in the text, (title, file name) pairs from file-only listings)."""
    items: list[str] = []
    files: list[tuple[str | None, str]] = []
    titles: list[str] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line == "Document":
            continue
        if (m := FILE.match(line)) and i and lines[i - 1] == "Document":
            files.append((titles[-1] if titles else None, m.group(1)))
            titles = []
        elif "Dr." in line or ADVISOR.search(line):
            # An advisor list wrapped onto its own line belongs to the poster line above it.
            if ADVISOR.match(line) and i:
                line = lines[i - 1] + ", " + line
                if items and items[-1] == lines[i - 1]:
                    items.pop()
            items.append(line)
            titles = []
        elif line.startswith(("#", "-", "[")):
            titles = []
        else:
            titles.append(line)
    clean = (LINK.sub(r"\1", BOILERPLATE.sub("", item)).removeprefix("- ") for item in items)
    return [re.sub(r"^\d+\s+", "", item).strip(" ,") for item in clean], files


def _labs_for_item(item: str, labs: list[Lab]) -> list[Lab]:
    m = ADVISOR.search(item)
    advisors = _name_key(item[m.start():] if m else item)
    return [lab for lab in labs if any(_name_key(d) in advisors for d in lab.directors)]


def _advisor_for_file(name: str, labs: list[Lab]) -> tuple[Lab | None, str]:
    words = re.sub(r"^20\d\d\s+", "", name).split()
    joined = _key("".join(words[:2]))
    by_name = {d: lab for lab in labs for d in lab.directors}
    for prefix in sorted(FILE_PREFIXES, key=len, reverse=True):
        if joined.startswith(prefix) and FILE_PREFIXES[prefix] in by_name:
            return by_name[FILE_PREFIXES[prefix]], FILE_PREFIXES[prefix]
    surnames = defaultdict(list)
    for director, lab in by_name.items():
        surnames[_key(director.split()[-1])].append(lab)
    first = _key(words[0]) if words else ""
    if len(surnames.get(first, [])) == 1:
        return surnames[first][0], ""
    return None, words[0] if words else name


def build_digest(labs_text: str, pages: list[tuple[int, str, str]]) -> str:
    """pages is (year, url, extracted text). Returns markdown for the knowledge base."""
    labs = parse_labs(labs_text)
    other: list[tuple[int, str]] = []
    for year, _, text in pages:
        items, files = parse_posters(text)
        for item in items:
            matched = _labs_for_item(item, labs)
            for lab in matched:
                lab.posters.append((year, item))
            if not matched:
                other.append((year, item))
        for title, name in files:
            lab, advisor = _advisor_for_file(name, labs)
            entry = f"{title or 'Untitled poster'} (file: {name})"
            if lab:
                lab.posters.append((year, entry))
            elif title:
                other.append((year, f"{advisor}: {entry}"))
    out = [
        f"Source: {INDEX_URL}",
        f"# {TITLE}",
        "",
        f"Regenerated automatically from the Research Labs page ({LABS_URL}) and the yearly Choose Ohio "
        "First (COF) poster conference pages linked from Creative Poster Projects. Students present these posters at "
        "the annual COF poster conference.",
        "",
        "The poster pages list advisors, not labs, so posters are grouped under the lab whose director advised them: "
        "\"lab\" here means \"advised by that lab's director\". From 2025 on, the pages give only a poster title and "
        "a file name; the file name starts with the advisor's surname, followed by the students' surnames.",
    ]
    for lab in labs:
        if not lab.posters:
            continue
        out += ["", f"## {lab.name}, director{'s' if len(lab.directors) > 1 else ''} "
                + " and ".join(f"Dr. {d}" for d in lab.directors), ""]  # fmt: skip
        if lab.site:
            out.append(f"Lab site: {lab.site}")
        out += [f"- {year}: {entry}" for year, entry in sorted(lab.posters, key=lambda p: -p[0])]
    if other:
        out += ["", "## Posters advised by faculty without a listed lab", ""]
        out += [f"- {year}: {entry}" for year, entry in sorted(other, key=lambda p: -p[0])]
    without = [lab.name for lab in labs if not lab.posters]
    if without:
        out += ["", "## Labs with no posters on these pages", "", "- " + "; ".join(without)]
    return "\n".join(out) + "\n"
