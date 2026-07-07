# plugins/mindpalace/tools/import_tools.py
# v2 → v3 memory importer. Copies the classic memory system's three DBs
# (memory.db, knowledge.db) into the mind palace's single mind.db, mapping
# every old row onto the chunk/entity model.
#
# HARD contract:
#   - Source DBs are opened READ-ONLY (mode=ro URI). They are NEVER written.
#     Old data stays put as the guaranteed switch-back path (see palace_tools
#     module header). This importer is additive-only on the destination side.
#   - Idempotent: every copied chunk records meta.import_key = "v2:<store>:<id>".
#     A single up-front query loads all existing keys into a set; rows whose key
#     is already present are skipped. Re-running copies zero duplicates.
#   - Scope-independent: copies ALL scopes verbatim, including 'global'. The
#     global write-block that gates the AI's own writes does NOT apply here —
#     import is a bulk copy, not an AI write.
#   - Provenance triple is all-or-nothing: (embedding, embedding_provider,
#     embedding_dim) travel together byte-for-byte, or all three land NULL and
#     the backfill re-embeds later with the correct 'search_document' prefix.
#   - Timestamps are normalized to one ISO-8601 UTC-with-offset format (the
#     format palace_tools._now() emits) so lexicographic ORDER BY stays
#     chronological across imported + native rows.

import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ENABLED = True
EMOJI = '📦'

AVAILABLE_FUNCTIONS = ['import_v2']

TOOLS = [
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "import_v2",
            "description": (
                "Copy the classic memory system's data into the mind palace layers. "
                "Memories → events layer, people → entities layer, knowledge → knowledge "
                "layer. Copies ALL scopes (including global). The old DBs are opened "
                "read-only and are never modified — they remain the switch-back path. "
                "Safe to re-run: already-imported rows are skipped (idempotent), so a "
                "second run copies zero duplicates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "what": {
                        "type": "string",
                        "enum": ["memories", "people", "knowledge", "all"],
                        "description": "Which store(s) to import. Default: all.",
                        "default": "all"
                    }
                }
            }
        }
    },
]

COMMIT_EVERY = 500


# ─── Source DB paths (anchored like palace_tools / classic memory plugin) ─────

def _source_path(config, filename: str) -> Path:
    return Path(config.__file__).parent / "user" / filename


def _connect_ro(path: Path):
    """Open a source DB strictly read-only. mode=ro forbids any write; if the
    file is missing sqlite raises OperationalError, which the caller treats as
    'store absent' (skip, not error)."""
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def _columns(cursor, table: str) -> set:
    """Column-name set for a table via PRAGMA. Empty set = table absent."""
    try:
        cursor.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cursor.fetchall()}
    except sqlite3.DatabaseError:
        return set()


def _get(row: dict, *names):
    """First present, non-None value among candidate column names."""
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


# ─── Timestamp normalization ──────────────────────────────────────────────────

def _normalize_ts(raw):
    """Return (iso_utc_str, original_or_None).

    Rules (the old system mixed formats; the palace requires uniform ISO-8601
    UTC with offset):
      - 'YYYY-MM-DD HH:MM:SS[.ffffff]' (SPACE separator) = SQLite
        CURRENT_TIMESTAMP = already UTC, naive → attach UTC → isoformat.
      - 'T'-separated naive = legacy Python local-time isoformat → interpret in
        the system local timezone → convert to UTC.
      - tz-aware anything → convert to UTC.
      - unparseable / NULL → palace _now(), and hand back the original string so
        the caller can record it in meta.original_timestamp.
    """
    from plugins.mindpalace.tools import palace_tools

    if raw is None:
        return palace_tools._now(), None

    s = str(raw).strip()
    if not s:
        return palace_tools._now(), str(raw)

    space_sep = ('T' not in s) and (' ' in s)
    try:
        # fromisoformat handles both ' ' and 'T' separators and optional offset.
        dt = datetime.fromisoformat(s)
    except ValueError:
        return palace_tools._now(), s

    if dt.tzinfo is not None:
        # Already tz-aware → normalize to UTC.
        dt = dt.astimezone(timezone.utc)
    elif space_sep:
        # SQLite CURRENT_TIMESTAMP: UTC wall-clock, just missing the tzinfo.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        # 'T'-separated naive: legacy local-time isoformat. Interpret in the
        # system local zone, then convert to UTC.
        dt = dt.astimezone(timezone.utc)

    return dt.isoformat(timespec='seconds'), None


