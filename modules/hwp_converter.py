import io
import os
import re
import sys
from pathlib import Path
import pandas as pd
import streamlit as st

import hwp5.hwp5txt
from modules.config import (
    DATA_DIR,
    ARTICLE_ID_PATTERN,
    HO_PATTERN,
    HANG_PATTERN,
    MOK_PATTERN,
    CHAPTER_PATTERN,
    SECTION_PATTERN,
)

def convert_hwp_to_txt_st():
    """Streamlit 환경에서 실행하기 위한 HWP -> TXT 파싱 로직"""
    target_dir = Path(DATA_DIR)
    
    if not target_dir.is_dir():
        return -1, 0, 0, f"'{DATA_DIR}' 폴더가 없습니다."

    hwp_files = list(target_dir.glob("*.hwp"))
    if not hwp_files:
        return 0, 0, 0, f"'{DATA_DIR}' 폴더 내에 .hwp 파일이 없습니다."

    converted = 0
    skipped = 0
    errors = 0
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for idx, hwp_path in enumerate(hwp_files):
        txt_path = hwp_path.with_suffix(".txt")
        
        if txt_path.exists():
            skipped += 1
        else:
            status_text.text(f"처리 중: {hwp_path.name}")
            
            # 파이썬 내부 모듈(hwp5txt)을 직접 호출
            original_argv = sys.argv
            sys.argv = ['hwp5txt', '--output', str(txt_path), str(hwp_path)]
            
            try:
                hwp5.hwp5txt.main()
                converted += 1
            except SystemExit as e:
                if e.code == 0 or e.code is None:
                    converted += 1
                else:
                    st.error(f"'{hwp_path.name}' 변환 실패 (에러 코드: {e.code})")
                    errors += 1
            except Exception as e:
                st.error(f"'{hwp_path.name}' 처리 중 알 수 없는 오류: {e}")
                errors += 1
            finally:
                sys.argv = original_argv
                
        progress_bar.progress((idx + 1) / len(hwp_files))
        
    status_text.empty()
    progress_bar.empty()
    return converted, skipped, errors, "완료"

def convert_single_hwp_to_txt(hwp_path: Path):
    """단일 HWP 파일을 TXT로 변환. pyhwp TextTransform 모듈을 사용하여 메모리 스트림 기반으로 안전하게 추출."""
    txt_path = hwp_path.with_suffix(".txt")
    try:
        from contextlib import closing
        from hwp5.hwp5txt import TextTransform, Hwp5File

        tt = TextTransform()
        with closing(Hwp5File(str(hwp_path))) as hwp5file:
            buf = io.BytesIO()
            tt.transform_hwp5_to_text(hwp5file, buf)
            text_content = buf.getvalue().decode('utf-8', errors='ignore')

        with open(txt_path, "w", encoding="utf-8") as dest:
            dest.write(text_content)
        return txt_path, None
    except Exception as e:
        return None, f"HWP 파싱 실패: {e}"

def read_source_text(filename: str) -> str:
    path = Path(filename)
    if not path.exists():
        raise FileNotFoundError(f'"{filename}" 파일을 찾을 수 없습니다.')

    encodings_to_try = ["utf-8", "cp949", "euc-kr"]
    text = None
    for enc in encodings_to_try:
        try:
            with path.open("r", encoding=enc) as f:
                text = f.read()
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        with path.open("rb") as f:
            text = f.read().decode("utf-8", errors="ignore")
    return text

