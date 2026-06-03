#!/usr/bin/env python3
"""
V2: Convert russian-law-mcp SQLite → Obsidian vault .md files
Fixes: dotted article numbers, full titles from content, proper cross-refs,
       correct chapter/section mapping for all 534 articles.
"""

import sqlite3, json, re, os
from collections import Counter, defaultdict

DB_PATH = "/home/clawd/node_modules/@ansvar/russian-law-mcp/data/database.db"
VAULT = "/home/clawd/uk-rf-vault"
STRUCT_PATH = f"{VAULT}/03-Resources/УК-РФ/structure.json"
ARTICLES_DIR = f"{VAULT}/03-Resources/УК-РФ/Статьи"
MOC_DIR = f"{VAULT}/06-MOC"

# ─── Article number mapping (DB format → real format) ───

def db_to_real_article(db_num: str) -> str:
    """Convert DB article number to real dotted format.
    DB stores '1593' where real is '159.3', '1041' → '104.1' etc.
    """
    if not db_num.isdigit():
        return db_num
    
    num = int(db_num)
    if num <= 361:
        return db_num  # no change needed
    
    s = db_num
    # Try splitting from right: find longest valid base (1-361) + single-digit suffix
    for split_point in range(len(s) - 1, 0, -1):
        base = int(s[:split_point])
        suffix = int(s[split_point:])
        if 1 <= base <= 361 and 1 <= suffix <= 9:
            return f"{base}.{suffix}"
    
    return db_num  # fallback


# ─── Title extraction ───

def extract_full_title(content: str, db_title: str) -> str:
    """Extract full article title from content start.
    DB title is truncated to ~63 chars. Full title is in content before the first numbered paragraph.
    """
    if not db_title or db_title == '':
        return ''
    
    # If title doesn't end with comma, it's probably complete
    if not db_title.rstrip().endswith(','):
        return normalize_spaces(db_title)
    
    # Title continues in content: first lines before "1." or "(Наименование"
    parts = re.split(r'\n\n(?=1\.\s)', content, maxsplit=1)
    title_section = parts[0] if len(parts) > 1 else ''
    
    if not title_section:
        return normalize_spaces(db_title)
    
    # Remove amendment notes like "(Наименование в редакции..."
    title_section = re.sub(r'\n?\(Наименование.*?\)', '', title_section, flags=re.DOTALL)
    title_section = title_section.strip()
    
    if title_section:
        # Combine DB title start + continuation from content
        # Keep trailing comma (it belongs in the title!) and append continuation
        full_title = db_title.rstrip() + ' ' + title_section
        return normalize_spaces(full_title)
    
    return normalize_spaces(db_title)


def normalize_spaces(text: str) -> str:
    """Remove excessive internal spaces from law text."""
    text = re.sub(r'[^\S\n]{2,}', ' ', text)
    text = re.sub(r'\s+([,;:.!?)])', r'\1', text)
    return text.strip()


def roman_to_int(roman: str) -> int:
    vals = {'I':1,'V':5,'X':10,'L':50}
    result = 0
    for i, c in enumerate(roman):
        if i+1 < len(roman) and vals[c] < vals[roman[i+1]]:
            result -= vals[c]
        else:
            result += vals[c]
    return result


# ─── Cross-reference finder ───

def find_cross_refs(content: str, all_real_nums: set) -> set:
    """Find cross-references using Cyrillic patterns.
    Handles: ст.105, ст. 228.1, статьей 159.3, статьях 15, 16, статьи 104.1
    Returns set of real (dotted) article numbers.
    """
    refs = set()
    
    # Pattern 1: "ст.NN" or "ст. NN" or "ст.NN.N" (Cyrillic с)
    for m in re.finditer(r'\u0441\u0442\.?\s+(\d+(?:\.\d+)?)', content):
        num_str = m.group(1)
        # Convert from potential DB format: "1593" should be "159.3"
        real_num = db_to_real_article(num_str) if num_str.isdigit() and len(num_str) > 3 else num_str
        base = real_num.split('.')[0]
        if base.isdigit() and 1 <= int(base) <= 361 and real_num in all_real_nums:
            refs.add(real_num)
    
    # Pattern 2: "стать*" + numbers (Cyrillic)
    for m in re.finditer(r'\u0441\u0442\u0430\u0442[\u044c\u044f\u0439\u0435\u0438\u044e\u044e\u0443\u0432\u0445\u043c\u0430\u043b\u043d\u043e\u0434\u0436\u0437\u0440\u0441\u0442]+?\s+((?:\d+(?:\.\d+)?\s*(?:[,]\s*)?)+)', content):
        nums_part = m.group(1)
        for nm in re.finditer(r'(\d+(?:\.\d+)?)', nums_part):
            num_str = nm.group(1)
            real_num = db_to_real_article(num_str) if num_str.isdigit() and len(num_str) > 3 else num_str
            base = real_num.split('.')[0]
            if base.isdigit() and 1 <= int(base) <= 361 and real_num in all_real_nums:
                refs.add(real_num)
    
    # Pattern 3: also handle "ст. NNN" where NNN is DB format like "1593" for 159.3
    # This is for references INSIDE content that use the same DB format
    for m in re.finditer(r'\u0441\u0442\.?\s+(\d{3,4})', content):
        num_str = m.group(1)
        if num_str.isdigit() and int(num_str) > 361:
            real_num = db_to_real_article(num_str)
            base = real_num.split('.')[0]
            if base.isdigit() and 1 <= int(base) <= 361 and real_num in all_real_nums:
                refs.add(real_num)
    
    return refs


