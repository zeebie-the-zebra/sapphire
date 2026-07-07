# plugins/mindpalace/routes/browse.py
# Palace web UI routes — the windows. Serves the authenticated web UI (Krem's
# human surface), so unlike the tool executors these are NOT private_key- or
# global-write-gated: the UI shows everything in the requested scope, with
# private keys rendered as visible lock pills (classic Mind view precedent).
# Browsing is exact-scope (no global overlay) — you see what's in the box.

import json
import logging

logger = logging.getLogger(__name__)

VALID_KINDS = {'person', 'place', 'thing', 'other'}
MAX_LIMIT = 500

_MIND_DOMAIN = {'events': 'memory', 'self': 'memory',
                'entities': 'people', 'knowledge': 'knowledge'}


def _pt():
    from plugins.mindpalace.tools import palace_tools
    return palace_tools


def _publish(layer, scope, action):
    try:
        from core.mind_events import publish_mind_changed
        publish_mind_changed(_MIND_DOMAIN.get(layer, 'memory'), scope, action)
    except Exception:
        pass


def _parse_meta(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {'_raw': raw}


def _chunk_row(row):
    (cid, layer, content, entity_id, entity_name, tier, label,
     favorite, private_key, meta, created, source, chunk_index) = row
    return {
        'id': cid, 'layer': layer, 'content': content,
        'entity_id': entity_id, 'entity_name': entity_name, 'tier': tier,
        'label': label, 'favorite': bool(favorite), 'private_key': private_key,
        'meta': _parse_meta(meta), 'created': created,
        'source': source, 'chunk_index': chunk_index,
    }


_CHUNK_COLS = ('c.id, c.layer, c.content, c.entity_id, e.name, c.tier, '
               'c.label, c.favorite, c.private_key, c.meta, c.created, '
               'c.source, c.chunk_index')


def status(query=None, **_):
    """Dispatcher probe + header counts. 200 here == palace active."""
    pt = _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    scope = (query or {}).get('scope')
    with pt._get_connection() as conn:
        cur = conn.cursor()
        if scope:
            rows = cur.execute('SELECT layer, COUNT(*) FROM chunks WHERE scope = ? '
                               'GROUP BY layer', (scope,)).fetchall()
            ents = cur.execute('SELECT COUNT(*) FROM entities WHERE scope = ?',
                               (scope,)).fetchone()[0]
        else:
            rows = cur.execute('SELECT layer, COUNT(*) FROM chunks GROUP BY layer').fetchall()
            ents = cur.execute('SELECT COUNT(*) FROM entities').fetchone()[0]
        edges = cur.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
    return {'active': True, 'layers': dict(rows), 'entities': ents, 'edges': edges}


def list_chunks(query=None, **_):
    """Browse/search chunks. query: scope, layer, q, limit, offset."""
    pt = _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    q = query or {}
    scope = q.get('scope') or 'default'
    layer, err = pt._validate_layer(q.get('layer'))
    if err:
        return {'error': err}, 400
    try:
        limit = min(int(q.get('limit', 100)), MAX_LIMIT)
        offset = max(int(q.get('offset', 0)), 0)
    except ValueError:
        return {'error': 'limit/offset must be integers'}, 400
    search = (q.get('q') or '').strip()

    where = ['c.scope = ?']
    params = [scope]
    if layer:
        where.append('c.layer = ?')
        params.append(layer)

    with pt._get_connection() as conn:
        cur = conn.cursor()
        if search:
            rows = _search_rows(pt, cur, search, where, params, limit, offset)
        else:
            rows = cur.execute(
                f'SELECT {_CHUNK_COLS} FROM chunks c '
                f'LEFT JOIN entities e ON e.id = c.entity_id '
                f'WHERE {" AND ".join(where)} '
                f'ORDER BY c.created DESC LIMIT ? OFFSET ?',
                params + [limit, offset]).fetchall()
        total = cur.execute(
            f'SELECT COUNT(*) FROM chunks c WHERE {" AND ".join(where)}',
            params).fetchone()[0]
    return {'chunks': [_chunk_row(r) for r in rows], 'total': total,
            'limit': limit, 'offset': offset}


def _search_rows(pt, cur, search, where, params, limit, offset):
    """FTS AND → FTS OR+prefix → LIKE cascade, browse edition (no vectors —
    the UI browses text; the AI keeps the full cascade in the tools)."""
    for fts_q in (pt._sanitize_fts_query(search),
                  pt._sanitize_fts_query(search, use_or=True, use_prefix=True)):
        if not fts_q:
            continue
        try:
            rows = cur.execute(
                f'SELECT {_CHUNK_COLS} FROM chunks_fts f '
                f'JOIN chunks c ON c.id = f.rowid '
                f'LEFT JOIN entities e ON e.id = c.entity_id '
                f'WHERE chunks_fts MATCH ? AND {" AND ".join(where)} '
                f'ORDER BY f.rank LIMIT ? OFFSET ?',
                [fts_q] + params + [limit, offset]).fetchall()
            if rows:
                return rows
        except Exception as e:
            logger.debug(f"[MINDPALACE] Browse FTS failed ({e}), falling back")
    return cur.execute(
        f'SELECT {_CHUNK_COLS} FROM chunks c '
        f'LEFT JOIN entities e ON e.id = c.entity_id '
        f'WHERE c.content LIKE ? AND {" AND ".join(where)} '
        f'ORDER BY c.created DESC LIMIT ? OFFSET ?',
        [f'%{search}%'] + params + [limit, offset]).fetchall()


def create_chunk(body=None, **_):
    """Add from the UI. body: content, scope, layer, entity, label, favorite,
    private_key. Funnels through _save_memory so caps, entity upsert, metadata
    stamping, and edge seeding all apply (tool_context is unset on this thread
    → meta is honestly thinner: no chat/persona/model)."""
    b = body or {}
    content = (b.get('content') or '').strip()
    if not content:
        return {'error': 'Content is required'}, 400
    scope = b.get('scope') or 'default'
    pt = _pt()
    msg, ok = pt._save_memory(
        content, scope, layer=b.get('layer'), entity=b.get('entity'),
        label=b.get('label'), favorite=bool(b.get('favorite')),
        private_key=b.get('private_key'))
    if not ok:
        return {'error': msg}, 400
    return {'success': True, 'message': msg}


def delete_chunk(cid=None, query=None, **_):
    pt = _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return {'error': 'Invalid chunk id'}, 400
    with pt._get_connection() as conn:
        cur = conn.cursor()
        row = cur.execute('SELECT layer, scope FROM chunks WHERE id = ?',
                          (cid,)).fetchone()
        if not row:
            return {'error': 'Not found'}, 404
        cur.execute("DELETE FROM edges WHERE (src_type = 'chunk' AND src_id = ?) "
                    "OR (dst_type = 'chunk' AND dst_id = ?)", (cid, cid))
        cur.execute('DELETE FROM chunks WHERE id = ?', (cid,))
        conn.commit()
    _publish(row[0], row[1], 'delete')
    return {'success': True}


def toggle_favorite(cid=None, body=None, **_):
    """Favorite is the qualitative lever; 0.95 = never-fades band (same
    mapping as save — the number stays behind the curtain in tool responses,
    but favorite state itself is visible everywhere)."""
    pt = _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return {'error': 'Invalid chunk id'}, 400
    fav = bool((body or {}).get('favorite'))
    with pt._get_connection() as conn:
        cur = conn.cursor()
        row = cur.execute('SELECT layer, scope FROM chunks WHERE id = ?',
                          (cid,)).fetchone()
        if not row:
            return {'error': 'Not found'}, 404
        cur.execute('UPDATE chunks SET favorite = ?, importance = ?, updated = ? '
                    'WHERE id = ?',
                    (1 if fav else 0, 0.95 if fav else None, pt._now(), cid))
        conn.commit()
    _publish(row[0], row[1], 'update')
    return {'success': True, 'favorite': fav}


def list_entities(query=None, **_):
    """L2 spine for the Entities view: per-entity chunk + edge counts."""
    pt = _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    scope = (query or {}).get('scope') or 'default'
    with pt._get_connection() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            'SELECT e.id, e.name, e.kind, e.mentions, e.meta, e.created, e.updated, '
            '  (SELECT COUNT(*) FROM chunks c WHERE c.entity_id = e.id) AS chunk_count, '
            "  (SELECT COUNT(*) FROM edges d WHERE d.dst_type = 'entity' AND d.dst_id = e.id) AS edge_count "
            'FROM entities e WHERE e.scope = ? '
            'ORDER BY edge_count DESC, e.name COLLATE NOCASE', (scope,)).fetchall()
    return {'entities': [
        {'id': r[0], 'name': r[1], 'kind': r[2], 'mentions': r[3],
         'meta': _parse_meta(r[4]), 'created': r[5], 'updated': r[6],
         'chunk_count': r[7], 'edge_count': r[8]} for r in rows]}


