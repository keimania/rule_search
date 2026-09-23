#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
규정 동기화 파이프라인 스크립트 (CLI 및 GitHub Actions용)
- 규정/*.hwp 파일 감지 및 신규/변경 파일 자동 변환 (HWP -> TXT -> CSV)
- Supabase PostgreSQL 및 로컬 SQLite DB 자동 적재 및 동기화
- 실행 방법:
    python scripts/sync_regulations.py
    python scripts/sync_regulations.py --force
"""

import os
import sys
import glob
import io
import re
import argparse
import unicodedata
from pathlib import Path
from contextlib import closing
import pandas as pd

# 루트 디렉토리 설정
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
DATA_DIR = os.path.join(PROJECT_ROOT, "규정")
DB_FILE = os.path.join(PROJECT_ROOT, "regulation_master.db")

# 정규식 상수
ARTICLE_PATTERN = re.compile(r"^\s*제\s*(\d+(?:의\d+)*)\s*조(?:의(\d+))?\s*(?:\(([^)]+)\))?")
HANG_PATTERN = re.compile(r"^[①-⑮]|\s+[①-⑮]")
HO_PATTERN = re.compile(r"(?:^|\s+)(\d+(?:의\d+)?)\.\s+")
MOK_PATTERN = re.compile(r"(?:^|\s+)([가-하])\.\s+")
HANG_NUMS = ["①","②","③","④","⑤","⑥","⑦","⑧","⑨","⑩","⑪","⑫","⑬","⑭","⑮"]
HANG_MAP = {char: str(i + 1) for i, char in enumerate(HANG_NUMS)}


def get_db_url(args_url=None):
    """Supabase PostgreSQL URL 확인 (인자 -> 환경변수 -> .streamlit/secrets.toml)"""
    if args_url:
        return args_url
    if os.environ.get("SUPABASE_DB_URL"):
        return os.environ.get("SUPABASE_DB_URL")
    
    secrets_path = os.path.join(PROJECT_ROOT, ".streamlit", "secrets.toml")
    if os.path.exists(secrets_path):
        try:
            try:
                import tomllib
                with open(secrets_path, "rb") as f:
                    secrets = tomllib.load(f)
            except ImportError:
                import toml
                secrets = toml.load(secrets_path)
            if "SUPABASE_DB_URL" in secrets:
                return secrets["SUPABASE_DB_URL"]
            if "database" in secrets and "url" in secrets["database"]:
                return secrets["database"]["url"]
        except Exception:
            pass
    return ""


def normalize_regulation_name(name: str) -> str:
    if not name:
        return ""
    name = unicodedata.normalize('NFC', name)
    s = re.sub(r'[_ ]*전문(?=(_|\s|$))', '', name)
    s = re.sub(r'[_ ]*(개정문|일부개정)(?=(_|\s|$))', '', s)
    return unicodedata.normalize('NFC', s.strip(' _'))


def parse_filename_info(filename: str):
    base_name = os.path.basename(filename)
    name_without_ext = os.path.splitext(base_name)[0]
    name_without_ext = unicodedata.normalize('NFC', name_without_ext)
    
    date_match = re.search(r'(\d{8})', name_without_ext)
    if not date_match:
        return normalize_regulation_name(name_without_ext), None
        
    date_str = date_match.group(1)
    name_part = name_without_ext[:date_match.start()]
    clean_name = normalize_regulation_name(name_part)
    return clean_name, date_str


def convert_hwp_to_txt(hwp_path: Path):
    """pyhwp 라이브러리를 사용하여 HWP에서 TXT 추출"""
    txt_path = hwp_path.with_suffix(".txt")
    try:
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
        return None, str(e)


def read_source_text(filename: str) -> str:
    path = Path(filename)
    if not path.exists():
        raise FileNotFoundError(f'"{filename}" 파일을 찾을 수 없습니다.')
    for enc in ["utf-8", "cp949", "euc-kr"]:
        try:
            with path.open("r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with path.open("rb") as f:
        return f.read().decode("utf-8", errors="ignore")


def clean_text(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\t", " ")
    return re.sub(r"\s+", " ", s).strip()


def format_ho_ref(ho_num: str) -> str:
    if "의" in ho_num:
        main, sub = ho_num.split("의", 1)
        return f"제{main}호의{sub}"
    return f"제{ho_num}호"


def parse_moks(base_ref: str, article_id: str, title: str, hang: str, ho: str, ho_text: str):
    rows = []
    mok_matches = list(MOK_PATTERN.finditer(ho_text))
    if not mok_matches:
        return rows
    for i, m in enumerate(mok_matches):
        mok_char = m.group(1)
        start = m.start(1)
        end = mok_matches[i + 1].start(1) if i + 1 < len(mok_matches) else len(ho_text)
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
    hang_num = HANG_MAP.get(h_char, "")

    if not ho_matches:
        base_ref = f"{article_id}제{hang_num}항" if hang_num else article_id
        return [{
            "참조번호": base_ref, "조": article_id, "조명": title,
            "항": hang_num, "호": "", "목": "", "내용": block_raw
        }]

    h_lead_text = block_raw[:ho_matches[0].start()].strip()
    if h_lead_text:
        base_ref = f"{article_id}제{hang_num}항" if hang_num else article_id
        rows.append({
            "참조번호": base_ref, "조": article_id, "조명": title,
            "항": hang_num, "호": "", "목": "", "내용": h_lead_text
        })

    for j, ho_m in enumerate(ho_matches):
        ho_num = ho_m.group(1)
        ho_ref_str = format_ho_ref(ho_num)
        start = ho_m.start(1)
        end = ho_matches[j + 1].start(1) if j + 1 < len(ho_matches) else len(block_raw)
        ho_block = block_raw[start:end].strip()

        base_ref_ho = f"{article_id}제{hang_num}항{ho_ref_str}" if hang_num else f"{article_id}{ho_ref_str}"
        moks = parse_moks(base_ref_ho, article_id, title, hang_num, ho_num, ho_block)
        if moks:
            first_mok_pos = MOK_PATTERN.search(ho_block).start(1)
            ho_lead_text = ho_block[:first_mok_pos].strip()
            if ho_lead_text:
                rows.append({
                    "참조번호": base_ref_ho, "조": article_id, "조명": title,
                    "항": hang_num, "호": ho_num, "목": "", "내용": ho_lead_text
                })
            rows.extend(moks)
        else:
            rows.append({
                "참조번호": base_ref_ho, "조": article_id, "조명": title,
                "항": hang_num, "호": ho_num, "목": "", "내용": ho_block
            })
    return rows


def parse_all(text: str) -> pd.DataFrame:
    """텍스트 전문을 조/항/호/목 구조의 DataFrame으로 파싱"""
    lines = [clean_text(l) for l in text.split("\n")]
    blocks = []
    curr_art = None
    curr_title = ""
    curr_lines = []

    for line in lines:
        if not line:
            continue
        m = ARTICLE_PATTERN.match(line)
        if m:
            if curr_art:
                blocks.append((curr_art, curr_title, "\n".join(curr_lines)))
            base_art = m.group(1)
            sub_art = m.group(2)
            curr_art = f"제{base_art}조의{sub_art}" if sub_art else f"제{base_art}조"
            curr_title = m.group(3) or ""
            rest_line = line[m.end():].strip()
            curr_lines = [rest_line] if rest_line else []
        else:
            if curr_art:
                curr_lines.append(line)
    if curr_art:
        blocks.append((curr_art, curr_title, "\n".join(curr_lines)))

    parsed_rows = []
    for art, title, body in blocks:
        h_splits = list(HANG_PATTERN.finditer(body))
        if not h_splits:
            parsed_rows.extend(parse_h_block(art, title, "", body))
            continue

        lead_text = body[:h_splits[0].start()].strip()
        if lead_text:
            parsed_rows.extend(parse_h_block(art, title, "", lead_text))

        for idx, hm in enumerate(h_splits):
            h_char = hm.group().strip()
            start = hm.start()
            end = h_splits[idx + 1].start() if idx + 1 < len(h_splits) else len(body)
            hang_text = body[start:end].strip()
            parsed_rows.extend(parse_h_block(art, title, h_char, hang_text))

    rows_clean = []
    for r in parsed_rows:
        rows_clean.append({
            "구분": "", "장번호": "", "장명": "", "절번호": "", "절명": "",
            "참조번호": clean_text(r["참조번호"]),
            "조명": clean_text(r["조명"]),
            "조": r["조"], "항": r["항"], "호": r["호"], "목": r["목"],
            "내용": clean_text(r["내용"])
        })
    return pd.DataFrame(rows_clean, columns=["구분", "장번호", "장명", "절번호", "절명", "참조번호", "조명", "조", "항", "호", "목", "내용"])


def build_unique_keys(df: pd.DataFrame) -> list:
    key_counts = {}
    unique_keys = []
    for _, row in df.iterrows():
        base_key = f"{row.get('구분', '')}_{row.get('조', '')}_{row.get('항', '')}_{row.get('호', '')}_{row.get('목', '')}"
        count = key_counts.get(base_key, 0)
        key_counts[base_key] = count + 1
        ukey = base_key if count == 0 else f"{base_key}_{count}"
        unique_keys.append(ukey)
    return unique_keys


def sync_to_sqlite(csv_path: str, reg_name: str, reg_date: str, force: bool = False):
    """로컬 SQLite regulation_master.db에 적재"""
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS regulation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regulation_name TEXT, reg_date TEXT, unique_key TEXT,
            ref_no TEXT, article_title TEXT, content TEXT,
            UNIQUE(regulation_name, reg_date, unique_key)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS regulation_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT, regulation_name TEXT, reg_date TEXT,
            clause_count INTEGER DEFAULT 0, file_size INTEGER DEFAULT 0,
            uploaded_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    
    cur.execute("SELECT 1 FROM regulation_history WHERE regulation_name=? AND reg_date=? LIMIT 1", (reg_name, reg_date))
    exists = cur.fetchone() is not None
    if exists and not force:
        conn.close()
        return "skipped", 0

    df = pd.read_csv(csv_path)
    if df.empty:
        conn.close()
        return "empty", 0

    df['unique_key'] = build_unique_keys(df)
    batch = [
        (reg_name, reg_date, row['unique_key'], row.get('참조번호', ''), row.get('조명', ''), str(row.get('내용', '')))
        for _, row in df.iterrows()
    ]
    
    if exists and force:
        cur.execute("DELETE FROM regulation_history WHERE regulation_name=? AND reg_date=?", (reg_name, reg_date))

    cur.executemany('''
        INSERT INTO regulation_history (regulation_name, reg_date, unique_key, ref_no, article_title, content)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (regulation_name, reg_date, unique_key) DO UPDATE
        SET ref_no = excluded.ref_no, article_title = excluded.article_title, content = excluded.content
    ''', batch)
    
    cur.execute(
        "INSERT INTO regulation_uploads (filename, regulation_name, reg_date, clause_count, file_size) VALUES (?, ?, ?, ?, ?)",
        (os.path.basename(csv_path), reg_name, reg_date, len(batch), os.path.getsize(csv_path))
    )
    conn.commit()
    conn.close()
    return "inserted", len(batch)


def sync_to_supabase(csv_path: str, reg_name: str, reg_date: str, db_url: str, force: bool = False):
    """Supabase PostgreSQL에 적재"""
    import psycopg2
    from psycopg2.extras import execute_values
    
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    
    cur.execute("SELECT 1 FROM regulation_history WHERE regulation_name=%s AND reg_date=%s LIMIT 1", (reg_name, reg_date))
    exists = cur.fetchone() is not None
    if exists and not force:
        conn.close()
        return "skipped", 0

    df = pd.read_csv(csv_path)
    if df.empty:
        conn.close()
        return "empty", 0

    df['unique_key'] = build_unique_keys(df)
    batch = [
        (reg_name, reg_date, row['unique_key'], row.get('참조번호', ''), row.get('조명', ''), str(row.get('내용', '')))
        for _, row in df.iterrows()
    ]
    
    if exists and force:
        cur.execute("DELETE FROM regulation_history WHERE regulation_name=%s AND reg_date=%s", (reg_name, reg_date))

    query = '''
        INSERT INTO regulation_history (regulation_name, reg_date, unique_key, ref_no, article_title, content)
        VALUES %s
        ON CONFLICT (regulation_name, reg_date, unique_key) DO UPDATE
        SET ref_no = EXCLUDED.ref_no, article_title = EXCLUDED.article_title, content = EXCLUDED.content
    '''
    execute_values(cur, query, batch, page_size=1000)
    
    cur.execute(
        "INSERT INTO regulation_uploads (filename, regulation_name, reg_date, clause_count, file_size) VALUES (%s, %s, %s, %s, %s)",
        (os.path.basename(csv_path), reg_name, reg_date, len(batch), os.path.getsize(csv_path))
    )
    conn.commit()
    conn.close()
    return "inserted", len(batch)


def run_pipeline(force: bool = False, db_url_override: str = None):
    print("=" * 65)
    print("🚀 규정 자동 동기화 파이프라인 시작 (HWP -> TXT -> CSV -> DB)")
    print("=" * 65)

    if not os.path.exists(DATA_DIR):
        print(f"❌ '{DATA_DIR}' 폴더가 존재하지 않습니다.")
        return

    hwp_files = sorted(glob.glob(os.path.join(DATA_DIR, "*.hwp")))
    print(f"📂 탐색된 HWP 파일: 총 {len(hwp_files)}개")

    # 1. HWP -> TXT 변환
    txt_conv = 0
    txt_skip = 0
    txt_err = 0
    for hwp_str in hwp_files:
        hwp_path = Path(hwp_str)
        txt_path = hwp_path.with_suffix(".txt")
        if not txt_path.exists() or (hwp_path.stat().st_mtime > txt_path.stat().st_mtime) or force:
            print(f"  [HWP->TXT] 변환 중: {hwp_path.name}")
            _, err = convert_hwp_to_txt(hwp_path)
            if err:
                print(f"    ❌ 오류: {err}")
                txt_err += 1
            else:
                txt_conv += 1
        else:
            txt_skip += 1

    print(f"📄 TXT 변환 결과: 신규/갱신 {txt_conv}개, 건너뜀 {txt_skip}개, 오류 {txt_err}개")

    # 2. TXT -> CSV 파싱
    txt_files = sorted(glob.glob(os.path.join(DATA_DIR, "*.txt")))
    csv_conv = 0
    csv_skip = 0
    csv_err = 0
    for txt_str in txt_files:
        txt_path = Path(txt_str)
        csv_path = txt_path.with_suffix(".csv")
        if not csv_path.exists() or (txt_path.stat().st_mtime > csv_path.stat().st_mtime) or force:
            print(f"  [TXT->CSV] 파싱 중: {txt_path.name}")
            try:
                text = read_source_text(str(txt_path))
                df = parse_all(text)
                df.to_csv(csv_path, index=False, encoding="utf-8-sig")
                csv_conv += 1
            except Exception as e:
                print(f"    ❌ 오류: {e}")
                csv_err += 1
        else:
            csv_skip += 1

    print(f"📊 CSV 변환 결과: 신규/갱신 {csv_conv}개, 건너뜀 {csv_skip}개, 오류 {csv_err}개")

    # 3. DB 동기화
    db_url = get_db_url(db_url_override)
    csv_files = sorted(glob.glob(os.path.join(DATA_DIR, "*.csv")))
    
    print("\n🗄️ 데이터베이스 동기화 진행:")
    if db_url:
        print("  - 온라인 Supabase PostgreSQL: 연결 설정됨 (동기화 활성화)")
    else:
        print("  - 온라인 Supabase PostgreSQL: URL 미설정 (건너뜀)")
    print(f"  - 로컬 SQLite DB: {DB_FILE}")

    pg_synced = 0
    sqlite_synced = 0
    total_pg_rows = 0
    total_sqlite_rows = 0

    for csv_str in csv_files:
        reg_name, reg_date = parse_filename_info(csv_str)
        if not reg_date:
            continue

        # SQLite 동기화
        st_res, rows = sync_to_sqlite(csv_str, reg_name, reg_date, force=force)
        if st_res == "inserted":
            sqlite_synced += 1
            total_sqlite_rows += rows
            print(f"  [SQLite 적재] {reg_name} ({reg_date}): {rows:,}건")

        # Supabase 동기화
        if db_url:
            try:
                pg_res, pg_rows = sync_to_supabase(csv_str, reg_name, reg_date, db_url, force=force)
                if pg_res == "inserted":
                    pg_synced += 1
                    total_pg_rows += pg_rows
                    print(f"  [Supabase 적재] {reg_name} ({reg_date}): {pg_rows:,}건")
            except Exception as e:
                print(f"  ❌ Supabase 적재 실패 ({reg_name} {reg_date}): {e}")

    print("\n" + "=" * 65)
    print("✅ 동기화 파이프라인 완료 보고")
    print(f"  • HWP -> TXT 변환: {txt_conv}건 신규/갱신")
    print(f"  • TXT -> CSV 파싱: {csv_conv}건 신규/갱신")
    print(f"  • 로컬 SQLite 동기화: {sqlite_synced}개 규정 ({total_sqlite_rows:,}개 조항)")
    if db_url:
        print(f"  • Supabase DB 동기화: {pg_synced}개 규정 ({total_pg_rows:,}개 조항)")
    print("=" * 65)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="규정 HWP -> TXT -> CSV -> DB 자동 동기화")
    parser.add_argument("--force", action="store_true", help="기존 데이터가 있어도 강제 재파싱 및 재적재")
    parser.add_argument("--db-url", type=str, default=None, help="Supabase DB URL (미지정 시 환경변수 또는 secrets.toml 참조)")
    args = parser.parse_args()

    run_pipeline(force=args.force, db_url_override=args.db_url)