# ─── Build chapter/section mapping for ALL articles ───

def build_full_mapping(struct, db_provisions):
    """Map real article numbers → (section_roman, section_title, chapter_num, chapter_title)
    Handles dotted articles by placing them after their base article in the same chapter.
    """
    # First: map base articles (1-361) from structure.json
    article_to_chapter = {}  # num → (section_roman, glava_num)
    section_full = {}   # roman → full title
    chapter_full = {}   # (roman, glava) → full title
    
    for s in struct["sections"]:
        m = re.search(r'Razdel-([IVXL]+)', s["url"])
        if m:
            roman = m.group(1)
            section_full[roman] = s["title"]
    
    for c in struct["chapters"]:
        m = re.search(r'Razdel-([IVXL]+)/Glava-(\d+)', c["url"])
        if m:
            roman = m.group(1)
            glava = int(m.group(2))
            chapter_full[(roman, glava)] = c["title"]
    
    for a in struct["articles"]:
        m = re.search(r'Razdel-([IVXL]+)/Glava-(\d+)/Statya-(\d+)', a["url"])
        if m:
            roman = m.group(1)
            glava = int(m.group(2))
            art_num = int(m.group(3))
            article_to_chapter[art_num] = (roman, glava)
    
    # Now: assign dotted articles to same chapter as base
    full_mapping = {}
    for db_num, title, content in db_provisions:
        real_num = db_to_real_article(db_num)
        
        if '.' in real_num:
            base = int(real_num.split('.')[0])
        else:
            base = int(real_num) if real_num.isdigit() else None
        
        if base and base in article_to_chapter:
            roman, glava = article_to_chapter[base]
            full_mapping[real_num] = {
                'section_roman': roman,
                'section_title': section_full.get(roman, f'Раздел {roman}'),
                'chapter_num': glava,
                'chapter_title': chapter_full.get((roman, glava), f'Глава {glava}'),
            }
        else:
            full_mapping[real_num] = {
                'section_roman': None,
                'section_title': None,
                'chapter_num': None,
                'chapter_title': None,
            }
    
    return full_mapping, section_full, chapter_full


# ─── Write article .md ───

def write_article(filepath, real_num, full_title, content, mapping, outgoing_refs, is_repealed, amended_notes):
    """Write a single article .md file."""
    content = normalize_spaces(content)
    
    # YAML frontmatter
    yf = ["---"]
    yf.append(f'номер: "{real_num}"')
    # Ensure title is single-line for valid YAML
    safe_title = full_title.replace('\n', ' ').replace('"', "'")
    yf.append(f'заголовок: "{safe_title}"')
    if mapping.get('section_title'):
        yf.append(f'раздел: "Раздел {mapping["section_roman"]}"')
    if mapping.get('chapter_num'):
        yf.append(f'глава: "Глава {mapping["chapter_num"]}"')
    if is_repealed:
        yf.append('статус: "утратила силу"')
    if amended_notes:
        yf.append('редакции:')
        for note in amended_notes:
            yf.append(f'  - "{note}"')
    if outgoing_refs:
        yf.append('ссылки:')
        for ref in sorted(outgoing_refs, key=lambda x: (float(x) if '.' in x else int(x))):
            yf.append(f'  - "ст.{ref}"')
    yf.append("---")
    
    # Body
    # Title: ensure single-line
    safe_h1_title = full_title.replace('\n', ' ')
    body = [f"# Статья {real_num}. {safe_h1_title}", ""]
    
    # Context line
    ctx = []
    if mapping.get('section_roman'):
        ctx.append(f"Раздел: [[Раздел {mapping['section_roman']}]]")
    if mapping.get('chapter_num'):
        ctx.append(f"Глава: [[Глава {mapping['chapter_num']}]]")
    if ctx:
        body.append("> " + " | ".join(ctx))
        body.append("")
    
    # Main content
    body.append(content)
    body.append("")
    
    # Cross-refs
    if outgoing_refs:
        body.append("## Ссылки на другие статьи")
        body.append("")
        for ref in sorted(outgoing_refs, key=lambda x: (float(x) if '.' in x else int(x))):
            body.append(f"- [[ст.{ref}]]")
        body.append("")
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("\n".join(yf) + "\n" + "\n".join(body))


