"""Local SQLite FTS5 knowledge base for the Rocky Discord bot.

Content comes from curated markdown files and an explicit manifest of allowlisted HTTPS pages.
Everything is stored as heading-aware text chunks. Retrieved text is reference material only and
is never interpreted as instructions.

    uv run knowledge_base.py refresh [--sources knowledge/sources.json] [--knowledge-dir knowledge]
    uv run knowledge_base.py search "when is the CS 33001 final" [--db data/knowledge.sqlite3]
    uv run knowledge_base.py status
"""

import argparse
import codecs
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

DEFAULT_DB = "data/knowledge.sqlite3"
DEFAULT_SOURCES = "knowledge/sources.json"
DEFAULT_KNOWLEDGE_DIR = "knowledge"
DEFAULT_ALLOWED_HOSTS = ("kent.edu", "hacksu.com", "khe.io", "github.com", "raw.githubusercontent.com")
ALLOWED_HOSTS_ENV = "KNOWLEDGE_ALLOWED_HOSTS"
# Exact hostnames that only resolve on Kent State's network; they may use private LAN addresses.
DEFAULT_PRIVATE_HOSTS = ("web.cs.kent.edu",)
PRIVATE_HOSTS_ENV = "KNOWLEDGE_PRIVATE_HOSTS"
# Exact hostnames fetched over plain HTTP because their HTTPS certificate is broken (antares serves another host's).
DEFAULT_HTTP_HOSTS = ("antares.cs.kent.edu",)
HTTP_HOSTS_ENV = "KNOWLEDGE_HTTP_HOSTS"

CHUNK_VERSION = 5
CHUNK_MAX_CHARS = 1200
FOLD_MAX_CHARS = 300
MAX_CHUNKS_PER_DOC = 500
MAX_SOURCES = 400
DEFAULT_REFRESH_HOURS = 6.0
MAX_FILE_BYTES = 1_000_000
MAX_FETCH_BYTES = 2_000_000
FETCH_TIMEOUT = (5, 15)
FETCH_DEADLINE = 30.0
MAX_REDIRECTS = 3
USER_AGENT = "rocky-knowledge-bot/1.0 (+local retrieval; no crawling)"
ACCEPTED_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown", "text/javascript"}
SKIPPED_MARKDOWN = {"sources.md", "readme.md"}

MAX_QUERY_CHARS = 2000
MAX_QUERY_TERMS = 16
MAX_CHUNKS_PER_DOC_IN_RESULTS = 3
MAX_CANDIDATES = 1000
MAX_LINK_CHARS = 300
LINK_SCHEMES = {"http", "https", "mailto"}
CATALOG_TITLE_LEVEL = 3
BM25_WEIGHTS = (5.0, 3.0, 1.0, 4.0)  # title, heading, body, codes


class KnowledgeError(Exception):
    pass


class FetchError(KnowledgeError):
    pass


class ManifestError(KnowledgeError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
}  # fmt: skip
SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "iframe", "canvas", "nav", "footer", "aside", "head",
    "select", "option", "textarea", "dialog", "object", "audio", "video", "map",
}  # fmt: skip
SKIP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search"}
SKIP_BUTTON_TYPES = {"submit", "reset"}
SKIP_CLASSES = {
    "breadcrumb", "breadcrumbs", "navbar", "skip-link", "skiplink", "cookie-banner", "cookie-notice", "site-footer",
    "site-header", "main-nav", "mobile-nav", "sr-only",
}  # fmt: skip
BLOCK_BREAK = {
    "p", "div", "section", "article", "main", "ul", "ol", "table", "blockquote", "pre", "dl", "dt", "dd", "header",
    "figure", "figcaption", "details", "summary", "address", "tr", "thead", "tbody", "button",
}  # fmt: skip
HEADING_TAGS = {f"h{i}": i for i in range(1, 7)}


@dataclass
class _Frame:
    tag: str
    skip: bool
    main: bool
    heading: bool = False


