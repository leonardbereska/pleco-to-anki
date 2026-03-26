#!/usr/bin/env python3
"""
pleco_to_anki.py — Convert Pleco flashcard XML export to a modular Anki deck.

Usage:
    python3 pleco_to_anki.py                             # latest xml/ → apkg/
    python3 pleco_to_anki.py xml/2026-03-23.xml          # specific file → apkg/
    python3 pleco_to_anki.py flash.xml -o out.apkg       # custom output
    python3 pleco_to_anki.py --pinyin                    # include pinyin
    python3 pleco_to_anki.py --light                     # light mode

Defaults:
    - Input: latest .xml in xml/ by date-sorted filename
    - Output: apkg/<basename>.apkg
    - Dark mode (Pleco-style #2C2C2C background, pastel tone colors)
    - Pinyin hidden on answer side and in examples

Flags:
    --pinyin   Show pinyin on the answer card and in example sentences
    --light    White background with saturated tone colors

All Chinese text (headword + examples) is per-character tone-colored.
Headword and example Chinese text link to Pleco via plecoapi:// URLs.

Requirements:
    pip install genanki
"""

import xml.etree.ElementTree as ET
import genanki
import re
import hashlib
import sys
import os
import unicodedata

# ═══════════════════════════════════════════════════════════════════════
# TONE ENGINE
# ═══════════════════════════════════════════════════════════════════════

TONE_COLORS = {1: "#e74c3c", 2: "#e67e22", 3: "#27ae60", 4: "#2980b9", 5: "#888888"}
# CSS class names for tones (used in fields so template CSS controls color)
TONE_CLASSES = {1: "t1", 2: "t2", 3: "t3", 4: "t4", 5: "t5"}

TONE_MARK_TO_NUM = {}
for _ch, _t in [
    ('\u0101',1),('\u00e1',2),('\u01ce',3),('\u00e0',4),
    ('\u0113',1),('\u00e9',2),('\u011b',3),('\u00e8',4),
    ('\u012b',1),('\u00ed',2),('\u01d0',3),('\u00ec',4),
    ('\u014d',1),('\u00f3',2),('\u01d2',3),('\u00f2',4),
    ('\u016b',1),('\u00fa',2),('\u01d4',3),('\u00f9',4),
    ('\u01d6',1),('\u01d8',2),('\u01da',3),('\u01dc',4),
]:
    TONE_MARK_TO_NUM[_ch] = _t

PINYIN_RE = re.compile(
    r"[a-z\u00fc"
    r"\u0101\u00e1\u01ce\u00e0\u0113\u00e9\u011b\u00e8"
    r"\u012b\u00ed\u01d0\u00ec\u014d\u00f3\u01d2\u00f2"
    r"\u016b\u00fa\u01d4\u00f9\u01d6\u01d8\u01da\u01dc]", re.I
)


def _is_cjk(ch):
    return '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'


def tone_of_marked(s):
    for ch in s:
        if ch in TONE_MARK_TO_NUM:
            return TONE_MARK_TO_NUM[ch]
    return 5


def parse_numbered(s):
    """'zhi2jie1' → [('zhi', 2), ('jie', 1)]"""
    return [(m.group(1), int(m.group(2)))
            for m in re.finditer(r"([a-z\u00fc]+)([1-5])", s, re.I)]


def marked_to_syllables(pinyin_str):
    """Split diacritical pinyin into syllables with tones.

    Handles: spaces, apostrophes, multi-syllable tokens, erhua (diǎnr),
    toneless particles glued to the next word (dechùjiǎo → de+chù+jiǎo),
    and trailing punctuation.
    """
    # Known toneless particles that may be glued to the next syllable
    toneless_prefixes = {"de", "le", "ge", "me", "ne", "ma", "ba", "ya", "a"}

    result = []
    for word in pinyin_str.split():
        # Strip trailing punctuation (periods, commas, etc.)
        word_clean = word.rstrip('.,;:!?。，；：！？')
        if not word_clean:
            continue
        for part in word_clean.split("'"):
            if not part:
                continue
            # Check if part starts with a toneless prefix glued to a toned syllable
            # e.g. "dechùjiǎo" → "de" + "chùjiǎo"
            has_tone = any(c in TONE_MARK_TO_NUM for c in part)
            if has_tone:
                split_off = None
                for prefix in toneless_prefixes:
                    if part.lower().startswith(prefix) and len(part) > len(prefix):
                        remainder = part[len(prefix):]
                        if any(c in TONE_MARK_TO_NUM for c in remainder):
                            split_off = prefix
                            break
                if split_off:
                    result.append((part[:len(split_off)], 5))
                    sub_syls = _split_pinyin_word(part[len(split_off):])
                    result.extend(sub_syls)
                else:
                    sub_syls = _split_pinyin_word(part)
                    result.extend(sub_syls)
            else:
                # Entirely toneless (neutral particle)
                result.append((part, 5))
    return result


