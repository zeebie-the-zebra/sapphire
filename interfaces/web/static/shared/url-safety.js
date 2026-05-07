// shared/url-safety.js — URL scheme allowlist for community-authored content.
//
// Plugin metadata fields like author_url, github_url, screenshot_url come
// from sapphireblue.dev's catalog — anyone can submit a plugin, so we treat
// them as untrusted strings. This module is the single place that decides
// whether a URL is safe to render into href/src.
//
// Strict allowlist: https only. No bare http, no mailto/tel, no
// javascript:/data:/file: smuggling. Whitespace inside URL is rejected
// (common bypass via newlines or tabs). Used by:
//   - shared/markdown.js (existing — for plugin long-description anchors)
//   - views/store.js (cards: author_url, github_url, screenshot_url)
//   - views/settings-tabs/dashboard.js (Plugin Spotlight tile author_url)
//
// If you need a different scheme later, add it here, not at the call site.


export function isSafeHref(href) {
    if (typeof href !== 'string') return false;
    const trimmed = href.trim();
    if (/\s/.test(trimmed)) return false;
    return /^https:\/\//i.test(trimmed);
}


/**
 * Sanitize a URL for use in href/src attributes. Returns the URL if safe,
 * empty string otherwise. Use when you want a falsy fallback you can
 * conditional-render around.
 */
export function safeUrl(href) {
    return isSafeHref(href) ? href : '';
}
