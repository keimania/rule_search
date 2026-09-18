#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite (regulation_master.db) -> Supabase (PostgreSQL) 일괄 마이그레이션 스크립트
"""

import os
import sys
import time
import sqlite3
import psycopg2
from psycopg2.extras import execute_values

DB_FILE = "regulation_master.db"

def get_supabase_url():
    # 1. secrets.toml 확인
    secrets_path = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("SUPABASE_DB_URL"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            return parts[1].strip().strip('"').strip("'")
        except Exception:
            pass
    # 2. 환경변수 확인
    return os.environ.get("SUPABASE_DB_URL", "")

def migrate():
    url = get_supabase_url()
    if not url:
        print("오류: SUPABASE_DB_URL이 설정되지 않았습니다.")
        print("      .streamlit/secrets.toml 파일 또는 SUPABASE_DB_URL 환경변수를 확인해주세요.")
        sys.exit(1)

    if not os.path.exists(DB_FILE):
        print(f"오류: 로컬 DB 파일 '{DB_FILE}'을 찾을 수 없습니다.")
        sys.exit(1)

    print(f"1. 로컬 SQLite ('{DB_FILE}') 데이터 읽는 중...")
    t0 = time.time()
    s_conn = sqlite3.connect(DB_FILE)
    s_cur = s_conn.cursor()
    s_cur.execute("SELECT regulation_name, reg_date, unique_key, ref_no, article_title, content FROM regulation_history ORDER BY id")
    rows = s_cur.fetchall()
    s_conn.close()
    print(f"   └─ 총 {len(rows):,}건 읽기 완료 ({time.time() - t0:.2f}초)")

    print("\n2. Supabase PostgreSQL 연결 및 테이블 검증 중...")
    pg_conn = psycopg2.connect(url)
    pg_cur = pg_conn.cursor()

    # DDL
    ddl = """
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
    );
    CREATE INDEX IF NOT EXISTS idx_reg_name ON regulation_history(regulation_name);
    CREATE INDEX IF NOT EXISTS idx_reg_date ON regulation_history(reg_date);
    CREATE INDEX IF NOT EXISTS idx_ref_no ON regulation_history(ref_no);
    CREATE INDEX IF NOT EXISTS idx_name_date ON regulation_history(regulation_name, reg_date);
    """
    pg_cur.execute(ddl)
    pg_conn.commit()

    print("3. Supabase로 데이터 일괄 업로드(Batch Insert) 시작...")
    t1 = time.time()
    insert_sql = """
    INSERT INTO regulation_history 
    (regulation_name, reg_date, unique_key, ref_no, article_title, content)
    VALUES %s
    ON CONFLICT (regulation_name, reg_date, unique_key) DO NOTHING
    """
    batch_size = 5000
    total = len(rows)

    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        execute_values(pg_cur, insert_sql, batch, page_size=len(batch))
        pg_conn.commit()
        print(f"   └─ {min(i + batch_size, total):,} / {total:,} 건 처리 완료")

    pg_cur.execute("SELECT COUNT(*) FROM regulation_history")
    cnt = pg_cur.fetchone()[0]
    print(f"\n✅ 마이그레이션 완료! Supabase 총 레코드 수: {cnt:,}건 (소요 시간: {time.time() - t1:.2f}초)")
    pg_conn.close()

if __name__ == "__main__":
    migrate()
