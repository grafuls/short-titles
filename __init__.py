"""short-titles plugin — renames Telegram DM topics to 1-2 word names.

Hermes core auto-titles sessions with 3-7 word LLM titles and renames the topic
accordingly. This plugin refines those to at most two words, after core's rename
has landed (MIN_AGE_S delay), via a second tiny LLM call and the Bot API. Session
and topic info is read straight from the ``sessions`` table in state.db (root plus
every profile's); renames only fire for new threads (``since`` gate) and only while
``title_source='llm'`` (or, past DERIVED_GRACE_S, a still-``derived`` fallback), so a
manual ``/title`` is respected. Failures retry up to MAX_ATTEMPTS, then the core title stays.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("short-titles")

PLUGIN_DIR = Path(__file__).resolve().parent
STATE_PATH = PLUGIN_DIR / "state.json"

TICK_S = 30.0
MIN_AGE_S = 25.0          # let core's own topic rename land first
MAX_ATTEMPTS = 3          # per title; then keep core's name
MAX_PER_TICK = 5          # bound LLM+API burst (also drains the startup backlog gently)
MAX_TITLE_CHARS = 128     # Bot API topic-name cap
DERIVED_GRACE_S = 180.0   # rename from the raw derived title once the llm upgrade is this stale

_DB = None  # resolved lazily; tests monkeypatch module attrs

# Filler words dropped only when the name must be truncated to two.
_FILLER = {"the", "a", "an", "of", "to", "my", "your", "for", "on", "in"}


def condense(raw: object) -> str | None:
    """Model output -> clean <=2-word topic name, or None if nothing usable."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().strip('"').strip("'").strip("`").strip()
    text = re.sub(r"\s+", " ", text).rstrip(".").strip()
    if not text:
        return None
    words = text.split(" ")
    if len(words) > 2:
        kept = [w for w in words if w.lower().strip(",") not in _FILLER]
        words = (kept or words)[:2]
    return " ".join(words)[:MAX_TITLE_CHARS].strip() or None


def _fresh(title: str, now: float) -> dict:
    return {"title": title, "seen_at": now, "attempts": 0, "done": False, "name": "", "in_flight": False}


def plan(candidates: list[dict], state: dict, now: float) -> list[dict]:
    """Pure tick core: new titles are only observed; aged, unfinished ones become
    rename actions (bounded by MAX_PER_TICK). Duplicate-safe on every field."""
    eligible: list[dict] = []
    for c in candidates or []:
        sid = str(c["session_id"])
        rec = state.get(sid)
        if rec is None or rec.get("title") != c["title"]:
            state[sid] = _fresh(c["title"], now)
            continue
        if rec.get("done") or rec.get("in_flight") or rec.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        if now - rec.get("seen_at", now) < MIN_AGE_S:
            continue
        eligible.append({"session_id": sid, "chat_id": c["chat_id"], "thread_id": c["thread_id"],
                         "title": c["title"], "profile": c.get("profile", "")})
    for action in eligible[:MAX_PER_TICK]:
        state[action["session_id"]]["in_flight"] = True
    return eligible[:MAX_PER_TICK]


def mark_done(state: dict, sid: str, title: str, name: str) -> None:
    rec = state.setdefault(sid, _fresh(title, time.time()))
    rec.update(title=title, done=True, name=name, in_flight=False)


def mark_failed(state: dict, sid: str, title: str) -> None:
    rec = state.setdefault(sid, _fresh(title, time.time()))
    rec["attempts"] = int(rec.get("attempts", 0)) + 1
    rec["in_flight"] = False


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    except Exception:
        logger.warning("short-titles: unreadable state, starting fresh", exc_info=True)
        return {}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE_PATH)
    except Exception:
        logger.warning("short-titles: state save failed", exc_info=True)


def _db_path() -> Path:
    global _DB
    if _DB is None:
        import os
        _DB = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "state.db"
    return Path(_DB)


def _db_paths() -> list[Path]:
    """Root state.db plus every profile's — the gateway muxes SessionDB per profile."""
    root = _db_path()
    paths = [root] if root.exists() else []
    paths += sorted(p for p in root.parent.glob("profiles/*/state.db") if p.exists())
    return paths


def candidates_from_db(db_path=None, since: float | None = None, derived_grace_s: float | None = None) -> list[dict]:
    """Telegram DM-topic sessions with a renameable title, from every state.db.

    Read straight off ``sessions``; the ``telegram_dm_topic_bindings`` tables are
    optional and often absent. ``since`` restricts to threads created after that
    epoch (new threads only). ``derived_grace_s``: Hermes's llm title upgrade can
    silently never land (self-hosted providers); after that grace we rename from
    the raw ``derived`` title instead, and the llm title re-renames on arrival.
    """
    paths = [Path(db_path)] if db_path is not None else _db_paths()
    out: dict[str, dict] = {}
    for path in paths:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                sql = ("SELECT id, chat_id, thread_id, title, COALESCE(profile_name, '') FROM sessions "
                       "WHERE source = 'telegram' AND chat_type = 'dm' AND thread_id IS NOT NULL "
                       "AND title IS NOT NULL AND COALESCE(hidden, 0) = 0 AND (title_source = 'llm'")
                params: list = []
                if derived_grace_s is not None:
                    sql += " OR (title_source = 'derived' AND started_at < ?)"
                    params.append(time.time() - float(derived_grace_s))
                sql += ")"
                if since is not None:
                    sql += " AND started_at >= ?"
                    params.append(float(since))
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except Exception:
            logger.debug("short-titles: candidate query failed on %s", path, exc_info=True)
            continue
        for sid, chat_id, thread_id, title, profile in rows:
            out[str(sid)] = {"session_id": sid, "chat_id": chat_id, "thread_id": thread_id,
                             "title": title, "profile": profile}
    return list(out.values())


def bot_token() -> str:
    """platforms.telegram.token from config.yaml, else TELEGRAM_BOT_TOKEN (env/.env)."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("platforms") or {}
        token = ((cfg.get("telegram") or {}) if isinstance(cfg, dict) else {}).get("token")
        if token:
            return str(token)
    except Exception:
        logger.debug("short-titles: config token lookup failed", exc_info=True)
    token = ""
    try:
        import os
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
        if not token:
            from hermes_secrets import get_secret  # resolves $HERMES_HOME/.env too
            token = get_secret("TELEGRAM_BOT_TOKEN", "") or ""
    except Exception:
        logger.debug("short-titles: env/secret token lookup failed", exc_info=True)
    return token


def telegram_call(token: str, method: str, **params):
    import httpx
    resp = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=15)
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not data.get("ok"):
        desc = data.get("description") if isinstance(data, dict) else None
        raise RuntimeError(f"telegram {method} -> HTTP {resp.status_code}: {desc or str(resp.text)[:200]}; params={params}")
    return data.get("result")


def short_name(title: str) -> str | None:
    """One tiny LLM call: distill the session title to <=2 words."""
    from agent.auxiliary_client import call_llm
    resp = call_llm(
        task="title_generation",
        messages=[{"role": "user", "content":
                   f'Chat title: "{title}"\n\n'
                   "Name this chat's topic in at most 2 words, sentence case. "
                   'Reply with the name only, e.g. Hermes plugin'}],
        max_tokens=24,
    )
    try:
        text = resp.choices[0].message.content
    except Exception:
        text = None
    return condense(text) if text else None


def _home_for_profile(profile: str) -> Path:
    root = _db_path().parent
    if profile and profile.strip().lower() not in ("", "default"):
        cand = root / "profiles" / profile.strip()
        if cand.is_dir():
            return cand
    return root


_SCOPE_CACHE: dict[str, tuple] = {}


def _run_scoped(home: Path, fn):
    """Run fn under the home's secret scope (muxed gateways require a scope for get_secret)."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    key = str(home)
    if key not in _SCOPE_CACHE:
        _SCOPE_CACHE[key] = build_profile_secret_scope(home)
    token = set_secret_scope(_SCOPE_CACHE[key], profile_home=key)
    try:
        return fn()
    finally:
        reset_secret_scope(token)


def process(actions: list[dict], state: dict) -> None:
    root = _db_path().parent
    token = _run_scoped(root, bot_token)
    if not token:
        logger.warning("short-titles: no bot token; leaving topics to core renamer")
        for a in actions:
            mark_failed(state, str(a["session_id"]), a["title"])
        return
    for a in actions:
        sid = str(a["session_id"])
        home = _home_for_profile(a.get("profile", ""))
        try:
            name = _run_scoped(home, lambda: short_name(a["title"]))
            if not name:
                raise RuntimeError("empty name")
            telegram_call(token, "editForumTopic", chat_id=int(a["chat_id"]),
                          message_thread_id=int(a["thread_id"]), name=name)
            mark_done(state, sid, a["title"], name)
            logger.info("short-titles: renamed topic chat=%s thread=%s -> %r", a["chat_id"], a["thread_id"], name)
        except Exception:
            mark_failed(state, sid, a["title"])
            logger.warning("short-titles: rename failed for session %s", sid, exc_info=True)


def tick(state: dict | None = None) -> list[dict]:
    """One pass; returns the actions taken (also used for manual runs)."""
    owned = state is None
    state = _load_state() if owned else state
    if "_since" not in state:  # new-thread gate; backdated one day so threads orphaned by
        state["_since"] = time.time() - 86400  # title-pipeline flukes still get renamed once
    actions = plan(candidates_from_db(since=state["_since"], derived_grace_s=DERIVED_GRACE_S), state, time.time())
    process(actions, state)
    if owned:
        _save_state(state)
    return actions


def _loop() -> bool:
    while True:
        time.sleep(TICK_S)
        try:
            tick()
        except Exception:
            logger.warning("short-titles: tick failed", exc_info=True)


def register(ctx=None) -> None:
    threading.Thread(target=_loop, name="short-titles", daemon=True).start()
    logger.info("short-titles: watching for session titles (every %ss)", TICK_S)
    return True
