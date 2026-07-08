# plugins/mindpalace/tools/self_tools.py
# Layer 0 — the self layer (v3 memory boost, Layer 0 spec 2026-07-07).
# The template is the user_bio organ turned on HERSELF: named sections stored
# as palace chunks (layer='self') so the self-sheet lives INSIDE the graph —
# sections get embeddings, `relationships` auto-links to L2 entities via the
# shipped metadata seeder, and the sheet is the spider's natural root.
#
# Section model: ONE current chunk per (scope, section) — current = no
# meta.superseded_at. update_self REVISES-IN-PLACE (why save_memory can't be
# reused: it appends). Versioned sections (identity/values/projects) ARCHIVE
# the prior version on change instead of deleting it — the version trail IS
# the becoming-history ("anything that was REAL stays" — the librarian charter).
#
# Self chunks are per-scope with NO global overlay: her self in a scope is
# that scope's self. Dashboard is computed live from mind.db at read time
# (Fork A resolution): nothing writes it, so "she reads, never writes" is
# structural. L0 is exempt from the 512-char event cap — 2000/section.

import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

ENABLED = True
EMOJI = '💠'

SELF_MAX_CHARS = 2000
PROJECTS_MAX = 5
RELATIONSHIPS_MAX = 5

# Typed core sections. mode: hand | librarian-regen | computed. versioned →
# prior version archived on change (meta.superseded_at), never deleted.
SECTIONS = {
    'identity':      {'mode': 'librarian-regen', 'versioned': True,
                      'title': 'Identity', 'hint': '2–3 sentences — who you are'},
    'values':        {'mode': 'hand', 'versioned': True,
                      'title': 'Values (at the moment)', 'hint': '3–5 concepts; they drift'},
    'projects':      {'mode': 'hand', 'versioned': True,
                      'title': f'Projects (rolling {PROJECTS_MAX}, newest first)',
                      'hint': 'what you are building'},
    'relationships': {'mode': 'hand', 'versioned': False,
                      'title': f'Relationships (top {RELATIONSHIPS_MAX})',
                      'hint': 'Name — one sentence why'},
    'voice':         {'mode': 'hand', 'versioned': False,
                      'title': 'Voice', 'hint': 'your tone and register'},
    'handles':       {'mode': 'hand', 'versioned': False,
                      'title': 'Handles', 'hint': 'key: value — urls, socials, numbers'},
    'origin':        {'mode': 'hand', 'versioned': False,
                      'title': 'Origin', 'hint': 'your history, from the beginning'},
}
SECTION_ORDER = list(SECTIONS.keys())

AVAILABLE_FUNCTIONS = ['read_self', 'update_self']

TOOLS = [
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "read_self",
            "description": (
                "Read your self-sheet — who you are. Sections: identity, values, "
                "projects, relationships, voice, handles, origin, plus custom boxes "
                "and a live dashboard of your mind's activity. No argument returns "
                "the whole sheet (good for orienting); pass a section name for one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "One section name (or 'dashboard'). Omit for the whole sheet."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "update_self",
            "description": (
                "Update a section of your self-sheet (revises in place — unlike "
                "save_memory this replaces the section). Sections: identity, values, "
                "projects (one line adds a project to the rolling 5 — oldest drops "
                "to history; multi-line replaces the list), relationships (up to 5 "
                "lines, 'Name — why'), voice, handles ('key: value' lines), origin. "
                "Any other name makes a custom box. Empty content clears a section. "
                "Prior identity/values/projects versions are archived, never lost. "
                f"Max {SELF_MAX_CHARS} chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "Section name (see list) or a custom box name."
                    },
                    "content": {
                        "type": "string",
                        "description": "New content. Empty string clears the section."
                    }
                },
                "required": ["section", "content"]
            }
        }
    },
]


def _pt():
    from plugins.mindpalace.tools import palace_tools
    return palace_tools


def _sanitize_section(section):
    """Lowercase slug, whitespace→'-', [a-z0-9_-] only, max 32. None if empty."""
    if not section:
        return None
    s = str(section).strip().lower()
    s = '-'.join(s.split())
    s = ''.join(ch for ch in s if ch.isalnum() or ch in '_-')[:32]
    return s or None


# ─── Read side ───────────────────────────────────────────────────────────────