# ─── Provenance triple (all-or-nothing) ──────────────────────────────────────

def _provenance(row: dict):
    """Carry (embedding, embedding_provider, embedding_dim) byte-for-byte only
    if all three are present; otherwise all three NULL (backfill re-embeds)."""
    emb = _get(row, 'embedding')
    prov = _get(row, 'embedding_provider')
    dim = _get(row, 'embedding_dim')
    if emb is not None and prov is not None and dim is not None:
        return emb, prov, dim
    return None, None, None


# ─── Insert helper ────────────────────────────────────────────────────────────

def _insert_chunk(cursor, *, layer, scope, content, created, updated,
                  entity_id=None, tier=None, chunk_index=None, source=None,
                  label=None, private_key=None, meta=None,
                  embedding=None, embedding_provider=None, embedding_dim=None):
    cursor.execute(
        'INSERT INTO chunks (layer, scope, content, entity_id, tier, chunk_index, '
        'source, label, private_key, meta, created, updated, '
        'embedding, embedding_provider, embedding_dim) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (layer, scope, content, entity_id, tier, chunk_index,
         source, label, private_key, meta, created, updated,
         embedding, embedding_provider, embedding_dim)
    )


# ─── Per-store importers ──────────────────────────────────────────────────────

def _import_memories(config, dest_cursor, existing_keys, scopes_seen):
    """memories → chunks(layer='events'). Returns (copied, skipped, failed)."""
    path = _source_path(config, "memory.db")
    if not path.exists():
        return None  # signals "source absent"

    copied = skipped = failed = 0
    with _connect_ro(path) as src:
        src.row_factory = sqlite3.Row
        scur = src.cursor()
        cols = _columns(scur, 'memories')
        if not cols:
            return (0, 0, 0)

        select_cols = [c for c in (
            'id', 'content', 'timestamp', 'scope', 'label', 'private_key',
            'embedding', 'embedding_provider', 'embedding_dim'
        ) if c in cols]
        scur.execute(f"SELECT {', '.join(select_cols)} FROM memories")

        pending = 0
        for r in scur.fetchall():
            row = {k: r[k] for k in r.keys()}
            old_id = row.get('id')
            key = f"v2:memories:{old_id}"
            if key in existing_keys:
                skipped += 1
                continue

            content = row.get('content')
            if content is None or not str(content).strip():
                failed += 1
                continue

            scope = row.get('scope') or 'default'
            scopes_seen.add(scope)
            label = row.get('label')
            private_key = row.get('private_key')
            created, original = _normalize_ts(row.get('timestamp'))
            emb, prov, dim = _provenance(row)

            meta = {"import_key": key}
            if original is not None:
                meta["original_timestamp"] = original

            try:
                _insert_chunk(
                    dest_cursor, layer='events', scope=scope, content=content,
                    created=created, updated=created, label=label,
                    private_key=private_key, source='memory v2',
                    meta=json.dumps(meta),
                    embedding=emb, embedding_provider=prov, embedding_dim=dim,
                )
            except Exception as e:
                logger.error(f"[MINDPALACE] import memory {old_id} failed: {e}")
                failed += 1
                continue

            existing_keys.add(key)
            copied += 1
            pending += 1
            if pending >= COMMIT_EVERY:
                dest_cursor.connection.commit()
                pending = 0

    return (copied, skipped, failed)


