"""Current HacKSU meeting schedule from hacksu.com, parsed down to the current semester."""

import html
import re
from dataclasses import dataclass
from datetime import date, datetime

MEETINGS_URL = "https://hacksu.com/meetings"
HOME_URL = "https://hacksu.com"
SHOW_PAST = 2

DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
SECTION = re.compile(r'<section class="meeting-group[^"]*"[^>]*>(.*?)</section>', re.DOTALL)
CARD = re.compile(r'<div class="relative z-10[^"]*">(.*?)(?=<div class="relative z-10|$)', re.DOTALL)
TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL)
LINK = re.compile(r'<a href="(https?://[^"]+)"')
CARD_DATE = re.compile(rf"<strong>{DATE}</strong>")
PRESENTER = re.compile(r"Presented by <strong>(.*?)</strong>", re.DOTALL)
NEXT = re.compile(rf"<h2[^>]*>(.*?)</h2>.*?<strong>{DATE}</strong>\s*at\s*([^<]+?)\s*</span>\s*<strong>(.*?)</strong>", re.DOTALL)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


@dataclass
class Meeting:
    title: str
    day: date
    presenter: str = ""
    link: str = ""


def parse_meetings(page: str) -> tuple[str, list[Meeting]]:
    """Return the newest semester's name and meetings, oldest first."""
    section = SECTION.search(page)
    if not section:
        return "", []
    semester = _text(TITLE.search(section[1])[1]) if TITLE.search(section[1]) else ""
    meetings = []
    for card in CARD.findall(section[1]):
        title, when = TITLE.search(card), CARD_DATE.search(card)
        if not title or not when:
            continue
        link = LINK.search(card[: title.start()])
        presenter = PRESENTER.search(card)
        meetings.append(
            Meeting(
                _text(title[1]),
                datetime.strptime(when[1], "%B %d, %Y").date(),
                _text(presenter[1]) if presenter else "",
                link[1] if link else "",
            )
        )
    return semester, sorted(meetings, key=lambda m: m.day)


def parse_next(page: str) -> str:
    """The homepage's next-meeting card, the only place that lists time and room."""
    m = NEXT.search(page)
    return f"{_text(m[1])}, {m[2]} at {_text(m[3])}, {_text(m[4])}" if m else ""


def format_schedule(semester: str, meetings: list[Meeting], next_meeting: str, today: date) -> str:
    upcoming = [m for m in meetings if m.day >= today]
    past = [m for m in meetings if m.day < today][-SHOW_PAST:]
    if not upcoming and not past:
        return ""

    def line(m: Meeting) -> str:
        text = f"- {m.day:%a %b} {m.day.day}, {m.day.year}: {m.title}"
        if m.presenter:
            text += f" (presented by {m.presenter})"
        return text + (f" {m.link}" if m.link else "")

    lines = [f"HacKSU meetings, {semester or 'current semester'} (checked hourly from {MEETINGS_URL}):"]
    if next_meeting:
        lines.append(f"Next meeting per {HOME_URL}: {next_meeting}")
    lines += ["Upcoming:", *map(line, upcoming)] if upcoming else ["No more meetings listed this semester."]
    if past:
        lines += ["Recent past:", *map(line, past)]
    return "\n".join(lines)