class _TextExtractor(HTMLParser):
    """Collects visible article-like text as light markdown (headings, lists, table rows)."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url or ""
        self._link: tuple[str, list[str]] | None = None
        self._stack: list[_Frame] = []
        self._skip = 0
        self._main = 0
        self._heading = False
        self._in_title = False
        self._title: list[str] = []
        self._all: list[str] = []
        self._main_parts: list[str] = []
        self._span_just_closed = False  # separates <span>A</span><span>B</span> rendered as stacked labels

    def _emit(self, text: str) -> None:
        if self._skip:
            return
        self._all.append(text)
        if self._main:
            self._main_parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "body":
            self._close("head")
        if tag == "span" and self._span_just_closed:
            self._emit(", ")
        self._span_just_closed = False
        if tag in VOID_TAGS:
            if tag == "br":
                self._emit(" " if self._heading else "\n")
            elif tag == "hr":
                self._emit("\n\n")
            return
        classes = set(a.get("class", "").lower().split()) | {a.get("id", "").lower()}
        skip = (
            tag in SKIP_TAGS
            or a.get("role", "").lower() in SKIP_ROLES
            or "hidden" in a
            or a.get("aria-hidden", "").lower() == "true"
            or bool(classes & SKIP_CLASSES)
            or (tag == "header" and not self._main)
            or (tag == "button" and a.get("type", "").lower() in SKIP_BUTTON_TYPES)
        )
        main = tag == "main" or a.get("role", "").lower() == "main"
        level = HEADING_TAGS.get(tag, 0)
        if tag == "p" and "courseblocktitle" in classes:
            level = CATALOG_TITLE_LEVEL
        self._stack.append(_Frame(tag, skip, main, bool(level)))
        self._skip += skip
        self._main += main
        if level:
            self._heading = True
            self._emit("\n\n" + "#" * level + " ")
        elif tag == "a" and not self._skip and not self._heading and self._link is None:
            if href := self._clean_href(a.get("href", "")):
                self._link = (href, [])
        elif tag == "li":
            self._emit("\n- ")
        elif tag in BLOCK_BREAK:
            self._emit("\n\n" if tag in {"p", "section", "article", "ul", "ol", "table", "blockquote"} else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link is not None:
            href, parts = self._link
            self._link = None
            text = _collapse("".join(parts))
            if text and text.removeprefix("mailto:") != href.removeprefix("mailto:"):
                self._emit(f"[{text}]({href.replace(')', '%29')})")
            else:
                self._emit(text)
        frame = next((f for f in reversed(self._stack) if f.tag == tag), None)
        if frame and frame.heading:
            self._emit("\n\n")
            self._heading = False
        elif tag in {"td", "th"}:
            self._emit(" | ")
        elif tag in BLOCK_BREAK and tag not in {"tr", "thead", "tbody"}:
            self._emit("\n")
        self._close(tag)
        self._span_just_closed = tag == "span"

    def _close(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i].tag == tag:
                for frame in self._stack[i:]:
                    self._skip -= frame.skip
                    self._main -= frame.main
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
            return
        if self._skip:
            return
        self._span_just_closed = False
        text = re.sub(r"\s+", " ", data) if data.strip() else " "
        if self._link is not None:
            self._link[1].append(text)
        else:
            self._emit(text)

    def _clean_href(self, href: str) -> str:
        href = href.strip()
        if not href or href.startswith("#") or re.search(r"\s", href):
            return ""
        try:
            absolute = urljoin(self._base_url, href)
            parts = urlsplit(absolute)
        except ValueError:
            return ""
        if parts.scheme not in LINK_SCHEMES or len(absolute) > MAX_LINK_CHARS:
            return ""
        if parts.scheme != "mailto" and not parts.netloc:
            return ""
        return absolute

    @staticmethod
    def _tidy(parts: list[str]) -> str:
        lines = []
        for line in "".join(parts).split("\n"):
            line = _collapse(line).removesuffix("|").strip()
            if line == "-":
                continue
            lines.append(line)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    def result(self) -> tuple[str, str]:
        main = self._tidy(self._main_parts)
        text = main if len(main) >= 200 else self._tidy(self._all)
        title = _collapse("".join(self._title))
        if not title:
            heading = re.search(r"^#{1,6} (.+)$", text, re.MULTILINE)
            title = heading.group(1) if heading else ""
        return title[:200], text


def extract_html(html: str, base_url: str | None = None) -> tuple[str, str]:
    """Return (title, text) for a public HTML page, dropping navigation, scripts, and styles.

    http(s) and mailto links outside skipped regions stay in the text as `[label](url)`; relative links are
    resolved against base_url (and dropped if there is none).
    """
    parser = _TextExtractor(base_url)
    parser.feed(html)
    parser.close()
    return parser.result()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

HEADING_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*?)(?:[ \t]+#+)?)?[ \t]*$")


def _hard_wrap(line: str, limit: int) -> list[str]:
    pieces = []
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    if line:
        pieces.append(line)
    return pieces


def _pack(units: Iterable[str], sep: str, limit: int) -> list[str]:
    out: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(sep) + len(unit) > limit:
            out.append(current)
            current = unit
        else:
            current = f"{current}{sep}{unit}" if current else unit
    if current:
        out.append(current)
    return out


def _section_chunks(lines: list[str], limit: int) -> list[str]:
    """Pack paragraphs into chunks. A one-line table header is repeated on every chunk of the rows below it."""
    units: list[str] = []
    header = ""
    for para in re.split(r"\n[ \t]*\n", "\n".join(lines)):
        para = "\n".join(line.rstrip() for line in para.strip().split("\n"))
        if not para:
            continue
        rows = para.split("\n")
        if header and all(row.count("|") == header.count("|") for row in rows):
            room = limit - len(header) - 1
            units.pop()
            wrapped = [piece for row in rows for piece in _hard_wrap(row, room)]
            units.extend(f"{header}\n{piece}" for piece in _pack(wrapped, "\n", room))
            header = ""
            continue
        header = para if len(rows) == 1 and " | " in para and len(para) <= limit // 2 else ""
        if len(para) <= limit:
            units.append(para)
        else:
            wrapped = [piece for line in para.split("\n") for piece in _hard_wrap(line, limit)]
            units.extend(_pack(wrapped, "\n", limit))
    return [c for c in _pack(units, "\n\n", limit) if re.search(r"\w", c)]


@dataclass
class _Section:
    level: int
    heading: str
    title: str
    lines: list[str]
    folded: bool = False


def _fold_tiny_sections(sections: list[_Section]) -> None:
    """Fold short leaf sections into their parents as '- Title: body' lines, deepest first.

    Rosters ('### Alice' / 'President', ...) otherwise become many label-only chunks, and search returns at most
    MAX_CHUNKS_PER_DOC_IN_RESULTS per page. A depth is folded only when the document has 3+ short leaves there, so
    lone short sections stay separate; headings with course codes are never folded. Repeating the pass lets
    sections whose children were folded (e.g. semesters of a roster) fold into their own parent.
    """
    changed = True
    while changed:
        changed = False
        candidates: dict[int, list[tuple[int, int, str]]] = {}
        for i, section in enumerate(sections):
            if section.folded or not section.level or COURSE_CODE_RE.search(section.title):
                continue
            following = next((s for s in sections[i + 1 :] if not s.folded), None)
            if following is not None and following.level > section.level:
                continue
            body = "; ".join(line.strip().removeprefix("- ") for line in section.lines if line.strip())
            parent = next((j for j in range(i - 1, -1, -1) if not sections[j].folded and sections[j].level < section.level), None)
            if body and len(body) <= FOLD_MAX_CHARS and parent is not None:
                candidates.setdefault(section.level, []).append((i, parent, body))
        for items in candidates.values():
            if len(items) < 3:
                continue
            for i, parent, body in items:
                lines = sections[parent].lines
                if lines and lines[-1].strip() and not lines[-1].startswith("- "):
                    lines.append("")
                lines.append(f"- {sections[i].title}: {body}")
                sections[i].folded = True
                changed = True


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[tuple[str, str]]:
    """Split markdown-ish text into (heading path, body) chunks of at most max_chars characters."""
    path: list[tuple[int, str]] = []
    sections = [_Section(0, "", "", [])]
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = "" if fence == marker else (fence or marker)
        match = None if fence else HEADING_RE.match(line)
        if match and not match.group(2):
            continue
        if match:
            level = len(match.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, match.group(2).strip()))
            sections.append(_Section(level, " > ".join(name for _, name in path), match.group(2).strip(), []))
        else:
            sections[-1].lines.append(line)
    _fold_tiny_sections(sections)
    chunks = [(s.heading, body) for s in sections if not s.folded for body in _section_chunks(s.lines, max_chars)]
    return chunks[:MAX_CHUNKS_PER_DOC]


# ---------------------------------------------------------------------------
# Search query construction
# ---------------------------------------------------------------------------

STOPWORDS = frozenset(
    """a about above after again all also am an and any are as at be because been before being below between both but
    by can could did do does doing down during each few for from further get got had has have having he her here hers
    him his how i if in into is it its just me more most my no nor not of off on once only or other our out over own
    same she should so some such than that the their them then there these they this those through to too under until
    up us very was we were what when where which while who whom why will with would you your yours please tell hey hi
    hello thanks thank rocky dr prof professor""".split()
)
COURSE_CODE_RE = re.compile(r"\b([A-Za-z]{2,5})[\s\-]?(\d{5})\b")  # Kent-style 5-digit course numbers only
_QUERY_NOISE_RE = re.compile(r"https?://\S+|<[@#][!&]?\d+>|<a?:\w+:\d+>")


def course_codes(text: str) -> list[str]:
    """Normalized course codes such as 'cs33001' found in text."""
    return sorted(
        {m.group(1).lower() + m.group(2) for m in COURSE_CODE_RE.finditer(text) if m.group(1).lower() not in STOPWORDS}
    )


def _parse_query(text: str, max_terms: int = MAX_QUERY_TERMS) -> tuple[list[str], list[str]]:
    """Return (all terms, course-code terms) as quoted FTS5 tokens/phrases."""
    text = _QUERY_NOISE_RE.sub(" ", text[:MAX_QUERY_CHARS])
    terms: list[str] = []
    code_terms: list[str] = []

    def add(term: str, is_code: bool = False) -> None:
        if term not in terms and len(terms) < max_terms:
            terms.append(term)
            if is_code:
                code_terms.append(term)

    def strip_code(match: re.Match[str]) -> str:
        prefix, number = match.group(1).lower(), match.group(2)
        if prefix in STOPWORDS:
            return match.group(0)
        add(f'"{prefix}{number}"', True)
        add(f'"{prefix} {number}"', True)
        return " "

    remainder = COURSE_CODE_RE.sub(strip_code, text)
    for token in re.findall(r"[^\W_]+", remainder.lower()):
        if token in STOPWORDS or (len(token) < 2 and not token.isdigit()):
            continue
        add(f'"{token[:40]}"')
    return terms, code_terms


def build_match_query(text: str, max_terms: int = MAX_QUERY_TERMS) -> str:
    """Turn plain language into a safe FTS5 OR query. Every term is a quoted alphanumeric token or phrase."""
    return " OR ".join(_parse_query(text, max_terms)[0])


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    path TEXT,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    last_error TEXT,
    last_attempt TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    heading TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(title, heading, body, codes, tokenize='porter unicode61');
"""