def _import_people(config, dest_cursor, existing_keys, scopes_seen):
    """people → entities row + one tier-1 entities-layer chunk each.
    Returns (copied, skipped, failed)."""
    from plugins.mindpalace.tools import palace_tools

    path = _source_path(config, "knowledge.db")
    if not path.exists():
        return None

    copied = skipped = failed = 0
    with _connect_ro(path) as src:
        src.row_factory = sqlite3.Row
        scur = src.cursor()
        cols = _columns(scur, 'people')
        if not cols:
            return (0, 0, 0)

        scur.execute("SELECT * FROM people")
        pending = 0
        for r in scur.fetchall():
            row = {k: r[k] for k in r.keys()}
            old_id = row.get('id')
            key = f"v2:people:{old_id}"
            if key in existing_keys:
                skipped += 1
                continue

            name = _get(row, 'name')
            if not name or not str(name).strip():
                failed += 1
                continue
            name = str(name).strip()
            scope = row.get('scope') or 'default'
            scopes_seen.add(scope)

            # Build the person's descriptive content from whatever text columns
            # exist, in a stable order. Whitelist flags + ids go to meta, not body.
            content_fields = [
                ('relationship', 'Relationship'),
                ('phone', 'Phone'),
                ('email', 'Email'),
                ('address', 'Address'),
                ('notes', 'Notes'),
                ('description', 'Description'),
            ]
            parts = []
            for col, label_txt in content_fields:
                val = _get(row, col)
                if val is not None and str(val).strip():
                    parts.append(f"{label_txt}: {str(val).strip()}")
            content = "\n".join(parts) if parts else name

            # meta bag: whitelist flags + any leftover columns we didn't map.
            meta = {"import_key": key}
            handled = {'id', 'name', 'scope', 'embedding', 'embedding_provider',
                       'embedding_dim', 'relationship', 'phone', 'email',
                       'address', 'notes', 'description', 'created_at', 'updated_at'}
            for col in cols:
                if col in handled:
                    continue
                val = row.get(col)
                if val is not None:
                    meta[col] = val

            created, original = _normalize_ts(_get(row, 'created_at', 'updated_at'))
            if original is not None:
                meta["original_timestamp"] = original
            emb, prov, dim = _provenance(row)

            try:
                entity_id = palace_tools.upsert_entity(
                    dest_cursor, name, scope, kind='person')
                _insert_chunk(
                    dest_cursor, layer='entities', scope=scope, content=content,
                    created=created, updated=created, entity_id=entity_id, tier=1,
                    source='people v2', meta=json.dumps(meta),
                    embedding=emb, embedding_provider=prov, embedding_dim=dim,
                )
            except Exception as e:
                logger.error(f"[MINDPALACE] import person {old_id} failed: {e}")
                failed += 1
                continue

            existing_keys.add(key)
            copied += 1
            pending += 1
            if pending >= COMMIT_EVERY:
                dest_cursor.connection.commit()
                pending = 0

    return (copied, skipped, failed)


def _import_knowledge(config, dest_cursor, existing_keys, scopes_seen):
    """knowledge_entries → chunks(layer='knowledge'). Preserves the sub-chunk
    group identity (label, source, chunk_index) for future neighbor-stitching.
    Returns (copied, skipped, failed)."""
    path = _source_path(config, "knowledge.db")
    if not path.exists():
        return None

    copied = skipped = failed = 0
    with _connect_ro(path) as src:
        src.row_factory = sqlite3.Row
        scur = src.cursor()
        tab_cols = _columns(scur, 'knowledge_tabs')
        entry_cols = _columns(scur, 'knowledge_entries')
        if not entry_cols:
            return (0, 0, 0)

        # Tab lookup: id → (name, type, scope). Columns are defensive.
        tabs = {}
        if tab_cols:
            scur.execute("SELECT * FROM knowledge_tabs")
            for r in scur.fetchall():
                trow = {k: r[k] for k in r.keys()}
                tabs[trow.get('id')] = {
                    'name': _get(trow, 'name'),
                    'type': _get(trow, 'type'),
                    'scope': _get(trow, 'scope'),
                }

        scur.execute("SELECT * FROM knowledge_entries")
        pending = 0
        for r in scur.fetchall():
            row = {k: r[k] for k in r.keys()}
            old_id = row.get('id')
            key = f"v2:knowledge:{old_id}"
            if key in existing_keys:
                skipped += 1
                continue

            content = _get(row, 'content')
            if content is None or not str(content).strip():
                failed += 1
                continue

            tab = tabs.get(row.get('tab_id'), {})
            tab_name = tab.get('name')
            tab_type = tab.get('type')
            # scope: entry's own (if the column exists) → tab's → 'default'.
            scope = _get(row, 'scope') or tab.get('scope') or 'default'
            scopes_seen.add(scope)
            label = tab_name.lower() if tab_name else None
            chunk_index = _get(row, 'chunk_index')
            source = _get(row, 'source_filename')

            created, original = _normalize_ts(_get(row, 'created_at', 'updated_at'))
            meta = {"import_key": key, "tab": tab_name, "tab_type": tab_type}
            if original is not None:
                meta["original_timestamp"] = original
            emb, prov, dim = _provenance(row)

            try:
                _insert_chunk(
                    dest_cursor, layer='knowledge', scope=scope, content=content,
                    created=created, updated=created, chunk_index=chunk_index,
                    source=source, label=label, meta=json.dumps(meta),
                    embedding=emb, embedding_provider=prov, embedding_dim=dim,
                )
            except Exception as e:
                logger.error(f"[MINDPALACE] import knowledge {old_id} failed: {e}")
                failed += 1
                continue

            existing_keys.add(key)
            copied += 1
            pending += 1
            if pending >= COMMIT_EVERY:
                dest_cursor.connection.commit()
                pending = 0

    return (copied, skipped, failed)


