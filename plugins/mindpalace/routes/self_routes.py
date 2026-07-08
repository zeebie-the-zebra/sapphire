# plugins/mindpalace/routes/self_routes.py
# Layer 0 app routes — the Self view's windows. Same trusted-surface stance as
# browse.py: the authenticated UI sees the requested scope plainly. All writes
# funnel through self_tools.write_section (one write path — cap, versioning,
# metadata stamping, entity linking identical to the tool).

import json
import logging

logger = logging.getLogger(__name__)


def _st():
    from plugins.mindpalace.tools import self_tools
    return self_tools


def _pt():
    from plugins.mindpalace.tools import palace_tools
    return palace_tools


def get_sheet(query=None, **_):
    """The whole self sheet: typed sections (present or empty), custom boxes,
    history counts, live dashboard. query: scope."""
    st, pt = _st(), _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    scope = (query or {}).get('scope') or 'default'
    with pt._get_connection() as conn:
        cursor = conn.cursor()
        current = st._current_sections(cursor, scope)
        history = st._history_counts(cursor, scope)
        dashboard = st.dashboard_data(cursor, scope)

    sections = []
    for sec, spec in st.SECTIONS.items():
        row = current.get(sec)
        sections.append({
            'section': sec, 'title': spec['title'], 'hint': spec['hint'],
            'mode': spec['mode'], 'versioned': bool(spec.get('versioned')),
            'content': row['content'] if row else '',
            'pairs': (row['meta'].get('pairs') if row else None) if sec == 'handles' else None,
            'updated': row['updated'] if row else None,
            'history_count': history.get(sec, 0),
        })
    custom = [{
        'section': sec, 'content': row['content'],
        'updated': row['updated'], 'history_count': history.get(sec, 0),
    } for sec, row in current.items() if sec not in st.SECTIONS]
    return {'scope': scope, 'sections': sections, 'custom': custom,
            'dashboard': dashboard}


def put_section(section=None, body=None, **_):
    """Replace one section. body: {content} or {pairs:[{key,value}]} for
    handles. Empty content clears typed / removes custom (user_bio semantics).
    App edition always replaces — no rolling-add magic here; the textarea IS
    the list."""
    st, pt = _st(), _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    b = body or {}
    scope = b.get('scope') or 'default'
    content = b.get('content')
    pairs = b.get('pairs')
    if pairs is not None:
        content = "\n".join(
            f"{(p.get('key') or '').strip()}: {(p.get('value') or '').strip()}"
            for p in pairs if (p.get('key') or '').strip())
    msg, ok = st.write_section(scope, section, content or '',
                               projects_replace=True)
    if not ok:
        return {'error': msg}, 400
    return {'success': True, 'message': msg}


def section_history(section=None, query=None, **_):
    """Archived versions of one section, newest first — the becoming trail."""
    st, pt = _st(), _pt()
    if not pt._ensure_db():
        return {'error': 'mind database unavailable'}, 500
    scope = (query or {}).get('scope') or 'default'
    sec = st._sanitize_section(section)
    if not sec:
        return {'error': 'Invalid section'}, 400
    with pt._get_connection() as conn:
        rows = conn.execute(
            "SELECT id, content, created, json_extract(meta, '$.superseded_at') "
            "FROM chunks WHERE layer = 'self' AND scope = ? "
            "AND json_extract(meta, '$.section') = ? "
            "AND json_extract(meta, '$.superseded_at') IS NOT NULL "
            "ORDER BY created DESC LIMIT 100", (scope, sec)).fetchall()
    return {'section': sec, 'versions': [
        {'id': r[0], 'content': r[1], 'created': r[2], 'superseded_at': r[3]}
        for r in rows]}
