import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from voice import Chat, Sources, Terminal, select_sound


class Events(unittest.TestCase):
    def test_chat_does_not_guess_success_or_announce_a_stop(self):
        for state, outcome, expected in (
            ('completed', None, None), ('completed', 'success', 'answer-ready'),
            ('completed', 'cancellation', None), ('completed', 'superseded', None),
            ('interrupted', None, 'interrupted'), ('unverifiable', None, 'result-unconfirmed'),
            ('completed', 'failure', 'error'),
        ):
            self.assertEqual(Chat().event('turn', state, outcome, True, False), expected)

    def test_prompt_kind_and_limit_take_precedence(self):
        self.assertEqual(Chat().event('turn', 'completed', 'failure', True, True), 'limit-reached')
        self.assertEqual(Chat().event('approval', 'pending', None, False, False), 'permission')
        self.assertEqual(Chat().event('question', 'pending', None, False, False), 'question')
        self.assertIsNone(Chat().event('question', 'answered', None, False, False))
        self.assertIsNone(Chat().event('turn', 'running', None, False, False))

    def test_terminal_only_acks_a_new_observed_turn_and_never_guesses(self):
        terminal = Terminal(100)
        self.assertIsNone(terminal.event('p', 'working', None, 90, 90, 'UserPromptSubmit', None))
        self.assertEqual(terminal.event('p', 'working', None, 101, 101, 'UserPromptSubmit', None), 'request-accepted')
        self.assertEqual(terminal.event('p', 'blocked', None, 110, 101, None, None), 'input-needed')
        self.assertIsNone(terminal.event('p', 'working', None, 112, 101, None, None))
        self.assertIsNone(terminal.event('p', 'done', None, 115, 101, 'Stop', None))
        self.assertEqual(terminal.event('p', 'done', 'success', 115, 101, 'Stop', None), 'answer-ready')
        self.assertIsNone(terminal.event('p', 'done', 'success', 115, 101, 'Stop', None))

    def test_multiple_clips_exclude_last_but_single_clip_repeats(self):
        a, b = Path('a.wav'), Path('b.wav')
        self.assertEqual(select_sound([a, b], a), b)
        self.assertEqual(select_sound([a], a), a)

    def test_reads_new_journal_and_terminal_rows_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / 'agent-hooks').mkdir()
            with sqlite3.connect(profile / 'agent-session-domain.db') as chat, sqlite3.connect(
                    profile / 'agent-hooks/last-status.db') as terminal:
                chat.executescript("""
                    CREATE TABLE journal_events(session_id, epoch, sequence, saved_at, changes_json);
                    CREATE TABLE journal_chat_state(session_id, epoch);
                    INSERT INTO journal_chat_state VALUES ('chat','epoch');
                    CREATE TABLE journal_items(session_id, epoch, item_id, kind, lifecycle_state,
                        payload_json, hidden, root, subagent_row, queue_command_turn, turn_marker,
                        turn_item_id, turn_id, codex_turn_id, sequence, sequence_index);
                    INSERT INTO journal_items VALUES ('chat','epoch','t','turn','completed',
                        '{"body":{"outcome":"failure"}}',0,1,0,0,1,NULL,'turn',NULL,1,0);
                    INSERT INTO journal_items VALUES ('chat','epoch','limit','status',NULL,
                        '{"body":{"failure":{"kind":"usageLimitReached"}}}',0,1,0,0,0,
                        't','turn',NULL,1,1);
                """)
                terminal.execute('CREATE TABLE rows(key TEXT PRIMARY KEY, seq, value)')
                terminal.commit()
                source = Sources(profile)
                source.started -= 10000
                chat.execute('INSERT INTO journal_events VALUES (?,?,?,?,?)',
                             ('chat', 'epoch', 1, source.started + 1,
                              '{"items":[{"itemId":"t"},{"itemId":"limit"}]}'))
                chat.commit()
                terminal.execute('INSERT INTO rows VALUES (?,?,?)', ('entry\x00sample', 1, json.dumps({
                    'stateStartedAt': source.states.started + 1,
                    'payload': {'state': 'waiting'}
                })))
                terminal.commit()
                self.assertEqual(list(source.poll()), ['limit-reached', 'input-needed'])
                self.assertEqual(list(source.poll()), [])


if __name__ == '__main__':
    unittest.main()
