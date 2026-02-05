import streamlit as st
import sqlite3
import pandas as pd
import glob
import os
import re
import io

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

# =========================================================
# 2. DB 핸들링 및 최적화 함수
# =========================================================

def get_connection():
    """DB 연결 및 성능 최적화 옵션 적용"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    # [최적화] WAL(Write-Ahead Logging) 모드 활성화 : 동시성 및 속도 향상
    conn.execute("PRAGMA journal_mode=WAL;")
    # [최적화] 동기화 모드 조정 : 쓰기 속도 향상
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
    
    # [최적화 핵심] 인덱스 생성
    # 검색 조건(WHERE)에 자주 사용되는 컬럼들에 인덱스를 걸어 풀스캔 방지
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_reg_name ON regulation_history(regulation_name);",
        "CREATE INDEX IF NOT EXISTS idx_reg_date ON regulation_history(reg_date);",
        "CREATE INDEX IF NOT EXISTS idx_ref_no ON regulation_history(ref_no);",
        # 복합 인덱스 (규정명+날짜 조회용)
        "CREATE INDEX IF NOT EXISTS idx_name_date ON regulation_history(regulation_name, reg_date);"
    ]
    
    for idx_sql in indexes:
        cursor.execute(idx_sql)
        
    conn.commit()
    conn.close()

# [캐싱] 규정 목록은 자주 바뀌지 않으므로 캐싱하여 메뉴 로딩 속도 향상
@st.cache_data(ttl=3600) 
def get_regulation_names():
    if not os.path.exists(DB_FILE):
        return []
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT DISTINCT regulation_name FROM regulation_history ORDER BY regulation_name", conn)
        return df['regulation_name'].tolist()
    except:
        return []
    finally:
        conn.close()

# [캐싱] 특정 규정의 날짜 목록 캐싱
@st.cache_data(ttl=3600)
def get_regulation_dates(reg_name):
    conn = get_connection()
    try:
        dates = pd.read_sql("SELECT DISTINCT reg_date FROM regulation_history WHERE regulation_name=? ORDER BY reg_date DESC", conn, params=(reg_name,))
        return dates['reg_date'].tolist()
    finally:
        conn.close()

def parse_filename_info(filename):
    base_name = os.path.basename(filename)
    name_without_ext = os.path.splitext(base_name)[0]
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
    """증분 업데이트 로직"""
    init_db()
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
        return -1

    conn = get_connection()
    cursor = conn.cursor()
    
    # 이미 등록된 파일(규정명+날짜) 확인 (메모리 낭비 방지)
    existing = set()
    try:
        cursor.execute("SELECT DISTINCT regulation_name, reg_date FROM regulation_history")
        for row in cursor.fetchall():
            existing.add((row[0], row[1]))
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
            
            # 1000건씩 끊어서 커밋 (메모리 절약)
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
    
    # 업데이트 후 캐시 초기화
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
                # 메모리 문제 방지를 위해 LIMIT를 걸 수도 있지만, 
                # 일단 요청대로 전체를 내보내되 최적화된 연결 사용
                df = pd.read_sql(f"SELECT * FROM {t_name}", conn)
                df.to_excel(writer, sheet_name=t_name, index=False)
    except Exception:
        conn.close()
        return None

    conn.close()
    return output.getvalue()

# =========================================================
# 3. 메인 UI 구성
# =========================================================
st.set_page_config(page_title="금융 규정 검색 시스템", layout="wide", page_icon="⚡")

# 사이드바
with st.sidebar:
    st.header("⚙️ 관리 및 메뉴")
    
    if st.button("🔄 DB 업데이트 (증분)"):
        with st.spinner(f"'{DATA_DIR}' 폴더 스캔 중..."):
            cnt, skip = load_files()
        
        if cnt == -1:
            st.warning(f"폴더가 생성되었습니다. CSV 파일을 '{DATA_DIR}'에 넣어주세요.")
        else:
            st.success(f"완료! 신규 {cnt}개, {skip}개 건너뜀.")
    
    st.write("")
    # 엑셀 다운로드는 무거우므로 필요할 때만 쿼리 실행
    if st.button("📥 DB 전체 엑셀로 변환 준비"):
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

# 규정 목록 로드 (캐시 사용으로 즉시 로딩)
reg_names = get_regulation_names()
default_reg_index = 0

if PREFERRED_REG_NAME in reg_names:
    default_reg_index = reg_names.index(PREFERRED_REG_NAME)

# =========================================================
# 4. 메뉴별 로직 (SQL 최적화 적용)
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
        # 날짜 목록도 캐시 사용
        dates = get_regulation_dates(target)
        with c2: date = st.selectbox("날짜", dates) if dates else st.selectbox("날짜", [])
        
        if st.button("조회"):
            conn = get_connection()
            # [최적화] 필요한 컬럼만 지정하여 SELECT
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
            # [최적화] 인덱스(idx_name, idx_ref_no) 활용
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
            # [최적화] 필요한 컬럼만 SELECT
            q = "SELECT regulation_name, reg_date, ref_no, article_title, content FROM regulation_history WHERE (content LIKE ? OR article_title LIKE ?)"
            p = [f"%{keyword}%", f"%{keyword}%"]
            if target != "전체 규정 (All)":
                q += " AND regulation_name = ?"
                p.append(target)
            
            # [최적화] 서브쿼리 최적화
            if latest:
                # SQLite에서는 튜플 IN 절이 느릴 수 있으므로 JOIN이나 EXISTS를 쓰는 게 좋지만,
                # 여기서는 인덱스를 활용하기 위해 단순화
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
                # 결과가 많을 경우 페이징 처리를 하면 좋으나, 여기서는 상위 100개만 보여주거나 스크롤 처리
                # Streamlit은 렌더링 부하가 있으므로 너무 많으면 경고
                if len(df) > 200:
                    st.warning("⚠️ 결과가 너무 많아 일부만 표시될 수 있습니다.")
                    
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
            
            # 파트너 이름 추론
            is_rule = "시행세칙" in target_reg
            partner_reg_name = target_reg.replace(" 시행세칙", "").replace("시행세칙", "").strip() if is_rule else f"{target_reg} 시행세칙"

            # 검색어 준비
            term_internal = target_art 
            term_partner = f"세칙 {target_art}" if is_rule else f"규정 {target_art}"
            term_external = f"「{target_reg}」 {target_art}"

            # [최적화 핵심]
            # 기존: 전체 데이터를 로드 -> Python for loop (30MB 로드 -> 느림)
            # 변경: SQL에서 OR 조건으로 필터링 -> 결과만 로드 (수 KB 로드 -> 빠름)
            
            base_query = """
                SELECT regulation_name, reg_date, ref_no, article_title, content
                FROM regulation_history
                WHERE 
                   (regulation_name = ? AND content LIKE ?) OR 
                   (regulation_name LIKE ? AND content LIKE ?) OR
                   (content LIKE ?)
            """
            
            # 파트너 규정명은 부분일치(LIKE)로 잡기 위해 처리
            partner_like = f"%{partner_reg_name}%"
            
            params = [
                target_reg, f"%{term_internal}%",  # 내부
                partner_like, f"%{term_partner}%", # 짝꿍
                f"%{term_external}%"               # 외부
            ]
            
            if latest_only:
                # 최신 날짜 필터링을 위한 CTE나 서브쿼리 사용
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
            
            # Python 측에서 정밀 분류 (SQL은 OR로 가져왔으므로 섞여 있음)
            results_internal = []
            results_partner = []
            results_external = []
            
            for _, row in df_filtered.iterrows():
                curr_reg = row['regulation_name']
                content = row['content']
                
                # 내부 인용
                if curr_reg == target_reg:
                    if term_internal in content:
                        results_internal.append(row)
                # 짝꿍 인용
                elif partner_reg_name in curr_reg: 
                    if term_partner in content:
                        results_partner.append(row)
                # 외부 인용
                else:
                    if term_external in content:
                        results_external.append(row)

            # 결과 출력
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