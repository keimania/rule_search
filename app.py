import streamlit as st
import sqlite3
import pandas as pd
import glob
import os
import re
import io
from pathlib import Path
import unicodedata

# =========================================================
# 1. 설정 및 상수 정의
# =========================================================
DB_FILE = "regulation_master.db"
DATA_DIR = "규정"

MENU_NAMES = {
    "1": "1. 규정 목록 확인",
    "2": "2. 개정 일자 확인",
    "3": "3. 규정 전체 조회",
    "4": "4. 조항 히스토리 추적",
    "5": "5. 조항 상세 조회",
    "6": "6. 통합 키워드 검색",
    "7": "7. 조항 인용(역참조) 검색"
}

PREFERRED_REG_NAME = "유가증권시장 업무규정"
DEFAULT_ART_NO = "제20조의2"

# ----------------------------------------------------------------------
# [추가됨] TXT 파싱용 정규표현식 상수
# ----------------------------------------------------------------------
ARTICLE_ID_PATTERN = re.compile(r"^(제\d+조(?:의\d+)?)")
HO_PATTERN = re.compile(r"(^|\n)\s*(\d+(?:의\d+)*)\.\s*", re.MULTILINE)
HANG_PATTERN = re.compile(r"(^|\n)\s*([①-⑳])", re.MULTILINE)
MOK_PATTERN = re.compile(r"(^|\n)\s*([가-하])\.\s*", re.MULTILINE)
CHAPTER_PATTERN = re.compile(r"^제(\d+)장\s*(.+)")
SECTION_PATTERN = re.compile(r"^제(\d+)절\s*(.+)")


# =========================================================
# 2. TXT -> CSV 파싱 관련 함수 통합 (규정_txt_to_csv.py 내용)
# =========================================================
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
    if s is None: return ""
    s = s.replace("\t", " ")
    return re.sub(r"\s+", " ", s).strip()

def parse_moks(base_ref: str, article_id: str, title: str, hang: str, ho: str, ho_text: str):
    rows = []
    mok_matches = list(MOK_PATTERN.finditer(ho_text))
    if not mok_matches: return rows

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
        if mok_matches: ho_main = remainder[: mok_matches[0].start(0)].strip()
        else: ho_main = remainder.strip()

        base_ref = f"{article_id}제{ho_num}호"
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
        if mok_matches: ho_main = remainder[: mok_matches[0].start(0)].strip()
        else: ho_main = remainder.strip()

        base_ref = f"{article_id}제{ho_num}호"
        rows.append({
            "참조번호": base_ref, "조": article_id, "조명": title,
            "항": "0", "호": ho_num, "목": "0", "내용": ho_main
        })
        rows.extend(parse_moks(base_ref, article_id, title, "0", ho_num, remainder))
    return rows

def parse_article(article_text: str):
    rows = []
    lines_local = article_text.splitlines()
    if not lines_local: return rows

    header_line = lines_local[0]
    m = ARTICLE_ID_PATTERN.match(header_line)
    if not m: return rows
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
    if first_body_part is not None: body_lines_local.append(first_body_part.strip())
    if len(lines_local) > 1: body_lines_local.extend(lines_local[1:])
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

        if mok != "0": level = "목"
        elif ho != "0": level = "호"
        elif hang != "0": level = "항"
        else: level = "조"

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
    
    # UI 프로그레스 바를 위한 Generator 방식 또는 Streamlit 컨테이너 업데이트
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
        
        # 진행 상태 업데이트
        progress_bar.progress((idx + 1) / len(txt_files))
        
    status_text.empty()
    progress_bar.empty()
    return converted, skipped, errors, "완료"