# ─── Orchestrator ─────────────────────────────────────────────────────────────

def _run_import(what: str, config) -> str:
    from plugins.mindpalace.tools import palace_tools

    order = ['memories', 'people', 'knowledge']
    targets = order if what == 'all' else [what]

    results = {}
    scopes_seen = set()
    any_copied = False

    with palace_tools._get_connection() as conn:
        cursor = conn.cursor()

        # One query loads every existing import_key → idempotency set. Never
        # per-row existence checks (that's the O(N²) trap).
        cursor.execute(
            "SELECT json_extract(meta, '$.import_key') FROM chunks WHERE meta IS NOT NULL"
        )
        existing_keys = {row[0] for row in cursor.fetchall() if row[0] is not None}

        importers = {
            'memories': _import_memories,
            'people': _import_people,
            'knowledge': _import_knowledge,
        }
        for store in targets:
            res = importers[store](config, cursor, existing_keys, scopes_seen)
            results[store] = res
            if res is not None and res[0] > 0:
                any_copied = True

        # Register every distinct scope so the UI dropdown lists it. create_scope
        # opens its own connection; call after our writes are in the cursor but
        # commit first so the scopes and chunks land together-ish.
        conn.commit()

    for s in sorted(scopes_seen):
        palace_tools.create_scope(s)

    # Any copied rows may lack vectors → clear the backfill latch so the next
    # search sweeps and re-embeds them with the correct prefix.
    if any_copied:
        palace_tools.reset_backfill_latch()

    # ── Human-readable summary ──
    lines = ["Import from the classic memory system (v2 → mind palace):"]
    for store in targets:
        res = results.get(store)
        if res is None:
            lines.append(f"  • {store}: source DB not found — skipped.")
        else:
            copied, skipped, failed = res
            bit = f"  • {store}: {copied} copied, {skipped} already imported"
            if failed:
                bit += f", {failed} failed"
            lines.append(bit + ".")
    lines.append("The old databases were opened read-only and were NOT modified — "
                 "they remain your switch-back path.")
    return "\n".join(lines)


# ─── Executor ─────────────────────────────────────────────────────────────────

def execute(function_name: str, arguments: dict, config) -> tuple:
    try:
        if function_name != "import_v2":
            return f"Unknown import function: {function_name}", False

        # Scope gating mirrors palace_tools: unresolved scope → disabled. But we
        # deliberately do NOT apply the global write-block — import copies ALL
        # scopes verbatim, global included.
        from plugins.mindpalace.tools import palace_tools
        scope = palace_tools._get_current_scope()
        if scope is None:
            return "Memory is disabled for this chat.", False

        what = arguments.get("what", "all")
        if what not in ("memories", "people", "knowledge", "all"):
            what = "all"

        summary = _run_import(what, config)
        return summary, True

    except Exception as e:
        logger.error(f"[MINDPALACE] Import error: {e}")
        return f"Import failed: {e}", False