def _split_pinyin_word(word):
    """Split a multi-syllable pinyin word like 'dàjiātíng' into syllables.

    Uses known pinyin initials. Scans *backward* from the next tone-marked
    vowel so that ambiguous 'n'/'ng' are kept as finals of the current
    syllable rather than grabbed as initials of the next one.

    Examples:
        mínzú     → mín | zú
        gōngchéng → gōng | chéng
        dàjiātíng → dà | jiā | tíng
        zhíjiē    → zhí | jiē
        xiàngzhēng→ xiàng | zhēng
    """
    tone_positions = []
    for i, ch in enumerate(word):
        if ch in TONE_MARK_TO_NUM:
            tone_positions.append((i, TONE_MARK_TO_NUM[ch]))

    # Two-char initials must be checked before single-char ones
    initials_2 = {"zh", "ch", "sh"}
    initials_1 = {"b","p","m","f","d","t","n","l","g","k","h","j","q","x","z","c","s","r","w","y"}

    vowels_and_marks = set(
        "aeiouü"
        "\u0101\u00e1\u01ce\u00e0\u0113\u00e9\u011b\u00e8"
        "\u012b\u00ed\u01d0\u00ec\u014d\u00f3\u01d2\u00f2"
        "\u016b\u00fa\u01d4\u00f9\u01d6\u01d8\u01da\u01dc"
    )

    if not tone_positions:
        return [(word, 5)]

    def _find_trailing_neutral(w, last_tone_pos):
        """Find start of a trailing neutral syllable after last tone mark."""
        for pos in range(last_tone_pos + 1, len(w)):
            lo = w[pos:].lower()
            if len(lo) >= 2 and lo[:2] in initials_2:
                return pos
            if lo and lo[0] in initials_1:
                after = w[pos+1:]
                if after and any(c.lower() in vowels_and_marks for c in after):
                    return pos
        return None

    if len(tone_positions) == 1:
        split = _find_trailing_neutral(word, tone_positions[0][0])
        if split is not None:
            return [(word[:split], tone_positions[0][1]), (word[split:], 5)]
        return [(word, tone_positions[0][1])]

    def _has_vowel_before_or_at(pos, limit):
        """Check the slice word[pos:limit+1] contains a vowel/tone-mark."""
        return any(word[k].lower() in vowels_and_marks for k in range(pos, min(limit + 1, len(word))))

    def _initial_len_at(pos):
        """Return length of a valid pinyin initial at pos, or 0."""
        lo = word[pos:pos+2].lower()
        if len(lo) >= 2 and lo[:2] in initials_2:
            return 2
        if lo and lo[0] in initials_1:
            return 1
        return 0

    syllables = []
    prev_end = 0

    for idx in range(len(tone_positions) - 1):
        curr_tone_pos = tone_positions[idx][0]
        next_tone_pos = tone_positions[idx + 1][0]

        # The gap between the two tone marks contains the boundary.
        # Scan backward from next_tone_pos: find the latest position
        # where a valid initial starts and still reaches the next tone vowel.
        # "Latest" = maximally assigns characters to the current syllable's
        # final (keeping n, ng, r as finals when possible).
        best_split = None
        for pos in range(next_tone_pos, curr_tone_pos, -1):
            # Check for 2-char initial starting one position back (zh, ch, sh)
            # This must be checked first so we don't greedily grab just "h"
            if pos >= 1:
                two_char = word[pos-1:pos+1].lower()
                if two_char in initials_2 and _has_vowel_before_or_at(pos + 1, next_tone_pos) and pos - 1 > curr_tone_pos:
                    best_split = pos - 1
                    break
            il = _initial_len_at(pos)
            if il > 0 and _has_vowel_before_or_at(pos + il, next_tone_pos):
                best_split = pos
                break
            # Also allow zero-initial syllables (starting with a vowel)
            if word[pos].lower() in vowels_and_marks:
                best_split = pos
                # Don't break; keep scanning backward for a consonant initial

        if best_split is None:
            best_split = next_tone_pos  # fallback

        syllables.append((word[prev_end:best_split], tone_positions[idx][1]))
        prev_end = best_split

    syllables.append((word[prev_end:], tone_positions[-1][1]))
    return syllables


def _tspan(text, tone):
    """Wrap in a span with tone class."""
    cls = TONE_CLASSES.get(tone, "t5")
    return f'<span class="{cls}">{text}</span>'


# ── Colorize functions ──

def colorize_chars_numbered(chars, pinyin_numbered):
    """Color CJK chars using numbered pinyin ('zhi2jie1')."""
    syls = parse_numbered(pinyin_numbered)
    cjk = [c for c in chars if _is_cjk(c)]
    if len(cjk) != len(syls):
        return chars
    out, si = [], 0
    for c in chars:
        if _is_cjk(c) and si < len(syls):
            out.append(_tspan(c, syls[si][1]))
            si += 1
        else:
            out.append(c)
    return "".join(out)


