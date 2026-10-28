import re
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import live_info

TUTORING = (
    "FALL 2026 CS TUTORING SCHEDULE\n\nIN PERSON (Room MSB 243)\n\n"
    "\t     online    inperson\n"
    "Mon  1-2              \n"
    "     2-3:30            TJ\n"
    "     5-7\t       Ben \n"
    "     7-9     Simon\n\n"
    "Tue  1-3     Sumedh    Nate     \n\n"
    "Sat  off\n\n\n"
    "CS2 Lab (162 MSB):\n"
    "Section 001:    T  12:05pm-01:45pm  - Simon\n"
)
TODAY = date(2026, 10, 27)


class TutoringTests(unittest.TestCase):
    def test_columns_become_explicit_modes(self):
        text = live_info.render_tutoring([TUTORING], TODAY)
        self.assertIn("Mon 1-2: nobody scheduled", text)
        self.assertIn("Mon 2-3:30: TJ (in person)", text)
        self.assertIn("Mon 5-7: Ben (in person)", text)  # tab-aligned row
        self.assertIn("Mon 7-9: Simon (online)", text)
        self.assertIn("Tue 1-3: Sumedh (online), Nate (in person)", text)
        self.assertIn("Sat: off", text)
        self.assertIn("Section 001:    T  12:05pm-01:45pm  - Simon", text)
        self.assertIn("IN PERSON (Room MSB 243)", text)

    def test_unrecognized_layout(self):
        self.assertEqual(live_info.render_tutoring(["Tutoring is cancelled this week."], TODAY), "")


SEMINARS = r"""// data.js: speaker registry
// add { date: '2099-01-01' } entries below
export const talks = [
  // ---- Fall 2026 ----
  { date: '2026-11-04', speaker: ['Ada Lovelace', 'Grace O\'Hopper'], title: "Compilers, \"briefly\"" },
  {
    date: '2026-10-13',
    speaker: 'NASA Lab',
    role: 'NASA Computer scientist',
    title: 'Agents',
    photos: ['NASA.jpg'],
  },
  { date: '2026-09-01', speaker: 'NSA Liaison', title: 'IT Careers' },
  { date: '2026-08-25', speaker: 'Kept', title: 'Third' },
  { date: '2026-08-18', speaker: 'Too Old', title: 'Dropped' },
  { date: 'TBD', speaker: 'Undated', title: 'Skipped' },
];
"""


class SeminarTests(unittest.TestCase):
    def test_upcoming_and_recent_talks(self):
        text = live_info.render_seminars([SEMINARS], TODAY)
        upcoming, past = text.split("Recent past:")
        self.assertIn("Wed Nov 4, 2026: Ada Lovelace, Grace O'Hopper: Compilers, \"briefly\"", upcoming)
        self.assertIn("NASA Lab (NASA Computer scientist): Agents", past)
        self.assertIn("NSA Liaison: IT Careers", past)
        self.assertNotIn("Too Old", text)
        self.assertNotIn("Undated", text)
        self.assertNotIn("2099", text)

    def test_nothing_upcoming(self):
        self.assertIn("No upcoming talks", live_info.render_seminars([SEMINARS], date(2027, 1, 1)))

    def test_unrecognized_file(self):
        self.assertEqual(live_info.render_seminars(["export const talks = [];"], TODAY), "")


class Source(live_info.LiveSource):
    def __init__(self, pages, when=None):
        super().__init__(("https://example.edu/x",), lambda p, today: p[0], when)
        self.responses = list(pages)
        self.calls = 0

    async def fetch(self, session):
        self.calls += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return [result]


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_for_an_hour(self):
        source = Source(["first", "second"])
        info = live_info.LiveInfo([source])
        self.assertEqual(await info.get("q", TODAY), "first")
        self.assertEqual(await info.get("q", TODAY), "first")
        self.assertEqual(source.calls, 1)
        source.fetched -= live_info.CACHE_SECONDS + 1
        self.assertEqual(await info.get("q", TODAY), "second")

    async def test_failure_keeps_last_copy(self):
        source = Source(["first", TimeoutError()])
        info = live_info.LiveInfo([source])
        await info.get("q", TODAY)
        source.fetched = float("-inf")
        with self.assertLogs("rocky-bot", level="WARNING"):
            self.assertEqual(await info.get("q", TODAY), "first")

    async def test_unparseable_page_keeps_last_copy(self):
        source = Source(["first", ""])
        info = live_info.LiveInfo([source])
        await info.get("q", TODAY)
        source.fetched = float("-inf")
        self.assertEqual(await info.get("q", TODAY), "first")

    async def test_first_fetch_soon_after_boot(self):
        source = Source(["first"])
        with patch.object(live_info, "time", SimpleNamespace(monotonic=lambda: 60.0)):
            self.assertEqual(await live_info.LiveInfo([source]).get("q", TODAY), "first")

    async def test_only_matching_sources_are_sent(self):
        always, tutoring = Source(["meetings"]), Source(["tutoring"], re.compile("tutor", re.I))
        info = live_info.LiveInfo([always, tutoring])
        self.assertEqual(await info.get("what is express?", TODAY), "meetings")
        self.assertEqual(tutoring.calls, 0)
        self.assertEqual(await info.get("When is Tutoring?", TODAY), "meetings\n\ntutoring")


if __name__ == "__main__":
    unittest.main()
