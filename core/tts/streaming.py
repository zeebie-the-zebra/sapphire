"""Streaming chunkifier for TTS — turns an LLM token stream into speakable
chunks at sentence/paragraph boundaries (with max-char fallback).

Used by the v2.7.0 streaming TTS pipeline. Pure-Python: no Sapphire imports,
no I/O, no Kokoro. Unit-testable in isolation.

Behavior:
- Strips `<think>...</think>`, `<reasoning>...</reasoning>`, `<tools>...</tools>`
  blocks and ```code fences``` from the stream — even when the opening tag
  arrives in one token and the closer arrives in a later token.
- Detects sentence ends via `.!?` followed by whitespace+capital
  (so `Dr. Smith`, `3.14`, `3:45 PM`, URLs don't split).
- Paragraph breaks (`\\n\\n`) always split.
- Ellipsis (`...`) is its own boundary with a longer post-pause.
- Secondary punctuation (`;` `:`) splits only when the chunk has already
  grown past max_chars * 0.5 — avoids tiny chunks on natural prose pauses.
- If no boundary appears within `max_chars`, force-splits on the last
  whitespace inside the window.
- Empty/all-stripped stream yields no chunks.

Each yielded chunk carries metadata for the downstream player (boundary
type + suggested post-chunk pause).
"""
import logging
import re
from typing import Iterable, Iterator, Optional, Tuple

log = logging.getLogger(__name__)

# Pause duration (milliseconds) the player honors after each boundary type.
PAUSE_AFTER_MS = {
    "sentence":   250,   # . ! ?
    "ellipsis":   500,   # ...
    "secondary":  150,   # ; :
    "paragraph":  600,   # \n\n
    "maxlen":     0,     # mid-thought split, no pause
    "end":        0,     # final flush at end of stream
}

# Block-level openers that are hidden entirely. Each (opener, closer) pair.
BLOCK_TAGS: list = [
    ("<think>", "</think>"),
    ("<reasoning>", "</reasoning>"),
    ("<tools>", "</tools>"),
]

# Secondary punctuation splits when the chunk has grown past this fraction
# of max_chars. Avoids splitting "Yes:" into its own tiny chunk.
_SECONDARY_SPLIT_FRACTION = 0.5


def chunkify_for_speech(
    token_stream: Iterable[str],
    max_chars: int = 200,
    min_chars: int = 15,
) -> Iterator[dict]:
    """Consume an arbitrary token stream, yield speakable chunks.

    Args:
        token_stream: iterable yielding text deltas. Tokens may be any
            size — single characters, words, or whole paragraphs.
        max_chars: emit-anyway threshold. If no boundary inside this
            window, force a split on the nearest preceding whitespace.
        min_chars: don't emit a chunk shorter than this if more text
            could still arrive (prevents fragmented short chunks).

    Yields:
        dict per chunk:
          {
            "text": str,            # cleaned, ready for TTS
            "boundary": str,        # 'sentence'|'paragraph'|'ellipsis'|
                                    #  'secondary'|'maxlen'|'end'
            "pause_after_ms": int,  # suggested gap before next chunk
            "index": int,           # 0-indexed within this stream
          }
    """
    raw_buf = ""    # accumulates raw tokens; may contain partial tags
    clean_buf = ""  # cleaned text ready to scan for chunk boundaries
    chunk_index = 0

    for token in token_stream:
        if not token:
            continue
        raw_buf += token
        # Move whatever is "safe to process" (no dangling open tag past
        # this point) from raw into clean.
        safe_len = _safe_prefix_len(raw_buf)
        if safe_len > 0:
            piece = raw_buf[:safe_len]
            raw_buf = raw_buf[safe_len:]
            clean_buf += _clean_piece(piece)
        # Drain any chunks now visible in clean_buf
        while True:
            cut = _find_split(clean_buf, max_chars, min_chars)
            if cut is None:
                break
            chunk_text, boundary, remainder = cut
            chunk_text = chunk_text.strip()
            clean_buf = remainder
            if not chunk_text:
                continue
            yield _make_chunk(chunk_text, boundary, chunk_index)
            chunk_index += 1

    # Stream ended. Anything still in raw_buf is "final" — even if it
    # contains an unclosed tag, drop it on the floor (matches the legacy
    # _process_text_for_tts behavior).
    if raw_buf:
        # Force-strip any leftover open tags
        leftover = raw_buf
        for opener, closer in BLOCK_TAGS:
            if opener in leftover:
                # Drop everything from opener to end of buffer
                idx = leftover.find(opener)
                leftover = leftover[:idx]
        # Drop dangling code fences too
        if leftover.count("```") % 2 == 1:
            leftover = leftover[:leftover.rfind("```")]
        clean_buf += _clean_piece(leftover)
        raw_buf = ""

    # Drain remaining boundaries in clean_buf (now that more text may have
    # arrived from the leftover flush).
    while True:
        cut = _find_split(clean_buf, max_chars, min_chars)
        if cut is None:
            break
        chunk_text, boundary, remainder = cut
        chunk_text = chunk_text.strip()
        clean_buf = remainder
        if not chunk_text:
            continue
        yield _make_chunk(chunk_text, boundary, chunk_index)
        chunk_index += 1

    # Final chunk for whatever's left (regardless of min_chars — this is
    # the end of the stream, won't get more text).
    final_text = clean_buf.strip()
    if final_text:
        yield _make_chunk(final_text, "end", chunk_index)


