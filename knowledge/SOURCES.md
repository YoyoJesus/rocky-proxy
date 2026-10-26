# Source selection notes

Researched October 21, 2026 with Gemini; imported pages are checked again by the refresh command.
The source list covers public Kent State CS department pages, current university catalog pages,
HacKSU club pages, Kent Hack Enough (`khe.io`), and Kent Hack It (`ctf.hacksu.com`).

## Canonical sources

- Department advising, tutoring, faculty/office hours, contact information and FAQs: `www.kent.edu/cs`.
- Faculty and staff: every page of the `/cs/faculty-staff` directory, each linked profile page, and
  personal faculty websites on `cs.kent.edu` and `personal.kent.edu` hosts that load over HTTPS. Added October 26, 2026.
  Most profile pages only list title, office, contact and education; fuller bios and research descriptions
  come from personal sites, which some faculty no longer update. Skipped sites: those returning 403
  (Melton, Guo), off-campus hosts (Kwangtaek Kim's Google Site, raifulhasan.com), an HTTP-only redirect
  (Nesterenko), a host resolving to a private address (Bhati) and a page without readable text (Sharma).
  New faculty are not added automatically; recheck the directory each semester.
- Course descriptions and degree requirements: `catalog.kent.edu`, currently under `/colleges/sh/cs/`.
  Legacy `/colleges/as/cs/` URLs may no longer work. Requirements depend on a student's catalog year;
  these URLs track the current catalog, not every historical catalog.
- Club membership, leadership, meeting archive, resources and contact: `hacksu.com`.
- Hackathon dates, venue and participation FAQ: `khe.io`.
- CTF overview, event dates and public Kali setup guide: `ctf.hacksu.com`.
- Public repositories: a curated directory in `github.md` linking to `github.com/hacksu`, with selected READMEs fetched from
  `raw.githubusercontent.com`. These cover the club/event websites, Flask, FastAPI/Gemini,
  MongoDB, an AI workshop and the presentation template. Source links to raw Markdown are public.

## Caveats

- Office hours and tutoring schedules can change by semester. A recent download does not establish
  that a page's schedule is current. Cite the source and any semester shown in it.
- Event pages and archives may contain past dates. Compare actual dates with today before describing
  an event as upcoming. The bot receives today's date in Eastern time.
- Gemini found that `/lessons` depends on client rendering; `/meetings` provides a readable archive.
  HTML extraction does not execute JavaScript, so interactive content may be missing.
- Useful links are retained as references; linked pages are not automatically crawled. Registration,
  sign-in, competition accounts, student records and private Discord content are not imported.
- The undergraduate advising page selected is `/cs/computer-science-advising`; an alternative
  `/cs/academic-advising-undergraduates` URL failed during research.
- Repository files are explicit entries, not an automatic clone of every repository. Add more
  public README or documentation URLs to expand coverage. READMEs track the default branch
  selected during research (`main`) and may contain old lesson advice or promotions.
- The university organization portal is omitted in favor of focused first-party pages.

This file documents source choices and is excluded from the searchable index. Use `status` to
inspect actual download dates and errors; no availability claim here replaces refresh output.
