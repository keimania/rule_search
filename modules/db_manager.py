import glob
import io
import os
import re
import sqlite3
import unicodedata
import urllib.parse
from pathlib import Path
import pandas as pd
import streamlit as st

from modules.config import DB_FILE, DATA_DIR, HAS_PYHWP
from modules.hwp_converter import (
    convert_single_hwp_to_txt,
    read_source_text,
    parse_all,
)

# 전역 DB 연결 상태 진단 객체
DB_DIAGNOSTICS = {
    "supabase_configured": False,
    "supabase_connected": False,
    "connected_url": None,
    "connected_host": None,
    "project_ref": None,
    "error_message": None,
    "candidate_results": []
}

def mask_db_url(url: str) -> str:
    """보안을 위해 URL 내 비밀번호 마스킹"""
    if not url:
        return ""
    return re.sub(r":([^@]+)@", r":****@", url)

def extract_host_from_url(url: str) -> str:
    """URL에서 호스트 및 포트 추출"""
    if not url:
        return ""
    m = re.search(r"@([^/]+)", url)
    return m.group(1) if m else "Supabase Cloud"

def normalize_pg_url(url: str) -> str:
    """PostgreSQL URL 내 비밀번호의 특수문자(%21, %40 등) 안전 인코딩 및 정규화"""
    if not url or not url.startswith("postgresql://"):
        return url
    prefix = "postgresql://"
    rest = url[len(prefix):]
    if "@" not in rest:
        return url
    last_at = rest.rfind("@")
    userinfo = rest[:last_at]
    hostinfo = rest[last_at + 1:]
    
    if ":" in userinfo:
        user, pwd = userinfo.split(":", 1)
        decoded_pwd = urllib.parse.unquote(pwd)
        encoded_pwd = urllib.parse.quote(decoded_pwd, safe="")
        return f"{prefix}{user}:{encoded_pwd}@{hostinfo}"
    return url

def get_candidate_db_urls(raw_url: str) -> list:
    """다양한 연결 형태(세션 풀러, 직접 연결, 트랜잭션 풀러 등)의 후보 URL 목록 생성"""
    if not raw_url or not raw_url.startswith("postgresql://"):
        return [raw_url] if raw_url else []
    
    prefix = "postgresql://"
    rest = raw_url[len(prefix):]
    if "@" not in rest:
        return [raw_url]
    
    last_at = rest.rfind("@")
    userinfo = rest[:last_at]
    host_db = rest[last_at + 1:]
    
    if "/" not in host_db:
        return [raw_url]
    
    host_port, dbname = host_db.split("/", 1)
    if ":" in host_port:
        host, port = host_port.split(":", 1)
    else:
        host, port = host_port, "5432"
        
    if ":" in userinfo:
        user, pwd = userinfo.split(":", 1)
        encoded_pwd = urllib.parse.quote(urllib.parse.unquote(pwd), safe="")
    else:
        user = userinfo
        encoded_pwd = ""
        
    pwd_part = f":{encoded_pwd}" if encoded_pwd else ""
    
    # Supabase 프로젝트 Ref 식별
    project_ref = None
    if "." in user:
        project_ref = user.split(".", 1)[1]
    elif "supabase.co" in host:
        m = re.search(r"(?:db\.)?([a-z0-9]+)\.supabase\.co", host)
        if m:
            project_ref = m.group(1)
            
    candidates = []
    # 1. 정규화된 원본 URL (최우선 시도)
    candidates.append(f"{prefix}{user}{pwd_part}@{host_port}/{dbname}")
    
    # 2. Supabase 프로젝트인 경우 연결 후보 다각화
    if project_ref:
        # 후보 A: 도쿄 세션 풀러 (Session mode, IPv4 지원, 포트 5432)
        candidates.append(f"{prefix}postgres.{project_ref}{pwd_part}@aws-0-ap-northeast-1.pooler.supabase.com:5432/{dbname}")
        # 후보 B: 도쿄 트랜잭션 풀러 (Transaction mode, IPv4 지원, 포트 6543)
        candidates.append(f"{prefix}postgres.{project_ref}{pwd_part}@aws-0-ap-northeast-1.pooler.supabase.com:6543/{dbname}")
        # 후보 C: 직접 연결 (Direct connection, 포트 5432)
        candidates.append(f"{prefix}postgres{pwd_part}@db.{project_ref}.supabase.co:5432/{dbname}")
        # 후보 D: 서울 세션 풀러 (서울 리전 풀러 포트 5432)
        candidates.append(f"{prefix}postgres.{project_ref}{pwd_part}@aws-0-ap-northeast-2.pooler.supabase.com:5432/{dbname}")
        
    # 중복 제거 (순서 보존)
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result

