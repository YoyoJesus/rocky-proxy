import unittest
from datetime import date

import hacksu_meetings as hm


def card(title: str, when: str, presenter: str = "", link: str = "") -> str:
    head = f'<a href="{link}" class="x"><h2 class="t">{title}</h2></a>' if link else f'<h2 class="t">{title}</h2>'
    by = f'<p class="m-0 text-right">Presented by <strong>{presenter}</strong></p>' if presenter else ""
    return f'<div class="relative z-10 svelte-1"><div>{head}<p class="m-0"><strong>{when}</strong></p>{by}</div></div>'


PAGE = (
    '<main><section class="meeting-group svelte-1"><h2 class="page-title">Fall 2026<!--[-1--></h2><div>'
    + card("Unity", "December 1, 2026", "Conner Brunner")
    + card("Express.js", "October 27, 2026")
    + card("Hacker &amp; You", "October 13, 2026", "Noah", "https://github.com/hacksu/hacker")
    + card("Old", "October 6, 2026")
    + card("Older", "September 29, 2026")
    + '</div></section><section class="meeting-group svelte-1"><h2 class="page-title">Spring 2026</h2>'
    + card("Last term", "April 1, 2026")
    + "</section></main>"
)
HOME = (
    '<h2 class="m-0">Express.js</h2></span></div> <div class="mt-1"><span><strong>October 27, 2026</strong> at 7:00 PM'
    "</span> <strong>MSB 228</strong></div>"
)


class MeetingTests(unittest.TestCase):
    def test_parses_only_newest_semester(self):
        semester, meetings = hm.parse_meetings(PAGE)
        self.assertEqual(semester, "Fall 2026")
        self.assertEqual([m.title for m in meetings], ["Older", "Old", "Hacker & You", "Express.js", "Unity"])
        self.assertEqual(meetings[2].presenter, "Noah")
        self.assertEqual(meetings[2].link, "https://github.com/hacksu/hacker")
        self.assertEqual(meetings[-1].day, date(2026, 12, 1))

    def test_next_meeting_has_time_and_room(self):
        self.assertEqual(hm.parse_next(HOME), "Express.js, October 27, 2026 at 7:00 PM, MSB 228")

    def test_upcoming_uses_today_not_page_badges(self):
        _, meetings = hm.parse_meetings(PAGE)
        text = hm.format_schedule("Fall 2026", meetings, "", date(2026, 10, 28))
        upcoming, past = text.split("Recent past:")
        self.assertIn("Unity (presented by Conner Brunner)", upcoming)
        self.assertNotIn("Express.js", upcoming)
        self.assertIn("Express.js", past)
        self.assertIn("Hacker & You", past)
        self.assertNotIn("Old", past.replace("Older", ""))
        self.assertNotIn("Older", text)

    def test_semester_over(self):
        _, meetings = hm.parse_meetings(PAGE)
        self.assertIn("No more meetings", hm.format_schedule("Fall 2026", meetings, "", date(2026, 12, 20)))

    def test_unrecognized_page(self):
        self.assertEqual(hm.parse_meetings("<html></html>"), ("", []))
        self.assertEqual(hm.format_schedule("", [], "", date(2026, 1, 1)), "")


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_fetch_keeps_last_schedule(self):
        schedule = hm.MeetingSchedule()
        schedule.semester, schedule.meetings = hm.parse_meetings(PAGE)

        async def fail():
            raise TimeoutError

        schedule.refresh = fail
        with self.assertLogs("rocky-bot", level="WARNING"):
            text = await schedule.get(date(2026, 10, 26))
        self.assertIn("Express.js", text)
        schedule.refresh = None  # cached now; must not refetch
        await schedule.get(date(2026, 10, 26))


if __name__ == "__main__":
    unittest.main()
