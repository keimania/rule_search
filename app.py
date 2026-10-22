import os
import glob
import streamlit as st
import pandas as pd
import warnings

warnings.filterwarnings('ignore', message='.*pandas only supports SQLAlchemy connectable.*')

# =========================================================
# 모듈 임포트 및 하위 호환성 Re-export
# =========================================================
from modules.config import (
    DB_FILE,
    DATA_DIR,
    MENU_NAMES,
    PREFERRED_REG_NAME,
    DEFAULT_ART_NO,
    FILENAME_GUIDE,
    FILENAME_EXAMPLE,
    ARTICLE_ID_PATTERN,
    HO_PATTERN,
    HANG_PATTERN,
    MOK_PATTERN,
    CHAPTER_PATTERN,
    SECTION_PATTERN,
    CIRCLED_TO_ARABIC,
    ARABIC_TO_CIRCLED,
    HAS_PYHWP,
)

from modules.citation_parser import (
    to_circled_hang,
    to_arabic_hang,
    normalize_art_input,
    get_hang_variants,
    get_citation_terms,
    highlight_terms,
    is_external_prefixed,
    parse_clause_citations,
    match_article,
    classify_reference,
    find_valid_internal_matches,
    highlight_spans,
    generate_inline_diff_html,
    extract_outgoing_citations,
    is_substantive_change,
)

from modules.hwp_converter import (
    convert_hwp_to_txt_st,
    convert_single_hwp_to_txt,
    read_source_text,
    clean_text,
    parse_moks,
    format_ho_ref,
    parse_h_block,
    parse_article_with_hang,
    parse_article_no_hang,
    parse_article,
    parse_all,
    convert_txt_files_to_csv,
)

from modules.db_manager import (
    DB_DIAGNOSTICS,
    mask_db_url,
    extract_host_from_url,
    normalize_pg_url,
    get_candidate_db_urls,
    get_db_url,
    to_sqlalchemy_url,
    _connect_postgres_cached,
    get_db_engine,
    is_postgres,
    sql_ph,
    get_connection,
    clear_db_cache,
    _insert_history_batch,
    normalize_regulation_name,
    clean_legacy_regulation_names,
    init_db,
    get_sqlite_file_size,
    optimize_sqlite_db,
    get_db_stats,
    get_regulation_names,
    get_regulation_dates,
    get_clause_count,
    parse_filename_info,
    generate_key,
    build_unique_keys,
    load_files,
    export_db_to_excel,
    validate_regulation_filename,
    validate_hwp_filename,
    load_single_csv,
    process_uploaded_file,
    process_uploaded_hwp,
    get_recent_uploads,
    delete_regulation_revision,
    auto_sync_missing_hwps,
    ensure_db_initialized,
)

from modules.queries import (
    get_menu1_summary,
    get_menu2_history,
    get_menu3_full_text,
    get_menu4_history,
    get_menu5_detail,
    get_menu6_keyword_search,
    get_target_article_content,
    get_menu7_reference_analysis,
    parse_search_query,
    export_dataframe_to_excel,
    export_menu7_to_excel,
)

# =========================================================
# 메인 UI 구성
# =========================================================
st.set_page_config(page_title="금융 규정 검색 시스템", layout="wide", page_icon="⚡")

# 앱 구동 시 DB 연결 및 스키마 검증/레거시 데이터 정규화 및 누락 HWP 자동 동기화 1회 자동 실행
sync_result = ensure_db_initialized(is_postgres())
db_stats = get_db_stats()