def get_db_url():
    """Supabase PostgreSQL 연결 URL 확인 (Streamlit secrets -> 환경 변수 -> secrets.toml)"""
    # 1. Streamlit secrets 확인
    try:
        for key in ["SUPABASE_DB_URL", "DATABASE_URL", "POSTGRES_URL"]:
            if key in st.secrets and st.secrets[key]:
                return str(st.secrets[key]).strip()
        if "database" in st.secrets and "url" in st.secrets["database"] and st.secrets["database"]["url"]:
            return str(st.secrets["database"]["url"]).strip()
        if "connections" in st.secrets:
            conns = st.secrets["connections"]
            if "supabase" in conns and "url" in conns["supabase"] and conns["supabase"]["url"]:
                return str(conns["supabase"]["url"]).strip()
            if "postgresql" in conns and "url" in conns["postgresql"] and conns["postgresql"]["url"]:
                return str(conns["postgresql"]["url"]).strip()
    except Exception:
        pass
        
    # 2. OS 환경 변수 확인
    for env_key in ["SUPABASE_DB_URL", "DATABASE_URL", "POSTGRES_URL"]:
        env_val = os.environ.get(env_key, "").strip()
        if env_val:
            return env_val
        
    # 3. 로컬 .streamlit/secrets.toml 직접 탐색
    secrets_path = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
    if secrets_path.exists():
        try:
            try:
                import tomllib
                with open(secrets_path, "rb") as f:
                    s_data = tomllib.load(f)
            except ImportError:
                import toml
                s_data = toml.load(str(secrets_path))
            for key in ["SUPABASE_DB_URL", "DATABASE_URL", "POSTGRES_URL"]:
                if key in s_data and s_data[key]:
                    return str(s_data[key]).strip()
            if "database" in s_data and "url" in s_data["database"] and s_data["database"]["url"]:
                return str(s_data["database"]["url"]).strip()
        except Exception:
            pass
            
    return ""

def to_sqlalchemy_url(url: str) -> str:
    """SQLAlchemy dialect driver 지정 (psycopg2 우선, 없으면 psycopg3)"""
    if not url:
        return url
    if url.startswith("postgresql://"):
        try:
            import psycopg2
            return url.replace("postgresql://", "postgresql+psycopg2://", 1)
        except ImportError:
            try:
                import psycopg
                return url.replace("postgresql://", "postgresql+psycopg://", 1)
            except ImportError:
                return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url

@st.cache_resource(show_spinner=False)
def _connect_postgres_cached(candidate_url: str):
    """지정된 URL로 커넥션 풀을 생성하고 실제 핑(ping) 테스트를 거쳐 캐싱"""
    from sqlalchemy import create_engine, text
    sa_url = to_sqlalchemy_url(candidate_url)
    engine = create_engine(
        sa_url,
        pool_size=5,
        max_overflow=10,
        pool_timeout=8,
        pool_recycle=1800,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5}
    )
    with engine.connect() as conn:
        conn.execute(text("SELECT 1;"))
    return engine