def colorize_chars_marked(chars, pinyin_marked):
    """Color CJK chars using diacritical pinyin ('zhíjiē huìwù').
    Aligns CJK characters to pinyin syllables positionally.
    Handles erhua (儿) and slight mismatches with best-effort coloring."""
    syls = marked_to_syllables(pinyin_marked)
    cjk = [c for c in chars if _is_cjk(c)]

    # If counts match, perfect alignment
    if len(cjk) == len(syls):
        out, si = [], 0
        for c in chars:
            if _is_cjk(c) and si < len(syls):
                out.append(_tspan(c, syls[si][1]))
                si += 1
            else:
                out.append(c)
        return "".join(out)

    # Handle erhua: if we have one more CJK char than syllables,
    # look for 儿 and give it the tone of the preceding syllable
    if len(cjk) == len(syls) + 1:
        # Expand syllable list: duplicate the tone for any syllable
        # whose text ends in 'r' (erhua), inserting a neutral tone for 儿
        expanded = []
        for syl_text, syl_tone in syls:
            expanded.append(syl_tone)
            if syl_text.lower().rstrip('.,;:!?').endswith('r') and len(syl_text) > 1:
                expanded.append(5)  # 儿 gets neutral tone
        if len(expanded) == len(cjk):
            out, si = [], 0
            for c in chars:
                if _is_cjk(c) and si < len(expanded):
                    out.append(_tspan(c, expanded[si]))
                    si += 1
                else:
                    out.append(c)
            return "".join(out)

    # Best-effort fallback: color as many as we can from the start
    out, si = [], 0
    for c in chars:
        if _is_cjk(c):
            if si < len(syls):
                out.append(_tspan(c, syls[si][1]))
                si += 1
            else:
                out.append(_tspan(c, 5))  # grey for unmatched
        else:
            out.append(c)
    return "".join(out)


def colorize_pinyin_numbered(s):
    syls = parse_numbered(s)
    return "".join(_tspan(f"{sy}{t}", t) for sy, t in syls) if syls else s


def colorize_pinyin_marked(text):
    """Colorize diacritical pinyin per-syllable, preserving spaces/punct."""
    out, i = [], 0
    while i < len(text):
        if PINYIN_RE.match(text[i]):
            j = i
            while j < len(text) and (PINYIN_RE.match(text[j]) or text[j] == "'"):
                j += 1
            token = text[i:j]
            # Split on apostrophes first, then into syllables
            for k, part in enumerate(token.split("'")):
                if part:
                    syls = _split_pinyin_word(part)
                    for syl_text, syl_tone in syls:
                        out.append(_tspan(syl_text, syl_tone))
                if k < len(token.split("'")) - 1:
                    out.append("'")
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def pleco_link(text, query=None):
    """Wrap text in a Pleco deep-link."""
    q = query or re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', text)
    if not q:
        return text
    return f'<a href="plecoapi://x-callback-url/s?q={q}" class="pleco-link">{text}</a>'


# ═══════════════════════════════════════════════════════════════════════
# DEFINITION PARSER (same logic as before)
# ═══════════════════════════════════════════════════════════════════════

POS_TAGS = [
    "well-known phrase", "adjective", "conjunction", "preposition",
    "interjection", "adverb", "idiom", "noun", "verb",
]
POS_MAP = {
    "well-known phrase": "PHRASE", "adjective": "ADJ", "conjunction": "CONJ",
    "preposition": "PREP", "interjection": "INTJ", "adverb": "ADV",
    "idiom": "IDIOM", "noun": "NOUN", "verb": "VERB",
}
DOMAIN_LABELS = [
    "philosophy", "medicine", "mechanics", "biology", "mathematics",
    "radio", "computing", "telecommunications", "statistics",
    "economics", "archaic", "literary", "figurative", "colloquial",
    "dialect", "zoology", "general name for", "also pr.",
]


def _extract_pos(text):
    lower = text.lower()
    for tag in POS_TAGS:
        if lower.startswith(tag):
            return POS_MAP[tag], text[len(tag):].strip()
    return "", text


