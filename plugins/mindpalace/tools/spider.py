# plugins/mindpalace/tools/spider.py
# The memory spider — graph traversal behind the depth= param (v3 step 3).
#
# Krem's pinned model (tmp/v3-memory-boost.md, "Distance model — the train
# rail"): depth is a STRENGTH/budget the traversal spends, not a hop count.
# Dijkstra from the epicenter(s) with two edge prices — structure is cheap,
# metadata is expensive — so the spider travels far along the structural rail
# (entity ↔ its own tiered chunks) but exhausts quickly jumping sideways
# through metadata (mention edges). Descent costs 1 per tier.
#
# Epicenter resolution = the G4 ladder, zero main-LLM tokens:
#   1. exact entity-name match in the query (reuses metadata.match_entities)
#   2. semantic: query embedding vs entities-layer chunk embeddings (>= 0.55 —
#      people-search precedent; dense entity strings over-match at 0.40)
#   3. base search hits are ALWAYS micro-epicenters (G4.4: RAG first, then hop)
# Competing epicenters share one frontier — the stronger neighborhood
# naturally dominates the capped output. No disambiguation dialog.
#
# Gates carried: scope overlay (scope + read-only global) and private_key on
# every node the spider touches. The AI never sees budgets/weights/importance.

import heapq
import logging

logger = logging.getLogger(__name__)

STRUCT_COST = 1.0          # entity ↔ own chunk (per tier on descent)
META_COST = 2.0            # sideways: mention edges (and future metadata kinds)
BUDGET_PER_DEPTH = 2.0     # depth strength → budget units
ENTITY_SIM_THRESHOLD = 0.55
MAX_ENTITIES_SHOWN = 5     # output caps — token-budget flattening, v1
MAX_CHUNKS_SHOWN = 10
MAX_CHUNK_PREVIEW = 160
MAX_BLOCK_CHARS = 2000

# Edge-kind → traversal cost. Unknown kinds default to metadata price so a
# future librarian edge class is walkable (expensively) the day it appears.
EDGE_COSTS = {'mentions': META_COST}


def _visible_chunk_clause(pt, scope, private_key):
    scope_sql, scope_params = pt._scope_condition(scope, col='c.scope')
    pk_sql, pk_params = pt._private_key_clause(private_key, col='c.private_key')
    return f"{scope_sql} AND {pk_sql}", scope_params + pk_params


def resolve_entity_epicenters(pt, cursor, query, scope, private_key):
    """G4 rungs 1+2 → {entity_id}. Rung 3 (base hits as micro-epicenters)
    is handled by the caller seeding hit chunks directly."""
    seeds = set()
    scope_sql, scope_params = pt._scope_condition(scope, col='scope')
    rows = cursor.execute(
        f'SELECT id, name FROM entities WHERE {scope_sql}', scope_params).fetchall()
    if not rows:
        return seeds

    # Rung 1: exact name match inside the query (NOCASE word-boundary).
    try:
        from plugins.mindpalace.tools import metadata as md
        by_name = {n.lower(): i for i, n in rows}
        for name in md.match_entities(query, [n for _, n in rows]):
            seeds.add(by_name[name.lower()])
    except Exception as e:
        logger.debug(f"[SPIDER] Exact epicenter match failed: {e}")

    # Rung 2: semantic — query embedding vs entities-layer chunk embeddings,
    # best score per entity. Provenance-matched SQL-side + plain dot (vectors
    # are normalized) — the exact _vector_search pattern. Skips cleanly when
    # the embedder is down.
    try:
        embedder = pt._get_embedder()
        if embedder.available:
            embs = embedder.embed([query], prefix='search_query')
            if embs is not None:
                import numpy as np
                qv = embs[0]
                qdim = int(qv.shape[0])
                provider = getattr(embedder, 'provider_id', None)
                vis_sql, vis_params = _visible_chunk_clause(pt, scope, private_key)
                cand = cursor.execute(
                    f"SELECT c.entity_id, c.embedding FROM chunks c "
                    f"WHERE c.layer = 'entities' AND c.entity_id IS NOT NULL "
                    f"AND c.embedding IS NOT NULL AND c.embedding_provider = ? "
                    f"AND c.embedding_dim = ? AND {vis_sql}",
                    [provider, qdim] + vis_params).fetchall()
                best = {}
                for eid, blob in cand:
                    try:
                        vec = np.frombuffer(blob, dtype=np.float32)
                        if vec.shape[0] != qdim:
                            continue
                        score = float(np.dot(qv, vec))
                        if np.isnan(score) or np.isinf(score):
                            continue
                    except Exception:
                        continue
                    if score > best.get(eid, 0.0):
                        best[eid] = score
                seeds.update(eid for eid, s in best.items() if s >= ENTITY_SIM_THRESHOLD)
    except Exception as e:
        logger.debug(f"[SPIDER] Semantic epicenter match failed: {e}")
    return seeds