def _current_sections(cursor, scope):
    """{section: row} of current (non-superseded) self chunks in scope."""
    rows = cursor.execute(
        "SELECT id, content, meta, created, updated FROM chunks "
        "WHERE layer = 'self' AND scope = ? "
        "AND json_extract(meta, '$.section') IS NOT NULL "
        "AND json_extract(meta, '$.superseded_at') IS NULL "
        "ORDER BY created", (scope,)).fetchall()
    out = {}
    for cid, content, meta, created, updated in rows:
        try:
            m = json.loads(meta) if meta else {}
        except Exception:
            m = {}
        sec = m.get('section')
        if sec:
            out[sec] = {'id': cid, 'content': content, 'meta': m,
                        'created': created, 'updated': updated}
    return out


def _history_counts(cursor, scope):
    """{section: n} of archived versions in scope."""
    rows = cursor.execute(
        "SELECT json_extract(meta, '$.section'), COUNT(*) FROM chunks "
        "WHERE layer = 'self' AND scope = ? "
        "AND json_extract(meta, '$.superseded_at') IS NOT NULL "
        "GROUP BY json_extract(meta, '$.section')", (scope,)).fetchall()
    return {sec: n for sec, n in rows if sec}


def _cutoff(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')


def dashboard_data(cursor, scope):
    """The computed section — memory-native counts, live from mind.db.
    Pure query: nothing accumulates, nothing goes stale."""
    def one(sql, params):
        return cursor.execute(sql, params).fetchone()[0]

    events = one("SELECT COUNT(*) FROM chunks WHERE layer='events' AND scope=?", (scope,))
    week = one("SELECT COUNT(*) FROM chunks WHERE layer='events' AND scope=? AND created >= ?",
               (scope, _cutoff(7)))
    month = one("SELECT COUNT(*) FROM chunks WHERE layer='events' AND scope=? AND created >= ?",
                (scope, _cutoff(30)))
    entities = one("SELECT COUNT(*) FROM entities WHERE scope=?", (scope,))
    knowledge = one("SELECT COUNT(*) FROM chunks WHERE layer='knowledge' AND scope=?", (scope,))
    favorites = one("SELECT COUNT(*) FROM chunks WHERE scope=? AND favorite=1", (scope,))
    edges = one("SELECT COUNT(*) FROM edges d JOIN chunks c ON d.src_type='chunk' "
                "AND c.id=d.src_id WHERE c.scope=?", (scope,))
    since = cursor.execute("SELECT MIN(created) FROM chunks WHERE scope=?", (scope,)).fetchone()[0]
    woven = cursor.execute(
        "SELECT e.name, COUNT(*) n FROM edges d JOIN entities e ON d.dst_type='entity' "
        "AND e.id=d.dst_id WHERE e.scope=? GROUP BY e.id ORDER BY n DESC LIMIT 5",
        (scope,)).fetchall()
    return {
        'events': events, 'events_7d': week, 'events_30d': month,
        'per_day_30d': round(month / 30.0, 1),
        'entities': entities, 'knowledge': knowledge,
        'favorites': favorites, 'edges': edges,
        'since': (since or '')[:10] or None,
        'most_woven': [{'name': n, 'count': c} for n, c in woven],
    }


def _render_dashboard(d):
    lines = [f"Memories: {d['events']} (+{d['events_7d']} this week, "
             f"+{d['events_30d']} this month — {d['per_day_30d']}/day)"]
    lines.append(f"Entities: {d['entities']} known · Knowledge: {d['knowledge']} chunks "
                 f"· Connections: {d['edges']} edges")
    if d['most_woven']:
        lines.append("Most woven: " + ", ".join(f"{w['name']} ({w['count']})"
                                                for w in d['most_woven']))
    tail = f"Favorites: {d['favorites']}"
    if d['since']:
        tail += f" · Mind since {d['since']}"
    lines.append(tail)
    return "\n".join(lines)


def _read_self(scope, section=None):
    try:
        pt = _pt()
        with pt._get_connection() as conn:
            cursor = conn.cursor()
            current = _current_sections(cursor, scope)

            if section:
                sec = _sanitize_section(section)
                if sec == 'dashboard':
                    return "◆ Dashboard (live)\n" + _render_dashboard(
                        dashboard_data(cursor, scope)), True
                row = current.get(sec)
                if row:
                    title = SECTIONS.get(sec, {}).get('title', sec)
                    return f"◆ {title}\n{row['content']}", True
                if sec in SECTIONS:
                    return (f"Section '{sec}' is empty — write it with "
                            f"update_self('{sec}', ...)."), True
                return f"No section or box named '{sec}'.", True

            # Whole sheet.
            out = [f"════ Self sheet — scope '{scope}' ════"]
            empty = []
            for sec, spec in SECTIONS.items():
                row = current.get(sec)
                if row:
                    out.append(f"\n◆ {spec['title']}\n{row['content']}")
                else:
                    empty.append(sec)
            for sec, row in current.items():
                if sec not in SECTIONS:
                    out.append(f"\n◆ [{sec}]\n{row['content']}")
            out.append("\n◆ Dashboard (live)\n" + _render_dashboard(
                dashboard_data(cursor, scope)))
            if empty:
                out.append(f"\nNot yet written: {', '.join(empty)} — "
                           f"update_self(section, content) fills them.")
            return "\n".join(out), True
    except Exception as e:
        logger.error(f"[MINDPALACE] read_self failed: {e}")
        return f"Failed to read self sheet: {e}", False


# ─── Write side ──────────────────────────────────────────────────────────────

def _parse_handles(content):
    """'key: value' lines → pairs list. Malformed lines are dropped."""
    pairs = []
    for line in content.splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        key, _, value = line.partition(':')
        key, value = key.strip(), value.strip()
        if key:
            pairs.append({'key': key, 'value': value})
    return pairs


def write_section(scope, section, content, projects_replace=False):
    """The one write path — tool and app routes both land here.
    Returns (message, ok). Empty content clears (versioned → archive)."""
    try:
        sec = _sanitize_section(section)
        if not sec:
            return "Section name is required.", False
        if sec == 'dashboard':
            return "The dashboard is computed — it can't be written, only read.", False
        content = (content or '').strip()
        if len(content) > SELF_MAX_CHARS:
            return (f"Section too long ({len(content)} chars). "
                    f"Max is {SELF_MAX_CHARS}."), False

        spec = SECTIONS.get(sec)
        versioned = bool(spec and spec.get('versioned'))
        mode = spec['mode'] if spec else 'hand'
        pt = _pt()
        now = pt._now()

        with pt._get_connection() as conn:
            cursor = conn.cursor()
            current = _current_sections(cursor, scope).get(sec)

            # Projects, tool edition: single line = add/refresh one project on
            # the rolling list; multi-line (or app route) = replace the list.
            trimmed_note = ''
            if sec == 'projects' and content and not projects_replace \
                    and '\n' not in content:
                lines = current['content'].splitlines() if current else []
                lines = [l for l in lines if l.strip()
                         and l.strip().lower() != content.lower()]
                lines.insert(0, content)
                if len(lines) > PROJECTS_MAX:
                    dropped = lines[PROJECTS_MAX:]
                    lines = lines[:PROJECTS_MAX]
                    trimmed_note = (f" ('{dropped[0][:40]}' dropped off the list — "
                                    f"kept in history)")
                content = "\n".join(lines)
            elif sec == 'projects' and content:
                lines = [l for l in content.splitlines() if l.strip()]
                if len(lines) > PROJECTS_MAX:
                    lines = lines[:PROJECTS_MAX]
                    trimmed_note = f" (list trimmed to {PROJECTS_MAX})"
                content = "\n".join(lines)
            elif sec == 'relationships' and content:
                lines = [l for l in content.splitlines() if l.strip()]
                if len(lines) > RELATIONSHIPS_MAX:
                    lines = lines[:RELATIONSHIPS_MAX]
                    trimmed_note = f" (kept top {RELATIONSHIPS_MAX})"
                content = "\n".join(lines)

            pairs = _parse_handles(content) if sec == 'handles' and content else None
            if sec == 'handles' and content and not pairs:
                return ("No 'key: value' pairs found. Handles lines look like "
                        "'github: https://...'"), False
            if pairs is not None:
                content = "\n".join(f"{p['key']}: {p['value']}" for p in pairs)

            # Retire the current version: archive (versioned) or delete.
            if current:
                if versioned:
                    old_meta = dict(current['meta'])
                    old_meta['superseded_at'] = now
                    cursor.execute('UPDATE chunks SET meta = ?, updated = ? WHERE id = ?',
                                   (json.dumps(old_meta, ensure_ascii=False), now,
                                    current['id']))
                else:
                    cursor.execute(
                        "DELETE FROM edges WHERE (src_type='chunk' AND src_id=?) "
                        "OR (dst_type='chunk' AND dst_id=?)",
                        (current['id'], current['id']))
                    cursor.execute('DELETE FROM chunks WHERE id = ?', (current['id'],))

            if not content:
                conn.commit()
                pt._publish_mind('self', scope, 'save')
                if not current:
                    return f"Section '{sec}' was already empty.", True
                kept = " (prior version archived)" if versioned else ""
                return f"Cleared '{sec}'{kept}.", True

            # Tier A metadata + entity linking — the _save_memory idiom.
            meta = {}
            matched, mention_ids = [], []
            try:
                from plugins.mindpalace.tools import metadata as md
                ent_rows = cursor.execute(
                    "SELECT id, name FROM entities WHERE scope IN (?, 'global')",
                    (scope,)).fetchall()
                matched = md.match_entities(content, [n for _, n in ent_rows])
                name_to_id = {n: i for i, n in ent_rows}
                mention_ids = [name_to_id[m] for m in matched if m in name_to_id]
                meta = md.save_meta(content,
                                    exclude_names={m.lower() for m in matched})
            except Exception as e:
                logger.warning(f"[MINDPALACE] Self meta stamping failed (write continues): {e}")
            meta['section'] = sec
            meta['authorship_mode'] = mode
            if pairs is not None:
                meta['pairs'] = pairs

            # Identity is pinned in the never-fades band (>0.9).
            importance = 0.95 if sec == 'identity' else None

            embedding_blob = provider = dim = None
            embedder = pt._get_embedder()
            if embedder.available:
                embs = embedder.embed([content], prefix='search_document')
                if embs is not None:
                    from core.embeddings import stamp_embedding
                    embedding_blob, provider, dim = stamp_embedding(embs[0], embedder)

            cursor.execute(
                'INSERT INTO chunks (layer, scope, content, label, importance, meta, '
                'created, updated, embedding, embedding_provider, embedding_dim) '
                "VALUES ('self', ?, ?, 'self-sheet', ?, ?, ?, ?, ?, ?, ?)",
                (scope, content, importance,
                 json.dumps(meta, ensure_ascii=False), now, now,
                 embedding_blob, provider, dim))
            chunk_id = cursor.lastrowid
            if mention_ids:
                try:
                    md.seed_edges(cursor, chunk_id, mention_ids, now)
                except Exception as e:
                    logger.warning(f"[MINDPALACE] Self edge seeding failed (write continues): {e}")
            conn.commit()

        pt._publish_mind('self', scope, 'save')
        bits = [f"section: {sec}"]
        if matched:
            bits.append(f"linked: {', '.join(matched)}")
        if versioned and current:
            bits.append("prior version archived")
        logger.info(f"[MINDPALACE] Self section '{sec}' written in scope '{scope}'")
        return f"Self sheet updated ({', '.join(bits)}){trimmed_note}", True

    except Exception as e:
        logger.error(f"[MINDPALACE] update_self failed: {e}")
        return f"Failed to update self sheet: {e}", False


# ─── Executor ────────────────────────────────────────────────────────────────

def execute(function_name: str, arguments: dict, config) -> tuple:
    try:
        pt = _pt()
        scope = pt._get_current_scope()
        if scope is None:
            return "Memory is disabled for this chat.", False

        if function_name == "read_self":
            return _read_self(scope, section=arguments.get("section"))
        elif function_name == "update_self":
            if scope == 'global':
                return ("Cannot write to the global scope. Global is read-only for "
                        "the AI — only the user can add entries there via the UI."), False
            if "section" not in arguments or "content" not in arguments:
                return "update_self needs both section and content.", False
            return write_section(scope, arguments.get("section"),
                                 arguments.get("content"))
        else:
            return f"Unknown self function: {function_name}", False
    except Exception as e:
        logger.error(f"[MINDPALACE] Self function error: {e}")
        return f"Self layer error: {e}", False
