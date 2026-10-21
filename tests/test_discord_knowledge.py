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


if __name__ == "__main__":
    unittest.main()