with st.sidebar:
    st.subheader("🗄️ 데이터베이스 연결 현황")
    if is_postgres():
        st.success("🟢 **온라인 DB (Supabase PostgreSQL)**")
        conn_host = DB_DIAGNOSTICS.get("connected_host") or "Supabase Cloud"
        st.caption(f"✅ 클라우드 연결됨 (`{conn_host}`) — 업로드/변경 시 데이터가 영구 보존됩니다.")
    else:
        if DB_DIAGNOSTICS.get("supabase_configured"):
            st.error("🔴 **Supabase 연결 불가 (로컬 SQLite 대체 작동 중)**")
            err_msg = DB_DIAGNOSTICS.get("error_message") or "원인 미상"
            project_ref = DB_DIAGNOSTICS.get("project_ref")
            
            st.warning(f"⚠️ **실패 사유**: {err_msg}")
            
            with st.expander("🛠️ 해결 방법 (프로젝트 복원 안내)", expanded=True):
                if project_ref:
                    dashboard_url = f"https://supabase.com/dashboard/project/{project_ref}"
                    st.markdown(
                        f"1. **Supabase 프로젝트 일시정지 확인**:\n"
                        f"   Supabase 무료 티어는 7일간 접속이 없으면 자동으로 인스턴스가 일시정지(Paused)됩니다.\n\n"
                        f"2. 👉 **[Supabase 프로젝트 복원 바로가기]({dashboard_url})**\n"
                        f"   대시보드에서 **[Restore project]** 버튼을 클릭하시면 1~2분 내로 활성화됩니다.\n\n"
                        f"3. 활성화 후 아래 버튼을 클릭하면 즉시 온라인 모드로 재연결됩니다."
                    )
                else:
                    st.markdown(
                        "1. Supabase 대시보드에서 프로젝트 상태를 확인해주세요.\n\n"
                        "2. `.streamlit/secrets.toml`의 `SUPABASE_DB_URL` 정보를 확인해주세요."
                    )
                
                if st.button("🔄 Supabase 다시 연결", type="primary", use_container_width=True):
                    _connect_postgres_cached.clear()
                    ensure_db_initialized.clear()
                    clear_db_cache()
                    st.rerun()
            st.caption("ℹ️ 현재는 로컬 DB(`regulation_master.db`)를 읽어 정상 검색이 가능합니다. (새로 추가한 데이터는 로컬에만 반영됨)")
        else:
            st.warning("📁 **로컬 DB (SQLite)**")
            st.caption("⚠️ SUPABASE_DB_URL이 설정되지 않아 로컬 SQLite 모드로 동작합니다. 영구 저장을 위해 Secrets에 URL을 등록해주세요.")

    # 실데이터 통계 및 로컬 DB 용량 표시
    sqlite_mb = get_sqlite_file_size()
    st.caption(f"📊 실데이터: **{db_stats['reg_count']}개 규정** / **{db_stats['row_count']:,}건 조항**")
    if sqlite_mb > 0:
        if sqlite_mb >= 50.0:
            st.caption(f"💾 로컬 DB 파일: **{sqlite_mb} MB** ⚠️ (권장 용량 50MB 초과)")
        else:
            st.caption(f"💾 로컬 DB 파일: **{sqlite_mb} MB**")
            
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

                target_count = get_clause_count(del_reg_name, del_reg_date)
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

        # --- DB 용량 최적화 (VACUUM) ---
        st.markdown("**(5) 로컬 DB 용량 최적화 (VACUUM)**")
        st.caption("데이터 적재/삭제 후 남은 빈 공간을 정리하여 파일 크기를 압축하고 인덱스를 최적화합니다.")
        cur_size = get_sqlite_file_size()
        st.write(f"현재 파일 크기: **{cur_size} MB**")
        if st.button("🧹 로컬 DB 즉시 최적화 (압축)"):
            with st.spinner("SQLite VACUUM 및 인덱스 최적화 진행 중..."):
                opt_res = optimize_sqlite_db()
            if opt_res.get("status") == "success":
                st.success(
                    f"✅ 최적화 완료! 용량 변화: {opt_res['before_mb']} MB ➜ **{opt_res['after_mb']} MB** "
                    f"(절감: {opt_res['saved_mb']} MB)"
                )
                st.rerun()
            else:
                st.error(f"최적화 실패: {opt_res.get('message')}")

        st.write("")
        st.markdown("---")
        st.markdown("**(6) 데이터 백업 및 내보내기**")
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
# 메뉴별 렌더링 로직
# =========================================================

if menu == MENU_NAMES["1"]:
    st.subheader("📂 시스템 DB 등록 규정 목록 (실시간 실데이터)")
    st.caption("※ 본 목록은 '규정' 폴더의 파일 목록이 아닌, **실제 데이터베이스(DB)에 적재된 실데이터**를 집계하여 실시간으로 표시합니다.")
    df_summary = get_menu1_summary()
    if not df_summary.empty:
        st.dataframe(df_summary, width='stretch', hide_index=True)
        st.caption(f"📊 총 {len(df_summary)}개 규정 / {df_summary['총 조항(레코드) 수'].sum():,}건 조항 등록됨")
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다. 사이드바의 '참고 자료 관리'에서 HWP/CSV를 DB에 등록해주세요.")