def traverse(pt, cursor, seed_chunks, seed_entities, budget, scope, private_key):
    """Multi-source Dijkstra over the chunk/entity graph within `budget`.
    Returns ({chunk_id: dist}, {entity_id: dist}) EXCLUDING the seeds.
    Neighbor expansion is lazy SQL per node — the frontier is small because
    the budget is small (<= 2 * max depth)."""
    vis_sql, vis_params = _visible_chunk_clause(pt, scope, private_key)
    ent_sql, ent_params = pt._scope_condition(scope, col='scope')

    dist = {}
    heap = []
    for cid in seed_chunks:
        heapq.heappush(heap, (0.0, 'c', cid))
    for eid in seed_entities:
        heapq.heappush(heap, (0.0, 'e', eid))

    def neighbors(kind, node_id):
        out = []
        if kind == 'e':
            # Structural descent: entity → own chunks, cost = tier (1/tier rule;
            # untiered rows priced as facts).
            for cid, tier in cursor.execute(
                    f"SELECT c.id, c.tier FROM chunks c WHERE c.entity_id = ? AND {vis_sql}",
                    [node_id] + vis_params).fetchall():
                out.append(('c', cid, STRUCT_COST * (tier if tier in (1, 2, 3) else 2)))
            # Sideways: memories that mention this entity.
            for cid, ekind in cursor.execute(
                    f"SELECT d.src_id, d.kind FROM edges d "
                    f"JOIN chunks c ON c.id = d.src_id "
                    f"WHERE d.dst_type = 'entity' AND d.dst_id = ? AND d.src_type = 'chunk' "
                    f"AND {vis_sql}", [node_id] + vis_params).fetchall():
                out.append(('c', cid, EDGE_COSTS.get(ekind, META_COST)))
        else:
            # Structural: chunk → its own entity.
            row = cursor.execute(
                f"SELECT e.id FROM entities e JOIN chunks c ON c.entity_id = e.id "
                f"WHERE c.id = ? AND e.scope IN (?, 'global')",
                (node_id, scope)).fetchone()
            if row:
                out.append(('e', row[0], STRUCT_COST))
            # Sideways: entities this chunk mentions.
            for eid, ekind in cursor.execute(
                    f"SELECT d.dst_id, d.kind FROM edges d "
                    f"JOIN entities e ON e.id = d.dst_id "
                    f"WHERE d.src_type = 'chunk' AND d.src_id = ? AND d.dst_type = 'entity' "
                    f"AND {ent_sql.replace('scope', 'e.scope')}",
                    [node_id] + ent_params).fetchall():
                out.append(('e', eid, EDGE_COSTS.get(ekind, META_COST)))
        return out

    while heap:
        d, kind, node_id = heapq.heappop(heap)
        key = (kind, node_id)
        if key in dist and dist[key] <= d:
            continue
        dist[key] = d
        for nkind, nid, cost in neighbors(kind, node_id):
            nd = d + cost
            nkey = (nkind, nid)
            if nd <= budget and (nkey not in dist or nd < dist[nkey]):
                heapq.heappush(heap, (nd, nkind, nid))

    chunks = {nid: d for (k, nid), d in dist.items() if k == 'c' and nid not in seed_chunks}
    entities = {nid: d for (k, nid), d in dist.items() if k == 'e' and nid not in seed_entities}
    return chunks, entities


def spider_block(pt, query, scope, private_key, hit_chunk_ids, depth):
    """The depth= entry point. Returns a formatted 'Connected memories' block
    (or '' when the walk finds nothing new). Failures degrade to '' — the
    spider must never break a search that already succeeded."""
    try:
        depth = max(0, min(int(depth), 3))
    except (TypeError, ValueError):
        return ''
    if depth == 0:
        return ''
    budget = depth * BUDGET_PER_DEPTH

    try:
        with pt._get_connection() as conn:
            cursor = conn.cursor()
            seed_entities = resolve_entity_epicenters(pt, cursor, query, scope, private_key)
            seed_chunks = set(hit_chunk_ids or [])
            # Seed chunks' own entities join the epicenter set implicitly via
            # traversal (structural cost 1) — no special-casing needed.
            if not seed_chunks and not seed_entities:
                return ''
            chunks, entities = traverse(pt, cursor, seed_chunks, seed_entities,
                                        budget, scope, private_key)
            if not chunks and not entities:
                return ''
            return _format_block(pt, cursor, chunks, entities, depth)
    except Exception as e:
        logger.warning(f"[SPIDER] Traversal failed (search unaffected): {e}")
        return ''


def _format_block(pt, cursor, chunks, entities, depth):
    """Flatten the reached subgraph, closest-first, hard-capped. Entities show
    name/kind + their nearest headline; chunks show the standard [id] format."""
    lines = [f"── Connected memories (depth {depth}) ──"]

    ent_order = sorted(entities.items(), key=lambda x: x[1])[:MAX_ENTITIES_SHOWN]
    for eid, _d in ent_order:
        row = cursor.execute('SELECT name, kind FROM entities WHERE id = ?', (eid,)).fetchone()
        if not row:
            continue
        name, kind = row
        head = cursor.execute(
            "SELECT content FROM chunks WHERE entity_id = ? AND tier = 1 "
            "ORDER BY created DESC LIMIT 1", (eid,)).fetchone()
        kind_bit = f" ({kind})" if kind else ""
        head_bit = f": {head[0][:MAX_CHUNK_PREVIEW]}" if head else ""
        lines.append(f"• {name}{kind_bit}{head_bit}")

    chunk_order = sorted(chunks.items(), key=lambda x: x[1])[:MAX_CHUNKS_SHOWN]
    if chunk_order:
        ids = [cid for cid, _ in chunk_order]
        ph = ','.join('?' * len(ids))
        rows = {r[0]: r for r in cursor.execute(
            pt.SELECT_CHUNK + f'WHERE c.id IN ({ph})', ids).fetchall()}
        for cid, _d in chunk_order:
            r = rows.get(cid)
            if r:
                content = r[1] if len(r[1]) <= MAX_CHUNK_PREVIEW else r[1][:MAX_CHUNK_PREVIEW] + '…'
                lines.append(pt._format_chunk(r[0], content, r[2], r[3], r[4], r[5]))

    if len(lines) == 1:
        return ''
    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        block = block[:MAX_BLOCK_CHARS].rsplit('\n', 1)[0]
    return block