def _make_chunk(text: str, boundary: str, index: int) -> dict:
    # Collapse whitespace inside the chunk for clean TTS input
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "text": text,
        "boundary": boundary,
        "pause_after_ms": PAUSE_AFTER_MS.get(boundary, 0),
        "index": index,
    }


def _safe_prefix_len(raw: str) -> int:
    """Return the length of the prefix of `raw` that we can safely process.
    Anything past this position might be inside a half-formed tag/fence and
    should wait for more tokens."""
    n = len(raw)
    safe = n

    # Block tags — find any opener without a matching closer
    for opener, closer in BLOCK_TAGS:
        idx = 0
        while True:
            o = raw.find(opener, idx)
            if o < 0:
                break
            c = raw.find(closer, o + len(opener))
            if c < 0:
                safe = min(safe, o)
                break
            idx = c + len(closer)

    # Code fences — count triple-backticks; if odd, last one is unclosed
    fence_positions = []
    idx = 0
    while True:
        f = raw.find("```", idx)
        if f < 0:
            break
        fence_positions.append(f)
        idx = f + 3
    if len(fence_positions) % 2 == 1:
        safe = min(safe, fence_positions[-1])

    # Trailing `<` without `>` → partial HTML tag. Hold back.
    last_lt = raw.rfind("<", 0, safe)
    if last_lt >= 0 and raw.find(">", last_lt, safe) < 0:
        safe = min(safe, last_lt)

    # Trailing 1-2 backticks could become a triple → hold back
    s = raw[:safe]
    while s.endswith("`") and not s.endswith("```"):
        # If 2 trailing backticks, also hold (could become ``` next token)
        safe -= 1
        s = raw[:safe]
        if not s.endswith("`"):
            break

    return max(0, safe)


# Inline cleanup for safe-prefix pieces. Block tags should already be
# handled via safe-prefix gating; these patterns are belt-and-suspenders
# plus inline markdown handling.
_INLINE_PATTERNS = [
    (re.compile(r"<think>.*?</think>", re.DOTALL), " "),
    (re.compile(r"<reasoning>.*?</reasoning>", re.DOTALL), " "),
    (re.compile(r"<tools>.*?</tools>", re.DOTALL), " "),
    (re.compile(r"```[\s\S]*?```"), " "),
    (re.compile(r"`[^`]+`"), " "),                          # inline code
    (re.compile(r"!\[.*?\]\(.*?\)"), " "),                  # image markdown
    (re.compile(r"\|[^\n]*\|(?:\n\|[^\n]*\|)*"), " "),      # tables
    (re.compile(r"<[^>]+>"), " "),                          # complete HTML tag
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),          # markdown link → text
    (re.compile(r"\*+"), ""),                               # bold/italic stars
    (re.compile(r"(?<!\w)_+(?!\w)"), ""),                   # underscore emphasis
]


def _clean_piece(text: str) -> str:
    for pat, repl in _INLINE_PATTERNS:
        text = pat.sub(repl, text)
    return text


# Sentence boundary: . ! ? optionally followed by closing quote/paren,
# then whitespace, then uppercase letter.
# Capture group 1 = punctuation; group 2 = optional closing quotes/parens.
_SENTENCE_RE = re.compile(r"([\.!?])([\"'\)\]]*)\s+(?=[A-Z])")

# Ellipsis: 3+ dots, then whitespace, then uppercase.
_ELLIPSIS_RE = re.compile(r"(\.{3,})\s+(?=[A-Z])")

# Paragraph: at least two newlines in a row (allow whitespace between).
_PARAGRAPH_RE = re.compile(r"\n\s*\n")

# Secondary: ; or : followed by whitespace + uppercase. Only splits when
# the chunk is already large (see _SECONDARY_SPLIT_FRACTION above).
_SECONDARY_RE = re.compile(r"([;:])\s+(?=[A-Z])")


def _find_split(buf: str, max_chars: int, min_chars: int) -> Optional[Tuple[str, str, str]]:
    """Find earliest valid split point in buf. Returns (chunk, boundary,
    remainder) or None if no split is justified yet."""
    if len(buf) < min_chars:
        return None

    # 1. Paragraph: always splits (even ignoring min_chars logically — but
    # we already gated on min_chars above).
    m = _PARAGRAPH_RE.search(buf)
    if m:
        return buf[:m.start()], "paragraph", buf[m.end():]

    # 2. Ellipsis: check before sentence_re since `...` contains `.` that
    # sentence_re would match as a plain period.
    m = _ELLIPSIS_RE.search(buf)
    if m:
        return buf[:m.end(1)], "ellipsis", buf[m.end():]

    # 3. Sentence: .!?
    m = _SENTENCE_RE.search(buf)
    if m:
        end = m.end(2) if m.group(2) else m.end(1)
        return buf[:end], "sentence", buf[m.end():]

    # 4. Secondary: ; : — only if chunk is already big enough
    if len(buf) >= max_chars * _SECONDARY_SPLIT_FRACTION:
        m = _SECONDARY_RE.search(buf)
        if m:
            return buf[:m.end(1)], "secondary", buf[m.end():]

    # 5. Max-char fallback: split on the last whitespace inside the window.
    if len(buf) >= max_chars:
        cut = buf.rfind(" ", min_chars, max_chars)
        if cut < 0:
            cut = max_chars
        return buf[:cut], "maxlen", buf[cut:].lstrip()

    return None