# =========================================================
# 3. DB 핸들링 및 최적화 함수
# =========================================================
def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS regulation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regulation_name TEXT,
            reg_date TEXT,
            unique_key TEXT,
            ref_no TEXT,
            article_title TEXT,
            content TEXT,
            UNIQUE(regulation_name, reg_date, unique_key)
        )
    ''')
    
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_reg_name ON regulation_history(regulation_name);",
        "CREATE INDEX IF NOT EXISTS idx_reg_date ON regulation_history(reg_date);",
        "CREATE INDEX IF NOT EXISTS idx_ref_no ON regulation_history(ref_no);",
        "CREATE INDEX IF NOT EXISTS idx_name_date ON regulation_history(regulation_name, reg_date);"
    ]
    for idx_sql in indexes: cursor.execute(idx_sql)
    conn.commit()
    conn.close()

@st.cache_data(ttl=3600) 
def get_regulation_names():
    if not os.path.exists(DB_FILE): return []
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT DISTINCT regulation_name FROM regulation_history ORDER BY regulation_name", conn)
        return df['regulation_name'].tolist()
    except: return []
    finally: conn.close()

@st.cache_data(ttl=3600)
def get_regulation_dates(reg_name):
    conn = get_connection()
    try:
        dates = pd.read_sql("SELECT DISTINCT reg_date FROM regulation_history WHERE regulation_name=? ORDER BY reg_date DESC", conn, params=(reg_name,))
        return dates['reg_date'].tolist()
    finally: conn.close()

def parse_filename_info(filename):
    base_name = os.path.basename(filename)
    name_without_ext = os.path.splitext(base_name)[0]
    
    # Mac(NFD)과 Windows(NFC)의 한글 인코딩 차이를 NFC(결합형)로 통일
    name_without_ext = unicodedata.normalize('NFC', name_without_ext)
    
    date_match = re.search(r'(\d{8})', name_without_ext)
    reg_date = date_match.group(1) if date_match else None
    
    if '_전문_' in name_without_ext:
        reg_name = name_without_ext.split('_전문_')[0]
    elif reg_date:
        reg_name = name_without_ext.replace(reg_date, '').strip('_')
    else:
        reg_name = name_without_ext
    return reg_name, reg_date

def generate_key(row):
    return f"{row['장번호']}_{row['조']}_{row['항']}_{row['호']}_{row['목']}"

def load_files():
    init_db()
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
        return -1, 0

    conn = get_connection()
    cursor = conn.cursor()
    
    existing = set()
    try:
        cursor.execute("SELECT DISTINCT regulation_name, reg_date FROM regulation_history")
        for row in cursor.fetchall(): existing.add((row[0], row[1]))
    except: pass

    files = glob.glob(os.path.join(DATA_DIR, "*.csv"))
    count = 0
    skipped = 0
    batch_data = []
    
    for filepath in files:
        reg_name, reg_date = parse_filename_info(filepath)
        if not reg_date: continue

        if (reg_name, reg_date) in existing:
            skipped += 1
            continue

        try:
            df = pd.read_csv(filepath)
            df['unique_key'] = df.apply(generate_key, axis=1)
            
            for _, row in df.iterrows():
                batch_data.append((
                    reg_name, reg_date, row['unique_key'],
                    row.get('참조번호', ''), row.get('조명', ''), str(row.get('내용', ''))
                ))
            
            if len(batch_data) >= 1000:
                cursor.executemany('''
                    INSERT OR IGNORE INTO regulation_history 
                    (regulation_name, reg_date, unique_key, ref_no, article_title, content) 
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', batch_data)
                batch_data = []
            
            count += 1
        except Exception:
            pass
            
    if batch_data:
        cursor.executemany('''
            INSERT OR IGNORE INTO regulation_history 
            (regulation_name, reg_date, unique_key, ref_no, article_title, content) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', batch_data)
        
    conn.commit()
    conn.close()
    
    get_regulation_names.clear()
    get_regulation_dates.clear()
    
    return count, skipped

def export_db_to_excel():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    
    output = io.BytesIO()
    try:
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for table_name in tables:
                t_name = table_name[0]
                df = pd.read_sql(f"SELECT * FROM {t_name}", conn)
                df.to_excel(writer, sheet_name=t_name, index=False)
    except Exception:
        conn.close()
        return None

    conn.close()
    return output.getvalue()


# =========================================================
# 4. 메인 UI 구성
# =========================================================
st.set_page_config(page_title="금융 규정 검색 시스템", layout="wide", page_icon="⚡")

with st.sidebar:
    st.header("⚙️ 관리 및 메뉴")
    
    # --- [추가됨] TXT -> CSV 변환 버튼 ---
    st.markdown("**(1) 원본 파일 처리**")
    if st.button("📄 TXT -> CSV 변환"):
        with st.spinner("TXT 파일을 파싱하여 CSV로 변환 중입니다..."):
            conv, skip, err, msg = convert_txt_files_to_csv()
            if conv == -1:
                st.warning(msg)
            elif conv == 0 and skip == 0:
                st.info(msg)
            else:
                st.success(f"변환 완료! (신규: {conv}개, 건너뜀: {skip}개, 오류: {err}개)")
    
    st.markdown("**(2) 시스템 DB 등록**")
    if st.button("🔄 DB 업데이트 (증분)"):
        with st.spinner(f"'{DATA_DIR}' 폴더 스캔 중..."):
            cnt, skip = load_files()
        
        if cnt == -1:
            st.warning(f"폴더가 생성되었습니다. CSV/TXT 파일을 '{DATA_DIR}'에 넣어주세요.")
        else:
            st.success(f"DB 업데이트 완료! (신규: {cnt}개, 건너뜀: {skip}개)")
    
    st.write("")
    st.markdown("**(3) 데이터 내보내기**")
    if st.button("📥 DB 전체 엑셀 다운로드 준비"):
        with st.spinner("엑셀 파일 생성 중... (데이터 양에 따라 시간이 걸릴 수 있습니다)"):
            if os.path.exists(DB_FILE):
                excel_data = export_db_to_excel()
                if excel_data:
                    st.download_button(
                        label="💾 엑셀 파일 다운로드",
                        data=excel_data,
                        file_name="regulation_db_dump.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                else:
                    st.error("오류 발생")

    st.markdown("---")
    st.header("🔍 기능 선택")
    menu = st.radio("메뉴 선택", list(MENU_NAMES.values()))

st.title(f"⚡ {menu}")

reg_names = get_regulation_names()
default_reg_index = 0
if PREFERRED_REG_NAME in reg_names:
    default_reg_index = reg_names.index(PREFERRED_REG_NAME)

# =========================================================
# 5. 메뉴별 로직 
# =========================================================

if menu == MENU_NAMES["1"]:
    st.subheader("📂 시스템에 등록된 규정 목록")
    if reg_names: st.table(pd.DataFrame(reg_names, columns=["규정명"]))
    else: st.info("데이터가 없습니다.")

elif menu == MENU_NAMES["2"]:
    st.subheader("📅 규정별 개정 히스토리")
    if reg_names:
        target = st.selectbox("규정 선택", reg_names, index=default_reg_index)
        dates = get_regulation_dates(target)
        st.write(f"**{target}** 개정일 목록:")
        st.table(pd.DataFrame(dates, columns=["개정일자"]))

elif menu == MENU_NAMES["3"]:
    st.subheader("📖 규정 전문 조회")
    if reg_names:
        c1, c2 = st.columns(2)
        with c1: target = st.selectbox("규정", reg_names, index=default_reg_index)
        dates = get_regulation_dates(target)
        with c2: date = st.selectbox("날짜", dates) if dates else st.selectbox("날짜", [])
        
        if st.button("조회"):
            conn = get_connection()
            df = pd.read_sql("SELECT ref_no as '조항', article_title as '조명', content as '내용' FROM regulation_history WHERE regulation_name=? AND reg_date=? ORDER BY id", conn, params=(target, date))
            conn.close()
            st.dataframe(df, width='stretch', height=600)

elif menu == MENU_NAMES["4"]:
    st.subheader("🕰️ 조항 변경 이력 추적")
    if reg_names:
        c1, c2 = st.columns(2)
        with c1: target = st.selectbox("규정", reg_names, index=default_reg_index)
        with c2: ref = st.text_input("조항 번호", value=DEFAULT_ART_NO)
        
        if st.button("히스토리 검색"):
            conn = get_connection()
            df = pd.read_sql("SELECT reg_date, ref_no, article_title, content, unique_key FROM regulation_history WHERE regulation_name=? AND ref_no LIKE ? ORDER BY unique_key, reg_date", conn, params=(target, f"%{ref}%"))
            conn.close()
            
            if df.empty: st.warning("결과가 없습니다.")
            else:
                for r_no, group in df.groupby('ref_no'):
                    with st.expander(f"📌 {r_no} ({group.iloc[0]['article_title']})", expanded=True):
                        prev = None
                        for _, row in group.iterrows():
                            if prev is None: badge, color = "🆕 신설", "blue"
                            elif prev != row['content']: badge, color = "✏️ 변경", "orange"
                            else: badge, color = "─ 유지", "grey"
                            st.markdown(f":{color}[**[{row['reg_date']}] {badge}**]")
                            if badge == "✏️ 변경": st.code(row['content'], language=None)
                            else: st.caption(row['content'])
                            st.divider()
                            prev = row['content']

elif menu == MENU_NAMES["5"]:
    st.subheader("🔎 특정 시점 조항 상세 조회")
    if reg_names:
        c1, c2, c3 = st.columns(3)
        with c1: target = st.selectbox("규정", reg_names, index=default_reg_index)
        dates = get_regulation_dates(target)
        with c2: date = st.selectbox("날짜", dates) if dates else st.selectbox("날짜", [])
        with c3: ref = st.text_input("조항 번호", value=DEFAULT_ART_NO)
        
        if st.button("조회"):
            conn = get_connection()
            df = pd.read_sql("""
                SELECT ref_no AS '조항', article_title AS '조명', content AS '내용' 
                FROM regulation_history 
                WHERE regulation_name=? AND reg_date=? AND ref_no LIKE ?
            """, conn, params=(target, date, f"%{ref}%"))
            conn.close()
            st.table(df)

elif menu == MENU_NAMES["6"]:
    st.subheader("🔍 통합 키워드 검색")
    if reg_names:
        c1, c2 = st.columns([1, 2])
        with c1:
            target = st.selectbox("대상", ["전체 규정 (All)"] + reg_names, index=0)
            latest = st.checkbox("최신 규정만", value=True)
        with c2:
            keyword = st.text_input("검색어", placeholder="예: 공매도")
            btn = st.button("검색")

        if btn and keyword:
            conn = get_connection()
            q = "SELECT regulation_name, reg_date, ref_no, article_title, content FROM regulation_history WHERE (content LIKE ? OR article_title LIKE ?)"
            p = [f"%{keyword}%", f"%{keyword}%"]
            if target != "전체 규정 (All)":
                q += " AND regulation_name = ?"
                p.append(target)
            
            if latest:
                q += """
                    AND (regulation_name, reg_date) IN (
                        SELECT regulation_name, MAX(reg_date)
                        FROM regulation_history
                        GROUP BY regulation_name
                    )
                """
            q += " ORDER BY regulation_name, reg_date DESC, id"
            
            df = pd.read_sql(q, conn, params=p)
            conn.close()
            
            if df.empty: st.warning("결과 없음")
            else:
                st.success(f"총 {len(df)}건 검색됨")
                if len(df) > 200: st.warning("⚠️ 결과가 너무 많아 일부만 표시될 수 있습니다.")
                    
                for _, row in df.iterrows():
                    with st.container(border=True):
                        st.markdown(f"**📌 [{row['regulation_name']}] {row['ref_no']} {row['article_title']}** :grey[{row['reg_date']}]")
                        st.markdown(row['content'].replace(keyword, f":red[**{keyword}**]"))

elif menu == MENU_NAMES["7"]:
    st.subheader("🔗 조항 인용 및 역참조 분석")
    st.info("특정 규정의 조항이 내/외부에서 어떻게 인용되고 있는지 분석합니다.")
    
    if reg_names:
        col1, col2 = st.columns(2)
        with col1:
            target_reg = st.selectbox("관심 규정", reg_names, index=default_reg_index)
        with col2:
            target_art = st.text_input("관심 조항 번호", value=DEFAULT_ART_NO)
            
        latest_only = st.checkbox("최신 규정 내용에서만 찾기 (권장)", value=True)
        search_btn = st.button("인용 분석 시작", type="primary")
        
        if search_btn and target_art:
            conn = get_connection()
            
            is_rule = "시행세칙" in target_reg
            partner_reg_name = target_reg.replace(" 시행세칙", "").replace("시행세칙", "").strip() if is_rule else f"{target_reg} 시행세칙"

            term_internal = target_art 
            term_partner = f"세칙 {target_art}" if is_rule else f"규정 {target_art}"
            term_external = f"「{target_reg}」 {target_art}"

            base_query = """
                SELECT regulation_name, reg_date, ref_no, article_title, content
                FROM regulation_history
                WHERE 
                   (regulation_name = ? AND content LIKE ?) OR 
                   (regulation_name LIKE ? AND content LIKE ?) OR
                   (content LIKE ?)
            """
            
            partner_like = f"%{partner_reg_name}%"
            params = [
                target_reg, f"%{term_internal}%",
                partner_like, f"%{term_partner}%",
                f"%{term_external}%"
            ]
            
            if latest_only:
                full_query = f"""
                    WITH LatestDates AS (
                        SELECT regulation_name, MAX(reg_date) as max_date
                        FROM regulation_history
                        GROUP BY regulation_name
                    )
                    SELECT h.regulation_name, h.reg_date, h.ref_no, h.article_title, h.content
                    FROM regulation_history h
                    JOIN LatestDates ld ON h.regulation_name = ld.regulation_name AND h.reg_date = ld.max_date
                    WHERE 
                       (h.regulation_name = ? AND h.content LIKE ?) OR 
                       (h.regulation_name LIKE ? AND h.content LIKE ?) OR
                       (h.content LIKE ?)
                    ORDER BY h.regulation_name, h.id
                """
            else:
                full_query = base_query + " ORDER BY regulation_name, id"

            df_filtered = pd.read_sql(full_query, conn, params=params)
            conn.close()
            
            results_internal, results_partner, results_external = [], [], []
            
            for _, row in df_filtered.iterrows():
                curr_reg = row['regulation_name']
                content = row['content']
                
                if curr_reg == target_reg:
                    if term_internal in content: results_internal.append(row)
                elif partner_reg_name in curr_reg: 
                    if term_partner in content: results_partner.append(row)
                else:
                    if term_external in content: results_external.append(row)

            st.success(f"분석 완료: 내부 {len(results_internal)}건 / {partner_reg_name} {len(results_partner)}건 / 타 규정 {len(results_external)}건")
            
            st.markdown(f"### 🏠 [{target_reg}] 내부 참조")
            if results_internal:
                for row in results_internal:
                    with st.container(border=True):
                        st.markdown(f"**📌 {row['ref_no']} {row['article_title']}**")
                        st.markdown(row['content'].replace(term_internal, f":red[**{term_internal}**]"))
            else:
                st.caption("결과 없음")

            st.markdown(f"### 🤝 [{partner_reg_name}] 참조")
            st.info(f"검색 조건: '{term_partner}'")
            if results_partner:
                for row in results_partner:
                    with st.container(border=True):
                        st.markdown(f"**📌 {row['ref_no']} {row['article_title']}**")
                        st.markdown(row['content'].replace(term_partner, f":blue[**{term_partner}**]"))
            else:
                st.caption("결과 없음")

            st.markdown(f"### 🌏 타 규정 참조")
            st.info(f"검색 조건: '{term_external}'")
            if results_external:
                for row in results_external:
                    with st.container(border=True):
                        st.markdown(f"**📌 [{row['regulation_name']}] {row['ref_no']} {row['article_title']}**")
                        st.markdown(row['content'].replace(term_external, f":green[**{term_external}**]"))
            else:
                st.caption("결과 없음")