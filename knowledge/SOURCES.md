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
  (Nesterenko) and a page without readable text (Sharma).
  New faculty are not added automatically; recheck the directory each semester.
- Current class schedule (sections, CRNs, days/times, instructors, rooms, enrollment): `web.cs.kent.edu/schedule/`.
  It lists one semester (Fall 2026 when added on October 26, 2026) and enrollment numbers change daily.
  `web.cs.kent.edu` only resolves on Kent State's network (to a private address), so it is the one host in
  `DEFAULT_PRIVATE_HOSTS`; refreshing from off campus fails for it and keeps the last good copy.
- CS tutoring schedule: `antares.cs.kent.edu/~tutoring/schedule.txt` (plain text, linked from the tutoring page).
  Its HTTPS certificate names another host, so it is fetched over HTTP via `DEFAULT_HTTP_HOSTS`.
- Industry connections (added October 27, 2026): `industry.cs.kent.edu`, the career fair and internship guide
  subdomains, and the seminar speaker registry `industry.cs.kent.edu/seminar/js/data.js`, which the seminar page
  renders with JavaScript. The seminar page has no time or room; the alumni page is only a Google Form and is skipped.
- Course descriptions and degree requirements: `catalog.kent.edu`, currently under `/colleges/sh/cs/`.
  Legacy `/colleges/as/cs/` URLs may no longer work. Requirements depend on a student's catalog year;
  these URLs track the current catalog, not every historical catalog.
- Club membership, leadership, meeting archive, resources and contact: `hacksu.com`.
- Hackathon dates, venue and participation FAQ: `khe.io`.
- CTF overview, event dates and public Kali setup guide: `ctf.hacksu.com`.
- Public repositories: `github.md` lists highlights and every public non-fork repository in `github.com/hacksu`
  (185 on October 26, 2026) with its description and last-updated year. The README of every one of those
  repositories with a README of at least 200 bytes is fetched from `raw.githubusercontent.com` (136, including old
  and archived lessons, titled with their last-updated year), plus the club constitution (`hacksu/constitution`,
  Typst source). New repositories are not added automatically. Source links to raw Markdown are public.
- Bot identity: `rocky-the-flying-squirrel.md` summarizes the Wikipedia article on the character the bot's name
  comes from. Short always-on facts (creator, nicknames, Discord servers, tutoring) live in `../bot_facts.md`, not here.

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