def parse_defn(defn_text, headword):
    if not defn_text:
        return "", "", []
    text = defn_text.strip()
    pos, text = _extract_pos(text)
    clean = re.sub(r'\(opp\.[^)]*\)', '', text)
    clean = re.sub(r'See\s+\d+\S*', '', clean)
    # VARIANT OF with possible glued CJK + numbered pinyin + CJK
    clean = re.sub(r'VARIANT OF\s+\d*\S*', '', clean, flags=re.I)
    # Remove "general name for" references
    clean = re.sub(r'general name for\b', '', clean, flags=re.I)
    # Remove numbered pinyin like shen2jing1guo4min3
    clean = re.sub(r'\b[a-z]+\d[a-z\d]+\b', '', clean, flags=re.I)
    # Remove sense numbers with optional domain labels: "2 figurative", "1 zoology"
    clean = re.sub(r'\b\d+\s+(?:' + '|'.join(re.escape(d) for d in DOMAIN_LABELS) + r')\b', '', clean, flags=re.I)
    # Remove bare sense numbers
    clean = re.sub(r'\b\d+\s+', ' ', clean)
    m = re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]{2,}', clean)
    if m:
        english = clean[:m.start()].strip()
        example_region = text
    else:
        english = clean.strip()
        example_region = ""
    english = re.sub(r'\(opp\.[^)]*\)', '', english)
    english = re.sub(r'\s+', ' ', english).strip().rstrip(';').strip()
    english = re.sub(r'^\d+\s+', '', english)
    for d in DOMAIN_LABELS:
        if english.lower().startswith(d):
            english = english[len(d):].strip().lstrip(',').strip()
    # Final cleanup: remove stray "verb", "noun" etc that leaked through
    for tag in POS_TAGS:
        english = re.sub(r'\b' + re.escape(tag) + r'\b', '', english, flags=re.I)
    english = re.sub(r'\s+', ' ', english).strip().rstrip(';').strip()
    # Remove consecutive duplicate words (e.g. "stammer stammer")
    english = re.sub(r'\b(\w+)\s+\1\b', r'\1', english, flags=re.I)
    # Deduplicate semicolon-separated terms
    if ';' in english:
        seen_terms = []
        for term in english.split(';'):
            t = term.strip()
            if t and t.lower() not in [s.lower() for s in seen_terms]:
                seen_terms.append(t)
        english = '; '.join(seen_terms)
    examples = _parse_examples(example_region, headword) if example_region else []
    return pos, english, examples[:4]


def _parse_examples(text, headword):
    results = []
    text = re.sub(r'\(opp\.[^)]*\)', '', text)
    text = re.sub(r'\((?:esp\.|or )[^)]*\)', '', text)
    # Clean See references (may be glued to CJK: "See 29779968神经过敏")
    text = re.sub(r'See\s+\d+\S*', '', text)
    text = re.sub(r'VARIANT OF \d+\S*', '', text, flags=re.I)
    text = re.sub(r'\[[^\]]*\]', '', text)
    # Remove numbered pinyin like "shen2jing1guo4min3" (digits mixed with latin)
    text = re.sub(r'\b[a-z]+\d[a-z\d]+\b', '', text, flags=re.I)
    # Remove stray bare numbers
    text = re.sub(r'(?<!\S)\d{5,}(?!\S)', '', text)
    for tag in POS_TAGS:
        text = re.sub(r'\b' + re.escape(tag) + r'\b', '|||', text, flags=re.I)
    for d in DOMAIN_LABELS:
        text = re.sub(r'\b' + re.escape(d) + r'\b', '', text, flags=re.I)
    text = re.sub(r'\b\d+\s+', ' ', text)
    # Pre-split on Chinese sentence-final punctuation (。？！) so that
    # two sentences glued together (e.g. "你赞成吗？我完全赞成。") become
    # separate blocks. Insert a space after each sentence-ender.
    text = re.sub(r'([。？！])', r'\1 ', text)
    # CJK block pattern: match sequences of CJK + Chinese punctuation
    # Sentence-enders (。？！) can appear at the END but not in the middle
    cjk_pat = re.compile(
        r'([\u4e00-\u9fff\u3400-\u4dbf]'
        r'[\u4e00-\u9fff\u3400-\u4dbf'
        r'\uff0c\u3001\uff1b\uff1a'
        r'\u201c\u201d\u2018\u2019\uff08\uff09'
        r'\u300a\u300b\u3010\u3011,\s]*'
        r'[\u4e00-\u9fff\u3400-\u4dbf\u3002\uff01\uff1f。？！]?)'
    )
    for section in text.split('|||'):
        section = section.strip()
        if not section:
            continue
        blocks = list(cjk_pat.finditer(section))
        for idx, bm in enumerate(blocks):
            chinese = bm.group().strip()
            if sum(1 for c in chinese if _is_cjk(c)) < 2:
                continue
            start = bm.end()
            end = blocks[idx + 1].start() if idx + 1 < len(blocks) else len(section)
            after = section[start:end].strip()
            after = re.sub(r'^[)\]\s;,]+', '', after).strip()
            if not after:
                continue
            pinyin, english = _split_py_en(after)
            if not english or len(english) <= 1:
                continue
            # Validation: skip if pinyin is empty (likely garbage)
            if not pinyin:
                continue
            # Validation: skip if english contains numbered pinyin
            if re.search(r'[a-z]+\d[a-z\d]+', english, re.I):
                continue
            # Validation: pinyin syllable count should roughly match CJK char count
            # Allow some slack for punctuation and particles
            n_cjk = sum(1 for c in chinese if _is_cjk(c))
            n_syls = len(marked_to_syllables(pinyin))
            if n_syls > 0 and (n_syls > n_cjk * 2 or n_cjk > n_syls * 2):
                continue  # wildly misaligned, skip
            # Clean: remove trailing "or" fragments and stray punctuation
            english = english.rstrip('. ').strip()
            if english:
                results.append((chinese, pinyin, english))
    return results