def entity_detail(eid=None, **_):
    """One entity: tiered chunks + 'mentioned in' (edges → source chunks)."""
    pt = _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        return {'error': 'Invalid entity id'}, 400
    with pt._get_connection() as conn:
        cur = conn.cursor()
        e = cur.execute('SELECT id, name, scope, kind, mentions, meta, created, updated '
                        'FROM entities WHERE id = ?', (eid,)).fetchone()
        if not e:
            return {'error': 'Not found'}, 404
        chunks = cur.execute(
            'SELECT id, content, tier, label, favorite, private_key, meta, created '
            'FROM chunks WHERE entity_id = ? '
            'ORDER BY COALESCE(tier, 9), created DESC', (eid,)).fetchall()
        mentions = cur.execute(
            "SELECT c.id, c.layer, c.content, c.created, d.weight "
            "FROM edges d JOIN chunks c ON c.id = d.src_id "
            "WHERE d.dst_type = 'entity' AND d.dst_id = ? AND d.src_type = 'chunk' "
            "ORDER BY c.created DESC LIMIT 100", (eid,)).fetchall()
    return {
        'entity': {'id': e[0], 'name': e[1], 'scope': e[2], 'kind': e[3],
                   'mentions': e[4], 'meta': _parse_meta(e[5]),
                   'created': e[6], 'updated': e[7]},
        'chunks': [{'id': c[0], 'content': c[1], 'tier': c[2], 'label': c[3],
                    'favorite': bool(c[4]), 'private_key': c[5],
                    'meta': _parse_meta(c[6]), 'created': c[7]} for c in chunks],
        'mentioned_in': [{'id': m[0], 'layer': m[1], 'content': m[2],
                          'created': m[3], 'weight': m[4]} for m in mentions],
    }


def update_entity(eid=None, body=None, **_):
    """L2 rework, UI edition: kind is human-editable (person/place/thing/other
    or clear). Name/scope stay tool/import territory."""
    pt = _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        return {'error': 'Invalid entity id'}, 400
    kind = (body or {}).get('kind')
    if kind is not None:
        kind = str(kind).strip().lower() or None
    if kind is not None and kind not in VALID_KINDS:
        return {'error': f"kind must be one of {sorted(VALID_KINDS)} or empty"}, 400
    with pt._get_connection() as conn:
        cur = conn.cursor()
        row = cur.execute('SELECT scope FROM entities WHERE id = ?', (eid,)).fetchone()
        if not row:
            return {'error': 'Not found'}, 404
        cur.execute('UPDATE entities SET kind = ?, updated = ? WHERE id = ?',
                    (kind, pt._now(), eid))
        conn.commit()
    _publish('entities', row[0], 'update')
    return {'success': True, 'kind': kind}
