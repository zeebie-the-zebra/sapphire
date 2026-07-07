# plugins/mindpalace/tools/palace_tools.py
# Mind Palace v1 — layered memory framework (v3 memory boost, step 1).
#
# Uniform unit everywhere: chunks + metadata. Layers on the rail:
#   0 self       — who she is, decisions, history (per-scope)
#   1 events     — things that happened; default write target; librarian raw material
#   2 entities   — people/places/things; tiered chunks (1=headline 2=facts 3=trivia)
#   3 knowledge  — big reference data (sub-chunked groups)
#
# Deliberately shares tool NAMES with plugins/memory — the two plugins are
# mutually exclusive (function_manager refuses the second by design). This
# module NEVER touches memory.db/knowledge.db/goals.db; import_tools.py reads
# them and copies. Old DBs stay untouched as the switch-back path.
#
# Invariants carried from v2 (load-bearing — do not "simplify" away):
#   - prefix asymmetry: embed with 'search_document' at write, 'search_query' at read
#   - provenance triple: (embedding, embedding_provider, embedding_dim) travel
#     together or the row is invisible to vector search
#   - scope fail-disabled: ContextVar resolution failure → None → tools disabled,
#     never a silent fall-through to 'default'
#   - 'global' scope: read-only overlay, AI writes blocked
#   - private_key: plaintext gate, cross-persona behavioral separation
#
# Corruption policy v1: integrity failure → preserve as .corrupted, recreate
# fresh. No salvage-rebuild yet — in v1 the old DBs remain the source of truth
# and import_v2 re-runs idempotently, so recovery = re-import. Revisit once
# the palace holds data that exists nowhere else (librarian output).

import json
import sqlite3
import logging
import re
import threading
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager

logger = logging.getLogger(__name__)

ENABLED = True
EMOJI = '🏛️'

_db_path = None
_db_initialized = False
_db_lock = threading.Lock()

from core.embeddings import get_embedder as _get_embedder

SUGGESTED_LABELS = "family, preferences, technical, stories, routines, opinions"

# Layer registry, v1: internal seed. The `layers` table is the durable registry;
# plugin-registered layers (v1.1) will insert rows + register handlers here.
LAYERS = {
    'self':      {'num': 0, 'label': 'Self'},
    'events':    {'num': 1, 'label': 'Events'},
    'entities':  {'num': 2, 'label': 'Entities'},
    'knowledge': {'num': 3, 'label': 'Knowledge'},
}
LAYER_KEYS = list(LAYERS.keys())

AVAILABLE_FUNCTIONS = [
    'save_memory',
    'search_memory',
    'get_recent_memories',
    'delete_memory',
]

TOOLS = [
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "save_memory",
            "description": (
                "Save to layered long-term memory. Keep under 450 chars. Layers: "
                "'events' (default — things that happened), 'self' (who you are, "
                "decisions, changes of mind), 'entities' (a fact about a person/place/"
                "thing — requires entity name), 'knowledge' (reference material). "
                f"Suggested labels: {SUGGESTED_LABELS}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The information to remember"
                    },
                    "layer": {
                        "type": "string",
                        "enum": LAYER_KEYS,
                        "description": "Destination layer. Default: events."
                    },
                    "entity": {
                        "type": "string",
                        "description": "Entity name (person/place/thing) this fact belongs to. Required when layer=entities."
                    },
                    "label": {
                        "type": "string",
                        "description": "Category label"
                    },
                    "favorite": {
                        "type": "boolean",
                        "description": "Mark as a favorite memory — one you want to keep close."
                    },
                    "private_key": {
                        "type": "string",
                        "description": "Optional gating word. Set only if user asked to make this memory private with a specific word."
                    }
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "search_memory",
            "description": "Semantic + full-text search across all memory layers. Optionally restrict to one layer or filter by label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms or topic"
                    },
                    "layer": {
                        "type": "string",
                        "enum": LAYER_KEYS,
                        "description": "Restrict to one layer. Omit to search all."
                    },
                    "label": {
                        "type": "string",
                        "description": "Filter by label(s), comma-separated"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results",
                        "default": 10
                    },
                    "private_key": {
                        "type": "string",
                        "description": "Gating word — pass to include private rows saved with this word."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "get_recent_memories",
            "description": "Get most recent memories, optionally from one layer or filtered by label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "How many",
                        "default": 10
                    },
                    "layer": {
                        "type": "string",
                        "enum": LAYER_KEYS,
                        "description": "Restrict to one layer. Omit for all."
                    },
                    "label": {
                        "type": "string",
                        "description": "Filter by label(s), comma-separated"
                    },
                    "private_key": {
                        "type": "string",
                        "description": "Gating word — pass to include private rows saved with this word."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "delete_memory",
            "description": "Delete a memory by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "integer",
                        "description": "Memory ID (shown in brackets like [42])"
                    },
                    "private_key": {
                        "type": "string",
                        "description": "Required to delete a private row. Must match save-time word."
                    }
                },
                "required": ["memory_id"]
            }
        }
    },
]

