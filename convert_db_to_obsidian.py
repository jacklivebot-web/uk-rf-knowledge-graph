#!/usr/bin/env python3
"""
Convert russian-law-mcp SQLite database (uk-rf) → Obsidian vault .md files
with YAML frontmatter, wiki-links, and MOCs.

Usage: python3 convert_db_to_obsidian.py
"""

import sqlite3
import json
import re
import os
import unicodedata

DB_PATH = "/home/clawd/node_modules/@ansvar/russian-law-mcp/data/database.db"
VAULT = "/home/clawd/uk-rf-vault"
STRUCT_PATH = f"{VAULT}/03-Resources/УК-РФ/structure.json"
ARTICLES_DIR = f"{VAULT}/03-Resources/УК-РФ/Статьи"
MOC_DIR = f"{VAULT}/06-MOC"

# ─── Helpers ───────────────────────────────────────────────

def normalize_spaces(text: str) -> str:
    """Remove excessive internal spaces from law text (DB artifact)."""
    # Replace multiple spaces with single, but preserve paragraph breaks
    text = re.sub(r'[^\S\n]{2,}', ' ', text)
    # Remove space before punctuation
    text = re.sub(r'\s+([,;:.!?)])', r'\1', text)
    # Remove space after opening bracket
    text = re.sub(r'([(])\s+', r'\1', text)
    return text.strip()


def roman_to_int(roman: str) -> int:
    """Convert Roman numeral to int."""
    vals = {'I':1,'V':5,'X':10,'L':50}
    result = 0
    for i, c in enumerate(roman):
        if i+1 < len(roman) and vals[c] < vals[roman[i+1]]:
            result -= vals[c]
        else:
            result += vals[c]
    return result


def int_to_roman(num: int) -> str:
    """Convert int to Roman numeral."""
    maps = [(12,'XII'),(11,'XI'),(10,'X'),(9,'IX'),(8,'VIII'),(7,'VII'),
            (6,'VI'),(5,'V'),(4,'IV'),(3,'III'),(2,'II'),(1,'I')]
    for n, r in maps:
        if num >= n:
            return r
    return str(num)