elif menu == MENU_NAMES["2"]:
    st.subheader("📅 규정별 개정 히스토리 (실시간 실데이터)")
    st.caption("※ 선택한 규정의 **실제 DB 적재 개정본** 목록 및 각 개정본별 조항 수를 실시간 표시합니다.")
    if reg_names:
        target = st.selectbox("규정 선택", reg_names, index=default_reg_index)
        df_dates = get_menu2_history(target)
        st.write(f"**{target}** 개정일 목록 (DB 실데이터 기준 총 {len(df_dates)}개 개정본):")
        st.dataframe(df_dates, width='stretch', hide_index=True)
    else:
        st.info("데이터가 없습니다.")

elif menu == MENU_NAMES["3"]:
    st.subheader("📖 규정 전문 조회")
    if reg_names:
        c1, c2 = st.columns(2)
        with c1:
            target = st.selectbox("규정", reg_names, index=default_reg_index)
        dates = get_regulation_dates(target)
        with c2:
            date = st.selectbox("날짜", dates) if dates else st.selectbox("날짜", [])
        
        if st.button("조회"):
            df = get_menu3_full_text(target, date)
            st.dataframe(df, width='stretch', height=600)
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

elif menu == MENU_NAMES["4"]:
    st.subheader("🕰️ 조항 변경 이력 추적")
    st.caption("조항 번호를 입력하여 역대 개정 이력과 개정 전·후 신구대조표(Inline Diff)를 조회합니다.")
    if reg_names:
        c1, c2 = st.columns(2)
        with c1:
            target = st.selectbox("규정", reg_names, index=default_reg_index)
        with c2:
            ref = st.text_input("조항 번호", value=DEFAULT_ART_NO)
        
        if st.button("히스토리 검색"):
            df = get_menu4_history(target, ref)
            if df.empty:
                st.warning("결과가 없습니다.")
            else:
                for r_no, group in df.groupby('ref_no'):
                    with st.expander(f"📌 {r_no} ({group.iloc[0]['article_title']})", expanded=True):
                        prev = None
                        for _, row in group.iterrows():
                            if prev is None:
                                badge, color = "🆕 신설", "blue"
                            elif is_substantive_change(prev, row['content']):
                                badge, color = "✏️ 변경", "orange"
                            else:
                                badge, color = "─ 유지", "grey"
                            st.markdown(f":{color}[**[{row['reg_date']}] {badge}**]")
                            if badge == "✏️ 변경":
                                diff_html = generate_inline_diff_html(prev, row['content'])
                                st.markdown(
                                    f"<div style='background-color:#f8f9fa; padding:12px; border-radius:6px; border:1px solid #e9ecef; margin-bottom:8px; line-height:1.6;'>{diff_html}</div>",
                                    unsafe_allow_html=True
                                )
                                with st.expander("📄 개정 후 전문 보기", expanded=False):
                                    st.caption(row['content'])
                            elif badge == "🆕 신설":
                                st.markdown(f"> {row['content']}")
                            else:
                                st.caption(row['content'])
                            st.divider()
                            prev = row['content']
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

elif menu == MENU_NAMES["5"]:
    st.subheader("🔎 특정 시점 조항 상세 조회")
    if reg_names:
        c1, c2, c3 = st.columns(3)
        with c1:
            target = st.selectbox("규정", reg_names, index=default_reg_index)
        dates = get_regulation_dates(target)
        with c2:
            date = st.selectbox("날짜", dates) if dates else st.selectbox("날짜", [])
        with c3:
            ref = st.text_input("조항 번호", value=DEFAULT_ART_NO)
        
        if st.button("조회"):
            df = get_menu5_detail(target, date, ref)
            st.table(df)
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

