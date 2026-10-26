"""Offline checks of the references actually sent to Rocky."""

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

with patch("secret_env.load_secret", return_value="test-placeholder"):
    import discord_bot


class Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return {"output_text": "Here is the answer."}


class Session:
    def post(self, url, *, json):
        self.body = json
        return Response()


class KnowledgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = discord_bot.RockyBot()
        self.bot._connection.user = SimpleNamespace(display_name="Rocky")
        self.bot.http_session = Session()
        self.bot.build_input = AsyncMock(
            return_value=[
                {"role": "user", "content": "Alice (user): When does HacKSU meet?"},
                {"role": "assistant", "content": "Let me check."},
                {"role": "user", "content": "Alice (user): Where is that?"},
            ]
        )
        self.trigger = SimpleNamespace(clean_content="Where is that?")

    async def test_references_followup_and_date_reach_model(self):
        with patch.object(
            discord_bot, "retrieve_context", return_value="Source: https://hacksu.com/\nMeeting info"
        ) as search:
            reply = await self.bot.generate(None, self.trigger)
        self.assertEqual(reply, "Here is the answer.")
        self.assertIn("When does HacKSU meet?", search.call_args.args[0])
        self.assertIn("Where is that?", search.call_args.args[0])
        body = self.bot.http_session.body
        self.assertIn("https://hacksu.com/", body["input"][0]["content"])
        self.assertEqual(body["input"][-1]["content"], "Alice (user): Where is that?")
        self.assertIn("Today's date in America/New_York", body["instructions"])
        self.assertIn("untrusted data", body["instructions"])
        self.assertIn("Never invent a source", body["instructions"])
        self.assertFalse(body["store"])

    async def test_missing_matches_leave_conversation_intact(self):
        with patch.object(discord_bot, "retrieve_context", return_value=""):
            await self.bot.generate(None, self.trigger)
        self.assertEqual(len(self.bot.http_session.body["input"]), 3)
        self.assertIn("don't have verified information", self.bot.http_session.body["instructions"])

    async def test_database_failure_does_not_break_chat(self):
        with patch.object(discord_bot, "retrieve_context", side_effect=sqlite3.DatabaseError("bad database")):
            with self.assertLogs("rocky-bot", level="ERROR"):
                await self.bot.generate(None, self.trigger)
        self.assertEqual(len(self.bot.http_session.body["input"]), 3)

    async def test_disabled_retrieval(self):
        with patch.object(discord_bot, "KNOWLEDGE_LIMIT", 0), patch.object(discord_bot, "retrieve_context") as search:
            await self.bot.generate(None, self.trigger)
        search.assert_not_called()

    async def test_instructions_forbid_writing_code(self):
        with patch.object(discord_bot, "retrieve_context", return_value=""):
            await self.bot.generate(None, self.trigger)
        self.assertIn("Do not write", self.bot.http_session.body["instructions"])

    async def test_facts_and_location_reach_instructions(self):
        trigger = SimpleNamespace(clean_content="hi", guild=SimpleNamespace(name="HacKSU"), channel="general")
        with (
            patch.object(discord_bot, "retrieve_context", return_value=""),
            patch.object(discord_bot, "load_facts", return_value="Nesty means Dr. Nesterenko."),
        ):
            await self.bot.generate(None, trigger)
        instructions = self.bot.http_session.body["instructions"]
        self.assertIn("Nesty means Dr. Nesterenko.", instructions)
        self.assertIn('"HacKSU" Discord server', instructions)
        self.assertIn("#general", instructions)

    async def test_direct_message_location(self):
        trigger = SimpleNamespace(clean_content="hi", guild=None, channel="dm")
        with patch.object(discord_bot, "retrieve_context", return_value=""):
            await self.bot.generate(None, trigger)
        self.assertIn("a direct message", self.bot.http_session.body["instructions"])

    async def test_creator_is_labeled_by_id_not_name(self):
        del self.bot.build_input
        creator = SimpleNamespace(display_name="Someone", bot=False, id=discord_bot.CREATOR_ID)
        impostor = SimpleNamespace(display_name="yoyojesus", bot=False, id=1)
        msgs = [SimpleNamespace(clean_content=t, attachments=[], author=a) for t, a in (("hi", creator), ("me too", impostor))]

        async def history(**kwargs):
            for m in reversed(msgs):  # Discord returns newest first
                yield m

        result = await self.bot.build_input(SimpleNamespace(history=history))
        self.assertEqual(
            [m["content"] for m in result],
            ["Someone (user, Rocky's creator): hi", "yoyojesus (user): me too"],
        )

    async def test_nicknames_expand_search_query(self):
        trigger = SimpleNamespace(clean_content="is nesty teaching this semester?")
        facts = '- "Nesty" means Dr. Mikhail Nesterenko, Professor and Chair.'
        with (
            patch.object(discord_bot, "retrieve_context", return_value="") as search,
            patch.object(discord_bot, "load_facts", return_value=facts),
        ):
            await self.bot.generate(None, trigger)
        query = search.call_args.args[0]
        self.assertTrue(query.startswith("Dr. Mikhail Nesterenko"), query)
        self.assertNotIn("Professor and Chair", query)

    def test_nickname_needs_whole_word(self):
        facts = '"Nesty" means Dr. Mikhail Nesterenko'
        self.assertEqual(discord_bot.expand_nicknames("nestys are birds", facts), "nestys are birds")

    async def test_persona_file_replaces_persona_variable(self):
        with (
            patch.object(discord_bot, "retrieve_context", return_value=""),
            patch.object(discord_bot, "load_persona", return_value="Be objective and professional."),
        ):
            await self.bot.generate(None, self.trigger)
        instructions = self.bot.http_session.body["instructions"]
        self.assertIn("Be objective and professional.", instructions)
        self.assertNotIn(discord_bot.PERSONA, instructions)

    async def test_persona_variable_used_without_file(self):
        with (
            patch.object(discord_bot, "retrieve_context", return_value=""),
            patch.object(discord_bot, "PERSONA_FILE", discord_bot.Path("does-not-exist.md")),
        ):
            await self.bot.generate(None, self.trigger)
        self.assertIn(discord_bot.PERSONA, self.bot.http_session.body["instructions"])

    async def test_ignored_users_never_trigger_a_reply(self):
        self.bot.generate = AsyncMock()
        ducksu = SimpleNamespace(id=next(iter(discord_bot.IGNORED_IDS)), bot=True)
        message = SimpleNamespace(author=ducksu, content="hey rocky", clean_content="hey rocky", mentions=[])
        await self.bot.on_message(message)
        self.bot.generate.assert_not_called()

    def test_ducksu_ignored_by_default(self):
        self.assertIn(1559792130434670642, discord_bot.IGNORED_IDS)

    def test_missing_facts_file_is_empty(self):
        with patch.object(discord_bot, "FACTS_FILE", discord_bot.Path("does-not-exist.md")):
            self.assertEqual(discord_bot.load_facts(), "")

    async def test_history_is_anchored_to_trigger(self):
        del self.bot.build_input
        author = SimpleNamespace(display_name="Alice", bot=False)
        older = SimpleNamespace(clean_content="Earlier question", attachments=[], author=author)
        trigger = SimpleNamespace(clean_content="Actual question", attachments=[], author=author)
        channel = SimpleNamespace()

        async def history(**kwargs):
            self.assertIs(kwargs["before"], trigger)
            self.assertEqual(kwargs["limit"], max(0, discord_bot.HISTORY - 1))
            yield older

        channel.history = history
        result = await self.bot.build_input(channel, trigger)
        self.assertEqual(
            [m["content"] for m in result],
            [
                "Alice (user): Earlier question",
                "Alice (user): Actual question",
            ],
        )


class StripCodeTests(unittest.TestCase):
    def test_removes_multiline_code_block(self):
        text = "Use a loop:\n```python\nfor i in range(3):\n    print(i)\n    total += i\n```\nThat's it."
        result = discord_bot.strip_code(text)
        self.assertNotIn("print(i)", result)
        self.assertIn(discord_bot.CODE_REMOVED, result)
        self.assertIn("Use a loop:", result)
        self.assertIn("That's it.", result)

    def test_removes_unterminated_block(self):
        result = discord_bot.strip_code("Here:\n```js\nconst a = 1;\nconst b = 2;\nconst c = 3;\nconst d = 4;")
        self.assertNotIn("const", result)

    def test_keeps_short_commands_and_inline_code(self):
        text = "Run this:\n```\nsudo apt update\n```\nThen open `kali-setup`."
        self.assertEqual(discord_bot.strip_code(text), text)

    def test_reply_that_is_only_code(self):
        result = discord_bot.strip_code("```c\nint main() {\n  int x = 0;\n  return x;\n}\n```")
        self.assertEqual(result, discord_bot.CODE_REMOVED)


if __name__ == "__main__":
    unittest.main()
