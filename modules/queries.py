import io
import re
import pandas as pd
import streamlit as st

from modules.citation_parser import (
    get_hang_variants,
    normalize_art_input,
    get_citation_terms,
)
from modules.db_manager import (
    get_connection,
    sql_ph,
)

@st.cache_data(ttl=600)
def get_menu1_summary():
    """메뉴 1: 시스템 DB 등록 규정 요약 집계 - 캐시 적용"""
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
        return pd.read_sql(q, conn)
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu2_history(target_reg):
    """메뉴 2: 특정 규정의 개정 히스토리 조회 - 캐시 적용"""
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
        return pd.read_sql(q, conn, params=(target_reg,))
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu3_full_text(target_reg, date):
    """메뉴 3: 특정 규정 전문 조회 - 캐시 적용"""
    conn = get_connection()
    try:
        q = sql_ph('SELECT ref_no as "조항", article_title as "조명", content as "내용" FROM regulation_history WHERE regulation_name=? AND reg_date=? ORDER BY id')
        return pd.read_sql(q, conn, params=(target_reg, date))
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu4_history(target, ref):
    """메뉴 4: 조항 변경 이력 추적 - 캐시 적용 (원문자/숫자 항 상호 호환)"""
    conn = get_connection()
    try:
        variants = get_hang_variants(ref)
        if not variants:
            variants = [ref]
        conds = " OR ".join(["ref_no LIKE ?" for _ in variants])
        q = f"SELECT reg_date, ref_no, article_title, content, unique_key FROM regulation_history WHERE regulation_name=? AND ({conds}) ORDER BY unique_key, reg_date"
        params = [target] + [f"%{v}%" for v in variants]
        return pd.read_sql(sql_ph(q), conn, params=params)
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu5_detail(target, date, ref):
    """메뉴 5: 특정 시점 조항 상세 조회 - 캐시 적용 (원문자/숫자 항 상호 호환)"""
    conn = get_connection()
    try:
        variants = get_hang_variants(ref)
        if not variants:
            variants = [ref]
        conds = " OR ".join(["ref_no LIKE ?" for _ in variants])
        q = f"""
            SELECT ref_no AS "조항", article_title AS "조명", content AS "내용" 
            FROM regulation_history 
            WHERE regulation_name=? AND reg_date=? AND ({conds})
            ORDER BY id
        """
        params = [target, date] + [f"%{v}%" for v in variants]
        return pd.read_sql(sql_ph(q), conn, params=params)
    finally:
        conn.close()

def parse_search_query(query_str: str):
    """
    복합 검색어를 AND / OR / NOT 구조로 파싱.
    - '|' 또는 ' OR ' : OR 분기
    - 공백 : AND 결합
    - '-단어' : NOT 제외
    """
    raw = query_str.strip()
    if not raw:
        return []
    or_chunks = re.split(r'\s*\|\s*|\s+OR\s+', raw)
    branches = []
    for chunk in or_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        tokens = chunk.split()
        must = []
        exclude = []
        for t in tokens:
            t = t.strip()
            if not t:
                continue
            if t.startswith("-") and len(t) > 1:
                exclude.append(t[1:])
            else:
                must.append(t)
        if must or exclude:
            branches.append({"must": must, "exclude": exclude})
    return branches

