import streamlit as st
import sqlite3
import pandas as pd
import glob
import os
import re
import io
import sys
from pathlib import Path
import unicodedata

# pyhwp 라이브러리 내부 모듈 임포트 시도
try:
    import hwp5.hwp5txt
    HAS_PYHWP = True
except ImportError:
    HAS_PYHWP = False

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
# 2. HWP -> TXT 및 TXT -> CSV 변환 관련 함수
# =========================================================
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
                # 에러 없이 정상 리턴되는 경우 카운트 증가
                converted += 1
            except SystemExit as e:
                # hwp5txt.main()이 내부적으로 sys.exit()을 호출하는 경우 에러 코드로 분기
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
        if mok_matches: ho_main = remainder[: mok_matches[0].start(0)].strip()
        else: ho_main = remainder.strip()

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
        if mok_matches: ho_main = remainder[: mok_matches[0].start(0)].strip()
        else: ho_main = remainder.strip()

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


# =========================================================
# 3. DB 핸들링 및 최적화 함수
# =========================================================
def get_db_url():
    """Supabase PostgreSQL 연결 URL 확인 (Streamlit secrets 또는 환경 변수)"""
    try:
        if "SUPABASE_DB_URL" in st.secrets:
            return st.secrets["SUPABASE_DB_URL"]
        if "database" in st.secrets and "url" in st.secrets["database"]:
            return st.secrets["database"]["url"]
    except Exception:
        pass
    return os.environ.get("SUPABASE_DB_URL", "")

@st.cache_resource(ttl=300)
def check_postgres_available():
    db_url = get_db_url()
    if not db_url:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(db_url, connect_timeout=4)
        conn.close()
        return True
    except Exception:
        return False

def is_postgres():
    return check_postgres_available()

def sql_ph(query: str) -> str:
    """PostgreSQL에서는 %s, SQLite에서는 ? 로 플레이스홀더 변환"""
    if is_postgres():
        return query.replace("?", "%s")
    return query

def get_connection():
    db_url = get_db_url()
    if db_url:
        try:
            import psycopg2
            return psycopg2.connect(db_url)
        except Exception:
            pass
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def _insert_history_batch(cursor, batch_data):
    """PostgreSQL(Supabase) 및 SQLite 양쪽의 배치 삽입 호환 함수"""
    if not batch_data:
        return
    if is_postgres():
        from psycopg2.extras import execute_values
        query = '''
            INSERT INTO regulation_history 
            (regulation_name, reg_date, unique_key, ref_no, article_title, content) 
            VALUES %s
            ON CONFLICT (regulation_name, reg_date, unique_key) DO UPDATE
            SET ref_no = EXCLUDED.ref_no, article_title = EXCLUDED.article_title, content = EXCLUDED.content
        '''
        execute_values(cursor, query, batch_data, page_size=len(batch_data))
    else:
        cursor.executemany('''
            INSERT INTO regulation_history 
            (regulation_name, reg_date, unique_key, ref_no, article_title, content) 
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (regulation_name, reg_date, unique_key) DO UPDATE
            SET ref_no = excluded.ref_no, article_title = excluded.article_title, content = excluded.content
        ''', batch_data)

def normalize_regulation_name(name: str) -> str:
    """규정명 정규화:
    - Mac(NFD)과 Windows(NFC) 유니코드 정규화
    - '_전문', ' 전문', '_전문_' 등 전문 식별 태그 제거
    - '개정문', '일부개정' 등 부가 태그 제거
    - 앞뒤 언더스코어 및 공백 정리
    """
    if not name:
        return ""
    name = unicodedata.normalize('NFC', name)
    # 단어 내부의 '전문' (예: '전문직원 관리규정')을 훼손하지 않고
    # 구분자(_ 또는 공백)로 둘러싸이거나 접미사로 붙은 '전문' 태그 제거
    s = re.sub(r'[_ ]*전문(?=(_|\s|$))', '', name)
    s = re.sub(r'[_ ]*(개정문|일부개정)(?=(_|\s|$))', '', s)
    return unicodedata.normalize('NFC', s.strip(' _'))

