"""Small, frequently changing pages fetched at most hourly and added to every matching reply.

Large or slow-changing sources belong in knowledge/sources.json instead; those are searched, not always sent.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import aiohttp

from hacksu_meetings import HOME_URL, MEETINGS_URL, format_schedule, parse_meetings, parse_next

CACHE_SECONDS = 3600
MAX_PAGE_BYTES = 1_000_000
TUTORING_URL = "http://antares.cs.kent.edu/~tutoring/schedule.txt"  # its HTTPS certificate is for another host
SEMINAR_URL = "https://industry.cs.kent.edu/seminar/"
SEMINAR_DATA_URL = SEMINAR_URL + "js/data.js"
SEMINAR_PAST = 3

log = logging.getLogger("rocky-bot")

TUTOR_DAY = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b")
TUTOR_SLOT = re.compile(r"^(?:\w{3})?\s+(\d[\d:]*\s*-\s*\d[\d:]*)(.*)$")
JS_STRING = r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\""""
JS_FIELD = re.compile(rf"(\w+):\s*(\[[^\]]*\]|{JS_STRING})")


def render_tutoring(pages: list[str], today: date) -> str:
    """Rewrite the column table ('Mon 2-3:30 <online> <inperson>') as explicit lines the model cannot misalign."""
    lines = pages[0].expandtabs(8).splitlines()
    header = next((i for i, l in enumerate(lines) if "online" in l and "inperson" in l), None)
    if header is None:
        return ""
    online, inperson = lines[header].index("online"), lines[header].index("inperson")
    out, day = [line.rstrip() for line in lines[:header] if line.strip()], ""
    rows = lines[header + 1 :]
    for i, line in enumerate(rows):
        if not line.strip():
            continue
        if m := TUTOR_DAY.match(line):
            day = m[1]
        slot = TUTOR_SLOT.match(line)
        if not slot:
            if day and not line.startswith(" ") and len(line.split()) == 2:
                out.append(f"{day}: {line.split()[1]}")  # e.g. "Sat  off"
                continue
            out += [r.rstrip() for r in rows[i:] if r.strip()]  # notes after the table
            break
        who = []
        for name in re.finditer(r"\S+", slot[2]):
            col = slot.start(2) + name.start()
            who.append(f"{name[0]} ({'online' if abs(col - online) < abs(col - inperson) else 'in person'})")
        out.append(f"{day} {slot[1]}: {', '.join(who) or 'nobody scheduled'}")
    return f"CS tutoring schedule (checked hourly from {TUTORING_URL}):\n" + "\n".join(out)


def _js_strings(value: str) -> list[str]:
    return [re.sub(r"\\(.)", r"\1", a or b) for a, b in re.findall(JS_STRING, value)]


def render_seminars(pages: list[str], today: date) -> str:
    """Parse the seminar page's talk registry (one {date, speaker, role, title} object per talk)."""
    talks = []
    body = re.sub(r"^\s*//.*$", "", pages[0], flags=re.MULTILINE)
    for entry in re.findall(r"\{([^{}]*)\}", body):
        fields = {m[1]: ", ".join(_js_strings(m[2])) for m in JS_FIELD.finditer(entry)}
        try:
            day = date.fromisoformat(fields.get("date", ""))
        except ValueError:
            continue
        talks.append((day, fields))
    if not talks:
        return ""
    talks.sort(key=lambda t: t[0])

    def line(day: date, f: dict[str, str]) -> str:
        who = f.get("speaker") or "TBA"
        if f.get("role"):
            who += f" ({f['role']})"
        return f"- {day:%a %b} {day.day}, {day.year}: {who}: {f.get('title') or 'untitled'}"

    upcoming = [line(*t) for t in talks if t[0] >= today]
    past = [line(*t) for t in talks if t[0] < today][-SEMINAR_PAST:]
    lines = [f"Kent State CS Industry Partnership Seminar Series (checked hourly from {SEMINAR_URL}):"]
    lines += ["Upcoming:", *upcoming] if upcoming else ["No upcoming talks listed yet."]
    if past:
        lines += ["Recent past:", *past]
    return "\n".join(lines)


def render_meetings(pages: list[str], today: date) -> str:
    semester, meetings = parse_meetings(pages[0])
    return format_schedule(semester, meetings, parse_next(pages[1]), today)


@dataclass
class LiveSource:
    urls: tuple[str, ...]
    render: Callable[[list[str], date], str]
    when: re.Pattern | None = None  # only sent when the conversation matches; None means always
    pages: list[str] = field(default_factory=list)
    fetched: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def fetch(self, session: aiohttp.ClientSession) -> list[str]:
        pages = []
        for url in self.urls:
            async with session.get(url) as r:
                r.raise_for_status()
                body = bytearray()
                async for piece in r.content.iter_chunked(65536):
                    body += piece
                    if len(body) > MAX_PAGE_BYTES:
                        raise aiohttp.ClientPayloadError(f"{url} is over {MAX_PAGE_BYTES} bytes")
                pages.append(body.decode(r.charset or "utf-8", errors="replace"))
        return pages

    async def get(self, session: aiohttp.ClientSession, today: date) -> str:
        async with self.lock:
            if time.monotonic() - self.fetched > CACHE_SECONDS:
                # Back off on failure too; stale data beats stalling every reply on a dead site.
                self.fetched = time.monotonic()
                try:
                    pages = await self.fetch(session)
                    # A redesigned page that no longer parses keeps the last copy that did.
                    if self.render(pages, today) or not self.pages:
                        self.pages = pages
                except (aiohttp.ClientError, TimeoutError, UnicodeError, LookupError):
                    log.warning("Live fetch of %s failed; using cached copy", self.urls[0], exc_info=True)
        return self.render(self.pages, today) if self.pages else ""


def default_sources() -> list[LiveSource]:
    return [
        LiveSource((MEETINGS_URL, HOME_URL), render_meetings),
        LiveSource(
            (TUTORING_URL,),
            render_tutoring,
            re.compile(r"tutor|office hours|msb 243|\blabs?\b|homework help|\bhelp with\b", re.IGNORECASE),
        ),
        LiveSource(
            (SEMINAR_DATA_URL,),
            render_seminars,
            re.compile(r"seminar|speaker|industry|guest|career|recruit|\btalks?\b", re.IGNORECASE),
        ),
    ]


class LiveInfo:
    def __init__(self, sources: list[LiveSource] | None = None) -> None:
        self.sources = default_sources() if sources is None else sources

    async def get(self, query: str, today: date) -> str:
        wanted = [s for s in self.sources if s.when is None or s.when.search(query)]
        if not wanted:
            return ""
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "RockyBot (Kent State CS Discord bot)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            blocks = await asyncio.gather(*(s.get(session, today) for s in wanted))
        return "\n\n".join(b for b in blocks if b)