SIMILARITY_THRESHOLD = 0.40
MAX_CHUNK_LENGTH = 512
# Per (scope, layer) — the old system capped memories and knowledge separately
# at 50k each; layers restore that separation inside the single table.
MAX_CHUNKS_PER_SCOPE_LAYER = 50_000


def _now() -> str:
    """One timestamp format everywhere: ISO-8601 UTC with offset. Uniform format
    means lexicographic ORDER BY == chronological — that property is load-bearing."""
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ─── Database ────────────────────────────────────────────────────────────────

def _get_db_path():
    global _db_path
    if _db_path is None:
        # Anchor to config.py location (project root) — stable regardless of
        # where this file lives. Same pattern as plugins/memory (Phase 4 lesson).
        import config
        _db_path = Path(config.__file__).parent / "user" / "memory" / "mind.db"
    return _db_path


@contextmanager
def _get_connection():
    _ensure_db()
    conn = sqlite3.connect(_get_db_path(), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        yield conn
    finally:
        conn.close()


def _safe_rename_corrupted(db_path):
    """Rename db_path → .db.corrupted, timestamp-suffixed if a prior backup exists."""
    base_backup = db_path.with_suffix('.db.corrupted')
    if not base_backup.exists():
        target = base_backup
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        target = db_path.with_name(db_path.name + f'.corrupted.{ts}')
    try:
        db_path.rename(target)
        logger.error(f"[MINDPALACE] Corrupted DB preserved at {target} — "
                     f"fresh DB will be created; re-run import_v2 to recover copied data")
    except Exception as e:
        logger.error(f"[MINDPALACE] Could not preserve corrupted DB at {target}: {e}")


def _setup_fts(cursor):
    """FTS5 external-content table over chunks + sync triggers."""
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content, label,
            content=chunks, content_rowid=id
        )
    """)
    cursor.execute("DROP TRIGGER IF EXISTS chunks_fts_insert")
    cursor.execute("DROP TRIGGER IF EXISTS chunks_fts_delete")
    cursor.execute("DROP TRIGGER IF EXISTS chunks_fts_update")
    cursor.execute("""
        CREATE TRIGGER chunks_fts_insert
        AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, content, label)
            VALUES (new.id, new.content, new.label);
        END
    """)
    cursor.execute("""
        CREATE TRIGGER chunks_fts_delete
        AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content, label)
            VALUES ('delete', old.id, old.content, old.label);
        END
    """)
    # Only content/label changes touch the index — embedding updates must not.
    cursor.execute("""
        CREATE TRIGGER chunks_fts_update
        AFTER UPDATE OF content, label ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content, label)
            VALUES ('delete', old.id, old.content, old.label);
            INSERT INTO chunks_fts(rowid, content, label)
            VALUES (new.id, new.content, new.label);
        END
    """)
    cursor.execute("SELECT COUNT(*) FROM chunks")
    n_chunks = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM chunks_fts")
    n_fts = cursor.fetchone()[0]
    if n_chunks > 0 and n_fts == 0:
        logger.info(f"[MINDPALACE] Populating FTS index from {n_chunks} existing chunks...")
        cursor.execute("""
            INSERT INTO chunks_fts(rowid, content, label)
            SELECT id, content, label FROM chunks
        """)


def _ensure_db():
    global _db_initialized
    if _db_initialized:
        return True
    with _db_lock:
        if _db_initialized:
            return True
        try:
            db_path = _get_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)

            if db_path.exists():
                try:
                    conn = sqlite3.connect(db_path, timeout=10)
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    conn.close()
                    if result[0] != 'ok':
                        logger.error(f"[MINDPALACE] Integrity check failed: {result[0]}")
                        _safe_rename_corrupted(db_path)
                except sqlite3.DatabaseError as e:
                    logger.error(f"[MINDPALACE] Database corrupted: {e}")
                    _safe_rename_corrupted(db_path)

            for suffix in ['-wal', '-shm', '-journal']:
                stale = db_path.with_name(db_path.name + suffix)
                if stale.exists() and not db_path.exists():
                    stale.unlink()

            conn = sqlite3.connect(db_path, timeout=10)
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")

            # Layer registry — durable half of the registry; LAYERS dict is the
            # runtime half. Plugin-added layers (v1.1) insert here with their
            # own owner.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS layers (
                    key TEXT PRIMARY KEY,
                    num INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT 'mindpalace',
                    created TEXT NOT NULL
                )
            ''')
            for key, spec in LAYERS.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO layers (key, num, label, owner, created) "
                    "VALUES (?, ?, ?, 'mindpalace', ?)",
                    (key, spec['num'], spec['label'], _now())
                )

            # Entities — Layer 2's spine. UNIQUE NOCASE per scope (old people-store
            # upsert contract). `mentions` = count since last librarian pass.
            # `meta` = JSON bag for layer-specific extras (spiderable later).
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'default',
                    kind TEXT,
                    mentions INTEGER NOT NULL DEFAULT 0,
                    meta TEXT,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_name_scope
                ON entities(name COLLATE NOCASE, scope)
            ''')

            # Chunks — THE uniform unit. Every layer stores these.
            #   tier: L2 detail level (1=headline/epicenter, 2=facts, 3=trivia)
            #   chunk_index + source: L3 sub-chunk group identity (neighbor
            #     stitching later needs (label, source, chunk_index))
            #   importance: system-facing salience; the AI NEVER sees the number.
            #     favorite=true maps to 0.95 (>0.9 = never fades). NULL = unset.
            #   meta: JSON bag — write-time mechanical metadata + librarian's
            #     rich metadata later; plugin layers bring custom fields here.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    layer TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'default',
                    content TEXT NOT NULL,
                    entity_id INTEGER,
                    tier INTEGER,
                    chunk_index INTEGER,
                    source TEXT,
                    label TEXT,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    importance REAL,
                    mentions INTEGER NOT NULL DEFAULT 0,
                    private_key TEXT,
                    meta TEXT,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    embedding BLOB,
                    embedding_provider TEXT,
                    embedding_dim INTEGER
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chunks_layer_scope ON chunks(layer, scope)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chunks_created ON chunks(created)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chunks_entity ON chunks(entity_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chunks_label ON chunks(label)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chunks_private_key ON chunks(private_key)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)')

            # Edges — typed graph, schema NOW / spider LATER. weight: structural
            # rail edges 1.0, metadata edges < 1.0 (cost > 1 hop of budget).
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    src_type TEXT NOT NULL DEFAULT 'chunk',
                    src_id INTEGER NOT NULL,
                    dst_type TEXT NOT NULL DEFAULT 'chunk',
                    dst_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    created TEXT NOT NULL
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_type, src_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_type, dst_id)')

            try:
                _setup_fts(cursor)
            except sqlite3.DatabaseError as e:
                logger.warning(f"[MINDPALACE] FTS5 corrupted, rebuilding: {e}")
                cursor.execute("DROP TABLE IF EXISTS chunks_fts")
                cursor.execute("DROP TRIGGER IF EXISTS chunks_fts_insert")
                cursor.execute("DROP TRIGGER IF EXISTS chunks_fts_delete")
                cursor.execute("DROP TRIGGER IF EXISTS chunks_fts_update")
                conn.commit()
                _setup_fts(cursor)

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mind_scopes (
                    name TEXT PRIMARY KEY,
                    created TEXT NOT NULL
                )
            ''')
            cursor.execute("INSERT OR IGNORE INTO mind_scopes (name, created) VALUES ('default', ?)",
                           (_now(),))

            conn.commit()
            conn.close()

            _db_initialized = True
            logger.info(f"[MINDPALACE] Mind database ready at {db_path} "
                        f"({len(LAYERS)} layers, FTS5 + embeddings)")
            return True

        except Exception as e:
            logger.error(f"[MINDPALACE] Failed to initialize mind database: {e}")
            return False


_backfill_done = False

def _backfill_embeddings():
    """Embed + stamp provenance for chunks lacking either. Lazy, first-search.
    Flag only latches True on clean completion (transient-failure retry, v2 lesson)."""
    global _backfill_done
    if _backfill_done:
        return

    embedder = _get_embedder()
    if not embedder.available:
        _backfill_done = True
        return

    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, content FROM chunks '
            'WHERE embedding IS NULL OR embedding_provider IS NULL OR embedding_dim IS NULL'
        )
        rows = cursor.fetchall()

    if not rows:
        _backfill_done = True
        return

    logger.info(f"[MINDPALACE] Backfilling embeddings for {len(rows)} chunks...")
    batch_size = 32
    filled = 0
    transient_failure = False
    from core.embeddings import stamp_embedding
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ids = [r[0] for r in batch]
        texts = [r[1] for r in batch]
        embs = embedder.embed(texts, prefix='search_document')
        if embs is None:
            transient_failure = True
            break
        try:
            with _get_connection() as conn:
                cursor = conn.cursor()
                for row_id, emb in zip(ids, embs):
                    blob, provider_id, dim = stamp_embedding(emb, embedder)
                    cursor.execute(
                        'UPDATE chunks SET embedding = ?, embedding_provider = ?, embedding_dim = ? '
                        'WHERE id = ?',
                        (blob, provider_id, dim, row_id)
                    )
                conn.commit()
                filled += len(batch)
        except Exception as e:
            logger.error(f"[MINDPALACE] Backfill batch failed: {e}")
            transient_failure = True
            break

    if transient_failure:
        logger.warning(f"[MINDPALACE] Backfill incomplete: {filled}/{len(rows)} — retry next search")
    else:
        _backfill_done = True
        if filled:
            logger.info(f"[MINDPALACE] Backfill complete: {filled}/{len(rows)} chunks embedded")


def reset_backfill_latch():
    """Clear the backfill latch so the next search sweeps for unembedded rows.
    Called by import_tools after bulk-copying rows that may lack vectors."""
    global _backfill_done
    _backfill_done = False


def _get_current_scope():
    try:
        from core.chat.function_manager import scope_memory
        return scope_memory.get()
    except Exception as e:
        # None (not 'default') → executor disables cleanly. Silent-default was
        # a real bug class (2026-04-20 witch hunt) — never fall back to a real
        # scope name on error.
        logger.warning(f"[MINDPALACE] Could not get memory scope: {e}, returning None (disabled)")
        return None


def _scope_condition(scope, col='scope'):
    """(sql_fragment, params) including the read-only 'global' overlay."""
    if scope == 'global':
        return f"{col} = ?", [scope]
    return f"{col} IN (?, 'global')", [scope]


def _private_key_clause(private_key, col='private_key'):
    """No key → public rows only. Key → public rows + rows gated by that word.
    Plaintext compare on purpose — cross-persona behavioral gate, not crypto."""
    if private_key:
        return f"({col} IS NULL OR {col} = ?)", [private_key]
    return f"{col} IS NULL", []


def _validate_layer(layer):
    """Return (layer_or_None, error_or_None). None layer = all layers (reads)."""
    if layer is None or layer == '':
        return None, None
    layer = str(layer).strip().lower()
    if layer not in LAYERS:
        return None, f"Unknown layer '{layer}'. Valid layers: {', '.join(LAYER_KEYS)}."
    return layer, None


# ─── Public API (routes + import_tools) ──────────────────────────────────────

def get_scopes():
    try:
        with _get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT scope, COUNT(*) FROM chunks GROUP BY scope')
            counts = {row[0]: row[1] for row in cursor.fetchall()}
            cursor.execute('SELECT name FROM mind_scopes ORDER BY name')
            registered = [row[0] for row in cursor.fetchall()]
        all_scopes = set(registered) | set(counts.keys()) | {'default'}
        return [{"name": name, "count": counts.get(name, 0)} for name in sorted(all_scopes)]
    except Exception as e:
        logger.error(f"[MINDPALACE] Error getting scopes: {e}")
        return [{"name": "default", "count": 0}]


def create_scope(name: str) -> bool:
    try:
        with _get_connection() as conn:
            conn.execute("INSERT OR IGNORE INTO mind_scopes (name, created) VALUES (?, ?)",
                         (name, _now()))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"[MINDPALACE] Failed to create scope '{name}': {e}")
        return False


def delete_scope(name: str) -> dict:
    """Delete a mind scope and ALL its chunks/entities. Old-system data unaffected."""
    if name == 'default':
        return {"error": "Cannot delete the default scope"}
    try:
        with _get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM chunks WHERE scope = ?', (name,))
            count = cursor.fetchone()[0]
            # Edges referencing this scope's chunks/entities go too.
            cursor.execute('''
                DELETE FROM edges WHERE
                    (src_type = 'chunk'  AND src_id IN (SELECT id FROM chunks   WHERE scope = ?)) OR
                    (dst_type = 'chunk'  AND dst_id IN (SELECT id FROM chunks   WHERE scope = ?)) OR
                    (src_type = 'entity' AND src_id IN (SELECT id FROM entities WHERE scope = ?)) OR
                    (dst_type = 'entity' AND dst_id IN (SELECT id FROM entities WHERE scope = ?))
            ''', (name, name, name, name))
            cursor.execute('DELETE FROM chunks WHERE scope = ?', (name,))
            cursor.execute('DELETE FROM entities WHERE scope = ?', (name,))
            cursor.execute('DELETE FROM mind_scopes WHERE name = ?', (name,))
            conn.commit()
        logger.info(f"[MINDPALACE] Deleted scope '{name}' with {count} chunks")
        # Sweep chats whose settings still point at the deleted scope — the
        # memory_scope settings key is shared with the classic plugin (only one
        # is ever active), so the sweep is correct for whichever owns it now.
        try:
            from core.chat.scope_cleanup import sweep_orphaned_scope_ref
            sweep_orphaned_scope_ref('memory_scope', name)
        except Exception as e:
            logger.warning(f"[MINDPALACE] memory_scope sweep after delete failed: {e}")
        return {"deleted_count": count}
    except Exception as e:
        logger.error(f"[MINDPALACE] Failed to delete scope '{name}': {e}")
        return {"error": str(e)}


def upsert_entity(cursor, name: str, scope: str, kind: str = None) -> int:
    """Find-or-create an entity by (name NOCASE, scope) on an open cursor.
    Returns entity id. Shared with import_tools."""
    cursor.execute(
        'SELECT id FROM entities WHERE name = ? COLLATE NOCASE AND scope = ?',
        (name, scope)
    )
    row = cursor.fetchone()
    if row:
        if kind:
            cursor.execute('UPDATE entities SET kind = COALESCE(kind, ?), updated = ? WHERE id = ?',
                           (kind, _now(), row[0]))
        return row[0]
    now = _now()
    cursor.execute(
        'INSERT INTO entities (name, scope, kind, created, updated) VALUES (?, ?, ?, ?, ?)',
        (name.strip(), scope, kind, now, now)
    )
    return cursor.lastrowid


# ─── Formatting ──────────────────────────────────────────────────────────────

def _format_time_ago(timestamp_str: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        import config
        tz_name = getattr(config, 'USER_TIMEZONE', 'UTC') or 'UTC'
        try: user_tz = ZoneInfo(tz_name)
        except Exception: user_tz = ZoneInfo('UTC')
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZoneInfo('UTC'))
        diff = datetime.now(user_tz) - ts
        days, hours, minutes = diff.days, diff.seconds // 3600, (diff.seconds % 3600) // 60
        if days > 0:
            return f"{days}d ago"
        elif hours > 0:
            return f"{hours}h ago"
        elif minutes > 0:
            return f"{minutes}m ago"
        return "just now"
    except Exception:
        return ""


def _format_chunk(row_id, content, created, label, layer, entity_name=None):
    """[42] (2d ago) [events] content  /  [7] (1d ago) [entities:Krem] content
    The [id] marker is part of the conversational contract (delete_memory)."""
    time_ago = _format_time_ago(created)
    time_str = f" ({time_ago})" if time_ago else ""
    layer_tag = f"[{layer}:{entity_name}]" if entity_name else f"[{layer}]"
    label_str = f" [{label}]" if label else ""
    return f"[{row_id}]{time_str} {layer_tag}{label_str} {content}"


def _parse_labels(label) -> list:
    if not label:
        return []
    return [l.strip().lower() for l in label.split(',') if l.strip()]


def _sanitize_fts_query(query: str, use_or=False, use_prefix=False) -> str:
    sanitized = re.sub(r'[^\w\s"*]', ' ', query)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    if not sanitized or '"' in sanitized:
        return sanitized
    terms = sanitized.split()
    if use_prefix:
        terms = [t + '*' if not t.endswith('*') else t for t in terms]
    if use_or and len(terms) > 1:
        return ' OR '.join(terms)
    return ' '.join(terms)


def _read_filters(scope, labels, private_key, layer):
    """Shared WHERE fragments for the read paths: scope overlay + private gate
    + optional layer + optional labels. Returns (sql, params) to AND together."""
    scope_sql, scope_params = _scope_condition(scope, 'c.scope')
    pk_sql, pk_params = _private_key_clause(private_key, 'c.private_key')
    sql = [scope_sql, pk_sql]
    params = scope_params + pk_params
    if layer:
        sql.append('c.layer = ?')
        params.append(layer)
    if labels:
        placeholders = ','.join('?' * len(labels))
        sql.append(f'c.label IN ({placeholders})')
        params.extend(labels)
    return ' AND '.join(sql), params


SELECT_CHUNK = ('SELECT c.id, c.content, c.created, c.label, c.layer, e.name '
                'FROM chunks c LEFT JOIN entities e ON c.entity_id = e.id ')


# ─── Core operations ─────────────────────────────────────────────────────────

def _save_memory(content: str, scope: str, layer: str = None, entity: str = None,
                 label: str = None, favorite: bool = False, private_key: str = None) -> tuple:
    try:
        if not content or not content.strip():
            return "Cannot save empty memory.", False
        if len(content) > MAX_CHUNK_LENGTH:
            return (f"Memory too long ({len(content)} chars). Max is {MAX_CHUNK_LENGTH}. "
                    f"Write a shorter, more concise memory."), False

        layer, err = _validate_layer(layer)
        if err:
            return err, False
        layer = layer or 'events'
        entity = entity.strip() if (entity and entity.strip()) else None
        if layer == 'entities' and not entity:
            return "Saving to the entities layer requires an entity name (person/place/thing).", False

        with _get_connection() as conn:
            count = conn.execute(
                'SELECT COUNT(*) FROM chunks WHERE scope = ? AND layer = ?', (scope, layer)
            ).fetchone()[0]
        if count >= MAX_CHUNKS_PER_SCOPE_LAYER:
            return (f"Layer '{layer}' in scope '{scope}' is at the row limit "
                    f"({MAX_CHUNKS_PER_SCOPE_LAYER:,}). Delete some memories first."), False

        content = content.strip()
        label = label.strip().lower() if label else None
        private_key = private_key.strip() if (private_key and private_key.strip()) else None
        favorite = bool(favorite)
        # Favorite is the qualitative lever she controls; the number stays
        # behind the curtain. >0.9 = the never-fades band.
        importance = 0.95 if favorite else None

        embedding_blob = None
        embedding_provider = None
        embedding_dim = None
        embed_failed_mid_session = False
        embedder = _get_embedder()
        if embedder.available:
            embs = embedder.embed([content], prefix='search_document')
            if embs is not None:
                from core.embeddings import stamp_embedding
                embedding_blob, embedding_provider, embedding_dim = stamp_embedding(embs[0], embedder)
            else:
                embed_failed_mid_session = True
                logger.warning("[MINDPALACE] Embed returned None during save — "
                               "row stored with NULL vector, re-embedded on next search.")

        now = _now()
        with _get_connection() as conn:
            cursor = conn.cursor()
            entity_id = None
            tier = None
            if layer == 'entities':
                entity_id = upsert_entity(cursor, entity, scope)
                tier = 2  # facts; headlines (tier 1) are librarian/import territory

            # Tier A metadata + entity-match edge seeding — mechanical, never
            # blocks the save (failure degrades to a thinner meta row).
            meta_json = None
            matched, mention_ids = [], []
            try:
                from plugins.mindpalace.tools import metadata as md
                cursor.execute(
                    "SELECT id, name FROM entities WHERE scope IN (?, 'global')",
                    (scope,))
                ent_rows = cursor.fetchall()
                matched = md.match_entities(content, [n for _, n in ent_rows])
                if entity:
                    # Own entity is covered by the entity_id column — no
                    # self-edge, no "linked:" echo of the entity just named.
                    matched = [m for m in matched if m.lower() != entity.lower()]
                name_to_id = {n: i for i, n in ent_rows}
                mention_ids = [name_to_id[m] for m in matched if m in name_to_id]
                exclude = {m.lower() for m in matched}
                if entity:
                    exclude.add(entity.lower())
                meta_json = json.dumps(
                    md.save_meta(content, exclude_names=exclude),
                    ensure_ascii=False)
            except Exception as e:
                logger.warning(f"[MINDPALACE] Metadata stamping failed (save continues): {e}")

            cursor.execute(
                'INSERT INTO chunks (layer, scope, content, entity_id, tier, label, '
                'favorite, importance, private_key, meta, created, updated, '
                'embedding, embedding_provider, embedding_dim) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (layer, scope, content, entity_id, tier, label,
                 1 if favorite else 0, importance, private_key, meta_json, now, now,
                 embedding_blob, embedding_provider, embedding_dim)
            )
            chunk_id = cursor.lastrowid
            if mention_ids:
                try:
                    md.seed_edges(cursor, chunk_id, mention_ids, now)
                except Exception as e:
                    logger.warning(f"[MINDPALACE] Edge seeding failed (save continues): {e}")
            conn.commit()

        if embed_failed_mid_session:
            global _backfill_done
            _backfill_done = False

        bits = [f"ID: {chunk_id}", f"layer: {layer}"]
        if entity:
            bits.append(f"entity: {entity}")
        if matched:
            bits.append(f"linked: {', '.join(matched)}")
        if label:
            bits.append(f"label: {label}")
        if favorite:
            bits.append("favorite")
        if private_key:
            bits.append("private")
        logger.info(f"[MINDPALACE] Stored chunk {chunk_id} ({layer}) in scope '{scope}'")
        return f"Memory saved ({', '.join(bits)})", True

    except Exception as e:
        logger.error(f"[MINDPALACE] Error saving memory: {e}")
        return f"Failed to save memory: {e}", False


def _fts_search(cursor, fts_query, scope, labels, limit, private_key=None, layer=None):
    where, params = _read_filters(scope, labels, private_key, layer)
    cursor.execute(f'''
        SELECT c.id, c.content, c.created, c.label, c.layer, e.name,
               bm25(chunks_fts) as rank
        FROM chunks_fts f
        JOIN chunks c ON f.rowid = c.id
        LEFT JOIN entities e ON c.entity_id = e.id
        WHERE chunks_fts MATCH ? AND {where}
        ORDER BY rank LIMIT ?
    ''', [fts_query] + params + [limit])
    return cursor.fetchall()


def _vector_search(query: str, scope: str, labels: list, limit: int,
                   private_key: str = None, layer: str = None) -> list:
    """Cosine over provenance-matched rows. Recency-windowed candidate set."""
    embedder = _get_embedder()
    if not embedder.available:
        return []

    query_emb = embedder.embed([query], prefix='search_query')
    if query_emb is None:
        return []
    query_vec = query_emb[0]
    query_dim = int(query_vec.shape[0])
    active_provider = getattr(embedder, 'provider_id', None)

    where, params = _read_filters(scope, labels, private_key, layer)
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f'SELECT c.id, c.content, c.created, c.label, c.layer, e.name, c.embedding '
            f'FROM chunks c LEFT JOIN entities e ON c.entity_id = e.id '
            f'WHERE {where} AND c.embedding IS NOT NULL '
            f'AND c.embedding_provider = ? AND c.embedding_dim = ? '
            f'ORDER BY c.created DESC LIMIT 10000',
            params + [active_provider, query_dim])
        rows = cursor.fetchall()

    if not rows:
        return []

    scored = []
    for row_id, content, created, lbl, lyr, ename, emb_blob in rows:
        try:
            emb = np.frombuffer(emb_blob, dtype=np.float32)
            if emb.shape[0] != query_dim:
                continue
            sim = float(np.dot(query_vec, emb))
            if np.isnan(sim) or np.isinf(sim):
                continue
            if sim >= SIMILARITY_THRESHOLD:
                scored.append((row_id, content, created, lbl, lyr, ename, sim))
        except Exception:
            continue

    scored.sort(key=lambda x: x[6], reverse=True)
    return scored[:limit]


def _search_memory(query: str, scope: str, limit: int = 10, label: str = None,
                   layer: str = None, private_key: str = None) -> tuple:
    """v2 cascade with a layer filter: FTS-AND → FTS-OR+prefix → vector → LIKE.
    First non-empty strategy wins. Spider/depth arrives in the next step."""
    try:
        if not query or not query.strip():
            return "Search query cannot be empty.", False

        layer, err = _validate_layer(layer)
        if err:
            return err, False
        labels = _parse_labels(label)
        label_note = f" with labels '{label}'" if labels else ""
        layer_note = f" in layer '{layer}'" if layer else ""
        private_key = private_key.strip() if (private_key and private_key.strip()) else None

        _backfill_embeddings()

        with _get_connection() as conn:
            cursor = conn.cursor()
            fts_exact = _sanitize_fts_query(query)
            if fts_exact:
                try:
                    rows = _fts_search(cursor, fts_exact, scope, labels, limit,
                                       private_key=private_key, layer=layer)
                    if rows:
                        results = [_format_chunk(*r[:6]) for r in rows]
                        return f"Found {len(rows)} memories:\n" + "\n".join(results), True

                    fts_broad = _sanitize_fts_query(query, use_or=True, use_prefix=True)
                    if fts_broad != fts_exact:
                        rows = _fts_search(cursor, fts_broad, scope, labels, limit,
                                           private_key=private_key, layer=layer)
                        if rows:
                            results = [_format_chunk(*r[:6]) for r in rows]
                            return f"Found {len(rows)} memories:\n" + "\n".join(results), True
                except sqlite3.OperationalError as e:
                    logger.warning(f"[MINDPALACE] FTS5 query failed: {e}")

        vec_results = _vector_search(query, scope, labels, limit,
                                     private_key=private_key, layer=layer)
        if vec_results:
            results = [_format_chunk(*r[:6]) for r in vec_results]
            return f"Found {len(vec_results)} memories:\n" + "\n".join(results), True

        # LIKE fallback
        terms = query.lower().split()[:5]
        if terms:
            where, params = _read_filters(scope, labels, private_key, layer)
            conditions = ' OR '.join(['c.content LIKE ?' for _ in terms])
            like_params = [f'%{t}%' for t in terms]
            with _get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    SELECT_CHUNK + f'WHERE {where} AND ({conditions}) '
                    f'ORDER BY c.created DESC LIMIT ?',
                    params + like_params + [limit])
                rows = cursor.fetchall()
            if rows:
                results = [_format_chunk(*r) for r in rows]
                return f"Found {len(rows)} memories:\n" + "\n".join(results), True

        return f"No memories found for '{query}'{layer_note}{label_note}.", True

    except Exception as e:
        logger.error(f"[MINDPALACE] Error searching memory: {e}")
        return f"Search failed: {e}", False


def _get_recent_memories(scope: str, count: int = 10, label: str = None,
                         layer: str = None, private_key: str = None) -> tuple:
    try:
        layer, err = _validate_layer(layer)
        if err:
            return err, False
        labels = _parse_labels(label)
        private_key = private_key.strip() if (private_key and private_key.strip()) else None
        where, params = _read_filters(scope, labels, private_key, layer)
        with _get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                SELECT_CHUNK + f'WHERE {where} ORDER BY c.created DESC LIMIT ?',
                params + [count])
            rows = cursor.fetchall()
        if not rows:
            notes = (f" in layer '{layer}'" if layer else "") + (f" with labels '{label}'" if labels else "")
            return f"No memories stored{notes}.", True
        results = [_format_chunk(*r) for r in rows]
        return f"Recent {len(rows)} memories:\n" + "\n".join(results), True
    except Exception as e:
        logger.error(f"[MINDPALACE] Error getting recent memories: {e}")
        return f"Failed to retrieve memories: {e}", False


def _delete_memory(memory_id: int, scope: str, private_key: str = None) -> tuple:
    try:
        if not isinstance(memory_id, int) or memory_id < 1:
            return "Invalid memory ID. Use the number shown in brackets [N].", False
        private_key = private_key.strip() if (private_key and private_key.strip()) else None
        with _get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, content, private_key FROM chunks WHERE id = ? AND scope = ?',
                (memory_id, scope),
            )
            row = cursor.fetchone()
            if not row:
                return f"Memory [{memory_id}] not found in current memory slot.", False
            row_pk = row[2]
            if row_pk is not None and row_pk != private_key:
                return f"Memory [{memory_id}] is private — pass the matching private_key to delete.", False
            pk_sql, pk_params = _private_key_clause(private_key)
            cursor.execute(
                f'DELETE FROM chunks WHERE id = ? AND scope = ? AND {pk_sql}',
                [memory_id, scope] + pk_params,
            )
            # Keep the graph consistent from day one.
            cursor.execute(
                "DELETE FROM edges WHERE (src_type='chunk' AND src_id=?) OR (dst_type='chunk' AND dst_id=?)",
                (memory_id, memory_id))
            conn.commit()
        preview = row[1][:50] + ('...' if len(row[1]) > 50 else '')
        logger.info(f"[MINDPALACE] Deleted chunk {memory_id} from scope '{scope}'")
        return f"Deleted memory [{memory_id}]: {preview}", True
    except Exception as e:
        logger.error(f"[MINDPALACE] Error deleting memory: {e}")
        return f"Failed to delete memory: {e}", False


# ─── Executor ────────────────────────────────────────────────────────────────

def execute(function_name: str, arguments: dict, config) -> tuple:
    try:
        scope = _get_current_scope()
        if scope is None:
            return "Memory is disabled for this chat.", False
        if scope == 'global':
            return ("Cannot write to the global scope. Global is read-only for the AI — "
                    "only the user can add entries there via the UI."), False

        if function_name == "save_memory":
            return _save_memory(arguments.get("content", ""), scope,
                                layer=arguments.get("layer"),
                                entity=arguments.get("entity"),
                                label=arguments.get("label"),
                                favorite=arguments.get("favorite", False),
                                private_key=arguments.get("private_key"))
        elif function_name == "search_memory":
            return _search_memory(arguments.get("query", ""), scope,
                                  limit=arguments.get("limit", 10),
                                  label=arguments.get("label"),
                                  layer=arguments.get("layer"),
                                  private_key=arguments.get("private_key"))
        elif function_name == "get_recent_memories":
            return _get_recent_memories(scope, count=arguments.get("count", 10),
                                        label=arguments.get("label"),
                                        layer=arguments.get("layer"),
                                        private_key=arguments.get("private_key"))
        elif function_name == "delete_memory":
            memory_id = arguments.get("memory_id")
            if memory_id is None:
                return "Missing memory_id parameter.", False
            return _delete_memory(int(memory_id), scope,
                                  private_key=arguments.get("private_key"))
        else:
            return f"Unknown mind palace function: {function_name}", False
    except Exception as e:
        logger.error(f"[MINDPALACE] Function error: {e}")
        return f"Mind palace error: {e}", False