def clean_legacy_regulation_names(conn=None):
    """DB 내 기존에 존재하는 '_전문' 등 불필요한 접미사가 붙은 규정명을 정리하고 중복 시 병합"""
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT regulation_name FROM regulation_history")
        names = [r[0] for r in cursor.fetchall()]
        for old_name in names:
            new_name = normalize_regulation_name(old_name)
            if new_name != old_name:
                # 동일 (new_name, reg_date, unique_key) 충돌 방지용 중복 정리 후 업데이트
                if is_postgres():
                    cursor.execute("""
                        DELETE FROM regulation_history target
                        USING regulation_history src
                        WHERE src.regulation_name = %s
                          AND target.regulation_name = %s
                          AND target.reg_date = src.reg_date
                          AND target.unique_key = src.unique_key;
                    """, (old_name, new_name))
                    cursor.execute("""
                        UPDATE regulation_history
                        SET regulation_name = %s
                        WHERE regulation_name = %s;
                    """, (new_name, old_name))
                else:
                    cursor.execute("""
                        DELETE FROM regulation_history
                        WHERE regulation_name = ?
                          AND reg_date || '_' || unique_key IN (
                              SELECT reg_date || '_' || unique_key FROM regulation_history WHERE regulation_name = ?
                          );
                    """, (new_name, old_name))
                    cursor.execute("""
                        UPDATE regulation_history
                        SET regulation_name = ?
                        WHERE regulation_name = ?;
                    """, (new_name, old_name))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        if should_close:
            conn.close()

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    if is_postgres():
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS regulation_history (
                id BIGSERIAL PRIMARY KEY,
                regulation_name TEXT NOT NULL,
                reg_date VARCHAR(8) NOT NULL,
                unique_key TEXT NOT NULL,
                ref_no TEXT,
                article_title TEXT,
                content TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                CONSTRAINT uq_reg_date_key UNIQUE(regulation_name, reg_date, unique_key)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS regulation_uploads (
                id BIGSERIAL PRIMARY KEY,
                filename TEXT NOT NULL,
                regulation_name TEXT NOT NULL,
                reg_date VARCHAR(8) NOT NULL,
                clause_count INTEGER DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                uploaded_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
    else:
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS regulation_uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                regulation_name TEXT,
                reg_date TEXT,
                clause_count INTEGER DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                uploaded_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')
    
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_reg_name ON regulation_history(regulation_name);",
        "CREATE INDEX IF NOT EXISTS idx_reg_date ON regulation_history(reg_date);",
        "CREATE INDEX IF NOT EXISTS idx_ref_no ON regulation_history(ref_no);",
        "CREATE INDEX IF NOT EXISTS idx_name_date ON regulation_history(regulation_name, reg_date);",
        "CREATE INDEX IF NOT EXISTS idx_uploads_name ON regulation_uploads(regulation_name);"
    ]
    for idx_sql in indexes: cursor.execute(idx_sql)
    clean_legacy_regulation_names(conn)
    conn.commit()
    conn.close()

def get_db_stats():
    """실시간 DB 통계 조회 (총 규정 수, 총 레코드 수)"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT regulation_name), COUNT(*) FROM regulation_history")
        row = cursor.fetchone()
        if row:
            return {"reg_count": row[0] or 0, "row_count": row[1] or 0}
        return {"reg_count": 0, "row_count": 0}
    except Exception:
        return {"reg_count": 0, "row_count": 0}
    finally:
        conn.close()

def get_regulation_names():
    """DB 실데이터 기준 고유 규정명 목록 실시간 조회 (캐시 미사용으로 항상 최신 DB 반영)"""
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT DISTINCT regulation_name FROM regulation_history ORDER BY regulation_name", conn)
        return df['regulation_name'].tolist()
    except Exception:
        return []
    finally:
        conn.close()

def get_regulation_dates(reg_name):
    """DB 실데이터 기준 특정 규정의 개정일자 목록 실시간 조회"""
    conn = get_connection()
    try:
        q = sql_ph("SELECT DISTINCT reg_date FROM regulation_history WHERE regulation_name=? ORDER BY reg_date DESC")
        dates = pd.read_sql(q, conn, params=(reg_name,))
        return dates['reg_date'].tolist()
    except Exception:
        return []
    finally:
        conn.close()

def parse_filename_info(filename):
    base_name = os.path.basename(filename)
    name_without_ext = os.path.splitext(base_name)[0]
    
    # Mac(NFD)과 Windows(NFC)의 한글 인코딩 차이를 NFC(결합형)로 통일
    name_without_ext = unicodedata.normalize('NFC', name_without_ext)
    
    date_match = re.search(r'(\d{8})', name_without_ext)
    reg_date = date_match.group(1) if date_match else None
    
    s = name_without_ext
    if reg_date:
        s = re.sub(r'_?' + reg_date + r'(\.csv|\.txt|\.hwp)?$', '', s)
        s = s.replace(f'_{reg_date}', '').replace(reg_date, '')
    
    reg_name = normalize_regulation_name(s)
    return reg_name, reg_date

def generate_key(row):
    return f"{row['장번호']}_{row['조']}_{row['항']}_{row['호']}_{row['목']}"

def build_unique_keys(df: pd.DataFrame) -> list:
    """단일 규정 데이터프레임 내 고유키 생성 및 중복 키 고유화 (부칙 등 동일 조항 번호 반복 대응)"""
    for col in ['장번호', '조', '항', '호', '목']:
        if col not in df.columns:
            df[col] = "0"
    key_counts = {}
    unique_keys = []
    for _, row in df.iterrows():
        base_key = generate_key(row)
        if base_key in key_counts:
            key_counts[base_key] += 1
            unique_keys.append(f"{base_key}_{key_counts[base_key]}")
        else:
            key_counts[base_key] = 0
            unique_keys.append(base_key)
    return unique_keys

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
            df['unique_key'] = build_unique_keys(df)
            
            for _, row in df.iterrows():
                batch_data.append((
                    reg_name, reg_date, row['unique_key'],
                    row.get('참조번호', ''), row.get('조명', ''), str(row.get('내용', ''))
                ))
            
            if len(batch_data) >= 1000:
                _insert_history_batch(cursor, batch_data)
                batch_data = []
            
            count += 1
        except Exception:
            pass
            
    if batch_data:
        _insert_history_batch(cursor, batch_data)
        
    conn.commit()
    conn.close()
    
    return count, skipped

def export_db_to_excel():
    conn = get_connection()
    cursor = conn.cursor()
    if is_postgres():
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';")
        tables = cursor.fetchall()
    else:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
    
    output = io.BytesIO()
    try:
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for table_name in tables:
                t_name = table_name[0]
                if t_name == 'sqlite_sequence': continue
                df = pd.read_sql(f'SELECT * FROM "{t_name}" ORDER BY id', conn)
                df.to_excel(writer, sheet_name=t_name, index=False)
    except Exception:
        conn.close()
        return None

    conn.close()
    return output.getvalue()


# =========================================================
# 3-1. 규정 파일 업로드 자동 처리 파이프라인 (HWP/TXT/CSV -> DB)
# =========================================================
# 파일명 가이드라인: "규정명_전문_YYYYMMDD.hwp" (또는 .txt, .csv)
#  - YYYYMMDD(8자리 개정일자)는 DB 등록에 반드시 필요합니다.
#  - '_전문_' 구분자를 사용하면 규정명이 정확하게 추출됩니다.
FILENAME_GUIDE = "규정명_전문_YYYYMMDD.hwp (또는 .txt, .csv)"
FILENAME_EXAMPLE = "유가증권시장 업무규정_전문_20240315.hwp"


def validate_regulation_filename(filename: str):
    """업로드된 파일명이 DB 등록 요건(8자리 개정일자 포함)을 만족하는지 검증."""
    name = unicodedata.normalize('NFC', os.path.splitext(os.path.basename(filename))[0])
    if not re.search(r'\d{8}', name):
        return False, "파일명에 개정일자(YYYYMMDD, 8자리 숫자)가 없습니다. (예: 규정명_전문_20240315.hwp)"
    return True, ""

validate_hwp_filename = validate_regulation_filename


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


def load_single_csv(filepath: str, overwrite: bool = False, original_filename: str = None, file_size: int = 0):
    """단일 CSV를 DB에 증분 적재. 반환: {'status': inserted|updated|skipped|error, ...}"""
    init_db()
    reg_name, reg_date = parse_filename_info(filepath)
    if not reg_date:
        return {"status": "error", "message": "파일명에서 개정일자를 찾을 수 없습니다."}

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        sql_ph("SELECT 1 FROM regulation_history WHERE regulation_name=? AND reg_date=? LIMIT 1"),
        (reg_name, reg_date),
    )
    is_existing = cursor.fetchone() is not None
    if is_existing and not overwrite:
        conn.close()
        return {"status": "skipped", "reg_name": reg_name, "reg_date": reg_date}

    try:
        df = pd.read_csv(filepath)
        if df.empty:
            conn.close()
            return {"status": "error", "message": "파싱 결과가 0건입니다. 원본 파일 형식을 확인해주세요."}

        # 조, 항, 호, 목 누락 컬럼 기본값 보정 및 고유키 중복 해소
        df['unique_key'] = build_unique_keys(df)

        batch = [
            (reg_name, reg_date, row['unique_key'],
             row.get('참조번호', ''), row.get('조명', ''), str(row.get('내용', '')))
            for _, row in df.iterrows()
        ]

        # 덮어쓰기 시 기존 데이터 먼저 삭제하여 충돌 방지 및 완벽 갱신
        if is_existing and overwrite:
            cursor.execute(
                sql_ph("DELETE FROM regulation_history WHERE regulation_name=? AND reg_date=?"),
                (reg_name, reg_date)
            )

        _insert_history_batch(cursor, batch)

        # 영구 업로드 이력 로그 기록 (regulation_uploads)
        try:
            fname = original_filename or os.path.basename(filepath)
            fsize = file_size or (os.path.getsize(filepath) if os.path.exists(filepath) else 0)
            if is_postgres():
                cursor.execute(
                    "INSERT INTO regulation_uploads (filename, regulation_name, reg_date, clause_count, file_size) VALUES (%s, %s, %s, %s, %s)",
                    (fname, reg_name, reg_date, len(batch), fsize)
                )
            else:
                cursor.execute(
                    "INSERT INTO regulation_uploads (filename, regulation_name, reg_date, clause_count, file_size) VALUES (?, ?, ?, ?, ?)",
                    (fname, reg_name, reg_date, len(batch), fsize)
                )
        except Exception:
            pass

        conn.commit()
        conn.close()
        status_label = "updated" if (is_existing and overwrite) else "inserted"
        return {"status": status_label, "reg_name": reg_name, "reg_date": reg_date, "rows": len(batch)}
    except Exception as e:
        conn.close()
        return {"status": "error", "message": str(e)}


def process_uploaded_file(uploaded_file, overwrite: bool = False):
    """업로드된 파일(.hwp, .txt, .csv)을 파싱하여 DB(Supabase/SQLite)에 자동 적재."""
    filename = unicodedata.normalize('NFC', uploaded_file.name)
    result = {"filename": filename}
    ext = os.path.splitext(filename)[1].lower()

    # 1. 파일명 검증 (개정일자 필수)
    ok, msg = validate_regulation_filename(filename)
    if not ok:
        result.update(status="error", message=msg)
        return result

    # 2. 로컬 디스크에 임시/참고용 원본 저장
    os.makedirs(DATA_DIR, exist_ok=True)
    target_path = Path(DATA_DIR) / filename
    try:
        buffer = uploaded_file.getbuffer()
        file_size = len(buffer)
        with open(target_path, "wb") as f:
            f.write(buffer)
    except Exception as e:
        result.update(status="error", message=f"파일 임시 저장 실패: {e}")
        return result

    # 3. 확장자별 파싱 및 DB 적재 파이프라인
    if ext == ".hwp":
        if not HAS_PYHWP:
            result.update(status="error", message="pyhwp 라이브러리가 설치되어 있지 않습니다.")
            return result
        txt_path, err = convert_single_hwp_to_txt(target_path)
        if err:
            result.update(status="error", message=f"HWP→TXT 변환 실패: {err}")
            return result
        try:
            text = read_source_text(str(txt_path))
            df = parse_all(text)
            csv_path = txt_path.with_suffix(".csv")
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        except Exception as e:
            result.update(status="error", message=f"TXT→CSV 변환 실패: {e}")
            return result
        return load_single_csv(str(csv_path), overwrite=overwrite, original_filename=filename, file_size=file_size)

    elif ext == ".txt":
        try:
            text = read_source_text(str(target_path))
            df = parse_all(text)
            csv_path = target_path.with_suffix(".csv")
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        except Exception as e:
            result.update(status="error", message=f"TXT→CSV 변환 실패: {e}")
            return result
        return load_single_csv(str(csv_path), overwrite=overwrite, original_filename=filename, file_size=file_size)

    elif ext == ".csv":
        return load_single_csv(str(target_path), overwrite=overwrite, original_filename=filename, file_size=file_size)

    else:
        result.update(status="error", message=f"지원하지 않는 파일 형식입니다: {ext} (지원: .hwp, .txt, .csv)")
        return result

# 기존 호출 호환성 유지
process_uploaded_hwp = process_uploaded_file


def get_recent_uploads(limit: int = 10):
    """DB에 영구 기록된 최근 업로드 이력 조회"""
    conn = get_connection()
    try:
        q = sql_ph(f"SELECT filename, regulation_name, reg_date, clause_count, uploaded_at FROM regulation_uploads ORDER BY id DESC LIMIT {limit}")
        df = pd.read_sql(q, conn)
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def delete_regulation_revision(reg_name: str, reg_date: str, delete_local_files: bool = False):
    """DB에서 특정 규정의 특정 개정일자 데이터를 삭제.
    선택 시 '규정' 폴더의 로컬 참고 파일(.hwp, .txt, .csv)도 함께 삭제.
    반환: {'deleted_rows': int, 'deleted_files': list, 'status': 'success'|'error', 'message': str}
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # 1. 삭제 대상 레코드 수 확인
        cursor.execute(
            sql_ph("SELECT COUNT(*) FROM regulation_history WHERE regulation_name=? AND reg_date=?"),
            (reg_name, reg_date)
        )
        row = cursor.fetchone()
        count = row[0] if row else 0
        if count == 0:
            conn.close()
            return {"status": "error", "message": "해당 규정 및 일자의 데이터가 DB에 존재하지 않습니다."}

        # 2. DB 레코드 삭제
        cursor.execute(
            sql_ph("DELETE FROM regulation_history WHERE regulation_name=? AND reg_date=?"),
            (reg_name, reg_date)
        )
        # 업로드 이력에서도 삭제
        try:
            cursor.execute(
                sql_ph("DELETE FROM regulation_uploads WHERE regulation_name=? AND reg_date=?"),
                (reg_name, reg_date)
            )
        except Exception:
            pass

        conn.commit()

        # 3. 로컬 파일 삭제 옵션 처리
        deleted_files = []
        if delete_local_files and os.path.exists(DATA_DIR):
            for ext in ['.hwp', '.txt', '.csv']:
                pattern = os.path.join(DATA_DIR, f"*{reg_name}*{reg_date}*{ext}")
                for f in glob.glob(pattern):
                    try:
                        os.remove(f)
                        deleted_files.append(os.path.basename(f))
                    except Exception:
                        pass

        return {
            "status": "success",
            "deleted_rows": count,
            "deleted_files": deleted_files,
            "reg_name": reg_name,
            "reg_date": reg_date
        }
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def auto_sync_missing_hwps():
    """'규정' 폴더의 .hwp 파일 중 DB에 누락된 규정을 감지하여 자동으로 변환 및 DB 적재.
    앱 시작 시 1회 실행되어 항상 최신 DB 데이터를 보장합니다.
    """
    if not os.path.exists(DATA_DIR):
        return 0, 0, []

    conn = get_connection()
    cursor = conn.cursor()
    existing_pairs = set()
    try:
        cursor.execute("SELECT DISTINCT regulation_name, reg_date FROM regulation_history")
        for row in cursor.fetchall():
            existing_pairs.add((row[0], row[1]))
    except Exception:
        pass
    finally:
        conn.close()

    hwp_files = glob.glob(os.path.join(DATA_DIR, "*.hwp"))
    if not hwp_files:
        return 0, 0, []

    inserted_count = 0
    error_count = 0
    synced_items = []

    for hwp_path_str in hwp_files:
        reg_name, reg_date = parse_filename_info(hwp_path_str)
        if not reg_date:
            continue
        if (reg_name, reg_date) in existing_pairs:
            continue

        # DB에 없음 -> 자동 파싱 및 DB 적재 파이프라인 가동
        hwp_path = Path(hwp_path_str)
        txt_path, err = convert_single_hwp_to_txt(hwp_path)
        if err:
            error_count += 1
            continue

        try:
            text = read_source_text(str(txt_path))
            df = parse_all(text)
            csv_path = txt_path.with_suffix(".csv")
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")

            res = load_single_csv(
                str(csv_path),
                overwrite=False,
                original_filename=hwp_path.name,
                file_size=hwp_path.stat().st_size
            )
            if res.get("status") in ("inserted", "updated"):
                inserted_count += 1
                synced_items.append((reg_name, reg_date, res.get("rows", 0)))
                existing_pairs.add((reg_name, reg_date))
            else:
                error_count += 1
        except Exception:
            error_count += 1

    return inserted_count, error_count, synced_items


@st.cache_resource
def ensure_db_initialized():
    """앱 기동 시 DB 초기화, 레거시 데이터 정규화, 누락된 HWP 파일 자동 동기화 1회 보장"""
    init_db()
    ins, err, items = auto_sync_missing_hwps()
    return {"inserted": ins, "errors": err, "items": items}


# =========================================================
# 4. 메인 UI 구성
# =========================================================
st.set_page_config(page_title="금융 규정 검색 시스템", layout="wide", page_icon="⚡")

# 앱 구동 시 DB 연결 및 스키마 검증/레거시 데이터 정규화 및 누락 HWP 자동 동기화 1회 자동 실행
sync_result = ensure_db_initialized()
db_stats = get_db_stats()

with st.sidebar:
    st.subheader("🗄️ 데이터베이스 연결 현황")
    if is_postgres():
        st.success("🟢 **온라인 DB (Supabase PostgreSQL)**")
        st.caption("✅ 클라우드 DB 연결됨 — 업로드 시 데이터가 영구 보존됩니다.")
    else:
        st.warning("📁 **로컬 DB (SQLite)**")
        st.caption("⚠️ 로컬 모드: Streamlit Cloud 배포 시 재부팅마다 초기화되므로, 영구 저장을 위해 Secrets에 SUPABASE_DB_URL을 설정해주세요.")
    st.caption(f"📊 실데이터: **{db_stats['reg_count']}개 규정** / **{db_stats['row_count']:,}건 조항**")
    st.caption("※ 모든 규정 목록과 검색은 DB 실데이터를 직접 조회합니다.")
    st.markdown("---")

    # =====================================================
    # 참고 자료 관리 메뉴 (접기 가능) — 원본 파일 보관 및 수동 적재
    # =====================================================
    with st.expander("📁 참고 자료 관리 (원본 파일 보관 · DB 적재)", expanded=False):
        st.caption("ℹ️ '규정' 폴더의 파일들은 원본 참고 자료 및 백업용입니다. 시스템의 모든 검색과 목록은 위 DB 실데이터를 기준으로 실시간 동작합니다.")

        # --- 파일 업로드 → 자동 DB 등록 (원스톱) ---
        st.markdown("**(1) 신규 파일 업로드 → DB 자동 등록 (영구 저장)**")

        if is_postgres():
            st.info("💡 **Supabase 클라우드 저장**: 업로드 시 파싱된 모든 조항이 클라우드 DB에 즉시 적재되어 영구적으로 보존됩니다.")
        else:
            st.warning("⚠️ **주의**: 현재 로컬 SQLite 모드입니다. Streamlit Cloud에서는 재부팅 시 데이터가 소실되므로 `SUPABASE_DB_URL`을 설정하세요.")

        if st.checkbox("📋 파일명 가이드라인 보기 (필독)"):
            st.markdown(
                f"""
                업로드 시 파일명에서 **규정명**과 **개정일자**를 자동으로 추출하여 DB에 적재합니다.
                (파일명에 `_전문_`이 포함되어 있어도 DB에는 순수 규정명으로 정규화되어 저장됩니다.)

                **권장 형식**
                ```
                {FILENAME_GUIDE}
                ```
                **예시**
                ```
                {FILENAME_EXAMPLE}
                ```

                - `YYYYMMDD` : 개정일자 8자리 숫자 (예: `20240315`) — **필수**
                - `_전문_` : 규정명과 일자를 구분 (권장). 규정명이 정확히 추출됩니다.
                - `.hwp`, `.txt`, `.csv` 파일 형식을 모두 지원합니다.
                - 8자리 개정일자가 없으면 등록되지 않습니다.
                """
            )

        overwrite_opt = st.checkbox("🔄 기존 동일 개정일자 데이터 존재 시 덮어쓰기 (업데이트)", value=False)

        uploaded_files = st.file_uploader(
            "규정 파일 업로드 (.hwp, .txt, .csv / 복수 가능)",
            type=["hwp", "txt", "csv"],
            accept_multiple_files=True,
            help=f"권장 형식: {FILENAME_GUIDE}",
        )

        if st.button("🚀 업로드 파일 자동 처리 및 DB 적재", type="primary"):
            if not uploaded_files:
                st.warning("먼저 업로드할 파일을 선택해주세요.")
            else:
                ins = upd = skip = errs = 0
                progress_bar = st.progress(0)
                for i, uf in enumerate(uploaded_files):
                    with st.spinner(f"처리 중: {uf.name}"):
                        res = process_uploaded_file(uf, overwrite=overwrite_opt)
                    status = res.get("status")
                    if status == "inserted":
                        ins += 1
                        st.success(f"✅ [{res['reg_name']}] {res['reg_date']} 등록 완료 ({res.get('rows', 0)}건)")
                    elif status == "updated":
                        upd += 1
                        st.success(f"🔄 [{res['reg_name']}] {res['reg_date']} 덮어쓰기 완료 ({res.get('rows', 0)}건)")
                    elif status == "skipped":
                        skip += 1
                        st.info(f"⏭️ [{res['reg_name']}] {res['reg_date']} — 이미 등록되어 건너뜀 (덮어쓰려면 상단 체크박스 선택)")
                    else:
                        errs += 1
                        st.error(f"❌ {res['filename']} — {res.get('message', '알 수 없는 오류')}")
                    progress_bar.progress((i + 1) / len(uploaded_files))
                progress_bar.empty()
                st.success(f"처리 완료! (신규: {ins} / 갱신: {upd} / 건너뜀: {skip} / 오류: {errs})")
                if ins > 0 or upd > 0:
                    st.button("🔄 최신 데이터 즉시 반영 (새로고침)", on_click=st.rerun)

        # 영구 업로드 이력 보기
        recent_df = get_recent_uploads(5)
        if not recent_df.empty:
            with st.expander("📋 최근 업로드 이력 (DB 영구 기록)"):
                st.dataframe(recent_df, hide_index=True, use_container_width=True)

        st.markdown("---")

        # --- 규정 폴더 HWP 자동 검사 및 DB 적재 ---
        st.markdown("**(2) 규정 폴더 HWP 자동 검사 및 DB 적재**")
        st.caption("새로운 HWP 파일이 '규정' 폴더에 추가된 경우, DB 미등록 여부를 확인하여 자동으로 파싱 및 적재합니다.")
        if st.button("🔄 미등록 HWP 검사 및 자동 적재"):
            with st.spinner("규정 폴더 내 HWP 파일을 스캔하여 미등록 규정을 동기화 중입니다..."):
                ins, err, items = auto_sync_missing_hwps()
                if ins > 0:
                    st.success(f"✅ 총 {ins}건의 규정 개정본이 DB에 자동 등록되었습니다!")
                    for item in items:
                        st.write(f"- **{item[0]}** ({item[1]}): {item[2]:,}건 조항")
                    st.rerun()
                elif err > 0:
                    st.error(f"동기화 중 오류가 {err}건 발생했습니다.")
                else:
                    st.info("ℹ️ 모든 HWP 파일이 이미 DB에 등록되어 있습니다. (동기화 불필요)")

        st.markdown("---")

        # --- 원본 파일 처리 (HWP -> TXT -> CSV) ---
        st.markdown("**(3) 개별 수동 도구 (단계별 변환)**")

        if st.button("📄 HWP -> TXT 변환"):
            if not HAS_PYHWP:
                st.error("pyhwp 라이브러리가 설치되어 있지 않습니다. 터미널에서 'pip install pyhwp'를 실행해주세요.")
            else:
                with st.spinner("HWP 파일을 파싱하여 TXT로 변환 중입니다..."):
                    conv, skip, err, msg = convert_hwp_to_txt_st()
                    if conv == -1:
                        st.warning(msg)
                    elif conv == 0 and skip == 0:
                        st.info(msg)
                    else:
                        st.success(f"HWP->TXT 변환 완료! (신규: {conv}개, 건너뜀: {skip}개, 오류: {err}개)")

        if st.button("📄 TXT -> CSV 변환"):
            with st.spinner("TXT 파일을 파싱하여 CSV로 변환 중입니다..."):
                conv, skip, err, msg = convert_txt_files_to_csv()
                if conv == -1:
                    st.warning(msg)
                elif conv == 0 and skip == 0:
                    st.info(msg)
                else:
                    st.success(f"TXT->CSV 변환 완료! (신규: {conv}개, 건너뜀: {skip}개, 오류: {err}개)")

        if st.button("📄 CSV 파일 ➜ DB 수동 적재"):
            with st.spinner(f"'{DATA_DIR}' 폴더 내 CSV 파일 스캔 및 DB 적재 중..."):
                cnt, skip = load_files()

            if cnt == -1:
                st.warning(f"폴더가 생성되었습니다. CSV 파일을 '{DATA_DIR}'에 넣어주세요.")
            else:
                st.success(f"DB 동기화 완료! (신규: {cnt}개, 건너뜀: {skip}개)")

        st.write("")
        st.markdown("---")

        # --- DB 특정 일자 규정 삭제 ---
        st.markdown("**(4) DB 특정 개정본 삭제 관리**")
        st.caption("선택한 규정의 특정 개정일자 데이터를 DB에서 안전하게 삭제합니다. 삭제 후 필요 시 HWP 업로드나 자동 동기화로 다시 적재할 수 있습니다.")

        reg_names_for_del = get_regulation_names()
        if reg_names_for_del:
            del_reg_name = st.selectbox("삭제 대상 규정 선택", reg_names_for_del, key="del_reg_select")
            del_dates = get_regulation_dates(del_reg_name)
            if del_dates:
                del_reg_date = st.selectbox("삭제 대상 개정일자 선택", del_dates, key="del_date_select")

                # 대상 조항 수 표시
                target_count = 0
                _conn = get_connection()
                try:
                    _cur = _conn.cursor()
                    _cur.execute(sql_ph("SELECT COUNT(*) FROM regulation_history WHERE regulation_name=? AND reg_date=?"), (del_reg_name, del_reg_date))
                    _r = _cur.fetchone()
                    if _r: target_count = _r[0]
                except Exception:
                    pass
                finally:
                    _conn.close()

                st.warning(f"선택 항목: **[{del_reg_name}] {del_reg_date}** (총 {target_count:,}건 조항)")

                del_files_opt = st.checkbox(
                    "규정 폴더 내의 로컬 참고 파일(.hwp, .txt, .csv)도 함께 삭제",
                    value=False,
                    key="del_files_checkbox",
                    help="체크를 해제하면 DB에서만 삭제되므로, 추후 '미등록 HWP 자동 적재' 버튼이나 파일 업로드를 통해 다시 적재할 수 있습니다."
                )

                del_confirm = st.checkbox(
                    f"⚠️ 위 [{del_reg_name}] ({del_reg_date}) 데이터를 DB에서 삭제하는 것에 동의합니다.",
                    key="del_confirm_checkbox"
                )

                if st.button("🗑️ 선택한 개정본 DB에서 삭제", type="secondary", disabled=not del_confirm):
                    with st.spinner("DB에서 삭제 중입니다..."):
                        del_res = delete_regulation_revision(del_reg_name, del_reg_date, delete_local_files=del_files_opt)
                    if del_res.get("status") == "success":
                        msg = f"✅ [{del_res['reg_name']}] {del_res['reg_date']} 데이터 {del_res['deleted_rows']:,}건이 DB에서 정상 삭제되었습니다."
                        if del_res['deleted_files']:
                            msg += f" (로컬 파일 {len(del_res['deleted_files'])}개 삭제됨)"
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(f"삭제 실패: {del_res.get('message', '알 수 없는 오류')}")
            else:
                st.info("해당 규정에 등록된 개정일자가 없습니다.")
        else:
            st.info("DB에 등록된 규정이 없습니다.")

        st.write("")
        st.markdown("---")
        st.markdown("**(5) 데이터 백업 및 내보내기**")
        if st.button("📥 DB 전체 엑셀 다운로드 준비"):
            with st.spinner("엑셀 파일 생성 중... (데이터 양에 따라 시간이 걸릴 수 있습니다)"):
                if is_postgres() or os.path.exists(DB_FILE):
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

    # =====================================================
    # 기능 선택 (항상 노출 — 사용자가 바로 접근)
    # =====================================================
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
    st.subheader("📂 시스템 DB 등록 규정 목록 (실시간 실데이터)")
    st.caption("※ 본 목록은 '규정' 폴더의 파일 목록이 아닌, **실제 데이터베이스(DB)에 적재된 실데이터**를 집계하여 실시간으로 표시합니다.")
    conn = get_connection()
    try:
        q = """
            SELECT 
                regulation_name AS "규정명",
                COUNT(DISTINCT reg_date) AS "개정본 수",
                MAX(reg_date) AS "최신 개정일자",
                COUNT(*) AS "총 조항(레코드) 수"
            FROM regulation_history
            GROUP BY regulation_name
            ORDER BY regulation_name
        """
        df_summary = pd.read_sql(q, conn)
        if not df_summary.empty:
            st.dataframe(df_summary, width='stretch', hide_index=True)
            st.caption(f"📊 총 {len(df_summary)}개 규정 / {df_summary['총 조항(레코드) 수'].sum():,}건 조항 등록됨")
        else:
            st.info("데이터베이스에 등록된 규정 데이터가 없습니다. 사이드바의 '참고 자료 관리'에서 HWP/CSV를 DB에 등록해주세요.")
    finally:
        conn.close()

elif menu == MENU_NAMES["2"]:
    st.subheader("📅 규정별 개정 히스토리 (실시간 실데이터)")
    st.caption("※ 선택한 규정의 **실제 DB 적재 개정본** 목록 및 각 개정본별 조항 수를 실시간 표시합니다.")
    if reg_names:
        target = st.selectbox("규정 선택", reg_names, index=default_reg_index)
        conn = get_connection()
        try:
            q = sql_ph("""
                SELECT 
                    reg_date AS "개정일자",
                    COUNT(*) AS "등록 조항 수"
                FROM regulation_history 
                WHERE regulation_name=? 
                GROUP BY reg_date 
                ORDER BY reg_date DESC
            """)
            df_dates = pd.read_sql(q, conn, params=(target,))
            st.write(f"**{target}** 개정일 목록 (DB 실데이터 기준 총 {len(df_dates)}개 개정본):")
            st.dataframe(df_dates, width='stretch', hide_index=True)
        finally:
            conn.close()
    else:
        st.info("데이터가 없습니다.")

elif menu == MENU_NAMES["3"]:
    st.subheader("📖 규정 전문 조회")
    if reg_names:
        c1, c2 = st.columns(2)
        with c1: target = st.selectbox("규정", reg_names, index=default_reg_index)
        dates = get_regulation_dates(target)
        with c2: date = st.selectbox("날짜", dates) if dates else st.selectbox("날짜", [])
        
        if st.button("조회"):
            conn = get_connection()
            q = sql_ph('SELECT ref_no as "조항", article_title as "조명", content as "내용" FROM regulation_history WHERE regulation_name=? AND reg_date=? ORDER BY id')
            df = pd.read_sql(q, conn, params=(target, date))
            conn.close()
            st.dataframe(df, width='stretch', height=600)
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

elif menu == MENU_NAMES["4"]:
    st.subheader("🕰️ 조항 변경 이력 추적")
    if reg_names:
        c1, c2 = st.columns(2)
        with c1: target = st.selectbox("규정", reg_names, index=default_reg_index)
        with c2: ref = st.text_input("조항 번호", value=DEFAULT_ART_NO)
        
        if st.button("히스토리 검색"):
            conn = get_connection()
            q = sql_ph("SELECT reg_date, ref_no, article_title, content, unique_key FROM regulation_history WHERE regulation_name=? AND ref_no LIKE ? ORDER BY unique_key, reg_date")
            df = pd.read_sql(q, conn, params=(target, f"%{ref}%"))
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
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

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
            q = sql_ph("""
                SELECT ref_no AS "조항", article_title AS "조명", content AS "내용" 
                FROM regulation_history 
                WHERE regulation_name=? AND reg_date=? AND ref_no LIKE ?
            """)
            df = pd.read_sql(q, conn, params=(target, date, f"%{ref}%"))
            conn.close()
            st.table(df)
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

elif menu == MENU_NAMES["6"]:
    st.subheader("🔍 통합 키워드 검색")
    if reg_names:
        with st.form("keyword_search_form", border=False):
            c1, c2 = st.columns([1, 2])
            with c1:
                target = st.selectbox("대상", ["전체 규정 (All)"] + reg_names, index=0)
                latest = st.checkbox("최신 규정만", value=True)
            with c2:
                keyword = st.text_input("검색어", placeholder="예: 공매도")
                btn = st.form_submit_button("검색", type="primary")

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
            
            df = pd.read_sql(sql_ph(q), conn, params=p)
            conn.close()
            
            if df.empty: st.warning("결과 없음")
            else:
                st.success(f"총 {len(df)}건 검색됨")
                if len(df) > 200: st.warning("⚠️ 결과가 너무 많아 일부만 표시될 수 있습니다.")
                    
                for _, row in df.iterrows():
                    with st.container(border=True):
                        st.markdown(f"**📌 [{row['regulation_name']}] {row['ref_no']} {row['article_title']}** :grey[{row['reg_date']}]")
                        st.markdown(row['content'].replace(keyword, f":red[**{keyword}**]"))
        elif btn and not keyword:
            st.warning("검색어를 입력해주세요.")
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

elif menu == MENU_NAMES["7"]:
    st.subheader("🔗 조항 인용 및 역참조 분석")
    st.info("특정 규정의 조항이 내/외부에서 어떻게 인용되고 있는지 분석합니다.")
    
    if reg_names:
        with st.form("ref_analysis_form", border=False):
            col1, col2 = st.columns(2)
            with col1:
                target_reg = st.selectbox("관심 규정", reg_names, index=default_reg_index)
            with col2:
                target_art = st.text_input("관심 조항 번호", value=DEFAULT_ART_NO)
                
            latest_only = st.checkbox("최신 규정 내용에서만 찾기 (권장)", value=True)
            search_btn = st.form_submit_button("인용 분석 시작", type="primary")
        
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

            df_filtered = pd.read_sql(sql_ph(full_query), conn, params=params)
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
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")