def clean_text(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\t", " ")
    return re.sub(r"\s+", " ", s).strip()

def parse_moks(base_ref: str, article_id: str, title: str, hang: str, ho: str, ho_text: str):
    rows = []
    mok_matches = list(MOK_PATTERN.finditer(ho_text))
    if not mok_matches:
        return rows

    for i, m in enumerate(mok_matches):
        mok_char = m.group(2)
        start = m.start(2)
        end = mok_matches[i + 1].start(2) if i + 1 < len(mok_matches) else len(ho_text)
        mok_text = ho_text[start:end].strip()
        base_ref_mok = f"{base_ref}{mok_char}목"

        rows.append({
            "참조번호": base_ref_mok, "조": article_id, "조명": title,
            "항": hang, "호": ho, "목": mok_char, "내용": mok_text
        })
    return rows

def format_ho_ref(ho_num: str) -> str:
    if "의" in ho_num:
        main, sub = ho_num.split("의", 1)
        return f"제{main}호의{sub}"
    return f"제{ho_num}호"

def parse_h_block(article_id: str, title: str, h_char: str, block_raw: str):
    rows = []
    ho_matches = list(HO_PATTERN.finditer(block_raw))

    if not ho_matches:
        content = block_raw.strip()
        rows.append({
            "참조번호": f"{article_id}제{h_char}항", "조": article_id, "조명": title,
            "항": h_char, "호": "0", "목": "0", "내용": content
        })
    else:
        hang_main = block_raw[: ho_matches[0].start(0)].strip()
        if hang_main:
            rows.append({
                "참조번호": f"{article_id}제{h_char}항", "조": article_id, "조명": title,
                "항": h_char, "호": "0", "목": "0", "내용": hang_main
            })

    for i, match in enumerate(ho_matches):
        start = match.start(2)
        end = ho_matches[i + 1].start(0) if i + 1 < len(ho_matches) else len(block_raw)
        ho_text = block_raw[start:end].strip()

        m2 = re.match(r"(\d+(?:의\d+)*)\.\s*(.*)", ho_text, flags=re.S)
        if m2:
            ho_num = m2.group(1)
            remainder = m2.group(2)
        else:
            ho_num = match.group(2)
            remainder = ho_text

        mok_matches = list(MOK_PATTERN.finditer(remainder))
        if mok_matches:
            ho_main = remainder[: mok_matches[0].start(0)].strip()
        else:
            ho_main = remainder.strip()

        base_ref = f"{article_id}제{h_char}항{format_ho_ref(ho_num)}"
        rows.append({
            "참조번호": base_ref, "조": article_id, "조명": title,
            "항": h_char, "호": ho_num, "목": "0", "내용": ho_main
        })
        rows.extend(parse_moks(base_ref, article_id, title, h_char, ho_num, remainder))
    return rows

def parse_article_with_hang(article_id: str, title: str, body_text: str):
    rows = []
    hang_matches = list(HANG_PATTERN.finditer(body_text))
    for i, hm in enumerate(hang_matches):
        h_char = hm.group(2)
        start = hm.start(2)
        end = hang_matches[i + 1].start(2) if i + 1 < len(hang_matches) else len(body_text)
        block_raw = body_text[start:end].strip()
        rows.extend(parse_h_block(article_id, title, h_char, block_raw))
    return rows

def parse_article_no_hang(article_id: str, title: str, body_text: str):
    rows = []
    ho_matches = list(HO_PATTERN.finditer(body_text))

    if not ho_matches:
        content = body_text.strip()
        rows.append({
            "참조번호": article_id, "조": article_id, "조명": title,
            "항": "0", "호": "0", "목": "0", "내용": content
        })
        return rows

    base_text = body_text[: ho_matches[0].start(0)].strip()
    if base_text:
        rows.append({
            "참조번호": article_id, "조": article_id, "조명": title,
            "항": "0", "호": "0", "목": "0", "내용": base_text
        })

    for i, match in enumerate(ho_matches):
        start = match.start(2)
        end = ho_matches[i + 1].start(0) if i + 1 < len(ho_matches) else len(body_text)
        ho_text = body_text[start:end].strip()

        m2 = re.match(r"(\d+(?:의\d+)*)\.\s*(.*)", ho_text, flags=re.S)
        if m2:
            ho_num = m2.group(1)
            remainder = m2.group(2)
        else:
            ho_num = match.group(2)
            remainder = ho_text

        mok_matches = list(MOK_PATTERN.finditer(remainder))
        if mok_matches:
            ho_main = remainder[: mok_matches[0].start(0)].strip()
        else:
            ho_main = remainder.strip()

        base_ref = f"{article_id}{format_ho_ref(ho_num)}"
        rows.append({
            "참조번호": base_ref, "조": article_id, "조명": title,
            "항": "0", "호": ho_num, "목": "0", "내용": ho_main
        })
        rows.extend(parse_moks(base_ref, article_id, title, "0", ho_num, remainder))
    return rows

def parse_article(article_text: str):
    rows = []
    lines_local = article_text.splitlines()
    if not lines_local:
        return rows

    header_line = lines_local[0]
    m = ARTICLE_ID_PATTERN.match(header_line)
    if not m:
        return rows
    article_id = m.group(1)

    after = header_line[m.end() :]
    after_strip = after.lstrip()

    if after_strip.startswith("삭제"):
        rows.append({
            "참조번호": article_id, "조": article_id, "조명": "삭제",
            "항": "0", "호": "0", "목": "0", "내용": article_text.strip()
        })
        return rows

    idx_lp = header_line.find("(", len(article_id))
    idx_rp = header_line.find(")", idx_lp + 1) if idx_lp != -1 else -1
    if idx_lp == -1 or idx_rp == -1 or idx_rp < idx_lp:
        title = ""
        first_body_part = header_line[m.end() :]
    else:
        title = header_line[idx_lp + 1 : idx_rp]
        first_body_part = header_line[idx_rp + 1 :]

    body_lines_local = []
    if first_body_part is not None:
        body_lines_local.append(first_body_part.strip())
    if len(lines_local) > 1:
        body_lines_local.extend(lines_local[1:])
    body_text = "\n".join(body_lines_local).strip()

    if not body_text:
        rows.append({
            "참조번호": article_id, "조": article_id, "조명": title,
            "항": "0", "호": "0", "목": "0", "내용": ""
        })
        return rows

    if re.search(r"[①-⑳]", body_text):
        rows.extend(parse_article_with_hang(article_id, title, body_text))
    else:
        rows.extend(parse_article_no_hang(article_id, title, body_text))
    return rows

def parse_all(text: str):
    lines = text.splitlines()
    current_chapter_no = ""
    current_chapter_title = ""
    current_section_no = ""
    current_section_title = ""
    article_meta = []

    for idx, line in enumerate(lines):
        s = line.strip()
        m_ch = CHAPTER_PATTERN.match(s)
        if m_ch:
            current_chapter_no = m_ch.group(1)
            current_chapter_title = m_ch.group(2).strip()
            continue
        m_se = SECTION_PATTERN.match(s)
        if m_se:
            current_section_no = m_se.group(1)
            current_section_title = m_se.group(2).strip()
            continue
        if re.match(r"^제\d+조", s):
            article_meta.append((idx, current_chapter_no, current_chapter_title, current_section_no, current_section_title))

    article_texts = []
    for i, meta in enumerate(article_meta):
        start = meta[0]
        end = article_meta[i + 1][0] if i + 1 < len(article_meta) else len(lines)
        seg_lines = []
        for j in range(start, end):
            line = lines[j]
            t = line.strip()
            if t == "" or t == "조항 인쇄" or CHAPTER_PATTERN.match(t) or SECTION_PATTERN.match(t):
                continue
            seg_lines.append(line)
        article_texts.append((meta, "\n".join(seg_lines)))

    all_rows = []
    for meta, art_text in article_texts:
        _, ch_no, ch_title, se_no, se_title = meta
        rows = parse_article(art_text)
        for r in rows:
            r["장번호"] = ch_no
            r["장명"] = ch_title
            r["절번호"] = se_no
            r["절명"] = se_title
        all_rows.extend(r for r in rows)

    rows_clean = []
    for r in all_rows:
        hang = str(r.get("항", "0"))
        ho = str(r.get("호", "0"))
        mok = str(r.get("목", "0"))

        if mok != "0":
            level = "목"
        elif ho != "0":
            level = "호"
        elif hang != "0":
            level = "항"
        else:
            level = "조"

        rows_clean.append({
            "구분": level,
            "장번호": clean_text(r.get("장번호", "")),
            "장명": clean_text(r.get("장명", "")),
            "절번호": clean_text(r.get("절번호", "")),
            "절명": clean_text(r.get("절명", "")),
            "참조번호": clean_text(r.get("참조번호", "")),
            "조명": clean_text(r.get("조명", "")),
            "조": clean_text(r.get("조", "")),
            "항": hang, "호": ho, "목": mok,
            "내용": clean_text(r.get("내용", "")),
        })

    return pd.DataFrame(rows_clean, columns=["구분", "장번호", "장명", "절번호", "절명", "참조번호", "조명", "조", "항", "호", "목", "내용"])

def convert_txt_files_to_csv():
    """Streamlit 환경에서 실행하기 위한 파싱 로직 래핑 함수"""
    target_dir = Path(DATA_DIR)
    
    if not target_dir.is_dir():
        return -1, 0, 0, f"'{DATA_DIR}' 폴더가 없습니다."

    txt_files = list(target_dir.glob("*.txt"))
    if not txt_files:
        return 0, 0, 0, f"'{DATA_DIR}' 폴더 내에 .txt 파일이 없습니다."

    converted = 0
    skipped = 0
    errors = 0
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for idx, txt_path in enumerate(txt_files):
        output_csv_path = txt_path.with_suffix(".csv")
        
        if output_csv_path.exists():
            skipped += 1
        else:
            try:
                status_text.text(f"처리 중: {txt_path.name}")
                text = read_source_text(str(txt_path))
                df = parse_all(text)
                df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")
                converted += 1
            except Exception as e:
                st.error(f"'{txt_path.name}' 처리 중 오류: {e}")
                errors += 1
        
        progress_bar.progress((idx + 1) / len(txt_files))
        
    status_text.empty()
    progress_bar.empty()
    return converted, skipped, errors, "완료"