elif menu == MENU_NAMES["6"]:
    st.subheader("🔍 통합 키워드 검색")
    st.caption("💡 **복합 검색 연산자 지원**: 공백(AND) · `|` (OR) · `-` (NOT 제외) (예: `공매도 호가` / `거래정지 | 중단` / `정리매매 -세칙`)")
    if reg_names:
        with st.form("keyword_search_form", border=False):
            c1, c2 = st.columns([1, 2])
            with c1:
                target = st.selectbox("대상", ["전체 규정 (All)"] + reg_names, index=0)
                latest = st.checkbox("최신 규정만", value=True)
            with c2:
                keyword = st.text_input("검색어", placeholder="예: 공매도 호가 / 거래정지 | 중단")
                btn = st.form_submit_button("검색", type="primary")

        if btn and keyword:
            df = get_menu6_keyword_search(target, keyword, latest)
            if df.empty:
                st.warning("결과 없음")
            else:
                st.success(f"총 {len(df)}건 검색됨")
                
                # 엑셀 / CSV 다운로드 버튼
                col_dl1, col_dl2 = st.columns(2)
                safe_kw = keyword.replace(" ", "_").replace("|", "OR").replace("-", "NOT")
                with col_dl1:
                    excel_bytes = export_dataframe_to_excel(df, sheet_name="통합키워드검색")
                    st.download_button(
                        label="📥 검색결과 엑셀(.xlsx) 다운로드",
                        data=excel_bytes,
                        file_name=f"검색결과_{safe_kw}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                with col_dl2:
                    csv_bytes = df.to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="📥 검색결과 CSV 다운로드",
                        data=csv_bytes,
                        file_name=f"검색결과_{safe_kw}.csv",
                        mime="text/csv",
                        use_container_width=True
                    )

                if len(df) > 200:
                    st.warning("⚠️ 결과가 너무 많아 일부만 표시될 수 있습니다.")
                    
                branches = parse_search_query(keyword)
                kw_variants = []
                for b in branches:
                    for word in b.get("must", []):
                        kw_variants.extend(get_hang_variants(word) or [word])

                for _, row in df.iterrows():
                    with st.container(border=True):
                        st.markdown(f"**📌 [{row['regulation_name']}] {row['ref_no']} {row['article_title']}** :grey[{row['reg_date']}]")
                        st.markdown(highlight_terms(row['content'], kw_variants, "red"))
        elif btn and not keyword:
            st.warning("검색어를 입력해주세요.")
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")