def get_db_engine():
    """Supabase 연결 후보들을 순차적으로 검증하여 연결 가능한 최적의 엔진을 반환 (연결 가능 시 무조건 Supabase 우선)"""
    primary_url = get_db_url()
    if not primary_url:
        DB_DIAGNOSTICS["supabase_configured"] = False
        DB_DIAGNOSTICS["supabase_connected"] = False
        DB_DIAGNOSTICS["error_message"] = "SUPABASE_DB_URL이 설정되지 않았습니다."
        return None

    DB_DIAGNOSTICS["supabase_configured"] = True
    
    # 프로젝트 Ref 식별
    ref_match = re.search(r"postgres(?:\.([a-z0-9]+))?:[^@]+@(?:db\.([a-z0-9]+)\.supabase\.co|[a-z0-9.-]+)", primary_url)
    if ref_match:
        DB_DIAGNOSTICS["project_ref"] = ref_match.group(1) or ref_match.group(2)
        
    candidates = get_candidate_db_urls(primary_url)
    last_error = None
    results = []

    for cand_url in candidates:
        masked = mask_db_url(cand_url)
        try:
            engine = _connect_postgres_cached(cand_url)
            DB_DIAGNOSTICS["supabase_connected"] = True
            DB_DIAGNOSTICS["connected_url"] = cand_url
            DB_DIAGNOSTICS["connected_host"] = extract_host_from_url(cand_url)
            DB_DIAGNOSTICS["error_message"] = None
            return engine
        except Exception as e:
            err_str = str(e).strip()
            first_err_line = err_str.split("\n")[0]
            if "could not translate host name" in err_str:
                err_summary = "DNS 조회 실패 (호스트 주소를 찾을 수 없음)"
            elif "tenant/user" in err_str and "not found" in err_str:
                err_summary = f"프로젝트(Tenant) 미발견 ({DB_DIAGNOSTICS['project_ref']}) — 일시정지(Paused) 상태 가능성 높음"
            elif "password authentication failed" in err_str:
                err_summary = "비밀번호 인증 실패"
            elif "Connection refused" in err_str:
                err_summary = "연결 거부 (포트 닫힘)"
            else:
                err_summary = first_err_line[:80]
            results.append((masked, False, err_summary))
            last_error = err_str

    DB_DIAGNOSTICS["supabase_connected"] = False
    DB_DIAGNOSTICS["error_message"] = last_error
    DB_DIAGNOSTICS["candidate_results"] = results
    return None

def is_postgres():
    return get_db_engine() is not None

def sql_ph(query: str) -> str:
    """PostgreSQL에서는 %s, SQLite에서는 ? 로 플레이스홀더 변환"""
    if is_postgres():
        return query.replace("?", "%s")
    return query

def get_connection():
    """커넥션 풀에서 DB 연결 대여 (PostgreSQL) 또는 로컬 SQLite 연결 반환"""
    engine = get_db_engine()
    if engine is not None:
        try:
            return engine.raw_connection()
        except Exception:
            pass
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def clear_db_cache():
    """DB 데이터 변경(업로드/삭제/동기화) 시 Streamlit 읽기 캐시 전체 무효화"""
    st.cache_data.clear()

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
    if is_postgres():
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
            indexes.extend([
                "CREATE INDEX IF NOT EXISTS idx_reg_content_trgm ON regulation_history USING gin (content gin_trgm_ops);",
                "CREATE INDEX IF NOT EXISTS idx_reg_title_trgm ON regulation_history USING gin (article_title gin_trgm_ops);",
                "CREATE INDEX IF NOT EXISTS idx_reg_ref_no_trgm ON regulation_history USING gin (ref_no gin_trgm_ops);"
            ])
        except Exception:
            pass
    for idx_sql in indexes:
        cursor.execute(idx_sql)
    clean_legacy_regulation_names(conn)
    conn.commit()
    conn.close()

# =========================================================
# SQLite 데이터베이스 최적화 (VACUUM 및 PRAGMA optimize)
# =========================================================
def get_sqlite_file_size() -> float:
    """로컬 SQLite 파일 크기(MB 단위) 반환"""
    if os.path.exists(DB_FILE):
        return round(os.path.getsize(DB_FILE) / (1024 * 1024), 2)
    return 0.0