def _split_py_en(text):
    words = text.split()
    if not words:
        return "", ""
    py_chars = set(
        "abcdefghijklmnopqrstuvwxyz\u00fc"
        "\u0101\u00e1\u01ce\u00e0\u0113\u00e9\u011b\u00e8"
        "\u012b\u00ed\u01d0\u00ec\u014d\u00f3\u01d2\u00f2"
        "\u016b\u00fa\u01d4\u00f9\u01d6\u01d8\u01da\u01dc'-"
    )
    neutral_particles = {"de", "le", "ba", "ge", "me", "ne", "ma", "ya"}
    py_words = []
    en_start = 0
    for i, w in enumerate(words):
        cw = w.lower().rstrip('.,;:!?')
        has_tone = any(c in TONE_MARK_TO_NUM for c in cw)
        is_alpha = all(c in py_chars for c in cw) and len(cw) > 0
        if py_words and py_words[-1].rstrip()[-1:] in '.。！？!?':
            en_start = i
            break
        if is_alpha and has_tone:
            py_words.append(w)
            en_start = i + 1
        elif is_alpha and not has_tone and cw in neutral_particles and py_words:
            py_words.append(w)
            en_start = i + 1
        else:
            en_start = i
            break
    return " ".join(py_words), " ".join(words[en_start:])


# ═══════════════════════════════════════════════════════════════════════
# ANKI MODEL — MODULAR FIELDS + TEMPLATES
# ═══════════════════════════════════════════════════════════════════════

FIELDS = [
    "Hanzi",           # tone-colored headword, Pleco-linked
    "Pinyin",          # tone-colored numbered pinyin
    "POS",             # part of speech label
    "English",         # English definition (plain text)
    "HanziRaw",        # raw headword (for sorting, searching)
    "PinyinRaw",       # raw numbered pinyin
    "Ex1Chinese",      # example 1: tone-colored Chinese, Pleco-linked
    "Ex1Pinyin",       # example 1: tone-colored pinyin
    "Ex1English",      # example 1: English translation
    "Ex1Cloze",        # example 1: Chinese with headword replaced by ~
    "Ex1ClozePinyin",  # example 1: pinyin with headword pinyin replaced by ~
    "Ex2Chinese", "Ex2Pinyin", "Ex2English", "Ex2Cloze", "Ex2ClozePinyin",
    "Ex3Chinese", "Ex3Pinyin", "Ex3English", "Ex3Cloze", "Ex3ClozePinyin",
    "Ex4Chinese", "Ex4Pinyin", "Ex4English", "Ex4Cloze", "Ex4ClozePinyin",
]

CARD_CSS_DARK = """\
/* ── Base ── */
.card {
    font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei",
                 "Heiti SC", "Source Han Sans CN", sans-serif;
    background: #2C2C2C;
    color: #e0e0e0;
    padding: 24px;
    text-align: left;
    max-width: 520px;
    margin: 0 auto;
    line-height: 1.5;
}
/* ── Tone colors ── */
.t1 { color: #ff8080; }  /* T1 red */
.t2 { color: #80ff80; }  /* T2 green */
.t3 { color: #7070ff; }  /* T3 blue */
.t4 { color: #df80ff; }  /* T4 purple */
.t5 { color: #c6c6c6; }  /* T5 neutral grey */
/* ── Headword ── */
.headword {
    font-size: 64px;
    font-weight: 700;
    margin-bottom: 8px;
    line-height: 1.15;
    letter-spacing: 2px;
}
/* ── Pinyin ── */
.pinyin {
    font-size: 20px;
    margin-bottom: 10px;
    letter-spacing: 0.5px;
}
/* ── POS ── */
.pos {
    font-size: 13px;
    font-weight: 700;
    color: #8899aa;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin-bottom: 8px;
}
/* ── English ── */
.english {
    font-size: 18px;
    color: #d0d8e0;
    margin-bottom: 18px;
    line-height: 1.45;
}
/* ── Examples ── */
.examples {
    border-top: 1px solid rgba(255,255,255,0.1);
    padding-top: 14px;
}
.ex {
    margin-bottom: 16px;
    padding-left: 12px;
    border-left: 3px solid #3498db;
}
.ex-zh {
    font-size: 21px;
    color: #f0f0f0;
    margin-bottom: 3px;
}
.ex-py {
    font-size: 14px;
    color: #99aabb;
    margin-bottom: 2px;
}
.ex-en {
    font-size: 14px;
    color: #d0d8e0;
}
/* ── Pleco links ── */
.pleco-link {
    text-decoration: none;
    color: inherit;
}
"""

