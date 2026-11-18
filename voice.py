#!/usr/bin/env python3
import argparse
import json
import random
import sqlite3
import subprocess
import sys
import time
import wave
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import closing

PROFILE = Path.home() / 'Library/Application Support/orca'
OUTCOMES = {'success': 'answer-ready', 'failure': 'error', 'interruption': 'interrupted',
            'unconfirmed': 'result-unconfirmed'}
CHAT_SQL = """
WITH changed AS (
    SELECT DISTINCT e.session_id, e.epoch, json_extract(j.value,'$.itemId') AS id
    FROM journal_events e JOIN journal_chat_state s ON s.session_id=e.session_id AND s.epoch=e.epoch
    JOIN json_each(e.changes_json,'$.items') j
    WHERE e.rowid>? AND e.rowid<=? AND e.saved_at>=?
)
SELECT i.session_id, i.epoch, i.item_id, i.kind, i.lifecycle_state,
    CASE i.kind WHEN 'turn' THEN json_extract(i.payload_json,'$.body.outcome')
        ELSE json_extract(i.payload_json,'$.body.turnLifecycle.outcome') END,
    COALESCE(json_extract(i.payload_json,'$.body.userItemId'),
        json_extract(i.payload_json,'$.body.turnLifecycle.userItemId')) IS NOT NULL,
    EXISTS (SELECT 1 FROM journal_items f WHERE f.session_id=i.session_id AND f.epoch=i.epoch
        AND f.hidden=0 AND f.root=1 AND f.kind='status'
        AND (f.turn_item_id=i.item_id OR f.turn_id=i.turn_id OR f.codex_turn_id=i.codex_turn_id)
        AND json_extract(f.payload_json,'$.body.failure.kind')='usageLimitReached')
FROM changed c JOIN journal_items i ON i.session_id=c.session_id AND i.epoch=c.epoch AND i.item_id=c.id
WHERE i.hidden=0 AND i.root=1 AND i.subagent_row=0 AND i.queue_command_turn=0
    AND (i.kind IN ('turn','approval','question') OR (i.kind='status' AND i.turn_marker=1))
ORDER BY i.sequence, i.sequence_index
"""
TERMINAL_SQL = """
SELECT key, COALESCE(json_extract(value,'$.payload.mainAgent.state'),json_extract(value,'$.payload.state')),
    json_extract(value,'$.payload.mainAgent.outcome'),
    COALESCE(json_extract(value,'$.payload.mainAgent.stateStartedAt'),json_extract(value,'$.stateStartedAt')),
    json_extract(value,'$.turnStartedAt'), json_extract(value,'$.hookEventName'),
    json_extract(value,'$.payload.toolName')
FROM rows WHERE substr(CAST(key AS BLOB),1,6)=x'656e74727900'
    AND COALESCE(json_extract(value,'$.isReplay'),0)=0
    AND COALESCE(json_extract(value,'$.retainedForLiveness'),0)=0
    AND COALESCE(json_extract(value,'$.payload.sessionBoundary'),0)=0
"""


def select_sound(files, previous):
    return random.choice([f for f in files if f != previous] if len(files) > 1 else files)


class Chat:
    @staticmethod
    def event(kind, state, outcome, user_turn, limit):
        if state == 'pending':
            return {'approval': 'permission', 'question': 'question'}.get(kind)
        if kind not in ('turn', 'status'):
            return None
        if state == 'running':
            return 'request-accepted' if user_turn else None
        if outcome == 'failure' and limit:
            return 'limit-reached'
        if outcome:
            return OUTCOMES.get(outcome)
        return {'interrupted': 'interrupted', 'unverifiable': 'result-unconfirmed'}.get(state)


class Terminal:
    def __init__(self, started):
        self.started = started
        self.previous = {}
        self.acknowledged = {}

    def event(self, key, state, outcome, stamped, turn, hook, tool):
        current = (state, outcome, stamped, turn)
        before = self.previous.get(key)
        self.previous[key] = current
        if before == current or not stamped or stamped < self.started:
            return None
        if state == 'working' and hook == 'UserPromptSubmit' and turn and turn >= self.started:
            if self.acknowledged.get(key) != turn:
                self.acknowledged[key] = turn
                return 'request-accepted'
        if state in ('waiting', 'blocked') and (not before or before[0] not in ('waiting', 'blocked')):
            if tool in ('AskUserQuestion', 'request_user_input'):
                return 'question'
            if hook == 'PermissionRequest' and tool:
                return 'permission'
            return 'input-needed'
        if state == 'done':
            if before and before[0] == 'done' and before[1] == outcome and before[3] == turn:
                return None
            return OUTCOMES.get(outcome)
        return None