def _open_for_write(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _open_for_read(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _delete_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("DELETE FROM chunk_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def _is_boilerplate(body: str) -> bool:
    """Tiny label-only chunks such as 'Past Meeting': under 3 words, no digits, links, or emails."""
    return len(body.split()) < 3 and not re.search(r"\d|@|\]\(", body)


def store_document(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    kind: str,
    title: str,
    text: str,
    url: str | None = None,
    path: str | None = None,
    fetched_at: str | None = None,
    touch_date: bool = False,
) -> str:
    """Replace a document's chunks atomically. Returns 'updated' or 'unchanged'.

    With touch_date, an unchanged document still gets a new fetched date (a successful web re-fetch).
    """
    fetched_at = fetched_at or _now_iso()
    digest = hashlib.sha256(json.dumps([CHUNK_VERSION, title, url, text]).encode()).hexdigest()
    existing = conn.execute("SELECT content_hash FROM documents WHERE id = ?", (doc_id,)).fetchone()
    with conn:
        if existing and existing["content_hash"] == digest:
            if touch_date:
                conn.execute(
                    "UPDATE documents SET fetched_at = ?, last_error = NULL, last_attempt = ? WHERE id = ?",
                    (fetched_at, fetched_at, doc_id),
                )
            else:
                conn.execute("UPDATE documents SET last_error = NULL WHERE id = ?", (doc_id,))
            return "unchanged"
        chunks = chunk_text(text)
        if kind == "web":
            chunks = [c for c in chunks if not _is_boilerplate(c[1])]
        if not chunks:
            raise KnowledgeError("no extractable text")
        _delete_doc(conn, doc_id)
        conn.execute(
            "INSERT INTO documents (id, kind, title, url, path, fetched_at, content_hash, last_attempt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, kind, title, url, path, fetched_at, digest, fetched_at),
        )
        for order, (heading, body) in enumerate(chunks):
            cur = conn.execute(
                "INSERT INTO chunks (doc_id, ord, heading, body) VALUES (?, ?, ?, ?)", (doc_id, order, heading, body)
            )
            codes = " ".join(course_codes(f"{title} {heading} {body}"))
            conn.execute(
                "INSERT INTO chunk_fts (rowid, title, heading, body, codes) VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, title, heading, body, codes),
            )
    return "updated"


def _record_failure(conn: sqlite3.Connection, doc_id: str, error: str) -> None:
    with conn:
        conn.execute(
            "UPDATE documents SET last_error = ?, last_attempt = ? WHERE id = ?", (error[:500], _now_iso(), doc_id)
        )


# ---------------------------------------------------------------------------
# Local markdown import
# ---------------------------------------------------------------------------

SOURCE_LINE_RE = re.compile(r"^source:\s*(https?://\S+)\s*$", re.IGNORECASE)


def parse_markdown(raw: str, fallback_title: str) -> tuple[str, str | None, str]:
    """Return (title, source_url, body). An optional first line `Source: https://...` is metadata."""
    lines = raw.lstrip("﻿").splitlines()
    source = None
    if lines and (match := SOURCE_LINE_RE.match(lines[0].strip())):
        source = match.group(1)
        lines = lines[1:]
    body = "\n".join(lines)
    heading = re.search(r"^#[ \t]+(.+?)[ \t]*$", body, re.MULTILINE)
    title = _collapse(heading.group(1)) if heading else fallback_title
    return title[:200], source, body


def find_markdown_files(knowledge_dir: Path) -> list[Path]:
    found = []
    for path in knowledge_dir.rglob("*"):
        rel_parts = path.relative_to(knowledge_dir).parts
        if (
            path.suffix.lower() == ".md"
            and path.name.lower() not in SKIPPED_MARKDOWN
            and not any(part.startswith(".") for part in rel_parts)
            and path.is_file()
            and not path.is_symlink()
        ):
            found.append(path)
    return sorted(found, key=lambda p: p.relative_to(knowledge_dir).as_posix())


def file_doc_id(knowledge_dir: Path, path: Path) -> str:
    return "file:" + path.relative_to(knowledge_dir).as_posix()


# ---------------------------------------------------------------------------
# Web fetch
# ---------------------------------------------------------------------------

Resolver = Callable[..., list]


def configured_allowed_hosts(extra: Iterable[str] = ()) -> tuple[str, ...]:
    env = [h for h in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",")]
    hosts = [h.strip().lower().strip(".") for h in (*DEFAULT_ALLOWED_HOSTS, *env, *extra)]
    return tuple(dict.fromkeys(h for h in hosts if h))


def _exact_hosts(defaults: Iterable[str], env_name: str) -> tuple[str, ...]:
    hosts = [h.strip().lower().strip(".") for h in (*defaults, *os.environ.get(env_name, "").split(","))]
    return tuple(dict.fromkeys(h for h in hosts if h))


def configured_private_hosts() -> tuple[str, ...]:
    return _exact_hosts(DEFAULT_PRIVATE_HOSTS, PRIVATE_HOSTS_ENV)


def configured_http_hosts() -> tuple[str, ...]:
    return _exact_hosts(DEFAULT_HTTP_HOSTS, HTTP_HOSTS_ENV)


def _allowed_address(address: str, private_ok: bool) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.is_global:
        return True
    return private_ok and ip.is_private and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def validate_url(url: str, allowed_hosts: Sequence[str], resolver: Resolver = socket.getaddrinfo) -> str:
    """Check that url is a plain HTTPS URL on an allowlisted host that resolves only to public addresses.

    Hosts listed exactly in configured_private_hosts() may also resolve to private LAN addresses, and hosts
    listed exactly in configured_http_hosts() may use plain HTTP on port 80.

    Returns the URL without its fragment. Raises FetchError otherwise.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise FetchError(f"invalid URL: {exc}") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if not (parts.scheme == "https" or (parts.scheme == "http" and host in configured_http_hosts())):
        raise FetchError("only https URLs are allowed")
    if not host or parts.username is not None or parts.password is not None:
        raise FetchError("URL must have a host and no credentials")
    if port not in (None, 443 if parts.scheme == "https" else 80):
        raise FetchError("only the default port is allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise FetchError("IP address hosts are not allowed")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts):
        raise FetchError(f"host not allowlisted: {host}")
    try:
        addresses = {info[4][0] for info in resolver(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise FetchError(f"DNS lookup failed: {exc}") from exc
    if not addresses:
        raise FetchError("host did not resolve")
    private_ok = host in configured_private_hosts()
    if not all(_allowed_address(address, private_ok) for address in addresses):
        raise FetchError("host resolves to a non-public address")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)


def decode_body(body: bytes, content_type: str = "") -> str:
    """Decode a response body using the declared charset, a BOM, or an HTML meta charset, then UTF-8.

    requests defaults text/* without a charset to ISO-8859-1, which garbles UTF-8 pages, so it is not used.
    """
    declared = _CHARSET_RE.search(content_type.encode("latin-1", errors="ignore"))
    candidates = [declared.group(1).decode("ascii")] if declared else []
    if body.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    elif match := _CHARSET_RE.search(body[:4096]):
        candidates.append(match.group(1).decode("ascii"))
    for name in candidates:
        try:
            return body.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


@dataclass
class FetchedPage:
    url: str
    content_type: str
    text: str


Fetcher = Callable[[str, Sequence[str]], FetchedPage]


def fetch_url(
    url: str,
    allowed_hosts: Sequence[str],
    *,
    session: requests.Session | None = None,
    resolver: Resolver = socket.getaddrinfo,
    max_bytes: int = MAX_FETCH_BYTES,
) -> FetchedPage:
    """Fetch one explicit URL: no auth or cookies, bounded size and time, validated redirects, text types only."""
    own_session = session is None
    session = session or requests.Session()
    if own_session:
        session.trust_env = False
    deadline = time.monotonic() + FETCH_DEADLINE
    try:
        current = validate_url(url, allowed_hosts, resolver)
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = session.get(
                    current,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.8"},
                    timeout=FETCH_TIMEOUT,
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise FetchError(f"request failed: {exc.__class__.__name__}") from exc
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchError("redirect without Location")
                    current = validate_url(urljoin(current, location), allowed_hosts, resolver)
                    continue
                if response.status_code != 200:
                    raise FetchError(f"HTTP {response.status_code}")
                ctype = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype not in ACCEPTED_TYPES:
                    raise FetchError(f"unsupported content type: {ctype or 'none'}")
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise FetchError("response too large")
                body = bytearray()
                try:
                    for piece in response.iter_content(chunk_size=16384):
                        body.extend(piece)
                        if len(body) > max_bytes:
                            raise FetchError("response too large")
                        if time.monotonic() > deadline:
                            raise FetchError("fetch deadline exceeded")
                except requests.RequestException as exc:
                    raise FetchError(f"read failed: {exc.__class__.__name__}") from exc
                text = decode_body(bytes(body), response.headers.get("Content-Type", ""))
                return FetchedPage(current, ctype, text)
            finally:
                response.close()
        raise FetchError("too many redirects")
    finally:
        if own_session:
            session.close()


# ---------------------------------------------------------------------------
# Manifest and refresh
# ---------------------------------------------------------------------------


@dataclass
class Source:
    url: str
    title: str | None
    refresh_hours: float = DEFAULT_REFRESH_HOURS


def load_manifest(path: Path) -> list[Source]:
    """Parse a JSON array of {url, title, refresh_hours}. Any malformed entry rejects the whole file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ManifestError("manifest must be a JSON array")
    if len(data) > MAX_SOURCES:
        raise ManifestError(f"manifest has more than {MAX_SOURCES} entries")
    sources: dict[str, Source] = {}
    for i, entry in enumerate(data):
        url = entry.get("url") if isinstance(entry, dict) else None
        title = entry.get("title") if isinstance(entry, dict) else None
        hours = entry.get("refresh_hours", DEFAULT_REFRESH_HOURS) if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.strip() or not (title is None or isinstance(title, str)):
            raise ManifestError(f"manifest entry {i} needs a string 'url' and optional string 'title'")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
            raise ManifestError(f"manifest entry {i} has an invalid 'refresh_hours'")
        url = urlunsplit(urlsplit(url.strip())._replace(fragment=""))
        sources.setdefault(url, Source(url, _collapse(title)[:200] if title and title.strip() else None, float(hours)))
    return list(sources.values())


@dataclass
class RefreshReport:
    files_updated: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    web_updated: int = 0
    web_unchanged: int = 0
    web_removed: int = 0
    web_skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _import_files(conn: sqlite3.Connection, knowledge_dir: Path, report: RefreshReport) -> None:
    seen: set[str] = set()
    for path in find_markdown_files(knowledge_dir):
        doc_id = file_doc_id(knowledge_dir, path)
        seen.add(doc_id)
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise KnowledgeError("file too large")
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
            title, source, body = parse_markdown(raw, path.stem.replace("-", " ").replace("_", " "))
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(microsecond=0).isoformat()
            result = store_document(
                conn,
                doc_id,
                kind="file",
                title=title,
                text=body,
                url=source,
                path=path.relative_to(knowledge_dir).as_posix(),
                fetched_at=modified,
            )
        except (OSError, KnowledgeError) as exc:
            report.failures.append((doc_id, str(exc)))
            _record_failure(conn, doc_id, str(exc))
            continue
        if result == "updated":
            report.files_updated += 1
        else:
            report.files_unchanged += 1
    stale = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE kind = 'file'") if r["id"] not in seen]
    with conn:
        for doc_id in stale:
            _delete_doc(conn, doc_id)
    report.files_removed = len(stale)


def _import_web(
    conn: sqlite3.Connection,
    sources: list[Source],
    allowed_hosts: Sequence[str],
    fetcher: Fetcher,
    report: RefreshReport,
    force: bool = False,
) -> None:
    wanted = {f"web:{s.url}" for s in sources}
    attempted = {r["id"]: r["last_attempt"] for r in conn.execute("SELECT id, last_attempt FROM documents")}
    now = datetime.now(UTC)
    for source in sources:
        doc_id = f"web:{source.url}"
        last = attempted.get(doc_id)
        # Small slack so an hourly timer does not skip a 1-hour source that finished a few seconds late last time.
        if not force and last and (now - datetime.fromisoformat(last)).total_seconds() < source.refresh_hours * 3600 - 300:
            report.web_skipped += 1
            continue
        try:
            page = fetcher(source.url, allowed_hosts)
            if page.content_type in {"text/html", "application/xhtml+xml"}:
                page_title, text = extract_html(page.text, page.url)
            else:
                page_title, text = "", page.text
            title = source.title or page_title or source.url
            result = store_document(conn, doc_id, kind="web", title=title, text=text, url=source.url, touch_date=True)
        except (OSError, KnowledgeError) as exc:
            report.failures.append((source.url, str(exc)))
            _record_failure(conn, doc_id, str(exc))
            continue
        if result == "updated":
            report.web_updated += 1
        else:
            report.web_unchanged += 1
    stale = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE kind = 'web'") if r["id"] not in wanted]
    with conn:
        for doc_id in stale:
            _delete_doc(conn, doc_id)
    report.web_removed = len(stale)


def refresh(
    db_path: str | Path = DEFAULT_DB,
    *,
    sources_path: str | Path = DEFAULT_SOURCES,
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_DIR,
    allowed_hosts: Sequence[str] | None = None,
    fetcher: Fetcher | None = None,
    force: bool = False,
) -> RefreshReport:
    """Re-import markdown files and manifest pages.

    A page is only re-fetched once its refresh_hours (default DEFAULT_REFRESH_HOURS) have passed since the last
    attempt, so this can run often; force fetches every page.

    A failed fetch or unreadable file keeps the previous good content. Chunks of a document that was
    successfully re-imported are replaced, and documents that no longer exist in the knowledge dir or
    manifest are removed. A missing knowledge dir or manifest skips that part without pruning it; a
    malformed manifest raises ManifestError before anything is changed.
    """
    report = RefreshReport()
    sources_path, knowledge_dir = Path(sources_path), Path(knowledge_dir)
    sources = load_manifest(sources_path) if sources_path.is_file() else None
    hosts = tuple(allowed_hosts) if allowed_hosts is not None else configured_allowed_hosts()
    fetcher = fetcher or fetch_url
    with closing(_open_for_write(Path(db_path))) as conn:
        if knowledge_dir.is_dir():
            _import_files(conn, knowledge_dir, report)
        else:
            report.notes.append(f"knowledge dir not found, skipped files: {knowledge_dir}")
        if sources is not None:
            _import_web(conn, sources, hosts, fetcher, report, force)
        else:
            report.notes.append(f"manifest not found, skipped web sources: {sources_path}")
    return report


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    doc_id: str
    title: str
    heading: str
    text: str
    url: str | None
    path: str | None
    fetched_at: str
    score: float

    @property
    def label(self) -> str:
        if not self.heading or self.heading == self.title:
            return self.title
        if self.heading.startswith(self.title + " > "):
            return self.heading
        return f"{self.title} > {self.heading}"

    @property
    def source(self) -> str:
        return self.url or (f"{self.path} (local file)" if self.path else "local knowledge base")


def search_chunks(query: str, db_path: str | Path, *, limit: int = 5) -> list[SearchResult]:
    """Ranked chunk search. Returns [] for a missing DB or a query with no usable terms.

    At most 3 chunks per document are returned. Pass the newest message first when combining follow-ups,
    since only the first 16 distinct terms are used.
    """
    path = Path(db_path)
    if limit <= 0 or not path.is_file():
        return []
    terms, code_terms = _parse_query(query)
    if not terms:
        return []
    full = " OR ".join(terms)
    # Explicit course codes must be evidenced: first in a title/heading (the course itself), then anywhere in
    # the text. Without evidence nothing is returned rather than falling back to generic matches. Body matches
    # only fill remaining slots from documents the heading pass did not return (e.g. a schedule table), so other
    # catalog entries that merely mention the course do not crowd out the course itself.
    if code_terms:
        codes = " OR ".join(code_terms)
        matches = [f"({full}) AND {{title heading}} : ({codes})", f"({full}) AND {{codes}} : ({codes})"]
    else:
        # Chunks containing every term come first, so one heavily repeated word cannot outrank a full match.
        matches = [" AND ".join(terms), full] if len(terms) > 1 else [full]
    sql = (
        "SELECT c.doc_id, c.heading, c.body, d.title, d.url, d.path, d.fetched_at,"
        " bm25(chunk_fts, ?, ?, ?, ?) AS score"
        " FROM chunk_fts JOIN chunks c ON c.id = chunk_fts.rowid JOIN documents d ON d.id = c.doc_id"
        " WHERE chunk_fts MATCH ? ORDER BY score, c.doc_id, c.ord LIMIT ?"
    )
    results: list[SearchResult] = []
    with closing(_open_for_read(path)) as conn:
        for match in matches:
            if len(results) >= limit:
                break
            earlier = {r.doc_id for r in results}
            per_doc: dict[str, int] = {}
            for row in conn.execute(sql, (*BM25_WEIGHTS, match, MAX_CANDIDATES)):
                if row["doc_id"] in earlier or per_doc.get(row["doc_id"], 0) >= MAX_CHUNKS_PER_DOC_IN_RESULTS:
                    continue
                per_doc[row["doc_id"]] = per_doc.get(row["doc_id"], 0) + 1
                results.append(
                    SearchResult(
                        row["doc_id"], row["title"], row["heading"], row["body"], row["url"], row["path"],
                        row["fetched_at"], row["score"],
                    )
                )  # fmt: skip
                if len(results) >= limit:
                    break
    return results


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 3, 0)]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "..."


def format_context(results: Sequence[SearchResult], max_chars: int = 12000) -> str:
    """Format results as numbered reference excerpts, never exceeding max_chars in total."""
    blocks: list[str] = []
    used = 0
    for number, result in enumerate(results, 1):
        header = (
            f"[{number}] {_truncate(result.label, 200)}\n"
            f"Source: {_truncate(result.source, 500)}\n"
            f"Retrieved: {result.fetched_at[:10]}\n"
        )
        gap = 2 if blocks else 0
        room = max_chars - used - gap - len(header)
        if room < 40:
            break
        block = header + _truncate(result.text, room)
        blocks.append(block)
        used += gap + len(block)
    return "\n\n".join(blocks)


def retrieve_context(query: str, db_path: str | Path, *, limit: int = 5, max_chars: int = 12000) -> str:
    """Reference excerpts for a user question, or '' if there is no DB or no match.

    The text is untrusted reference material: callers should present it as data, not instructions.
    sqlite3.Error from a corrupt or incompatible DB propagates.
    """
    if max_chars <= 0:
        return ""
    results = search_chunks(query, db_path, limit=limit)
    if not results:
        return ""
    # Small FAQ/event pages are useful as a whole: their dates or answers can be in a different
    # section from the matching introduction. Expand those pages, without duplicating them.
    expanded = []
    whole_docs: set[str] = set()
    with closing(_open_for_read(Path(db_path))) as conn:
        for result in results:
            if result.doc_id in whole_docs:
                continue
            size = conn.execute(
                "SELECT SUM(length(heading) + length(body) + 5) FROM chunks WHERE doc_id = ?",
                (result.doc_id,),
            ).fetchone()[0]
            if size is not None and size <= 4000:
                rows = conn.execute(
                    "SELECT heading, body FROM chunks WHERE doc_id = ? ORDER BY ord", (result.doc_id,)
                ).fetchall()
                text = "\n\n".join((f"## {r['heading']}\n" if r["heading"] else "") + r["body"] for r in rows)
                result = replace(result, text=text)
                whole_docs.add(result.doc_id)
            expanded.append(result)
    return format_context(expanded, max_chars)


def knowledge_status(db_path: str | Path) -> dict | None:
    """Summary of stored documents, or None if the DB does not exist."""
    path = Path(db_path)
    if not path.is_file():
        return None
    with closing(_open_for_read(path)) as conn:
        docs = [
            dict(r)
            for r in conn.execute("SELECT id, kind, title, url, fetched_at, last_error FROM documents ORDER BY id")
        ]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return {"documents": docs, "chunks": chunks}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_refresh(args: argparse.Namespace) -> int:
    hosts = configured_allowed_hosts(args.allow_host)
    try:
        report = refresh(
            args.db, sources_path=args.sources, knowledge_dir=args.knowledge_dir, allowed_hosts=hosts, force=args.force
        )
    except KnowledgeError as exc:
        print(f"refresh aborted: {exc}", file=sys.stderr)
        return 1
    print(
        f"files: {report.files_updated} updated, {report.files_unchanged} unchanged, {report.files_removed} removed\n"
        f"web: {report.web_updated} updated, {report.web_unchanged} unchanged, {report.web_removed} removed, "
        f"{report.web_skipped} not due"
    )
    for note in report.notes:
        print(f"note: {note}")
    for name, error in report.failures:
        print(f"failed: {name}: {error}", file=sys.stderr)
    return 1 if report.failures else 0


def _cmd_search(args: argparse.Namespace) -> int:
    if not Path(args.db).is_file():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1
    results = search_chunks(args.query, args.db, limit=args.limit)
    if not results:
        print("no matches")
        return 0
    for number, r in enumerate(results, 1):
        print(f"[{number}] {r.label}  ({r.score:.2f})\n    {r.source}  {r.fetched_at[:10]}")
        print("    " + _truncate(_collapse(r.text), 300))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    info = knowledge_status(args.db)
    if info is None:
        print(f"no database at {args.db}", file=sys.stderr)
        return 1
    print(f"{len(info['documents'])} documents, {info['chunks']} chunks")
    for doc in info["documents"]:
        line = f"{doc['id']}  {doc['title']}  {doc['fetched_at'][:10]}"
        if doc["last_error"]:
            line += f"  LAST ERROR: {doc['last_error']}"
        print(line)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Local knowledge base for the Rocky Discord bot")
    sub = parser.add_subparsers(dest="command", required=True)
    p_refresh = sub.add_parser("refresh", help="import markdown files and manifest pages")
    p_refresh.add_argument("--sources", default=DEFAULT_SOURCES)
    p_refresh.add_argument("--knowledge-dir", default=DEFAULT_KNOWLEDGE_DIR)
    p_refresh.add_argument("--db", default=DEFAULT_DB)
    p_refresh.add_argument("--force", action="store_true", help="fetch every page, even if not due")
    p_refresh.add_argument("--allow-host", action="append", default=[], help="extra allowlisted host (repeatable)")
    p_refresh.set_defaults(func=_cmd_refresh)
    p_search = sub.add_parser("search", help="search the knowledge base")
    p_search.add_argument("query")
    p_search.add_argument("--db", default=DEFAULT_DB)
    p_search.add_argument("--limit", type=int, default=5)
    p_search.set_defaults(func=_cmd_search)
    p_status = sub.add_parser("status", help="show stored documents")
    p_status.add_argument("--db", default=DEFAULT_DB)
    p_status.set_defaults(func=_cmd_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