CARD_CSS_LIGHT = """\
/* ── Base ── */
.card {
    font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei",
                 "Heiti SC", "Source Han Sans CN", sans-serif;
    background: #ffffff;
    color: #1a1a1a;
    padding: 24px;
    text-align: left;
    max-width: 520px;
    margin: 0 auto;
    line-height: 1.5;
}
/* ── Tone colors ── */
.t1 { color: #e30000; }  /* T1 red */
.t2 { color: #01b31c; }  /* T2 green */
.t3 { color: #150ff0; }  /* T3 blue */
.t4 { color: #8800bf; }  /* T4 purple */
.t5 { color: #888888; }  /* T5 neutral grey */
/* ── Headword ── */
.headword {
    font-size: 64px;
    font-weight: 700;
    margin-bottom: 8px;
    line-height: 1.15;
    letter-spacing: 2px;
}
/* ── Pinyin ── */
.pinyin {
    font-size: 20px;
    margin-bottom: 10px;
    letter-spacing: 0.5px;
}
/* ── POS ── */
.pos {
    font-size: 13px;
    font-weight: 700;
    color: #667788;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    margin-bottom: 8px;
}
/* ── English ── */
.english {
    font-size: 18px;
    color: #2a2a2a;
    margin-bottom: 18px;
    line-height: 1.45;
}
/* ── Examples ── */
.examples {
    border-top: 1px solid rgba(0,0,0,0.1);
    padding-top: 14px;
}
.ex {
    margin-bottom: 16px;
    padding-left: 12px;
    border-left: 3px solid #3498db;
}
.ex-zh {
    font-size: 21px;
    color: #1a1a1a;
    margin-bottom: 3px;
}
.ex-py {
    font-size: 14px;
    color: #556677;
    margin-bottom: 2px;
}
.ex-en {
    font-size: 14px;
    color: #2a2a2a;
}
/* ── Pleco links ── */
.pleco-link {
    text-decoration: none;
    color: inherit;
}
"""

CARD_CSS = CARD_CSS_DARK  # default

# Template helper: conditionally show an example block
def _ex_block(n, cloze=False, pinyin=True):
    """Generate template snippet for example N (1-4)."""
    zh_field = f"Ex{n}Cloze" if cloze else f"Ex{n}Chinese"
    py_field = f"Ex{n}ClozePinyin" if cloze else f"Ex{n}Pinyin"
    py_line = f'<div class="ex-py">{{{{{py_field}}}}}</div>' if pinyin else ""
    return (
        f'{{{{#Ex{n}Chinese}}}}'
        f'<div class="ex">'
        f'<div class="ex-zh">{{{{{zh_field}}}}}</div>'
        f'{py_line}'
        f'<div class="ex-en">{{{{Ex{n}English}}}}</div>'
        f'</div>'
        f'{{{{/Ex{n}Chinese}}}}'
    )


def _build_templates(pinyin=False):
    """Build front/back templates. pinyin=False omits pinyin (default)."""
    front = (
        '<div class="pos">{{POS}}</div>'
        '<div class="english">{{English}}</div>'
        '{{#Ex1Chinese}}<div class="examples">'
        + _ex_block(1, cloze=True, pinyin=pinyin)
        + _ex_block(2, cloze=True, pinyin=pinyin)
        + _ex_block(3, cloze=True, pinyin=pinyin)
        + _ex_block(4, cloze=True, pinyin=pinyin)
        + '</div>{{/Ex1Chinese}}'
    )
    pinyin_line = '<div class="pinyin">{{Pinyin}}</div>' if pinyin else ""
    back = (
        '<div class="headword">{{Hanzi}}</div>'
        + pinyin_line
        + '<div class="pos">{{POS}}</div>'
        '<div class="english">{{English}}</div>'
        '{{#Ex1Chinese}}<div class="examples">'
        + _ex_block(1, cloze=False, pinyin=pinyin)
        + _ex_block(2, cloze=False, pinyin=pinyin)
        + _ex_block(3, cloze=False, pinyin=pinyin)
        + _ex_block(4, cloze=False, pinyin=pinyin)
        + '</div>{{/Ex1Chinese}}'
    )
    return front, back


MODEL_ID = int(hashlib.md5(b"pleco-modular-v3").hexdigest()[:8], 16)
DECK_ID = int(hashlib.md5(b"pleco-deck-modular-v3").hexdigest()[:8], 16)


def _build_model(pinyin=False, light=False):
    """Construct the genanki Model with the chosen options."""
    front, back = _build_templates(pinyin=pinyin)
    css = CARD_CSS_LIGHT if light else CARD_CSS_DARK
    return genanki.Model(
        MODEL_ID,
        "Pleco Chinese (Modular, Tone-Colored)",
        fields=[{"name": f} for f in FIELDS],
        templates=[{
            "name": "Recognition",
            "qfmt": front,
            "afmt": back,
        }],
        css=css,
        sort_field_index=FIELDS.index("HanziRaw"),
    )


# ═══════════════════════════════════════════════════════════════════════
# FIELD BUILDER
# ═══════════════════════════════════════════════════════════════════════

