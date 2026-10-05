import json
import os
import tempfile
import unittest

import bot

SESSION_ID = "11111111-2222-3333-4444-555555555555"


def user_entry(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_entry(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def text_block(text):
    return {"type": "text", "text": text}


class TranscriptTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.projects_directory = os.path.join(self.temporary_directory.name, "projects")
        os.makedirs(os.path.join(self.projects_directory, "project"))
        self.previous_configuration = bot.configuration
        bot.configuration = {"claude_home": self.temporary_directory.name}
        self.addCleanup(self.restore_configuration)

    def restore_configuration(self):
        bot.configuration = self.previous_configuration

    def write_transcript(self, entries, session_id=SESSION_ID):
        transcript_path = os.path.join(self.projects_directory, "project", session_id + ".jsonl")
        with open(transcript_path, "w", encoding="utf-8") as transcript_file:
            for entry in entries:
                transcript_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return transcript_path


class TextHelpersTest(unittest.TestCase):
    def test_shorten_keeps_first_sentences_and_strips_markdown(self):
        self.assertEqual(bot.shorten("**Готово.** Тесты прошли! Осталось ревью. Потом деплой.", max_sentences=2), "Готово. Тесты прошли!")

    def test_shorten_cuts_by_characters(self):
        self.assertEqual(bot.shorten("а" * 50, max_chars=10), "а" * 10 + "…")

    def test_split_keeps_every_chunk_within_limit(self):
        text = "\n".join(["строка " * 100] * 20)
        chunks = bot.split_text_into_chunks(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= bot.TELEGRAM_MESSAGE_LIMIT for chunk in chunks))

    def test_safe_cut_does_not_split_html_entity(self):
        text = "а" * (bot.TELEGRAM_MESSAGE_LIMIT - 3) + "&amp;" + "б" * 50
        self.assertEqual(bot.find_safe_cut(text, bot.TELEGRAM_MESSAGE_LIMIT), bot.TELEGRAM_MESSAGE_LIMIT - 3)

    def test_session_id_must_be_uuid(self):
        self.assertTrue(bot.is_valid_session_id(SESSION_ID))
        self.assertFalse(bot.is_valid_session_id("../../secrets"))
        self.assertFalse(bot.is_valid_session_id(None))


class SessionStateTest(TranscriptTestCase):
    def test_summary_reads_first_question_and_last_answer(self):
        transcript_path = self.write_transcript([user_entry("<command-name>/clear</command-name>"), user_entry("Почини тест"), assistant_entry([text_block("Починил.")])])
        summary = bot.read_session_summary(transcript_path)
        self.assertEqual(summary["first_user_text"], "Почини тест")
        self.assertEqual(summary["last_assistant"], "Починил.")
        self.assertFalse(summary["has_error"])

    def test_error_is_taken_from_last_answer_only(self):
        transcript_path = self.write_transcript([user_entry("Сделай"), assistant_entry([text_block("API Error: 500")]), user_entry("Повтори"), assistant_entry([text_block("Готово.")])])
        self.assertFalse(bot.read_session_summary(transcript_path)["has_error"])

    def test_trailing_question_mark_means_question(self):
        transcript_path = self.write_transcript([user_entry("Сделай"), assistant_entry([text_block("Какой вариант выбрать?")])])
        os.utime(transcript_path, (0, 0))
        self.assertEqual(bot.classify_session_state(bot.read_session_summary(transcript_path), transcript_path), ("question", "❓"))

    def test_unanswered_tool_use_means_working(self):
        transcript_path = self.write_transcript([user_entry("Сделай"), assistant_entry([{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}}])])
        self.assertEqual(bot.classify_session_state(bot.read_session_summary(transcript_path), transcript_path), ("working", "⏳"))

    def test_pending_question_is_found_until_answered(self):
        ask_block = {"type": "tool_use", "id": "ask-1", "name": "AskUserQuestion", "input": {"questions": [{"question": "Какую ветку?", "options": [{"label": "develop"}, {"label": "main"}]}]}}
        transcript_path = self.write_transcript([user_entry("Сделай"), assistant_entry([ask_block])])
        ask_id, ask_input = bot.find_pending_question(transcript_path)
        self.assertEqual(ask_id, "ask-1")
        self.assertEqual(bot.extract_question(ask_input), {"text": "Какую ветку?", "options": ["develop", "main"]})
        answered_path = self.write_transcript([user_entry("Сделай"), assistant_entry([ask_block]), {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "ask-1", "content": "develop"}]}}])
        self.assertEqual(bot.find_pending_question(answered_path), (None, None))


class TranscriptResolutionTest(TranscriptTestCase):
    def test_path_outside_projects_directory_is_rejected(self):
        outside_path = os.path.join(self.temporary_directory.name, "outside.jsonl")
        with open(outside_path, "w", encoding="utf-8") as outside_file:
            outside_file.write("{}\n")
        self.assertIsNone(bot.resolve_event_transcript({"transcript_path": outside_path, "session_id": "not-a-uuid"}))

    def test_falls_back_to_session_id_lookup(self):
        transcript_path = self.write_transcript([user_entry("Привет")])
        resolved = bot.resolve_event_transcript({"transcript_path": "", "session_id": SESSION_ID})
        self.assertEqual(os.path.realpath(resolved), os.path.realpath(transcript_path))


if __name__ == "__main__":
    unittest.main()
