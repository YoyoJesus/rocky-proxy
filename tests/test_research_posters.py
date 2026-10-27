import tempfile
import unittest
from pathlib import Path

import knowledge_base as kb
import research_posters as rp

LABS = (
    "<h1>Research Labs</h1><ul>"
    '<li><a href="http://www.atr.cs.kent.edu/">Advanced Tele-Robotics Lab (ATR)</a> - Dr. Jong-Hoon Kim</li>'
    "<li>Smart Communities and IoT (SCI) Lab - Dr. Jungyoon Kim</li>"
    "<li>Distributed Systems Lab - Dr. Mikhail Nesterenko</li>"
    "<li>Networking and Media Communications Research (MediaNet) Laboratory - Dr. Javed I. Khan</li>"
    "<li>Cybersecurity and Intelligence Analytics Lab (CIA) - Dr. Younghun Chae and Dr. Angela Guercio (Stark)</li>"
    "<li>Advanced Information Security and Privacy (AISP) Lab - TBA</li></ul>"
)
INDEX = (
    '<h1>Creative Poster Projects</h1><p><a href="https://www.kent.edu/cs/global-circ/2025-cof-poster-conference">'
    '2025</a></p><p><a href="https://www.kent.edu/cs/global-circ/2021-cof-research-poster-project">2021</a></p>'
)
YEAR_2025 = '<h1>2025 COF Poster Conference</h1><h2><a href="/cs/global-circ/2025-ksu-research-posters">Posters</a></h2>'
POSTERS_2025 = (
    "<h1>2025 KSU Research Posters</h1>"
    "<p>Value-Dependent Blockchain Confirmation</p><p>Document</p><p>2025 Nesterenko Hicks.pptx (1 MB)</p>"
    "<p>Political AI</p><p>Document</p><p>2025 JH Kim Grant.pptx (260.82 KB)</p>"
    "<p>Reducing Cybersickness in VR</p><p>Document</p><p>2025 JY Kim Kliskey.pdf (2.76 MB)</p>"
    "<p>Formal Verification of RSA</p><p>Document</p><p>2025 Allouzi Shaikh.pdf (185.24 KB)</p>"
)
POSTERS_2021 = (
    "<h1>2021 COF Research Poster Project</h1><ul>"
    "<li>Automation Robotic Control, Danny McCrea, Advisor: Dr. Jong-Hoon Kim, 2021 COF Research Showcase "
    'Conference, April 2021 [<a href="https://www.kent.edu/cs/global-circ/automation">ppt</a>]</li>'
    "<li>VR in the Courtroom, Makayla Henninge<br>Advisors: Dr. Angela Guercio, Dr. Younghun Chae</li>"
    "<li>Workflow Mapping, Avi Rathod, Advisor: Dr. Javed Khan</li></ul>"
)
PAGES = {
    rp.LABS_URL: LABS,
    rp.INDEX_URL: INDEX,
    "https://www.kent.edu/cs/global-circ/2025-cof-poster-conference": YEAR_2025,
    "https://www.kent.edu/cs/global-circ/2025-ksu-research-posters": POSTERS_2025,
    "https://www.kent.edu/cs/global-circ/2021-cof-research-poster-project": POSTERS_2021,
}


def fetcher(url, hosts):
    if url not in PAGES:
        raise kb.FetchError("HTTP 404")
    return kb.FetchedPage(url, "text/html", PAGES[url])


def section(digest: str, heading: str) -> str:
    return digest.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


class DigestTests(unittest.TestCase):
    def setUp(self):
        text = lambda url: kb.extract_html(PAGES[url], url)[1]  # noqa: E731
        pages = [(2025, u, text(u)) for u in list(PAGES)[3:4]] + [(2021, u, text(u)) for u in list(PAGES)[4:]]
        self.digest = rp.build_digest(text(rp.LABS_URL), pages)

    def test_file_listings_match_lab_directors(self):
        self.assertIn("Value-Dependent Blockchain Confirmation (file: 2025 Nesterenko Hicks.pptx)",
                      section(self.digest, "Distributed Systems Lab"))  # fmt: skip
        self.assertIn("Political AI", section(self.digest, "Advanced Tele-Robotics Lab"))
        self.assertIn("Reducing Cybersickness", section(self.digest, "Smart Communities"))

    def test_named_advisors_match_without_middle_initials(self):
        medianet = section(self.digest, "Networking and Media")
        self.assertIn("Workflow Mapping, Avi Rathod", medianet)

    def test_wrapped_advisor_line_joins_its_poster(self):
        cia = section(self.digest, "Cybersecurity")
        self.assertIn("VR in the Courtroom, Makayla Henninge, Advisors: Dr. Angela Guercio", cia)
        self.assertNotIn("Automation Robotic Control", cia)

    def test_boilerplate_removed(self):
        self.assertIn("- 2021: Automation Robotic Control, Danny McCrea, Advisor: Dr. Jong-Hoon Kim\n", self.digest)

    def test_unknown_advisor_goes_to_other(self):
        self.assertIn("2025: Allouzi: Formal Verification of RSA", section(self.digest, "Posters advised by faculty"))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db, self.manifest = root / "kb.sqlite3", root / "sources.json"
        self.manifest.write_text(f'[{{"url": "{rp.LABS_URL}"}}, {{"url": "{rp.INDEX_URL}"}}]', encoding="utf-8")

    def refresh(self, fetch=fetcher, force=False):
        return kb.refresh(self.db, sources_path=self.manifest, knowledge_dir=Path(self.tmp.name) / "none",
                          allowed_hosts=("kent.edu",), fetcher=fetch, force=force)  # fmt: skip

    def test_digest_follows_sub_pages_and_is_searchable(self):
        report = self.refresh()
        self.assertEqual(report.failures, [])
        results = kb.search_chunks("bitcoin blockchain confirmation lab", self.db)
        self.assertEqual(results[0].doc_id, kb.POSTER_DOC_ID)

    def test_failed_page_keeps_previous_digest(self):
        self.refresh()

        def broken(url, hosts):
            if "2021" in url:
                raise kb.FetchError("HTTP 500")
            return fetcher(url, hosts)

        report = self.refresh(broken, force=True)
        self.assertEqual(report.failures[0][0], kb.POSTER_DOC_ID)
        self.assertTrue(kb.search_chunks("Automation Robotic Control", self.db))

    def test_not_rebuilt_until_due(self):
        self.refresh()
        self.assertEqual(self.refresh(lambda u, h: self.fail("fetched")).web_skipped, 3)