class Sources:
    def __init__(self, profile):
        self.chat = profile / 'agent-session-domain.db'
        self.terminal = profile / 'agent-hooks/last-status.db'
        self.started = time.time_ns() // 1_000_000
        self.cursor = self.head()
        self.seen = OrderedDict()
        self.states = Terminal(self.started)

    @staticmethod
    def rows(path, sql, args=()):
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            return db.execute(sql, args).fetchall()

    def head(self):
        return self.rows(self.chat, 'SELECT COALESCE(MAX(rowid),0) FROM journal_events')[0][0]

    def poll(self):
        head = self.head()
        # Leave the latest transaction a short window to record a companion failure fact.
        ready = self.rows(self.chat, 'SELECT COALESCE(MAX(rowid),0) FROM journal_events WHERE saved_at<=?',
                          (time.time_ns() // 1_000_000 - 900,))[0][0]
        if head < self.cursor:
            self.cursor = head
        for session, epoch, item, kind, state, outcome, user_turn, limit in self.rows(
                self.chat, CHAT_SQL, (self.cursor, ready, self.started)):
            event = Chat.event(kind, state, outcome, user_turn, limit)
            key = (session, epoch, item, 'settled' if state not in ('pending', 'running') else state)
            if event and key not in self.seen:
                self.seen[key] = None
                if len(self.seen) > 4096:
                    self.seen.popitem(last=False)
                yield event
        self.cursor = max(self.cursor, ready)
        panes = self.rows(self.terminal, TERMINAL_SQL)
        for row in panes:
            event = self.states.event(*row)
            if event:
                yield event
        keys = {row[0] for row in panes}
        for history in (self.states.previous, self.states.acknowledged):
            for key in history.keys() - keys:
                del history[key]


def read_pack(config, folder):
    mapping = json.loads(config.read_text(encoding='utf-8'))
    files = {}
    for status, names in mapping.items():
        pool = list(dict.fromkeys(folder / name for name in names))
        if not pool:
            raise ValueError(f'No sounds for {status}')
        for path in pool:
            if path.suffix != '.wav' or path.resolve().parent != folder.resolve():
                raise ValueError(f'Expected a WAV directly inside the sounds folder: {path.name}')
            with wave.open(str(path)) as audio:
                if not audio.getnframes():
                    raise ValueError(f'Empty WAV: {path.name}')
        files[status] = pool
    return files


def play(path):
    subprocess.run(['/usr/bin/afplay', str(path)], check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description='Play local Orca voice notifications on macOS.')
    parser.add_argument('--profile', type=Path, default=PROFILE)
    parser.add_argument('--sounds', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('sounds.json'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--play', metavar='STATUS')
    args = parser.parse_args()
    try:
        pack = read_pack(args.config, args.sounds)
        if args.play:
            play(select_sound(pack[args.play], None))
            print(f'Played {args.play}', flush=True)
            return 0
        source = Sources(args.profile)
        list(source.poll())
    except (OSError, ValueError, sqlite3.Error, wave.Error, subprocess.SubprocessError, KeyError) as error:
        print(f'Cannot start: {error}', file=sys.stderr)
        return 1
    print(f'Loaded {sum(map(len, pack.values()))} WAV; both Orca SQLite sources readable.', flush=True)
    if args.check:
        return 0
    previous = {}

    def announce(status):
        path = select_sound(pack[status], previous.get(status))
        try:
            play(path)
        except (OSError, subprocess.SubprocessError) as error:
            print(f'Playback failed for {status}: {type(error).__name__}', file=sys.stderr, flush=True)
        else:
            previous[status] = path

    with ThreadPoolExecutor(max_workers=1) as player:
        while True:
            try:
                for status in source.poll():
                    if status in pack:
                        player.submit(announce, status)
            except sqlite3.Error as error:
                print(f'Orca source read failed: {error}', file=sys.stderr, flush=True)
                time.sleep(4)
            time.sleep(.75)


if __name__ == '__main__':
    sys.exit(main())