@st.cache_data(ttl=600)
def get_menu6_keyword_search(target, keyword, latest):
    """메뉴 6: 통합 키워드 검색 - 캐시 적용 (복합 연산자 AND / OR / NOT 및 원문자/숫자 항 상호 호환)"""
    conn = get_connection()
    try:
        branches = parse_search_query(keyword)
        if not branches:
            branches = [{"must": [keyword], "exclude": []}]

        branch_sql_list = []
        p = []

        for branch in branches:
            sub_conds = []
            # 1. 필수 포함 단어 (AND)
            for word in branch["must"]:
                variants = get_hang_variants(word) or [word]
                word_or = " OR ".join(["(content LIKE ? OR article_title LIKE ? OR ref_no LIKE ?)" for _ in variants])
                sub_conds.append(f"({word_or})")
                for v in variants:
                    p.extend([f"%{v}%", f"%{v}%", f"%{v}%"])

            # 2. 제외 단어 (NOT)
            for ex in branch["exclude"]:
                sub_conds.append("(content NOT LIKE ? AND article_title NOT LIKE ?)")
                p.extend([f"%{ex}%", f"%{ex}%"])

            if sub_conds:
                branch_sql_list.append("(" + " AND ".join(sub_conds) + ")")

        if branch_sql_list:
            where_expr = " OR ".join(branch_sql_list)
        else:
            where_expr = "1=1"

        q = f"SELECT regulation_name, reg_date, ref_no, article_title, content FROM regulation_history WHERE ({where_expr})"
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
        return pd.read_sql(sql_ph(q), conn, params=p)
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_target_article_content(target_reg: str, target_art: str, latest_only: bool = True):
    """메뉴 7: 관심 조항 번호에 따른 원문 내용 조회 (원문자/숫자 항 상호 호환)"""
    conn = get_connection()
    try:
        clean_art = normalize_art_input(target_art)
        variants = get_hang_variants(clean_art)
        if not variants:
            variants = [clean_art]
            
        conds = []
        params_art = []
        for v in variants:
            if "의" in v or "항" in v or "호" in v:
                conds.append("(ref_no = ? OR ref_no LIKE ? OR ref_no LIKE ?)")
                params_art.extend([v, f"{v}%", f"{v}(%"])
            else:
                conds.append("(ref_no = ? OR ref_no LIKE ? OR ref_no LIKE ?)")
                params_art.extend([v, f"{v}제%", f"{v}(%"])
                
        art_condition = "(" + " OR ".join(conds) + ")"

        if latest_only:
            query = f"""
                WITH LatestDate AS (
                    SELECT MAX(reg_date) as max_date 
                    FROM regulation_history 
                    WHERE regulation_name = ?
                )
                SELECT h.reg_date, h.ref_no, h.article_title, h.content
                FROM regulation_history h
                JOIN LatestDate ld ON h.regulation_name = ? AND h.reg_date = ld.max_date
                WHERE {art_condition}
                ORDER BY h.id
            """
            params = [target_reg, target_reg] + params_art
        else:
            query = f"""
                SELECT reg_date, ref_no, article_title, content
                FROM regulation_history
                WHERE regulation_name = ? AND {art_condition}
                ORDER BY reg_date DESC, id
            """
            params = [target_reg] + params_art

        df = pd.read_sql(sql_ph(query), conn, params=params)
        return clean_art, df
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu7_reference_analysis(target_reg, target_art, latest_only):
    """메뉴 7: 조항 인용 및 역참조 분석 - 캐시 적용 (연속 조항 열거 및 지배 접두어 문맥 분석 - 31b8d53 복구)"""
    conn = get_connection()
    try:
        clean_art, partner_reg_name, terms_internal, terms_partner, terms_external = get_citation_terms(target_reg, target_art)
        variants = get_hang_variants(clean_art) or [clean_art]
        
        prefix_tbl = "h." if latest_only else ""
        art_cond = " OR ".join([f"{prefix_tbl}content LIKE ?" for _ in variants])
        
        short_name = target_reg.split()[-1]
        ext_likes = [f"%{target_reg}%", f"%「{target_reg}」%"]
        if len(short_name) >= 3 and short_name != target_reg:
            ext_likes.append(f"%{short_name}%")
        ext_cond = " OR ".join([f"{prefix_tbl}content LIKE ?" for _ in ext_likes])
        
        partner_like = f"%{partner_reg_name}%"
        
        where_clause = f"""
            ({prefix_tbl}regulation_name = ? AND ({art_cond})) OR 
            ({prefix_tbl}regulation_name LIKE ? AND ({art_cond})) OR
            (({art_cond}) AND ({ext_cond}))
        """
        
        params = (
            [target_reg] + [f"%{v}%" for v in variants] +
            [partner_like] + [f"%{v}%" for v in variants] +
            [f"%{v}%" for v in variants] + ext_likes
        )
        
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
                WHERE {where_clause}
                ORDER BY h.regulation_name, h.id
            """
        else:
            full_query = f"""
                SELECT regulation_name, reg_date, ref_no, article_title, content
                FROM regulation_history
                WHERE {where_clause}
                ORDER BY regulation_name, id
            """

        df_res = pd.read_sql(sql_ph(full_query), conn, params=params)
        return df_res, partner_reg_name, clean_art, variants
    finally:
        conn.close()

# =========================================================
# 엑셀/CSV 데이터 내보내기 헬퍼 함수
# =========================================================
def export_dataframe_to_excel(df: pd.DataFrame, sheet_name: str = "검색결과") -> bytes:
    """단일 DataFrame을 엑셀(.xlsx) 바이너리 스트림으로 변환"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name=sheet_name[:30], index=False)
    return output.getvalue()

def export_menu7_to_excel(df_target: pd.DataFrame, results_internal: list, results_partner: list, results_external: list, target_reg: str, clean_art: str, partner_reg_name: str) -> bytes:
    """메뉴 7 역참조 분석 결과를 다중 시트 엑셀(.xlsx) 파일로 변환"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        has_sheet = False
        # 시트 1: 관심 조항 원문
        if not df_target.empty:
            df_target.to_excel(writer, sheet_name="관심조항원문", index=False)
            has_sheet = True
            
        # 시트 2: 내부 참조
        if results_internal:
            df_int = pd.DataFrame([r[0] for r in results_internal])
            df_int.to_excel(writer, sheet_name="내부참조", index=False)
            has_sheet = True
            
        # 시트 3: 파트너 규정 참조
        if results_partner:
            df_part = pd.DataFrame([r[0] for r in results_partner])
            df_part.to_excel(writer, sheet_name="파트너참조", index=False)
            has_sheet = True
            
        # 시트 4: 타 규정 참조
        if results_external:
            df_ext = pd.DataFrame([r[0] for r in results_external])
            df_ext.to_excel(writer, sheet_name="타규정참조", index=False)
            has_sheet = True

        if not has_sheet:
            pd.DataFrame([{"안내": "검색 결과가 없습니다."}]).to_excel(writer, sheet_name="결과없음", index=False)
            
    return output.getvalue()
