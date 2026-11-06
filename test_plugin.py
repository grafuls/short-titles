"""Tests for the short-titles plugin (pure logic; no hermes imports needed).

Run: <venv-python> -m pytest <this file> -q
The plugin module is loaded by path because the directory name has a hyphen.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("short_titles_mod", PLUGIN_DIR / "__init__.py")
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)


def _now() -> float:
    return 1_000_000.0


def test_condense_strips_quotes_padding_and_period():
    assert st.condense('  "Brainstorm Hermes plugin." ') == "Brainstorm Hermes"


def test_condense_caps_at_two_words():
    assert st.condense("Fix the login button") == "Fix login"


def test_condense_empty_returns_none():
    assert st.condense('""') is None
    assert st.condense("   ") is None


def test_condense_caps_length_at_128():
    assert len(st.condense("word " * 60)) <= 128


def test_plan_first_sighting_only_records():
    state = {}
    now = _now()
    actions = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "Brainstorm Hermes plugin"}],
                      state, now)
    assert actions == []
    assert state["s1"]["seen_at"] == now
    assert state["s1"]["attempts"] == 0


def test_plan_aged_candidate_becomes_rename():
    state = {}
    now = _now()
    actions = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "Brainstorm Hermes plugin"}], state, now)
    actions = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "Brainstorm Hermes plugin"}],
                      state, now + st.MIN_AGE_S + 1)
    assert actions == [{"session_id": "s1", "chat_id": 1, "thread_id": 2,
                        "title": "Brainstorm Hermes plugin", "profile": ""}]
    assert state["s1"]["in_flight"] is True


def test_in_flight_not_replanned_until_resolved():
    state = {}
    now = _now()
    st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}], state, now)
    st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}], state, now + st.MIN_AGE_S + 1)
    again = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}], state, now + st.MIN_AGE_S + 2)
    assert again == []


def test_mark_done_then_same_title_skips():
    state = {}
    now = _now()
    st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}], state, now)
    st.mark_done(state, "s1", "T", "Hermes")
    actions = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}],
                      state, now + st.MIN_AGE_S + 1)
    assert actions == []


def test_title_change_reprocesses():
    state = {}
    now = _now()
    st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "Old"}], state, now)
    st.mark_done(state, "s1", "Old", "Old")
    actions = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "New"}],
                      state, now + st.MIN_AGE_S + 1)
    # New title: re-observed on this tick, renamed next tick.
    assert actions == []
    actions = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "New"}],
                      state, now + 2 * st.MIN_AGE_S + 2)
    assert len(actions) == 1


def test_give_up_after_max_attempts():
    state = {}
    now = _now()
    st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}], state, now)
    for i in range(1, st.MAX_ATTEMPTS + 1):
        st.mark_failed(state, "s1", "T")
        assert state["s1"]["attempts"] == i
    later = st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}],
                    state, now + 10 * st.MIN_AGE_S)
    assert later == []  # attempts exhausted: no more renames for this title


def test_state_roundtrips_through_json():
    state = {"s1": {"title": "T", "seen_at": 1.0, "attempts": 0, "done": False, "name": "", "in_flight": False}}
    loaded = json.loads(json.dumps(state))
    assert st.plan([{"session_id": "s1", "chat_id": 1, "thread_id": 2, "title": "T"}], loaded, 1.0 + st.MIN_AGE_S + 1)


def test_batch_cap_limits_actions_per_tick():
    state = {}
    now = _now()
    rows = [{"session_id": f"s{i}", "chat_id": 1, "thread_id": i, "title": "T"} for i in range(10)]
    st.plan(rows, state, now)
    actions = st.plan(rows, state, now + st.MIN_AGE_S + 1)
    assert len(actions) == st.MAX_PER_TICK


def test_candidates_query_filters(tmp_path=None):
    import sqlite3
    import tempfile
    import time

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "state.db"
    now = time.time()
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, chat_type TEXT,
        thread_id TEXT, title TEXT, title_source TEXT, hidden INTEGER,
        started_at REAL, profile_name TEXT NOT NULL DEFAULT '')""")
    rows = [
        ("keep", "telegram", "1", "dm", "7", "Brainstorm Hermes plugin", "llm", 0, now - 10, ""),
        ("old", "telegram", "1", "dm", "5", "Old thread title here", "llm", 0, now - 99999, ""),
        ("no-thread", "telegram", "1", "dm", None, "T", "llm", 0, now - 10, ""),
        ("user-titled", "telegram", "1", "dm", "8", "T", "user", 0, now - 10, ""),
        ("hidden", "telegram", "1", "dm", "9", "T", "llm", 1, now - 10, ""),
        ("not-tg", "api", "1", "dm", "10", "T", "llm", 0, now - 10, ""),
        ("group", "telegram", "1", "group", "11", "T", "llm", 0, now - 10, ""),
    ]
    conn.executemany("INSERT INTO sessions (id, source, chat_id, chat_type, thread_id, title, title_source, hidden, started_at, profile_name) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()
    got = {r["session_id"]: r for r in st.candidates_from_db(db, since=now - 60, derived_grace_s=60)}
    assert list(got) == ["keep"], got
    assert got["keep"]["thread_id"] == "7"
    assert got["keep"]["title"] == "Brainstorm Hermes plugin"
    # No cutoff -> retroactive sweep (used only when the operator wipes state).
    all_ids = {r["session_id"] for r in st.candidates_from_db(db)}
    assert "old" in all_ids
    # A stale derived title is renameable; a fresh one waits for the llm upgrade.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE sessions SET title_source='derived', title='some long derived fragment' WHERE id='old'")
    conn.execute("INSERT INTO sessions (id, source, chat_id, chat_type, thread_id, title, title_source, hidden, started_at, profile_name)"
                 " VALUES ('fresh-derived','telegram','1','dm','12','x','derived',0,?,'')", (now - 5,))
    conn.commit(); conn.close()
    ids = {r["session_id"] for r in st.candidates_from_db(db, derived_grace_s=60)}
    assert "old" in ids and "fresh-derived" not in ids


if __name__ == "__main__":
    import sys
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