# ─── Main ───

def main():
    os.makedirs(ARTICLES_DIR, exist_ok=True)
    os.makedirs(MOC_DIR, exist_ok=True)
    
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()
    
    with open(STRUCT_PATH) as f:
        struct = json.load(f)
    
    # Get all provisions
    cur.execute("SELECT article, title, content FROM provisions WHERE law_id='uk-rf' ORDER BY order_index")
    db_provisions = cur.fetchall()
    db.close()
    
    print(f"DB provisions: {len(db_provisions)}")
    
    # Step 1: Convert all DB nums to real nums
    all_real_nums = set()
    db_to_real_map = {}
    for db_num, title, content in db_provisions:
        real = db_to_real_article(db_num)
        db_to_real_map[db_num] = real
        all_real_nums.add(real)
    
    print(f"Real article numbers: {len(all_real_nums)}")
    
    # Step 2: Build chapter/section mapping
    full_mapping, section_full, chapter_full = build_full_mapping(struct, db_provisions)
    
    # Count articles with no mapping
    no_map = sum(1 for v in full_mapping.values() if not v['section_roman'])
    print(f"Articles without section/chapter mapping: {no_map}")
    
    # Step 3: Extract full titles and find cross-refs
    article_data = {}
    for db_num, db_title, content in db_provisions:
        real_num = db_to_real_map[db_num]
        mapping = full_mapping[real_num]
        
        # Extract full title
        full_title = extract_full_title(content, db_title)
        
        # Detect repealed FIRST (need for title fallback)
        status = "действует"
        is_repealed = False
        if re.search(r'утратил[аи]?\s+силу', content, re.IGNORECASE):
            if len(content) < 300:  # short = fully repealed
                status = "утратила силу"
                is_repealed = True
        
        # If still empty, use contextual fallback
        if not full_title:
            if is_repealed:
                full_title = "Утратила силу"
            else:
                full_title = f"Статья {real_num}"
        
        # Amendments (max 1 note, truncated to 100 chars)
        amended_notes = re.findall(r'\((?:В редакции|Дополнение)[^)]*\)', content)
        # Keep only first note and truncate
        if amended_notes:
            first_note = amended_notes[0].replace('\n', ' ')
            if len(first_note) > 100:
                first_note = first_note[:97] + '...)'
            amended_notes = [first_note]
        else:
            amended_notes = []
        
        # Cross-refs
        refs = find_cross_refs(content, all_real_nums)
        refs.discard(real_num)  # no self-references
        
        article_data[real_num] = {
            'real_num': real_num,
            'db_num': db_num,
            'full_title': full_title,
            'content': content,
            'mapping': mapping,
            'is_repealed': is_repealed,
            'amended_notes': amended_notes,
            'outgoing_refs': refs,
        }
    
    # Step 4: Write all article .md files
    written = 0
    for real_num, data in sorted(article_data.items(), key=lambda x: (float(x[0].replace('.', '.')) if '.' in x[0] else int(x[0]))):
        filename = f"ст.{real_num}.md"
        filepath = os.path.join(ARTICLES_DIR, filename)
        
        write_article(
            filepath=filepath,
            real_num=real_num,
            full_title=data['full_title'],
            content=data['content'],
            mapping=data['mapping'],
            outgoing_refs=data['outgoing_refs'],
            is_repealed=data['is_repealed'],
            amended_notes=data['amended_notes'],
        )
        written += 1
    
    print(f"✅ Written {written} article .md files")
    
    # Step 5: Generate MOCs
    # Build hierarchy: section → chapter → articles
    hierarchy = defaultdict(lambda: defaultdict(list))
    
    for real_num, data in article_data.items():
        m = data['mapping']
        sr = m.get('section_roman')
        gn = m.get('chapter_num')
        if sr and gn:
            hierarchy[sr][gn].append(real_num)
    
    # Sort articles within chapters
    def sort_key(art_num):
        parts = art_num.split('.')
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    
    for sr in hierarchy:
        for gn in hierarchy[sr]:
            hierarchy[sr][gn].sort(key=sort_key)
    
    # Main MOC
    moc_path = os.path.join(MOC_DIR, "00-MOC-УК-РФ.md")
    lines = [
        "# Уголовный кодекс Российской Федерации",
        "",
        "> [!info] Навигация по УК РФ",
        f"> 12 разделов · 34 главы · {len(article_data)} статей",
        "",
        "## Структура кодекса",
        "",
    ]
    
    for roman in sorted(hierarchy.keys(), key=lambda r: roman_to_int(r)):
        full_title = section_full.get(roman, f"Раздел {roman}")
        lines.append(f"### [[Раздел {roman}|{full_title}]]")
        lines.append("")
        
        for gn in sorted(hierarchy[roman].keys()):
            ch_full = chapter_full.get((roman, gn), f"Глава {gn}")
            lines.append(f"#### [[Глава {gn}|{ch_full}]]")
            lines.append("")
            
            for art in hierarchy[roman][gn]:
                data = article_data[art]
                badge = " ⛔" if data['is_repealed'] else ""
                short_title = data['full_title'].replace('\n', ' ')[:80]
                lines.append(f"- [[ст.{art}]] — {short_title}{badge}")
            lines.append("")
    
    # Stats
    total = len(article_data)
    active = sum(1 for d in article_data.values() if not d['is_repealed'])
    repealed = sum(1 for d in article_data.values() if d['is_repealed'])
    total_refs = sum(len(d['outgoing_refs']) for d in article_data.values())
    
    lines.extend([
        "---", "",
        "## Статистика", "",
        f"- Всего статей: **{total}**",
        f"- Действующих: **{active}**",
        f"- Утративших силу: **{repealed}**",
        f"- Перекрёстных ссылок: **{total_refs}**",
    ])
    
    with open(moc_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"✅ Written main MOC")
    
    # Section MOCs
    for roman in sorted(hierarchy.keys(), key=lambda r: roman_to_int(r)):
        sec_path = os.path.join(MOC_DIR, f"Раздел {roman}.md")
        full_title = section_full.get(roman, f"Раздел {roman}")
        sec_lines = [f"# {full_title}", "", f"← [[00-MOC-УК-РФ|УК РФ]]", ""]
        
        for gn in sorted(hierarchy[roman].keys()):
            ch_full = chapter_full.get((roman, gn), f"Глава {gn}")
            sec_lines.append(f"## [[Глава {gn}|{ch_full}]]")
            sec_lines.append("")
            for art in hierarchy[roman][gn]:
                data = article_data[art]
                badge = " ⛔" if data['is_repealed'] else ""
                short_title = data['full_title'].replace('\n', ' ')[:80]
                sec_lines.append(f"- [[ст.{art}]] — {short_title}{badge}")
            sec_lines.append("")
        
        with open(sec_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(sec_lines))
    
    print(f"✅ Written {len(hierarchy)} section MOCs")
    
    # Chapter MOCs
    ch_count = 0
    for roman in hierarchy:
        for gn in hierarchy[roman]:
            ch_full = chapter_full.get((roman, gn), f"Глава {gn}")
            ch_path = os.path.join(MOC_DIR, f"Глава {gn}.md")
            ch_lines = [f"# {ch_full}", "", f"← [[Раздел {roman}|Раздел {roman}]] · [[00-MOC-УК-РФ|УК РФ]]", "", "## Статьи", ""]
            
            for art in hierarchy[roman][gn]:
                data = article_data[art]
                badge = " ⛔" if data['is_repealed'] else ""
                short_title = data['full_title'].replace('\n', ' ')[:80]
                ch_lines.append(f"- [[ст.{art}]] — {short_title}{badge}")
            
            with open(ch_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(ch_lines))
            ch_count += 1
    
    print(f"✅ Written {ch_count} chapter MOCs")
    
    print(f"\n{'='*60}")
    print(f"CONVERSION V2 COMPLETE")
    print(f"  Articles: {written}")
    print(f"  Section MOCs: {len(hierarchy)}")
    print(f"  Chapter MOCs: {ch_count}")
    print(f"  Cross-refs: {total_refs}")
    print(f"  Active: {active}, Repealed: {repealed}")


if __name__ == "__main__":
    main()