elif menu == MENU_NAMES["7"]:
    st.subheader("🔗 조항 인용 및 역참조 분석")
    st.caption("특정 규정의 조항이 내/외부에서 어떻게 인용되고 있는지(역참조), 그리고 해당 조항이 무엇을 참조하고 있는지(정방향) 종합 분석합니다.")
    
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
            # 1. 관심 조항 원문 내용 먼저 조회 및 표시
            clean_art, df_target = get_target_article_content(target_reg, target_art, latest_only)
            
            st.markdown(f"### 📖 [관심 조항 원문] {target_reg} {clean_art}")
            if not df_target.empty:
                first_title = df_target.iloc[0]['article_title']
                reg_date_val = df_target.iloc[0]['reg_date']
                title_suffix = f" ({first_title})" if first_title else ""
                
                with st.container(border=True):
                    st.markdown(f"#### 📌 **[{target_reg}] {clean_art}{title_suffix}** :grey[[시행일자: {reg_date_val}]]")
                    st.markdown("---")
                    for _, row in df_target.iterrows():
                        sub_ref = row['ref_no']
                        st.markdown(f"**{sub_ref}**")
                        st.markdown(f"> {row['content']}")
                        st.markdown("")

                # 1-1. 정방향 참조 (해당 조항이 인용하고 있는 조항들) 추출 및 팝오버 뷰어
                combined_content = "\n".join(df_target['content'].tolist())
                outgoing_list = extract_outgoing_citations(combined_content, curr_reg=target_reg)
                if outgoing_list:
                    st.markdown("##### ↗️ **[정방향 참조] 본 조항이 인용하고 있는 타 규정 및 조항**")
                    badge_strs = [f"`{item['full_label']}`" for item in outgoing_list]
                    st.markdown(" ".join(badge_strs))
                    with st.popover(f"🔍 정방향 인용 조항 상세 목록 ({len(outgoing_list)}건)"):
                        st.markdown(f"**[{target_reg} {clean_art}] 본문에서 인용 중인 조항 목록**")
                        out_df = pd.DataFrame([
                            {"구분/접두어": item["prefix"], "인용 조항": item["clause"], "원문 표기": item["raw"]}
                            for item in outgoing_list
                        ])
                        st.dataframe(out_df, hide_index=True, use_container_width=True)
            else:
                st.warning(f"⚠️ 선택하신 규정({target_reg})에서 '{clean_art}' 조항의 원문을 찾을 수 없습니다. (조항 번호를 확인해주세요)")

            st.markdown("---")
            
            # 2. 이어서 이를 참조하는 내역(기존 인용 분석 결과) 표시
            st.markdown(f"### 🔗 [{clean_art}] 인용 및 역참조 분석 결과")
            df_filtered, partner_reg_name, target_art_clean, variants = get_menu7_reference_analysis(target_reg, clean_art, latest_only)
            
            results_internal, results_partner, results_external = [], [], []
            
            for _, row in df_filtered.iterrows():
                curr_reg = row['regulation_name']
                content = row['content']
                citations = parse_clause_citations(content)
                
                int_spans, part_spans, ext_spans = [], [], []
                for c in citations:
                    if match_article(c, variants):
                        cat = classify_reference(curr_reg, target_reg, partner_reg_name, c['gov_prefix'])
                        if cat == "internal":
                            int_spans.append(c['span'])
                        elif cat == "partner":
                            part_spans.append(c['span'])
                        elif cat == "external":
                            ext_spans.append(c['span'])
                
                if int_spans:
                    results_internal.append((row, int_spans))
                if part_spans:
                    results_partner.append((row, part_spans))
                if ext_spans:
                    results_external.append((row, ext_spans))

            st.success(f"분석 완료: 내부 {len(results_internal)}건 / {partner_reg_name} {len(results_partner)}건 / 타 규정 {len(results_external)}건")

            # 엑셀 다운로드 버튼
            excel_m7_bytes = export_menu7_to_excel(
                df_target=df_target,
                results_internal=results_internal,
                results_partner=results_partner,
                results_external=results_external,
                target_reg=target_reg,
                clean_art=clean_art,
                partner_reg_name=partner_reg_name
            )
            safe_reg = target_reg.replace(" ", "_")
            st.download_button(
                label="📥 인용 분석 결과 엑셀(.xlsx) 다운로드",
                data=excel_m7_bytes,
                file_name=f"인용분석_{safe_reg}_{clean_art}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                help="관심 조항 원문 및 내부/파트너/타규정 참조 분석 결과를 각각의 시트로 분리하여 저장합니다."
            )
            st.write("")

            st.markdown(f"### 🏠 [{target_reg}] 내부 참조")
            if results_internal:
                for row, spans in results_internal:
                    with st.container(border=True):
                        st.markdown(f"**📌 {row['ref_no']} {row['article_title']}** :grey[[시행일자: {row['reg_date']}]]")
                        st.markdown(highlight_spans(row['content'], spans, "red"))
            else:
                st.caption("결과 없음")

            st.markdown(f"### 🤝 [{partner_reg_name}] 참조")
            st.caption("※ 연속 조항 열거(예: '규정 제6조, 제25조') 및 조문 범위 인용('제A조부터 제B조까지'), 대명사 문맥을 자동 반영하여 분석합니다.")
            if results_partner:
                for row, spans in results_partner:
                    with st.container(border=True):
                        st.markdown(f"**📌 {row['ref_no']} {row['article_title']}** :grey[[시행일자: {row['reg_date']}]]")
                        st.markdown(highlight_spans(row['content'], spans, "blue"))
            else:
                st.caption("결과 없음")

            st.markdown("### 🌏 타 규정 참조")
            if results_external:
                for row, spans in results_external:
                    with st.container(border=True):
                        st.markdown(f"**📌 [{row['regulation_name']}] {row['ref_no']} {row['article_title']}** :grey[[시행일자: {row['reg_date']}]]")
                        st.markdown(highlight_spans(row['content'], spans, "green"))
            else:
                st.caption("결과 없음")
    else:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")
