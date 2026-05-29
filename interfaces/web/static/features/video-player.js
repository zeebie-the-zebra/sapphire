// features/video-player.js — reusable YouTube (privacy-nocookie) player.
//
// Deliberately dumb: given a container + videoId, render the embed. v1 mounts
// it in the Video Guide modal. v2 (pip) mounts the SAME component into a
// floating mini-player — so the cool feature is a mount-swap, not a rewrite.
//
// Uses youtube-nocookie.com (no tracking cookies until play) — the CSP
// `frame-src` allows exactly this origin.

export function mountPlayer(container, videoId, { autoplay = true } = {}) {
    if (!container || !videoId) return null;
    const params = new URLSearchParams({
        autoplay: autoplay ? '1' : '0',
        rel: '0',              // keep related-video sprawl to this channel where possible
        modestbranding: '1',
    });
    const iframe = document.createElement('iframe');
    iframe.className = 'vg-player-frame';
    iframe.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(videoId)}?${params.toString()}`;
    iframe.title = 'Video player';
    iframe.setAttribute('frameborder', '0');
    iframe.allow = 'accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture; fullscreen';
    iframe.allowFullscreen = true;
    container.innerHTML = '';
    container.appendChild(iframe);
    return iframe;
}

export function clearPlayer(container) {
    if (container) container.innerHTML = '';
}