def slugify(text: str) -> str:
    """Create filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    return text[:80].rstrip('-')


def find_cross_refs(content: str) -> set:
    """
    Find article cross-references in law text and return set of article numbers.
    Patterns: ст.15, ст. 15, статьей 158, статьями 15 и 16, статьях 30, 31
    """
    refs = set()
    
    # Pattern 1: ст.NN or ст. NN (most common)
    for m in re.finditer(r'ст\.?\s*(\d{1,3}(?:\.\d)?)', content):
        num_str = m.group(1)
        # Filter: must be a valid УК РФ article (1-361 or dotted like 104.1)
        base = num_str.split('.')[0]
        if base.isdigit() and 1 <= int(base) <= 361:
            refs.add(num_str)
    
    # Pattern 2: стат*/стать* NN[, NN, NN]
    for m in re.finditer(r'стать(?:ь[ямив]?|ей|ёй|ями|ях|и)\s+((?:\d{1,3}(?:\.\d)?\s*(?:[,и]\s*)?)+)', content):
        nums_part = m.group(1)
        for nm in re.finditer(r'(\d{1,3}(?:\.\d)?)', nums_part):
            num_str = nm.group(1)
            base = num_str.split('.')[0]
            if base.isdigit() and 1 <= int(base) <= 361:
                refs.add(num_str)
    
    return refs


def wiki_link_article(article_num: str, content: str, existing_articles: set) -> str:
    """
    Replace references to articles with Obsidian wiki-links.
    Only link if the target article exists in our vault.
    """
    def replace_ref(m):
        prefix = m.group(1)  # "ст." or "статьей " etc
        num = m.group(2)
        base = num.split('.')[0]
        if base.isdigit() and num in existing_articles:
            return f"{prefix}[[ст.{num}|{num}]]"
        return m.group(0)  # leave as-is
    
    # Replace ст.NN and ст. NN
    content = re.sub(
        r'(ст\.?\s+)(\d{1,3}(?:\.\d)?)',
        replace_ref,
        content
    )
    
    return content


def article_filename(article_num: str) -> str:
    """Get filename for an article."""
    return f"ст.{article_num}.md"


def write_article_md(filepath: str, frontmatter: dict, title: str, content: str, 
                      outgoing_refs: list, amended_notes: list):
    """Write a single article .md file."""
    # Clean content
    content = normalize_spaces(content)
    
    # Build YAML frontmatter
    yf_lines = ["---"]
    for key in ['номер', 'заголовок', 'раздел', 'глава', 'статус', 'утратила_силу']:
        if key in frontmatter and frontmatter[key]:
            val = frontmatter[key]
            if isinstance(val, str) and ('"' in val or ':' in val or '#' in val):
                yf_lines.append(f'{key}: "{val}"')
            else:
                yf_lines.append(f'{key}: {val}')
    if amended_notes:
        yf_lines.append(f'редакции:')
        for note in amended_notes:
            yf_lines.append(f'  - "{note}"')
    if outgoing_refs:
        yf_lines.append(f'ссылки:')
        for ref in sorted(outgoing_refs, key=lambda x: (float(x) if '.' in x else int(x))):
            yf_lines.append(f'  - ст.{ref}')
    yf_lines.append("---")
    
    # Body
    body_lines = [f"# Статья {frontmatter.get('номер', '')}. {title}", ""]
    
    # Add section/chapter context
    if frontmatter.get('раздел') or frontmatter.get('глава'):
        ctx = []
        if frontmatter.get('раздел'):
            ctx.append(f"Раздел: [[{frontmatter['раздел']}]]")
        if frontmatter.get('глава'):
            ctx.append(f"Глава: [[{frontmatter['глава']}]]")
        body_lines.append(" > " + " | ".join(ctx))
        body_lines.append("")
    
    # Content
    body_lines.append(content)
    body_lines.append("")
    
    # Outgoing refs section
    if outgoing_refs:
        body_lines.append("## Ссылки на другие статьи")
        body_lines.append("")
        for ref in sorted(outgoing_refs, key=lambda x: (float(x) if '.' in x else int(x))):
            body_lines.append(f"- [[ст.{ref}]]")
        body_lines.append("")
    
    full_content = "\n".join(yf_lines) + "\n" + "\n".join(body_lines)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(full_content)


# ─── Main conversion ───────────────────────────────────────

def main():
    os.makedirs(ARTICLES_DIR, exist_ok=True)
    os.makedirs(MOC_DIR, exist_ok=True)
    
    # Connect to DB
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()
    
    # Load structure
    with open(STRUCT_PATH) as f:
        struct = json.load(f)
    
    # Build section/chapter maps from structure.json
    section_map = {}  # roman → title (short)
    chapter_map = {}  # "Раздел-Roman/Глава-N" → short title
    chapter_full = {}  # glava_num → full title
    section_full = {}  # roman → full title
    article_to_chapter = {}  # art_num → (razdel_roman, glava_num)
    
    for s in struct["sections"]:
        m = re.search(r'Razdel-([IVXL]+)', s["url"])
        if m:
            roman = m.group(1)
            section_full[roman] = s["title"]
            # Short title: "Раздел I"
            section_map[roman] = f"Раздел {roman}"
    
    for c in struct["chapters"]:
        m = re.search(r'Razdel-([IVXL]+)/Glava-(\d+)', c["url"])
        if m:
            roman = m.group(1)
            glava = int(m.group(2))
            chapter_full[glava] = (roman, c["title"])
    
    for a in struct["articles"]:
        m = re.search(r'Razdel-([IVXL]+)/Glava-(\d+)/Statya-(\d+)', a["url"])
        if m:
            roman = m.group(1)
            glava = int(m.group(2))
            art_num = int(m.group(3))
            article_to_chapter[art_num] = (roman, glava)
    
    # Get all provisions from DB
    cur.execute("SELECT article, title, content FROM provisions WHERE law_id='uk-rf' ORDER BY order_index")
    provisions = cur.fetchall()
    db.close()
    
    # First pass: collect all article numbers for wiki-link validation
    all_article_nums = set()
    for art, title, content in provisions:
        all_article_nums.add(art)
    
    print(f"Found {len(provisions)} provisions, {len(all_article_nums)} unique article numbers")
    
    # Second pass: find all cross-refs and build article data
    article_data = {}  # art_num → {title, content, section_roman, glava, status, outgoing_refs, amended}
    
    for art, title, content in provisions:
        # Determine section and chapter
        art_int = int(art) if art.isdigit() else None
        section_roman = None
        glava_num = None
        section_title = None
        chapter_title = None
        
        if art_int and art_int in article_to_chapter:
            section_roman, glava_num = article_to_chapter[art_int]
            section_title = section_map.get(section_roman, f"Раздел {section_roman}")
            if glava_num in chapter_full:
                _, ch_full = chapter_full[glava_num]
                # Short chapter title: "Глава 16"
                chapter_title = f"Глава {glava_num}"
        
        # Detect status
        status = "действует"
        is_repealed = False
        if "утратил" in content.lower() or "утратила" in content.lower():
            if len(content) < 200:  # Short content = likely fully repealed
                status = "утратила силу"
                is_repealed = True
        
        # Find amendments
        amended_notes = re.findall(r'\(В редакции .*?\)', content)
        amended_notes += re.findall(r'\(Дополнение.*?\)', content)
        
        # Clean title
        clean_title = normalize_spaces(title) if title else ""
        if not clean_title:
            clean_title = f"Статья {art}"
        
        # Find cross-refs
        refs = find_cross_refs(content)
        # Remove self-reference
        refs.discard(art)
        # Only keep refs to existing articles
        refs = refs & all_article_nums
        
        article_data[art] = {
            'title': clean_title,
            'content': content,
            'section_roman': section_roman,
            'section_title': section_title,
            'glava_num': glava_num,
            'chapter_title': chapter_title,
            'chapter_full': chapter_full.get(glava_num, (None, None))[1] if glava_num else None,
            'status': status,
            'is_repealed': is_repealed,
            'amended_notes': amended_notes,
            'outgoing_refs': sorted(refs),
            'amended': len(amended_notes) > 0,
        }
    
    # Third pass: build incoming refs (backlinks)
    incoming_refs = {}  # art_num → set of source articles
    for art, data in article_data.items():
        for ref in data['outgoing_refs']:
            if ref not in incoming_refs:
                incoming_refs[ref] = set()
            incoming_refs[ref].add(art)
    
    # Fourth pass: write article .md files
    written = 0
    for art, data in article_data.items():
        filename = article_filename(art)
        filepath = os.path.join(ARTICLES_DIR, filename)
        
        frontmatter = {
            'номер': art,
            'заголовок': data['title'],
        }
        if data['section_title']:
            frontmatter['раздел'] = data['section_title']
        if data['chapter_title']:
            frontmatter['глава'] = data['chapter_title']
        if data['status'] != 'действует':
            frontmatter['статус'] = data['status']
        if data['is_repealed']:
            frontmatter['утратила_силу'] = True
        
        write_article_md(
            filepath=filepath,
            frontmatter=frontmatter,
            title=data['title'],
            content=data['content'],
            outgoing_refs=data['outgoing_refs'],
            amended_notes=data['amended_notes'][:5],  # cap at 5 in frontmatter
        )
        written += 1
    
    print(f"✅ Written {written} article .md files to {ARTICLES_DIR}")
    
    # ─── Generate MOC by sections ─────────────────────────
    
    # Build section → chapters → articles hierarchy
    hierarchy = {}  # section_roman → {chapter_num → [article_nums]}
    
    for art, data in article_data.items():
        sr = data['section_roman']
        gn = data['glava_num']
        if sr and gn:
            if sr not in hierarchy:
                hierarchy[sr] = {}
            if gn not in hierarchy[sr]:
                hierarchy[sr][gn] = []
            hierarchy[sr][gn].append(art)
    
    # Sort within each level
    for sr in hierarchy:
        for gn in hierarchy[sr]:
            hierarchy[sr][gn].sort(key=lambda x: (float(x) if '.' in x else int(x)))
    
    # Write main УК РФ MOC
    moc_path = os.path.join(MOC_DIR, "00-MOC-УК-РФ.md")
    moc_lines = [
        "# Уголовный кодекс Российской Федерации",
        "",
        "> [!info] Навигация по УК РФ",
        "> 12 разделов · 34 главы · 361 статья",
        "",
        "## Структура кодекса",
        "",
    ]
    
    for roman in sorted(hierarchy.keys(), key=lambda r: roman_to_int(r)):
        full_title = section_full.get(roman, f"Раздел {roman}")
        moc_lines.append(f"### [[Раздел {roman}|{full_title}]]")
        moc_lines.append("")
        
        for gn in sorted(hierarchy[roman].keys()):
            ch_info = chapter_full.get(gn, (roman, f"Глава {gn}"))
            ch_full = ch_info[1] if isinstance(ch_info, tuple) else ch_info
            moc_lines.append(f"#### [[Глава {gn}|{ch_full}]]")
            moc_lines.append("")
            
            for art in hierarchy[roman][gn]:
                data = article_data.get(art, {})
                title = data.get('title', '')
                status_badge = " ⛔" if data.get('is_repealed') else ""
                moc_lines.append(f"- [[ст.{art}]] — {title}{status_badge}")
            moc_lines.append("")
    
    # Stats
    total = len(article_data)
    active = sum(1 for d in article_data.values() if not d['is_repealed'])
    repealed = sum(1 for d in article_data.values() if d['is_repealed'])
    total_refs = sum(len(d['outgoing_refs']) for d in article_data.values())
    
    moc_lines.extend([
        "---",
        "",
        "## Статистика",
        "",
        f"- Всего статей: **{total}**",
        f"- Действующих: **{active}**",
        f"- Утративших силу: **{repealed}**",
        f"- Перекрёстных ссылок: **{total_refs}**",
    ])
    
    with open(moc_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(moc_lines))
    print(f"✅ Written main MOC: {moc_path}")
    
    # Write section MOCs
    for roman in sorted(hierarchy.keys(), key=lambda r: roman_to_int(r)):
        section_title = f"Раздел {roman}"
        section_path = os.path.join(MOC_DIR, f"Раздел {roman}.md")
        full_title = section_full.get(roman, section_title)
        
        sec_lines = [f"# {full_title}", "", f"← [[00-MOC-УК-РФ|УК РФ]]", ""]
        
        for gn in sorted(hierarchy[roman].keys()):
            ch_info = chapter_full.get(gn, (roman, f"Глава {gn}"))
            ch_full = ch_info[1] if isinstance(ch_info, tuple) else ch_info
            sec_lines.append(f"## [[Глава {gn}|{ch_full}]]")
            sec_lines.append("")
            
            for art in hierarchy[roman][gn]:
                data = article_data.get(art, {})
                title = data.get('title', '')
                status_badge = " ⛔" if data.get('is_repealed') else ""
                sec_lines.append(f"- [[ст.{art}]] — {title}{status_badge}")
            sec_lines.append("")
        
        with open(section_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(sec_lines))
    
    print(f"✅ Written {len(hierarchy)} section MOCs")
    
    # Write chapter MOCs
    chapter_count = 0
    for roman in hierarchy:
        for gn in hierarchy[roman]:
            ch_info = chapter_full.get(gn, (roman, f"Глава {gn}"))
            ch_full = ch_info[1] if isinstance(ch_info, tuple) else ch_info
            ch_path = os.path.join(MOC_DIR, f"Глава {gn}.md")
            
            ch_lines = [
                f"# {ch_full}",
                "",
                f"← [[Раздел {roman}|Раздел {roman}]] · [[00-MOC-УК-РФ|УК РФ]]",
                "",
                "## Статьи",
                "",
            ]
            
            for art in hierarchy[roman][gn]:
                data = article_data.get(art, {})
                title = data.get('title', '')
                status_badge = " ⛔" if data.get('is_repealed') else ""
                ch_lines.append(f"- [[ст.{art}]] — {title}{status_badge}")
            
            with open(ch_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(ch_lines))
            chapter_count += 1
    
    print(f"✅ Written {chapter_count} chapter MOCs")
    
    # ─── Write summary stats ─────────────────────────────
    print("\n" + "="*60)
    print(f"CONVERSION COMPLETE")
    print(f"  Articles: {written}")
    print(f"  Section MOCs: {len(hierarchy)}")
    print(f"  Chapter MOCs: {chapter_count}")
    print(f"  Main MOC: 1")
    print(f"  Cross-refs found: {total_refs}")
    print(f"  Active articles: {active}")
    print(f"  Repealed articles: {repealed}")


if __name__ == "__main__":
    main()