def _colorize_chars_with_syls(ch_text, cjk_chars, py_syls, cloze_range=None):
    """Colorize CJK characters using pre-aligned syllables.
    If cloze_range=(start, end), replace those CJK chars with ~."""
    out = []
    si = 0
    for j, c in enumerate(ch_text):
        if _is_cjk(c) and si < len(py_syls):
            if cloze_range and cloze_range[0] <= si < cloze_range[1]:
                if si == cloze_range[0]:
                    out.append('<span class="t5">~</span>')
                # Skip the other chars in the cloze range (don't output them)
            else:
                _, tone = py_syls[si]
                out.append(_tspan(c, tone))
            si += 1
        else:
            # Non-CJK character (punctuation, spaces)
            # In cloze mode, skip punctuation that's "inside" the headword
            if cloze_range and si > cloze_range[0] and si <= cloze_range[1]:
                pass  # skip punctuation attached to clozed headword
            else:
                out.append(c)
    return "".join(out)


def _cloze_pinyin_marked(py, py_syls, hw_start, hw_end):
    """Replace syllables hw_start..hw_end in the pinyin with ~, colorize the rest.

    Works at the raw text level: reconstruct the pinyin string with the
    headword syllables replaced by ~, then colorize what remains.
    """
    # We need to map syllable indices back to character positions in py.
    # Rebuild: walk through py, assign characters to syllables, replace the
    # headword syllables' text with ~.

    # Collect the raw text for each syllable by re-splitting
    # Use the same splitting logic as marked_to_syllables
    words = py.split()
    syl_texts = []  # list of (text, is_space_after)
    for wi, word in enumerate(words):
        for part in word.split("'"):
            if part:
                sub_syls = _split_pinyin_word(part)
                for st, _ in sub_syls:
                    syl_texts.append(st)
        # Don't add space logic here; we'll reconstruct from words

    # Simpler approach: just rebuild from syllable texts with original spacing
    # Map each syllable to its word index, reconstruct with spaces
    result_parts = []
    si = 0
    for wi, word in enumerate(words):
        if wi > 0:
            result_parts.append(" ")
        apo_parts = word.split("'")
        for api, apart in enumerate(apo_parts):
            if api > 0:
                result_parts.append("'")
            if apart:
                sub_syls = _split_pinyin_word(apart)
                for syl_text, syl_tone in sub_syls:
                    if hw_start <= si < hw_end:
                        if si == hw_start:
                            result_parts.append(_tspan("~", 5))
                    else:
                        result_parts.append(_tspan(syl_text, syl_tone))
                    si += 1

    return "".join(result_parts)


