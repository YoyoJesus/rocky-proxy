# Bot knowledge

`sources.json` lists public HTML pages and plain-text Markdown URLs to import. Add explicit URLs and titles there, then run
`uv run knowledge_base.py refresh` from the repository root. The importer does not follow links
or fetch arbitrary URLs supplied by Discord users. See `SOURCES.md` for source research notes.

To add club knowledge that is missing online, create a Markdown file in this directory:

```markdown
Source: https://example.org/your-public-faq
# Topic title

## A common question
Your reviewed answer, with relevant dates or semester information.
```

Replace the example URL with an actual supporting public URL, or omit the `Source:` line
for local notes. Local notes without a public URL cannot provide a clickable public citation.
The importer skips this README and SOURCES.md. Keep each section focused on one topic.
After editing a file, run refresh again. Include catalog years for requirements and full
dates for events. Do not put tokens, student records, private-channel content, or personal
information in these files: indexed knowledge can be used in any channel or DM the bot answers.

The database is generated under `data/` and is ignored by Git. Refresh it on each bot host.
An imported page is a snapshot, not a live lookup. Refresh regularly and correct inaccurate
information at its source. Scheduled refresh is optional; no background task is installed.