def optimize_sqlite_db():
    """로컬 SQLite DB에 대해 VACUUM 및 PRAGMA optimize를 수행하여 파일 용량을 즉시 회수하고 인덱스 최적화"""
    if not os.path.exists(DB_FILE):
        return {"status": "error", "message": f"'{DB_FILE}' 파일이 존재하지 않습니다."}
    
    before_bytes = os.path.getsize(DB_FILE)
    try:
        conn = sqlite3.connect(DB_FILE)
        # WAL 체크포인트 후 VACUUM 및 optimize 실행
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("VACUUM;")
        conn.execute("PRAGMA optimize;")
        conn.close()
        after_bytes = os.path.getsize(DB_FILE)
        saved_bytes = max(0, before_bytes - after_bytes)
        return {
            "status": "success",
            "before_mb": round(before_bytes / (1024 * 1024), 2),
            "after_mb": round(after_bytes / (1024 * 1024), 2),
            "saved_mb": round(saved_bytes / (1024 * 1024), 2),
            "saved_bytes": saved_bytes
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@st.cache_data(ttl=600)
def get_db_stats():
    """실시간 DB 통계 조회 (총 규정 수, 총 레코드 수) - 캐시 적용"""
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

@st.cache_data(ttl=600)
def get_regulation_names():
    """DB 실데이터 기준 고유 규정명 목록 조회 - 캐시 적용"""
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT DISTINCT regulation_name FROM regulation_history ORDER BY regulation_name", conn)
        return df['regulation_name'].tolist()
    except Exception:
        return []
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_regulation_dates(reg_name):
    """DB 실데이터 기준 특정 규정의 개정일자 목록 조회 - 캐시 적용"""
    conn = get_connection()
    try:
        q = sql_ph("SELECT DISTINCT reg_date FROM regulation_history WHERE regulation_name=? ORDER BY reg_date DESC")
        dates = pd.read_sql(q, conn, params=(reg_name,))
        return dates['reg_date'].tolist()
    except Exception:
        return []
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_clause_count(reg_name, reg_date):
    """특정 규정/개정일자의 등록 조항 수 조회 - 캐시 적용"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql_ph("SELECT COUNT(*) FROM regulation_history WHERE regulation_name=? AND reg_date=?"), (reg_name, reg_date))
        r = cur.fetchone()
        return r[0] if r else 0
    except Exception:
        return 0
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
        for row in cursor.fetchall():
            existing.add((row[0], row[1]))
    except Exception:
        pass

    files = glob.glob(os.path.join(DATA_DIR, "*.csv"))
    count = 0
    skipped = 0
    batch_data = []
    
    for filepath in files:
        reg_name, reg_date = parse_filename_info(filepath)
        if not reg_date:
            continue

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
    if count > 0:
        clear_db_cache()
    
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
                if t_name == 'sqlite_sequence':
                    continue
                df = pd.read_sql(f'SELECT * FROM "{t_name}" ORDER BY id', conn)
                df.to_excel(writer, sheet_name=t_name, index=False)
    except Exception:
        conn.close()
        return None

    conn.close()
    return output.getvalue()

# =========================================================
# 파일 업로드 자동 처리 파이프라인
# =========================================================
def validate_regulation_filename(filename: str):
    """업로드된 파일명이 DB 등록 요건(8자리 개정일자 포함)을 만족하는지 검증."""
    name = unicodedata.normalize('NFC', os.path.splitext(os.path.basename(filename))[0])
    if not re.search(r'\d{8}', name):
        return False, "파일명에 개정일자(YYYYMMDD, 8자리 숫자)가 없습니다. (예: 규정명_전문_20240315.hwp)"
    return True, ""

validate_hwp_filename = validate_regulation_filename

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

        df['unique_key'] = build_unique_keys(df)

        batch = [
            (reg_name, reg_date, row['unique_key'],
             row.get('참조번호', ''), row.get('조명', ''), str(row.get('내용', '')))
            for _, row in df.iterrows()
        ]

        if is_existing and overwrite:
            cursor.execute(
                sql_ph("DELETE FROM regulation_history WHERE regulation_name=? AND reg_date=?"),
                (reg_name, reg_date)
            )

        _insert_history_batch(cursor, batch)

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
        clear_db_cache()
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

    ok, msg = validate_regulation_filename(filename)
    if not ok:
        result.update(status="error", message=msg)
        return result

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

process_uploaded_hwp = process_uploaded_file

@st.cache_data(ttl=600)
def get_recent_uploads(limit: int = 10):
    """DB에 영구 기록된 최근 업로드 이력 조회 - 캐시 적용"""
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
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            sql_ph("SELECT COUNT(*) FROM regulation_history WHERE regulation_name=? AND reg_date=?"),
            (reg_name, reg_date)
        )
        row = cursor.fetchone()
        count = row[0] if row else 0
        if count == 0:
            conn.close()
            return {"status": "error", "message": "해당 규정 및 일자의 데이터가 DB에 존재하지 않습니다."}

        cursor.execute(
            sql_ph("DELETE FROM regulation_history WHERE regulation_name=? AND reg_date=?"),
            (reg_name, reg_date)
        )
        try:
            cursor.execute(
                sql_ph("DELETE FROM regulation_uploads WHERE regulation_name=? AND reg_date=?"),
                (reg_name, reg_date)
            )
        except Exception:
            pass

        conn.commit()
        clear_db_cache()

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
    """'규정' 폴더의 .hwp 파일 중 DB에 누락된 규정을 감지하여 자동으로 변환 및 DB 적재."""
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
def ensure_db_initialized(is_pg_mode: bool):
    """앱 기동 시 DB 초기화, 레거시 데이터 정규화, 누락된 HWP 파일 자동 동기화 1회 보장"""
    init_db()
    ins, err, items = auto_sync_missing_hwps()
    return {"inserted": ins, "errors": err, "items": items}