def build_fields(hw, pinyin_num, pos, english, examples):
    """Build the list of field values for one note."""
    # Headword: tone-colored, Pleco-linked
    hanzi_colored = colorize_chars_numbered(hw, pinyin_num)
    hanzi_field = pleco_link(hanzi_colored, query=hw)

    # Pinyin: tone-colored
    pinyin_field = colorize_pinyin_numbered(pinyin_num)

    fields = {
        "Hanzi": hanzi_field,
        "Pinyin": pinyin_field,
        "POS": pos,
        "English": english,
        "HanziRaw": hw,
        "PinyinRaw": pinyin_num,
    }

    # Examples — filter to only those with correct CJK-pinyin alignment
    good_examples = []
    for ch, py, en in examples:
        cjk_chars = [(j, c) for j, c in enumerate(ch) if _is_cjk(c)]
        py_syls = marked_to_syllables(py)
        # Accept exact match, or erhua (+1 CJK char)
        if len(cjk_chars) == len(py_syls):
            good_examples.append((ch, py, en))
        elif len(cjk_chars) == len(py_syls) + 1:
            # Erhua case: check if any syllable ends in 'r'
            if any(s.lower().rstrip('.,;:!?').endswith('r') for s, _ in py_syls):
                good_examples.append((ch, py, en))
        # Otherwise: skip this example entirely

    for i in range(4):
        prefix = f"Ex{i+1}"
        if i < len(good_examples):
            ch, py, en = good_examples[i]

            # ── Step 1: Align CJK characters to pinyin syllables ──
            cjk_chars = [(j, c) for j, c in enumerate(ch) if _is_cjk(c)]
            py_syls = marked_to_syllables(py)
            aligned = len(cjk_chars) == len(py_syls)

            # ── Step 2: Find headword position in the Chinese text ──
            hw_pos = ch.find(hw)
            hw_cjk_count = sum(1 for c in hw if _is_cjk(c))
            # Index of the headword's first CJK char among all CJK chars
            if hw_pos >= 0:
                hw_syl_start = sum(1 for c in ch[:hw_pos] if _is_cjk(c))
                hw_syl_end = hw_syl_start + hw_cjk_count
            else:
                hw_syl_start = hw_syl_end = -1

            # ── Step 3: Build full Chinese (tone-colored, Pleco-linked) ──
            if aligned:
                ch_colored = _colorize_chars_with_syls(ch, cjk_chars, py_syls)
            else:
                ch_colored = ch  # fallback: uncolored
            ch_query = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', ch)
            ch_linked = pleco_link(ch_colored, query=ch_query)

            # ── Step 4: Build cloze Chinese ──
            if aligned and hw_pos >= 0 and hw_syl_end <= len(py_syls):
                ch_cloze = _colorize_chars_with_syls(
                    ch, cjk_chars, py_syls,
                    cloze_range=(hw_syl_start, hw_syl_end)
                )
            elif hw_pos >= 0:
                ch_cloze = ch.replace(hw, '<span class="t5">~</span>')
            else:
                ch_cloze = ch_colored
            ch_cloze_linked = pleco_link(ch_cloze, query=ch_query)

            # ── Step 5: Build full pinyin (tone-colored) ──
            py_colored = colorize_pinyin_marked(py)

            # ── Step 6: Build cloze pinyin ──
            if aligned and hw_pos >= 0 and hw_syl_end <= len(py_syls):
                py_cloze = _cloze_pinyin_marked(py, py_syls, hw_syl_start, hw_syl_end)
            else:
                py_cloze = py_colored

            fields[f"{prefix}Chinese"] = ch_linked
            fields[f"{prefix}Pinyin"] = py_colored
            fields[f"{prefix}English"] = en
            fields[f"{prefix}Cloze"] = ch_cloze_linked
            fields[f"{prefix}ClozePinyin"] = py_cloze
        else:
            fields[f"{prefix}Chinese"] = ""
            fields[f"{prefix}Pinyin"] = ""
            fields[f"{prefix}English"] = ""
            fields[f"{prefix}Cloze"] = ""
            fields[f"{prefix}ClozePinyin"] = ""

    return [fields[f] for f in FIELDS]


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def convert(xml_path, output_path, pinyin=False, light=False):
    model = _build_model(pinyin=pinyin, light=light)

    tree = ET.parse(xml_path)
    root = tree.getroot()
    cards_el = root.find("cards")
    if cards_el is None:
        print("ERROR: No <cards> element. Is this a Pleco export?")
        sys.exit(1)

    deck = genanki.Deck(DECK_ID, "Pleco Chinese Flashcards")
    seen = set()
    n_ok = n_skip = n_no_ex = 0

    for card_el in cards_el.findall("card"):
        entry = card_el.find("entry")
        if entry is None:
            continue
        hw_el = entry.find("headword[@charset='sc']")
        if hw_el is None or not hw_el.text:
            continue
        hw = hw_el.text.strip()
        if hw in seen:
            n_skip += 1
            continue
        seen.add(hw)

        pr_el = entry.find("pron[@type='hypy']")
        pinyin_num = pr_el.text.strip() if pr_el is not None and pr_el.text else ""

        df_el = entry.find("defn")
        defn = df_el.text.strip() if df_el is not None and df_el.text else ""

        pos, english, examples = parse_defn(defn, hw)
        if not english:
            english = defn[:120] if defn else "(no definition)"
        if not examples:
            n_no_ex += 1

        field_values = build_fields(hw, pinyin_num, pos, english, examples)

        note = genanki.Note(
            model=model,
            fields=field_values,
            guid=genanki.guid_for(hw),
        )
        deck.add_note(note)
        n_ok += 1

    pkg = genanki.Package(deck)
    pkg.write_to_file(output_path)

    mode = "light" if light else "dark"
    py_status = "with" if pinyin else "without"
    print(f"Done: {output_path}  ({mode} mode, {py_status} pinyin)")
    print(f"  {n_ok} cards ({n_ok - n_no_ex} with examples, {n_no_ex} definition-only)")
    if n_skip:
        print(f"  {n_skip} duplicates skipped")


def _find_latest_xml(directory="xml"):
    """Find the XML file with the latest date-based name in *directory*."""
    import glob
    xmls = sorted(glob.glob(os.path.join(directory, "*.xml")))
    if not xmls:
        return None
    return xmls[-1]  # lexicographic sort on YYYY-MM-DD names = chronological


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Convert Pleco flashcard XML export to an Anki deck (.apkg)."
    )
    parser.add_argument("input", nargs="?", default=None,
                        help="Pleco XML export file (default: latest in xml/)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output .apkg path (default: apkg/<basename>.apkg)")
    parser.add_argument("--pinyin", action="store_true",
                        help="Include pinyin on the answer side and in examples")
    parser.add_argument("--light", action="store_true",
                        help="Use light-mode colors (white background)")
    args = parser.parse_args()

    xml_path = args.input
    if xml_path is None:
        xml_path = _find_latest_xml()
        if xml_path is None:
            print("ERROR: No XML files found in xml/")
            sys.exit(1)
        print(f"Using latest export: {xml_path}")

    if not os.path.exists(xml_path):
        print(f"ERROR: File not found: {xml_path}")
        sys.exit(1)

    if args.output:
        out_path = args.output
    else:
        basename = os.path.splitext(os.path.basename(xml_path))[0]
        out_dir = "apkg"
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, basename + ".apkg")

    convert(xml_path, out_path, pinyin=args.pinyin, light=args.light)
