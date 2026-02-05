# AI Coding Agent Instructions for Korean Regulations Parser

## Project Overview

This codebase parses Korean stock exchange regulations (증권거래법 세칙) from text into structured CSV formats and generates comparison reports. Two main scripts work in sequence:

1. **Parser** (`세칙_파싱_v7-수정내용 진하게, 밑줄.py`): Extracts regulations from text file into CSV
2. **Comparator** (`세칙_비교_old_new_v4_docx.py`): Compares old vs new regulation versions, generates Word reports

## Korean Legal Document Structure

Regulations follow a strict hierarchical numbering system (this is critical for parsing):

- **장(Chapter)**: e.g., "제1장(목적)"  
- **절(Section)**: e.g., "제1절(정의)"  
- **조(Article)**: e.g., "제5조", "제30조의2" (note: supports "의N" suffixes like "의4")  
- **항(Paragraph)**: Marked with circled numbers ①, ②, ③ (not always present)  
- **호(Subsection)**: Numbered as 1., 2., 14의2. (supports "의N" format)  
- **목(Sub-subsection)**: Letters 가., 나., 다. (leaf level, includes in content field)

Articles may have special annotations like "[전문개정 2021. 3. 12.]" attached to title or body - these remain inline in the content field, never as separate rows.

## Critical Architecture Patterns

### CSV v4 Format Specification

Both parser and comparator use a strict 12-column format (in `BASE_COLS`):

```
["구분", "장번호", "장명", "절번호", "절명", "참조번호", "조명", "조", "항", "호", "목", "내용"]
```

The **참조번호** (reference number) field serves as the normalized location key, e.g.:
- "제5조" (article with no paragraphs)
- "제5조제①항" (article with paragraph ①)
- "제5조제①항제1호" (article with paragraph and subsection)
- "제5조제①항제1호가목" (with sub-subsection)

### Parsing Rule: Content vs Structure

- **Structure fields** (장/절/조/항/호/목) capture location only
- **내용 (content)** includes everything from that level down to (but not including) the next structural level
- **목(sub-subsection)** is never a separate structure field - only appears in content (파싱 결과에 "목" 컬럼은 구조, "내용"에는 목 텍스트가 포함)
- Deleted articles follow the format: `[조명]="삭제"`, `[항]=[호]='0'`, full deletion statement in content

### Comparison Logic (Word Report Generation)

The comparator uses 5-state classification:

1. **UNCHANGED**: Same position (장/절/조/항/호) + identical content
2. **MODIFIED_SAME_POSITION**: Same position, different content (similarity < 1.0)
3. **MOVED_OR_RENUMBERED**: Different position, content similarity ≥ threshold (default 0.8)
4. **DELETED**: Only in old CSV
5. **ADDED**: Only in new CSV

Matching algorithm (3-phase):
- Phase 1: Exact position match via 장/절/조/항/호 key
- Phase 2: Content similarity matching for unmatched positions (prefers same 조 candidates)
- Phase 3: Leftover rows → DELETED/ADDED classification

## Key Dependencies

- **python-docx**: Required for `.docx` generation (error check on import)
- **pandas**: DataFrame manipulation for comparison
- **openpyxl**: Excel workbook generation with rich formatting
- **difflib.SequenceMatcher**: Content similarity measurement (not fuzzy matching)

Install missing dependencies before running:
```
pip install python-docx openpyxl pandas
```

## Input/Output File Conventions

### Parser Input
- Source file: `유가증권시장 업무규정 시행세칙_YYYY.MM.DD.txt` (configurable path in code)
- Tries encodings in order: UTF-8 → CP949 → EUC-KR (with fallback to ignore errors)

### Comparator Input/Output
**Input** (same directory):
- `old.csv`: Previous regulation version
- `new.csv`: Current regulation version

**Output** (current working directory):
- `diff_report_old_new.docx`: Formatted Word report (primary deliverable)
- `diff_old_new.csv`: Detailed row-level comparison (optional debugging)
- `diff_summary_overall.csv`: Summary by status
- `diff_summary_by_chapter.csv`: Summary by chapter + status

## Command-Line Interface

### Parser
```bash
python 세칙_파싱_v7-수정내용\ 진하게,\ 밑줄.py
# Output: CSV files in current directory
```

### Comparator
```bash
# Run with default similarity threshold (0.8)
python 세칙_비교_old_new_v4_docx.py

# Run with custom threshold (e.g., 0.9 = stricter matching)
python 세칙_비교_old_new_v4_docx.py 0.9
```

## Normalization & Similarity Measurement

Text comparison uses:
- `normalize_text()`: Removes extra whitespace/tabs, collapses to single space
- `similarity()`: SequenceMatcher ratio on normalized text
- Threshold behavior: 1.0 = identical, ≥0.8 = MOVED_OR_RENUMBERED candidate

This means whitespace differences are ignored in similarity matching.

## Important Edge Cases

1. **Articles without paragraphs**: No ① markers → single row with 항='0', 호='0'
2. **Articles with only paragraphs (no subsections)**: First save paragraph row (호='0'), no additional rows
3. **Deleted clauses**: Full text preserved in content, 조명='삭제'
4. **Amendment annotations**: Remain inline with content, not parsed as metadata
5. **Complex subsection numbers**: "14의2", "14의2의3" supported via `\d+(?:의\d+)*` regex

## Regular Expression Patterns

Reference in [세칙_파싱_v7](세칙_파싱_v7-수정내용%20진하게,%20밑줄.py#L27-L35):
- Article: `^(제\d+조(?:의\d+)?)`
- Paragraph: `\s*([①-⑳])`
- Subsection: `(\d+(?:의\d+)*)\.\s*`
- Sub-subsection: `([가-하])\.\s*`
- Chapter/Section: `^제(\d+)장/절\s*(.+)`

## Common Development Workflows

### Debugging Parsing Issues
1. Check encodings tried in `read_source_text()` order
2. Use `clean_text()` to verify normalization
3. Print regex match positions to verify extraction boundaries

### Tuning Comparison
1. Adjust similarity threshold: `python 세칙_비교_old_new_v4_docx.py 0.75` (more aggressive matching)
2. Check `diff_old_new.csv` for false positives (manually review boundary matches)
3. Verify chapter-based grouping in `diff_summary_by_chapter.csv`

### Word Report Customization
Word output uses basic formatting in [세칙_비교_old_new_v4_docx.py](세칙_비교_old_new_v4_docx.py#L250+) - modify `add_section_to_doc()` for style changes.

---

**Last updated**: 2026-02-04 | **Language**: Python 3.7+ | **Target audience**: Korean regulatory document processors
