import streamlit as st
import sqlite3
import pandas as pd
import glob
import os
import re
import io
import sys
import difflib
import html
from pathlib import Path
import unicodedata
import urllib.parse
import warnings
warnings.filterwarnings('ignore', message='.*pandas only supports SQLAlchemy connectable.*')

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

# ----------------------------------------------------------------------
# [추가됨] 원문자(①~⑳) ↔ 아라비아 숫자(1~20) '항' 상호 호환 유틸리티
# ----------------------------------------------------------------------
CIRCLED_TO_ARABIC = {chr(9311 + i): str(i) for i in range(1, 21)}
ARABIC_TO_CIRCLED = {str(i): chr(9311 + i) for i in range(1, 21)}

def to_circled_hang(s: str) -> str:
    """아라비아 숫자 항(제1항, 1항 등)을 원문자 항(제①항, ①항 등)으로 변환"""
    def _repl(m):
        prefix = m.group(1) or ""
        num = m.group(2)
        suffix = m.group(3) or ""
        if num in ARABIC_TO_CIRCLED:
            return f"{prefix}{ARABIC_TO_CIRCLED[num]}{suffix}"
        return m.group(0)
    return re.sub(r"(제\s*)?(\d{1,2})(\s*항)", _repl, s)

def to_arabic_hang(s: str) -> str:
    """원문자 항(제①항, ①항 등)을 아라비아 숫자 항(제1항, 1항 등)으로 변환"""
    def _repl(m):
        prefix = m.group(1) or ""
        char = m.group(2)
        suffix = m.group(3) or ""
        return f"{prefix}{CIRCLED_TO_ARABIC.get(char, char)}{suffix}"
    return re.sub(r"(제\s*)?([①-⑳])(\s*항)?", _repl, s)

def normalize_art_input(text: str) -> str:
    """
    조항 번호 입력 스마트 정규화:
    - 하이픈 표기 보정: '20-2', '제20-2조' -> '제20조의2'
    - 접두어 보정: '20조' -> '제20조'
    - 가지조항 보정: '20조2', '제20조2', '제20조 2' -> '제20조의2'
    - 항/호/목 띄어쓰기 및 접두어 보정: '20조 1항 2호' -> '제20조제1항제2호'
    """
    s = text.strip() if text else ""
    if not s:
        return ""
    # 하이픈 가지조 표기 (예: '20-2', '제20-2', '제20-2조', '20-2조')
    m_hyph = re.match(r'^제?(\d+)-(\d+)조?(.*)$', s)
    if m_hyph:
        s = f"제{m_hyph.group(1)}조의{m_hyph.group(2)}{m_hyph.group(3)}"
    if not s.startswith("제") and re.match(r"^\d+조", s):
        s = f"제{s}"
    s = re.sub(r'^(제\d+조)\s*(\d+)(?!\d*항)', r'\1의\2', s)
    s = re.sub(r'^(제\d+조)\s*의\s*(\d+)', r'\1의\2', s)
    s = re.sub(r'(제\d+조(?:의\d+)?)\s*(\d+)항', r'\1제\2항', s)
    s = re.sub(r'(제\d+조(?:의\d+)?)\s*(제?[①-⑳\d]+항)', r'\1\2', s)
    s = re.sub(r'(항)\s*(\d+)호', r'\1제\2호', s)
    s = re.sub(r'(항)\s*(제\d+호)', r'\1\2', s)
    s = re.sub(r'(호(?:의\d+)?)\s*([가-하])목', r'\1\2목', s)
    return s

def normalize_article_query(text: str) -> str:
    """조항 번호 전용 입력창(메뉴 4, 5, 7)용 정규화: 순수 숫자('20')도 '제20조'로 자동 보정"""
    s = text.strip() if text else ""
    if not s:
        return ""
    if re.match(r"^\d+$", s):
        return f"제{s}조"
    return normalize_art_input(s)

def extract_base_article(ref_no: str) -> str:
    """'제20조의2제①항제1호' -> '제20조의2'처럼 기본 조 번호 추출"""
    if not ref_no:
        return ""
    m = re.match(r'^(제\d+조(?:의\d+)?)', ref_no.strip())
    return m.group(1) if m else ref_no.strip()

def natural_ref_sort_key(ref_no: str) -> tuple:
    """법령 조항을 자연 순서(제1조 -> 제2조 -> 제2조의2 -> 제10조, 제1호 -> 제2호 -> 제10호)로 정렬하기 위한 키"""
    if not ref_no:
        return (999999, 0, 0, 0, 0, 0, "")
    ar = to_arabic_hang(str(ref_no))
    m_art = re.search(r'제(\d+)조(?:의(\d+))?', ar)
    art_n = int(m_art.group(1)) if m_art else 999999
    art_sub = int(m_art.group(2)) if (m_art and m_art.group(2)) else 0
    m_hang = re.search(r'제?(\d+)항', ar)
    hang_n = int(m_hang.group(1)) if m_hang else 0
    m_ho = re.search(r'제?(\d+)호(?:의(\d+))?', ar)
    ho_n = int(m_ho.group(1)) if m_ho else 0
    ho_sub = int(m_ho.group(2)) if (m_ho and m_ho.group(2)) else 0
    m_mok = re.search(r'([가-하])목', ar)
    mok_n = ord(m_mok.group(1)) if m_mok else 0
    return (art_n, art_sub, hang_n, ho_n, ho_sub, mok_n, str(ref_no))

def format_date_kr(d: str) -> str:
    """'20260914' -> '2026-09-14' 가독성 포맷"""
    s = str(d or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s

# =========================================================
# 신구대조표 및 변경 감지 유틸리티
# =========================================================
def normalize_comparable_text(text: str) -> str:
    """
    조항 개정 비교를 위한 정규화:
    - 띄어쓰기(공백, 줄바꿈) 무시
    - 마침표(.), 쉼표(,), 따옴표, 괄호 등 단순 문장부호 유무 무시
    """
    if not text:
        return ""
    t = text.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
    t = t.replace('（', '(').replace('）', ')').replace('＜', '<').replace('＞', '>')
    return re.sub(r'[\s\.\,·~;:\'\"]', '', t)

def is_substantive_change(old_text: str, new_text: str) -> bool:
    """두 텍스트 사이에 단순 띄어쓰기나 마침표(.) 유무를 넘어선 실질적 내용 변경이 있는지 판별"""
    if old_text is None or new_text is None:
        return old_text != new_text
    return normalize_comparable_text(old_text) != normalize_comparable_text(new_text)

def generate_inline_diff_html(old_text: str, new_text: str) -> str:
    """
    이전 텍스트와 현재 텍스트의 어절/단어 단위 실질 변경 사항을 시각화하는 인라인 Diff HTML 생성.
    단순 띄어쓰기나 마침표(.) 유무 등 사소한 표기 차이는 변경으로 강조하지 않고 자연스럽게 유지합니다.
    라이트/다크 모드 모두에서 높은 가독성을 보장하는 대비 색상을 사용합니다.
    """
    del_style = "background-color: rgba(248, 81, 73, 0.22); color: #d1242f; text-decoration: line-through; padding: 1px 4px; border-radius: 3px; margin: 0 1px;"
    ins_style = "background-color: rgba(46, 160, 67, 0.22); color: #1a7f37; text-decoration: none; font-weight: 600; padding: 1px 4px; border-radius: 3px; margin: 0 1px;"

    if not old_text and not new_text:
        return ""
    if not old_text:
        return f'<ins style="{ins_style}">{html.escape(new_text)}</ins>'
    if not new_text:
        return f'<del style="{del_style}">{html.escape(old_text)}</del>'

    token_pattern = re.compile(r'[가-힣\w]+|[^\w\s]|\s+')
    old_tokens = token_pattern.findall(old_text)
    new_tokens = token_pattern.findall(new_text)

    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens)
    result = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_chunk = "".join(old_tokens[i1:i2])
        new_chunk = "".join(new_tokens[j1:j2])

        if tag == 'equal':
            result.append(html.escape(new_chunk))
        elif normalize_comparable_text(old_chunk) == normalize_comparable_text(new_chunk):
            result.append(html.escape(new_chunk))
        elif tag == 'delete':
            result.append(f'<del style="{del_style}">{html.escape(old_chunk)}</del>')
        elif tag == 'insert':
            result.append(f'<ins style="{ins_style}">{html.escape(new_chunk)}</ins>')
        elif tag == 'replace':
            result.append(f'<del style="{del_style}">{html.escape(old_chunk)}</del>')
            result.append(f'<ins style="{ins_style}">{html.escape(new_chunk)}</ins>')

    return "".join(result)

def render_diff_box_html(diff_html: str) -> str:
    """라이트/다크 모드 양쪽에서 선명하게 보이는 Diff 박스 래퍼"""
    return (
        "<div class='rule-diff-box' style="
        "'background-color:#f8f9fa; color:#1f2328; padding:12px 14px; "
        "border-radius:8px; border:1px solid #d0d7de; margin-bottom:8px; "
        "line-height:1.7; font-size:0.95rem;'>"
        f"{diff_html}</div>"
    )

# =========================================================
# 복합 검색어 파서 및 엑셀 다운로드 유틸리티
# =========================================================
def parse_search_query(query_str: str):
    """
    검색어 문자열을 분석하여 (or_groups, not_terms) 반환.
    - or_groups: OR(|)로 구분된 그룹들의 리스트. 각 그룹은 AND(공백) 조건 단어들의 리스트.
    - not_terms: 제외(-)할 단어들의 리스트.
    예: '공매도 호가 | 거래정지 -세칙'
      -> or_groups: [['공매도', '호가'], ['거래정지']]
      -> not_terms: ['세칙']
    """
    not_terms = []
    raw_tokens = query_str.strip().split()
    clean_tokens = []
    for tok in raw_tokens:
        if tok.startswith("-") and len(tok) > 1:
            not_terms.append(tok[1:])
        else:
            clean_tokens.append(tok)

    clean_query = " ".join(clean_tokens)
    if not clean_query:
        return [], not_terms

    or_parts = [p.strip() for p in clean_query.split("|") if p.strip()]
    or_groups = []
    for part in or_parts:
        and_terms = part.split()
        if and_terms:
            or_groups.append(and_terms)

    return or_groups, not_terms

_ILLEGAL_XML_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

def _sanitize_df_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    """
    openpyxl 엑셀 내보내기 안전화:
    - PostgreSQL TIMESTAMPTZ 등 타임존 포함 datetime 컬럼의 타임존 제거 (ValueError 방지)
    - HWP 파싱 잔여 제어문자(IllegalCharacterError) 제거
    """
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in out.columns:
        dtype = out[col].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_convert("Asia/Seoul").dt.tz_localize(None)
        elif dtype == object:
            def _clean_cell(val):
                if isinstance(val, str):
                    return _ILLEGAL_XML_RE.sub("", val)
                if hasattr(val, "tzinfo") and getattr(val, "tzinfo", None) is not None:
                    return val.replace(tzinfo=None)
                return val
            out[col] = out[col].map(_clean_cell)
    return out

def _write_formatted_sheet(writer, df: pd.DataFrame, sheet_name: str):
    """엑셀 시트 기록 및 자동 열 너비/줄바꿈 서식 적용"""
    safe_name = re.sub(r'[:\\/*?\[\]]', '_', sheet_name)[:31] or "Sheet"
    clean_df = _sanitize_df_for_excel(df)
    clean_df.to_excel(writer, sheet_name=safe_name, index=False)
    try:
        from openpyxl.styles import Alignment, Font
        ws = writer.sheets[safe_name]
        # 헤더 볼드 & 고정
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True)
        # 컬럼 너비 자동 조정 (내용 컬럼은 65자 고정 + 줄바꿈)
        for col_idx, col_name in enumerate(clean_df.columns, start=1):
            col_letter = ws.cell(row=1, column=col_idx).column_letter
            if any(k in str(col_name) for k in ("내용", "content", "개정 전", "개정 후")):
                ws.column_dimensions[col_letter].width = 65
                for r_idx in range(2, min(len(clean_df) + 2, 5000)):
                    ws.cell(row=r_idx, column=col_idx).alignment = Alignment(wrap_text=True, vertical="top")
            else:
                sample_lens = [len(str(col_name))] + [
                    len(str(v)) for v in clean_df[col_name].head(100).tolist() if v is not None
                ]
                ws.column_dimensions[col_letter].width = min(max(sample_lens) + 4, 34)
    except Exception:
        pass

def export_dataframe_to_excel(df: pd.DataFrame, sheet_name: str = "검색결과") -> bytes:
    """단일 DataFrame을 서식이 적용된 엑셀(.xlsx) 바이너리 스트림으로 변환"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        if df is not None and not df.empty:
            _write_formatted_sheet(writer, df, sheet_name)
        else:
            _write_formatted_sheet(writer, pd.DataFrame([{"안내": "데이터가 없습니다."}]), "결과없음")
    return output.getvalue()

def export_menu7_to_excel(
    df_target: pd.DataFrame,
    results_internal: list,
    results_partner: list,
    results_external: list,
    target_reg: str,
    clean_art: str,
    partner_reg_name: str,
    outgoing_list: list = None
) -> bytes:
    """메뉴 7 역참조·정방향 분석 결과를 다중 시트 엑셀(.xlsx) 파일로 변환"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        has_sheet = False
        if df_target is not None and not df_target.empty:
            _write_formatted_sheet(writer, df_target, "관심조항원문")
            has_sheet = True
        if results_internal:
            df_int = pd.DataFrame([r["row"] if isinstance(r, dict) else (r[0] if isinstance(r, (tuple, list)) else r) for r in results_internal])
            _write_formatted_sheet(writer, df_int, "내부참조")
            has_sheet = True
        if results_partner:
            df_part = pd.DataFrame([r["row"] if isinstance(r, dict) else (r[0] if isinstance(r, (tuple, list)) else r) for r in results_partner])
            _write_formatted_sheet(writer, df_part, "파트너참조")
            has_sheet = True
        if results_external:
            df_ext = pd.DataFrame([r["row"] if isinstance(r, dict) else (r[0] if isinstance(r, (tuple, list)) else r) for r in results_external])
            _write_formatted_sheet(writer, df_ext, "타규정참조")
            has_sheet = True
        if outgoing_list:
            df_out = pd.DataFrame([
                {"구분/접두어": o["prefix"], "인용 조항": o["clause"], "대상 규정(추정)": o.get("resolved_reg") or "-", "원문 표기": o["raw"]}
                for o in outgoing_list
            ])
            _write_formatted_sheet(writer, df_out, "정방향인용")
            has_sheet = True
        if not has_sheet:
            _write_formatted_sheet(writer, pd.DataFrame([{"안내": "검색 결과가 없습니다."}]), "결과없음")
    return output.getvalue()

def export_revision_diff_to_excel(diff_items: list, reg_name: str, old_date: str, new_date: str) -> bytes:
    """두 개정일 간 신구대조표 비교 결과를 엑셀(.xlsx)로 변환"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        summary_df = pd.DataFrame([
            {"항목": "규정명", "값": reg_name},
            {"항목": "비교 기준(구법)", "값": old_date},
            {"항목": "비교 대상(신법)", "값": new_date},
            {"항목": "신설 조항 수", "값": sum(1 for x in diff_items if x["status"] == "신설")},
            {"항목": "변경 조항 수", "값": sum(1 for x in diff_items if x["status"] == "변경")},
            {"항목": "삭제 조항 수", "값": sum(1 for x in diff_items if x["status"] == "삭제")},
        ])
        _write_formatted_sheet(writer, summary_df, "개정요약")
        if diff_items:
            detail_df = pd.DataFrame([
                {
                    "변경구분": x["status"],
                    "조항": x["label"],
                    "조명": x["title"],
                    f"개정 전 ({old_date})": x["old_content"] or "",
                    f"개정 후 ({new_date})": x["new_content"] or "",
                }
                for x in diff_items
            ])
            _write_formatted_sheet(writer, detail_df, "신구대조표")
    return output.getvalue()

def get_hang_variants(text: str) -> list:
    """입력된 조항/키워드에 대해 원문자(①)/숫자(1), 띄어쓰기 유무 등 가능한 모든 동등 표현 생성"""
    if not text:
        return []
    s = normalize_art_input(text)
    raw_list = [s]
    ar = to_arabic_hang(s)
    ci = to_circled_hang(s)
    raw_list.extend([ar, ci])

    expanded = []
    for item in raw_list:
        expanded.append(item)
        nospace = re.sub(r"\s+", "", item)
        expanded.append(nospace)
        m = re.search(r"(제\d+조(?:의\d+)?)(제?[①-⑳\d]+항.*)", nospace)
        if m:
            expanded.append(f"{m.group(1)} {m.group(2)}")

    for item in list(expanded):
        if re.match(r"^제([①-⑳\d]+항)$", item):
            expanded.append(re.sub(r"^제", "", item))
        elif re.match(r"^([①-⑳\d]+항)$", item):
            expanded.append(f"제{item}")

    seen = set()
    result = []
    for item in expanded:
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result

def get_citation_terms(target_reg: str, target_art: str):
    """관심 규정 및 조항에 대한 내부/파트너/타 규정 인용 검색어(동등 표현 포함) 생성"""
    clean_art = normalize_article_query(target_art)
    variants = get_hang_variants(clean_art)
    if not variants:
        variants = [clean_art]

    is_rule = "시행세칙" in target_reg
    partner_reg_name = target_reg.replace(" 시행세칙", "").replace("시행세칙", "").strip() if is_rule else f"{target_reg} 시행세칙"

    terms_internal = list(variants)

    terms_partner = []
    prefix_p = "세칙" if is_rule else "규정"
    for v in variants:
        terms_partner.extend([f"{prefix_p} {v}", f"{prefix_p}{v}"])

    terms_external = []
    target_nospace = re.sub(r"\s+", "", target_reg)
    for v in variants:
        terms_external.extend([
            f"「{target_reg}」 {v}",
            f"「{target_reg}」{v}",
            f"{target_reg} {v}",
            f"{target_reg}{v}",
            f"{target_nospace} {v}",
            f"{target_nospace}{v}",
        ])
    short_name = target_reg.split()[-1]
    if len(short_name) >= 3 and short_name != target_reg:
        for v in variants:
            terms_external.extend([f"{short_name} {v}", f"{short_name}{v}"])

    def _dedup(lst):
        seen = set()
        return [x for x in lst if not (x in seen or seen.add(x))]

    return clean_art, partner_reg_name, _dedup(terms_internal), _dedup(terms_partner), _dedup(terms_external)

def highlight_terms(text: str, terms: list, color: str = "red") -> str:
    """텍스트 내 일치하는 검색어들을 지정 색상 마크다운으로 하이라이트 (긴 구문 우선 · 영문 대소문자 무시)"""
    if not text or not terms:
        return text or ""
    clean_terms = [t for t in set(terms) if t and t.strip()]
    if not clean_terms:
        return text
    sorted_terms = sorted(clean_terms, key=len, reverse=True)
    pattern = re.compile("(" + "|".join(re.escape(t) for t in sorted_terms) + ")", flags=re.IGNORECASE)
    return pattern.sub(lambda m: f":{color}[**{m.group(1).replace(']', '&#93;')}**]", text)

# =========================================================
# 고정밀 조항 인용·역참조·정방향 인용 분석 엔진
# =========================================================
_MARKET_PREFIXES = ("유가증권시장", "코스닥시장", "코넥스시장", "파생상품시장", "증권시장")
_NON_LAW_WORDS = {
    "방법", "기법", "수법", "용법", "화법", "어법", "요법", "문법", "법칙",
    "운영", "경영", "반영", "촬영", "조영", "시영", "공영", "민영", "전영", "후영",
    "적법", "위법", "불법", "합법", "사법", "입법", "헌법", "준법"
}

_GOVERNING_PREFIX_RE = re.compile(
    r'(?:「[^」]{2,60}」|『[^』]{2,60}』|'
    r'(?<![가-힣\d])(?:(?:이|동|당해|해당|본|종전|같은)\s*)?'
    r'(?:(?:유가증권시장|코스닥시장|코넥스시장|파생상품시장|증권시장|시장감시)\s*)?'
    r'(?:(?:청산결제|업무|상장|공시|회원|수탁계약|시장감시)\s*)?'
    r'(?:(?:규정\s*)?시행세칙|세칙|규정|규칙)(?![가-힣])|'
    r'(?<![가-힣\d])(?:(?:이|동|당해|해당|본|같은)\s*)?(?:[가-힣]{2,15}(?:법률|법|시행령)|시행령|법률|법|영)(?![가-힣]))'
)

_CHAIN_TOKEN_RE = re.compile(
    r'^(?:[\s,·~및또는내지부터까지\(\)]|'
    r'제\s*\d+(?:의\s*\d+)?\s*조|제?\s*[①-⑳\d]+\s*항|제?\s*\d+(?:의\s*\d+)?\s*호|[가-하]\s*목|'
    r'각\s*(?:호|항|목)|같은\s*(?:조|항|호)|동(?:조|항)|전(?:항|호))+$'
)

_RANGE_CITE_RE = re.compile(
    r'제\s*(\d+)(?:의\s*(\d+))?\s*조\s*(?:부터|~|내지)\s*제\s*(\d+)(?:의\s*(\d+))?\s*조(?:\s*까지)?'
)

def extract_market_prefix(reg_name: str) -> str:
    """규정명에서 시장 소속 접두어('유가증권시장', '코스닥시장' 등) 추출"""
    if not reg_name:
        return ""
    for mk in _MARKET_PREFIXES:
        if reg_name.startswith(mk):
            return mk
    return ""

def parse_article_num_key(art_str: str):
    """단일 조 번호('제25조', '제20조의2')일 때만 (조, 가지조) 튜플 반환; 항/호 포함 시 None"""
    if not art_str:
        return None
    s = re.sub(r"\s+", "", art_str)
    m = re.match(r'^제(\d+)조(?:의(\d+))?$', s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)

def build_clause_pattern(clean_art: str):
    """
    가지조항·가지호 경계를 엄격히 보호하는 조항 매칭 정규식 생성:
    - '제20조'는 '제20조', '제20조제1항', '제20조제①항'에 매칭하되 '제20조의2'는 엄격 제외
    - '제26조제1항제2호'는 '제26조제①항제2호'에 매칭하되 '제26조제1항제2호의2'는 엄격 제외
    """
    ar = to_arabic_hang(re.sub(r'\s+', '', clean_art))
    parts = re.findall(r'제\d+조(?:의\d+)?|제?\d+항|제?\d+호(?:의\d+)?|[가-하]목', ar)
    if not parts or ''.join(parts) != ar:
        return re.compile(re.escape(clean_art))

    regex_parts = []
    for p in parts:
        m_hang = re.match(r'^(제?)(\d+)항$', p)
        if m_hang:
            num = m_hang.group(2)
            c = ARABIC_TO_CIRCLED.get(num)
            num_pat = f'(?:{num}|{c})' if c else num
            regex_parts.append(rf'제?\s*{num_pat}\s*항')
        else:
            regex_parts.append(re.escape(p))

    core = r'\s*'.join(regex_parts)
    last = parts[-1]
    if last.endswith('조') or last.endswith('호'):
        core += r'(?!\s*의\s*\d+)'
    elif re.search(r'의\d+$', last):
        core += r'(?!\d)'
    return re.compile(core)

def detect_governing_prefix(content: str, start: int):
    """
    content의 start 위치에서 시작하는 조항 인용을 지배하는 법령/규정 접두어를 탐지.
    - 직전 직접 접두어 ('규정 제25조', '「유가증권시장 업무규정」 제20조의2')
    - 연속 조항 열거 상속 ('규정 제6조제2항, 제25조' -> '규정')
    - '같은 규정' / '같은 법' -> 같은 문장 내 선행 「...」/『...』로 자동 복원
    - 따옴표 정의 '(이하 "OO"라 한다)'나 비법령어('방법', '운영')는 철저히 무시
    """
    chunk = content[max(0, start - 220):start]
    before = re.split(r'[\.\n;]', chunk)[-1]
    matches = list(_GOVERNING_PREFIX_RE.finditer(before))
    for m in reversed(matches):
        p_raw = m.group(0).strip()
        last_word = p_raw.split()[-1]
        if last_word in _NON_LAW_WORDS or any(last_word.endswith(w) for w in _NON_LAW_WORDS):
            continue
        after_p = before[m.end():]
        if after_p.strip() == '' or _CHAIN_TOKEN_RE.match(after_p):
            # '같은 규정' / '같은 세칙' / '같은 법'은 같은 문장 내 직전 「...」로 복원
            if re.match(r'^같은\s*(?:규정|세칙|시행세칙|규칙|법|법률|영|시행령)$', p_raw):
                quoted_before = re.findall(r'「[^」]{2,60}」|『[^』]{2,60}』', before[:m.start()])
                if quoted_before:
                    return quoted_before[-1]
            return p_raw
    return None

def classify_citation(curr_reg: str, target_reg: str, partner_reg: str, gov_prefix: str) -> str:
    """
    추출된 지배 접두어(gov_prefix)를 바탕으로 인용 분류 반환:
    'internal' | 'partner' | 'external' | 'ignore'
    """
    is_target_rule = ("시행세칙" in target_reg or "세칙" in target_reg or "규칙" in target_reg)
    p_clean = re.sub(r'[「」『』\"\'\s]+', '', gov_prefix or '')
    target_nospace = re.sub(r'\s+', '', target_reg)
    target_short = target_reg.split()[-1]

    # 1. 내부 참조 (curr_reg == target_reg)
    if curr_reg == target_reg:
        if not gov_prefix:
            return "internal"
        if is_target_rule:
            self_set = {
                "세칙", "시행세칙", "이세칙", "이시행세칙", "동세칙", "본세칙",
                "당해세칙", "해당세칙", "종전세칙", "같은세칙", "규칙", "이규칙", target_nospace
            }
            if p_clean in self_set or (len(target_nospace) >= 4 and target_nospace in p_clean):
                return "internal"
            return "ignore"
        else:
            self_set = {
                "규정", "이규정", "동규정", "본규정", "당해규정", "해당규정",
                "종전규정", "같은규정", target_short, f"이{target_short}", target_nospace
            }
            if p_clean in self_set or p_clean == target_nospace:
                return "internal"
            return "ignore"

    # 2. 파트너 규정 참조 (규정 ↔ 시행세칙)
    elif partner_reg and curr_reg == partner_reg:
        if not gov_prefix:
            return "ignore"
        if not is_target_rule:
            # 대상: 규정, 현재: 시행세칙 -> '규정', '업무규정', '유가증권시장업무규정' 등
            if "세칙" in p_clean or "법" in p_clean or p_clean in ("영", "시행령"):
                return "ignore"
            if p_clean in {"규정", "이규정", "당해규정", "해당규정", "종전규정", target_short, target_nospace} or target_nospace in p_clean:
                return "partner"
            return "ignore"
        else:
            # 대상: 시행세칙, 현재: 규정 -> '세칙', '시행세칙', full name
            if p_clean in {"세칙", "시행세칙", "당해세칙", "해당세칙", target_nospace} or target_nospace in p_clean:
                return "partner"
            return "ignore"

    # 3. 타 규정 참조 (제3의 규정)
    else:
        if not gov_prefix:
            return "ignore"
        # 대상이 규정일 때 '유가증권시장업무규정시행세칙'은 제외
        if not is_target_rule and "세칙" in p_clean:
            return "ignore"
        # 정식 명칭(공백 무시) 포함 매칭
        if p_clean == target_nospace or (len(target_nospace) >= 4 and target_nospace in p_clean):
            return "external"
        # 동일 시장 내 약칭 매칭 (예: 유가증권시장 상장규정 내 '업무규정 제20조의2' -> 유가증권시장 업무규정)
        target_mk = extract_market_prefix(target_reg)
        curr_mk = extract_market_prefix(curr_reg)
        if not is_target_rule and target_mk and target_mk == curr_mk and len(target_short) >= 3 and p_clean == target_short:
            return "external"
        return "ignore"

def find_article_citations_in_text(
    content: str,
    clean_art: str,
    curr_reg: str,
    target_reg: str,
    partner_reg: str,
    clause_pat=None
):
    """
    단일 조항 본문에서 target_reg의 clean_art를 인용하는 모든 유효 span을
    {'internal': [...], 'partner': [...], 'external': [...]} 및 범위인용 여부로 반환.
    """
    if not content:
        return {"internal": [], "partner": [], "external": []}, False

    if clause_pat is None:
        clause_pat = build_clause_pattern(clean_art)

    spans_by_cat = {"internal": [], "partner": [], "external": []}
    matched_via_range = False

    # 1. 직접 및 연속 열거 조항 매칭
    for m in clause_pat.finditer(content):
        gp = detect_governing_prefix(content, m.start())
        cat = classify_citation(curr_reg, target_reg, partner_reg, gp)
        if cat in spans_by_cat:
            spans_by_cat[cat].append(m.span())

    # 2. 단일 조 번호 검색 시 조문 범위 인용('제23조부터 제27조까지', '제23조 내지 제25조') 추가 탐지
    target_key = parse_article_num_key(clean_art)
    if target_key is not None and ("부터" in content or "내지" in content or "~" in content):
        for rm in _RANGE_CITE_RE.finditer(content):
            s_key = (int(rm.group(1)), int(rm.group(2)) if rm.group(2) else 0)
            e_key = (int(rm.group(3)), int(rm.group(4)) if rm.group(4) else 0)
            if s_key <= target_key <= e_key:
                # 이미 직접 매칭된 구간(시작/끝 조항 자체)과 겹치지 않는 범위 포함 여부 기록
                r_span = rm.span()
                gp = detect_governing_prefix(content, rm.start())
                cat = classify_citation(curr_reg, target_reg, partner_reg, gp)
                if cat in spans_by_cat:
                    already_covered = any(s <= r_span[1] and e >= r_span[0] for s, e in spans_by_cat[cat])
                    if not already_covered:
                        matched_via_range = True
                    spans_by_cat[cat].append(r_span)

    return spans_by_cat, matched_via_range

_OUTGOING_ART_RE = re.compile(
    r'제\s*\d+(?:의\s*\d+)?\s*조(?:\s*제?\s*[①-⑳\d]+\s*항)?(?:\s*제?\s*\d+(?:의\s*\d+)?\s*호)?(?:\s*[가-하]\s*목)?'
)

def extract_outgoing_citations(content: str, curr_reg: str = "", all_reg_names: list = None) -> list:
    """
    관심 조항 본문에서 인용하고 있는 타 조항/타 규정(정방향 인용)을 추출하고
    DB에 존재하는 규정명과 매칭하여 원클릭 탐색 정보를 함께 반환.
    """
    if not content:
        return []
    all_reg_names = all_reg_names or []
    is_curr_rule = ("시행세칙" in curr_reg or "세칙" in curr_reg or "규칙" in curr_reg)
    partner_reg = (
        curr_reg.replace(" 시행세칙", "").replace("시행세칙", "").strip()
        if is_curr_rule else f"{curr_reg} 시행세칙"
    )
    curr_mk = extract_market_prefix(curr_reg)
    reg_nospace_map = {re.sub(r"\s+", "", r): r for r in all_reg_names}

    def _resolve_reg(prefix: str):
        if not prefix:
            return curr_reg
        p_clean = re.sub(r'[「」『』\"\'\s]+', '', prefix)
        if p_clean in reg_nospace_map:
            return reg_nospace_map[p_clean]
        for k_ns, full_r in reg_nospace_map.items():
            if len(k_ns) >= 4 and k_ns in p_clean:
                if "세칙" not in p_clean and "세칙" in k_ns:
                    continue
                return full_r
        if is_curr_rule and p_clean in {"규정", "이규정", "당해규정", "해당규정"}:
            return partner_reg if partner_reg in all_reg_names else ""
        if not is_curr_rule and p_clean in {"세칙", "시행세칙"}:
            return partner_reg if partner_reg in all_reg_names else ""
        if p_clean in {"이규정", "본규정", "동규정", "이세칙", "본세칙", "동세칙", "이규칙"}:
            return curr_reg
        # 동일 시장 내 약칭 (예: 상장규정, 공시규정, 업무규정)
        if curr_mk:
            cand = f"{curr_mk} {p_clean}"
            if cand in all_reg_names:
                return cand
        return ""

    outgoing = []
    seen = set()
    for m in _OUTGOING_ART_RE.finditer(content):
        raw_clause = m.group(0)
        norm_clause = re.sub(r"\s+", "", raw_clause)
        gp = detect_governing_prefix(content, m.start())
        prefix_label = gp if gp else "자체 내부"
        resolved_reg = _resolve_reg(gp)
        key = (prefix_label, norm_clause)
        if key not in seen:
            seen.add(key)
            outgoing.append({
                "prefix": prefix_label,
                "clause": norm_clause,
                "raw": raw_clause,
                "full_label": f"{prefix_label} {norm_clause}" if gp else norm_clause,
                "resolved_reg": resolved_reg,
                "base_art": extract_base_article(norm_clause),
            })
    return outgoing

def is_external_prefixed(content: str, start: int, is_rule: bool) -> bool:
    """하위 호환 래퍼: 내부 조항이 아닌 외부 법규/상·하위 규정에 종속된 인용인지 판별"""
    gp = detect_governing_prefix(content, start)
    if not gp:
        return False
    p_clean = re.sub(r'[「」『』\"\'\s]+', '', gp)
    if is_rule:
        return p_clean not in {"세칙", "시행세칙", "이세칙", "본세칙", "동세칙", "당해세칙", "해당세칙", "종전세칙"}
    return p_clean not in {"규정", "이규정", "본규정", "동규정", "당해규정", "해당규정", "종전규정"}

def find_valid_internal_matches(content: str, terms: list, is_rule: bool):
    """하위 호환 래퍼: 외부 접두어가 붙지 않은 순수 내부 참조 구간 반환"""
    if not content or not terms:
        return []
    sorted_terms = sorted(set(terms), key=len, reverse=True)
    pattern = re.compile("(" + "|".join(re.escape(t) for t in sorted_terms) + ")")
    valid_spans = []
    for m in pattern.finditer(content):
        start, end = m.span()
        if not is_external_prefixed(content, start, is_rule):
            valid_spans.append((start, end))
    return valid_spans

def highlight_spans(text: str, spans: list, color: str = "red") -> str:
    """주어진 인덱스 구간(spans)들만 지정 색상 마크다운으로 하이라이트"""
    if not text or not spans:
        return text or ""
    sorted_spans = sorted(set(spans), key=lambda x: (x[0], -x[1]))
    merged = []
    for s, e in sorted_spans:
        if not merged:
            merged.append([s, e])
        else:
            prev = merged[-1]
            if s <= prev[1]:
                prev[1] = max(prev[1], e)
            else:
                merged.append([s, e])
    res = text
    for start, end in reversed(merged):
        seg = res[start:end].replace("]", "&#93;")
        res = res[:start] + f":{color}[**{seg}**]" + res[end:]
    return res


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

    # 조항명(제목)은 article_id 직후 공백 뒤 바로 '('로 시작하는 경우에만 인식하며 중첩 괄호 지원
    title = ""
    first_body_part = after
    if after_strip.startswith("("):
        depth = 0
        close_idx = -1
        for pos, ch in enumerate(after_strip):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_idx = pos
                    break
        if close_idx != -1:
            title = after_strip[1:close_idx].strip()
            first_body_part = after_strip[close_idx + 1:]

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
    # 1. Streamlit secrets 확인 (다양한 설정 키 및 섹션 지원)
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
    secrets_path = Path(__file__).resolve().parent / ".streamlit" / "secrets.toml"
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

@st.cache_resource(show_spinner=False)
def _resolve_db_engine_cached(primary_url: str):
    """Supabase 연결 후보들을 1회만 검증하여 (engine, diagnostics) 반환 (실패 시 반복 타임아웃 방지)"""
    diag = {
        "supabase_configured": False,
        "supabase_connected": False,
        "connected_url": None,
        "connected_host": None,
        "project_ref": None,
        "error_message": None,
        "candidate_results": []
    }
    if not primary_url:
        diag["error_message"] = "SUPABASE_DB_URL이 설정되지 않았습니다."
        return None, diag

    diag["supabase_configured"] = True
    ref_match = re.search(r"postgres(?:\.([a-z0-9]+))?:[^@]+@(?:db\.([a-z0-9]+)\.supabase\.co|[a-z0-9.-]+)", primary_url)
    if ref_match:
        diag["project_ref"] = ref_match.group(1) or ref_match.group(2)

    candidates = get_candidate_db_urls(primary_url)
    last_error = None
    results = []

    for cand_url in candidates:
        masked = mask_db_url(cand_url)
        try:
            engine = _connect_postgres_cached(cand_url)
            diag["supabase_connected"] = True
            diag["connected_url"] = cand_url
            diag["connected_host"] = extract_host_from_url(cand_url)
            diag["error_message"] = None
            return engine, diag
        except Exception as e:
            err_str = str(e).strip()
            first_err_line = err_str.split("\n")[0]
            if "could not translate host name" in err_str:
                err_summary = "DNS 조회 실패 (호스트 주소를 찾을 수 없음)"
            elif "tenant/user" in err_str and "not found" in err_str:
                err_summary = f"프로젝트(Tenant) 미발견 ({diag['project_ref']}) — 일시정지(Paused) 상태 가능성 높음"
            elif "password authentication failed" in err_str:
                err_summary = "DB 비밀번호 인증 실패"
            elif "Connection refused" in err_str:
                err_summary = "DB 포트 접속 거부 (서버 다운 또는 방화벽)"
            elif "timeout" in err_str.lower():
                err_summary = "접속 시간 초과 (5초 Timeout)"
            else:
                err_summary = first_err_line[:120]

            results.append((masked, err_summary))
            last_error = err_summary

    diag["supabase_connected"] = False
    diag["candidate_results"] = results
    diag["error_message"] = last_error or "연결 실패"
    return None, diag

def get_db_engine():
    """캐싱된 Supabase PostgreSQL 엔진 반환 (연결 가능 시 무조건 Supabase 우선)"""
    primary_url = get_db_url()
    engine, diag = _resolve_db_engine_cached(primary_url)
    DB_DIAGNOSTICS.update(diag)
    return engine

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
    return conn

def clear_db_cache():
    """DB 데이터 변경(업로드/삭제/동기화) 시 Streamlit 읽기 캐시 전체 무효화"""
    st.cache_data.clear()

def get_sqlite_file_size() -> float:
    """로컬 SQLite DB 파일 크기(MB) 반환"""
    if not os.path.exists(DB_FILE):
        return 0.0
    try:
        return round(os.path.getsize(DB_FILE) / (1024 * 1024), 2)
    except Exception:
        return 0.0

def optimize_sqlite_db() -> dict:
    """로컬 SQLite DB의 VACUUM 및 ANALYZE를 실행하여 빈 공간 회수 및 쿼리 최적화"""
    if not os.path.exists(DB_FILE):
        return {"status": "error", "message": f"로컬 DB 파일({DB_FILE})이 존재하지 않습니다."}
    before_mb = get_sqlite_file_size()
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("VACUUM;")
        conn.execute("ANALYZE;")
        conn.commit()
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()
    after_mb = get_sqlite_file_size()
    return {
        "status": "success",
        "before_mb": before_mb,
        "after_mb": after_mb,
        "saved_mb": round(max(0.0, before_mb - after_mb), 2),
    }

def _insert_history_batch(cursor, batch_data):
    """PostgreSQL(psycopg2 / psycopg v3) 및 SQLite 양쪽의 배치 삽입 완전 호환 함수"""
    if not batch_data:
        return
    if is_postgres():
        cursor_mod = type(cursor).__module__ or ""
        if "psycopg2" in cursor_mod:
            from psycopg2.extras import execute_values
            query = '''
                INSERT INTO regulation_history 
                (regulation_name, reg_date, unique_key, ref_no, article_title, content) 
                VALUES %s
                ON CONFLICT (regulation_name, reg_date, unique_key) DO UPDATE
                SET ref_no = EXCLUDED.ref_no, article_title = EXCLUDED.article_title, content = EXCLUDED.content
            '''
            execute_values(cursor, query, batch_data, page_size=min(len(batch_data), 1000))
        else:
            cursor.executemany('''
                INSERT INTO regulation_history 
                (regulation_name, reg_date, unique_key, ref_no, article_title, content) 
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (regulation_name, reg_date, unique_key) DO UPDATE
                SET ref_no = EXCLUDED.ref_no, article_title = EXCLUDED.article_title, content = EXCLUDED.content
            ''', batch_data)
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
        cursor.execute("ALTER TABLE regulation_history ENABLE ROW LEVEL SECURITY;")
        cursor.execute("ALTER TABLE regulation_uploads ENABLE ROW LEVEL SECURITY;")
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
    for idx_sql in indexes: cursor.execute(idx_sql)
    clean_legacy_regulation_names(conn)
    conn.commit()
    conn.close()

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

def _safe_str(val, default: str = "") -> str:
    """pandas NaN / None 값을 안전하게 빈 문자열 또는 기본값으로 치환"""
    if val is None or pd.isna(val):
        return default
    s = str(val).strip()
    return default if s.lower() == "nan" else s

def generate_key(row):
    return (
        f"{_safe_str(row.get('장번호'), '0')}_"
        f"{_safe_str(row.get('조'), '0')}_"
        f"{_safe_str(row.get('항'), '0')}_"
        f"{_safe_str(row.get('호'), '0')}_"
        f"{_safe_str(row.get('목'), '0')}"
    )

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
    except Exception:
        pass

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
                    _safe_str(row.get('참조번호', '')),
                    _safe_str(row.get('조명', '')),
                    _safe_str(row.get('내용', ''))
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
    """전체 DB를 엑셀(.xlsx)로 백업 (PostgreSQL TIMESTAMPTZ 타임존 제거 및 제어문자 정제 포함)"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if is_postgres():
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';")
            tables = cursor.fetchall()
        else:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            has_sheet = False
            for table_name in tables:
                t_name = table_name[0]
                if t_name == 'sqlite_sequence':
                    continue
                df = pd.read_sql(f'SELECT * FROM "{t_name}" ORDER BY id', conn)
                clean_df = _sanitize_df_for_excel(df)
                clean_df.to_excel(writer, sheet_name=t_name[:31], index=False)
                has_sheet = True
            if not has_sheet:
                pd.DataFrame([{"안내": "테이블이 없습니다."}]).to_excel(writer, sheet_name="empty", index=False)
        return output.getvalue()
    except Exception:
        return None
    finally:
        conn.close()


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
             _safe_str(row.get('참조번호', '')),
             _safe_str(row.get('조명', '')),
             _safe_str(row.get('내용', '')))
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


@st.cache_data(ttl=600)
def get_recent_uploads(limit: int = 10):
    """DB에 영구 기록된 최근 업로드 이력 조회 - 캐시 적용"""
    safe_limit = max(1, min(int(limit), 100))
    conn = get_connection()
    try:
        q = sql_ph("SELECT filename, regulation_name, reg_date, clause_count, uploaded_at FROM regulation_uploads ORDER BY id DESC LIMIT ?")
        df = pd.read_sql(q, conn, params=(safe_limit,))
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
        clear_db_cache()

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
def ensure_db_initialized(is_pg_mode: bool):
    """앱 기동 시 DB 초기화, 레거시 데이터 정규화, 누락된 HWP 파일 자동 동기화 1회 보장 (DB 모드별 개별 캐싱)"""
    init_db()
    ins, err, items = auto_sync_missing_hwps()
    return {"inserted": ins, "errors": err, "items": items}


# =========================================================
# 4. 캐싱된 고속 쿼리 함수군 (@st.cache_data)
# =========================================================
def _build_exact_ref_sql(variants: list, col_expr: str = "ref_no"):
    """조항 번호 변형 목록에 대해 가지조/가지호 오탐을 줄이는 SQL 후보 조건 생성"""
    conds = []
    params = []
    for v in variants:
        conds.append(f"({col_expr} = ? OR {col_expr} LIKE ?)")
        params.extend([v, f"{v}%"])
    return "(" + " OR ".join(conds) + ")", params

def _filter_df_by_exact_ref(df: pd.DataFrame, clean_ref: str, col_name: str = "ref_no") -> pd.DataFrame:
    """
    SQL LIKE 후보군 중 경계 정밀 정규식으로 실제 일치 조항 및 그 하위 항·호·목만 필터링.
    - '제20조' 조회 시 '제20조의2'~'제20조의8' 제외
    - '제26조제1항제2호' 조회 시 '제26조제1항제2호의2' 제외
    """
    if df is None or df.empty or not clean_ref:
        return df
    pat = re.compile(r"^\s*" + build_clause_pattern(clean_ref).pattern)
    mask = df[col_name].fillna("").astype(str).map(lambda x: bool(pat.match(x)))
    return df[mask].copy()

def _attach_occurrence_rank(df: pd.DataFrame, date_col: str = "reg_date", ref_col: str = "ref_no") -> pd.DataFrame:
    """
    동일 개정일자 내 동일 조항 번호(예: 본칙 제1조 vs 부칙 제1조)를 문서 순서(id) 기준으로 분리.
    - occ_idx == 0: 본칙
    - occ_idx >= 1: 부칙 (또는 후속 중복 조항)
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    if date_col in out.columns:
        out["occ_idx"] = out.groupby([date_col, ref_col]).cumcount()
    else:
        out["occ_idx"] = out.groupby(ref_col).cumcount()
    return out

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
def get_revision_diff(target_reg: str, old_date: str, new_date: str) -> dict:
    """
    메뉴 2 신규 기능: 동일 규정의 두 개정일자 간 전체 신구대조표 자동 비교.
    - 본칙/부칙을 occ_idx로 정밀 정렬하여 비교
    - 단순 띄어쓰기/마침표 차이는 is_substantive_change로 자동 무시
    """
    conn = get_connection()
    try:
        q = sql_ph("""
            SELECT id, reg_date, ref_no, article_title, content
            FROM regulation_history
            WHERE regulation_name = ? AND reg_date IN (?, ?)
            ORDER BY reg_date, id
        """)
        df = pd.read_sql(q, conn, params=(target_reg, old_date, new_date))
    finally:
        conn.close()

    if df.empty:
        return {"items": [], "counts": {"신설": 0, "변경": 0, "삭제": 0, "유지": 0}}

    df_old = _attach_occurrence_rank(df[df["reg_date"] == old_date].sort_values("id"), date_col="reg_date")
    df_new = _attach_occurrence_rank(df[df["reg_date"] == new_date].sort_values("id"), date_col="reg_date")

    old_map = {}
    for _, r in df_old.iterrows():
        old_map[(r["ref_no"], int(r["occ_idx"]))] = (r["article_title"] or "", r["content"] or "")
    new_map = {}
    for _, r in df_new.iterrows():
        new_map[(r["ref_no"], int(r["occ_idx"]))] = (r["article_title"] or "", r["content"] or "")

    all_keys = sorted(
        set(old_map.keys()) | set(new_map.keys()),
        key=lambda k: (k[1], natural_ref_sort_key(k[0]))
    )

    items = []
    counts = {"신설": 0, "변경": 0, "삭제": 0, "유지": 0}
    for ref_no, occ_idx in all_keys:
        in_old = (ref_no, occ_idx) in old_map
        in_new = (ref_no, occ_idx) in new_map
        suffix = f" [부칙{'' if occ_idx == 1 else f' #{occ_idx}'}]" if occ_idx > 0 else ""
        label = f"{ref_no}{suffix}"

        if not in_old and in_new:
            t_new, c_new = new_map[(ref_no, occ_idx)]
            counts["신설"] += 1
            items.append({
                "status": "신설", "ref_no": ref_no, "occ_idx": occ_idx, "label": label,
                "title": t_new, "old_content": "", "new_content": c_new
            })
        elif in_old and not in_new:
            t_old, c_old = old_map[(ref_no, occ_idx)]
            counts["삭제"] += 1
            items.append({
                "status": "삭제", "ref_no": ref_no, "occ_idx": occ_idx, "label": label,
                "title": t_old, "old_content": c_old, "new_content": ""
            })
        else:
            t_old, c_old = old_map[(ref_no, occ_idx)]
            t_new, c_new = new_map[(ref_no, occ_idx)]
            if is_substantive_change(c_old, c_new):
                counts["변경"] += 1
                items.append({
                    "status": "변경", "ref_no": ref_no, "occ_idx": occ_idx, "label": label,
                    "title": t_new or t_old, "old_content": c_old, "new_content": c_new
                })
            else:
                counts["유지"] += 1

    return {"items": items, "counts": counts}

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
    """메뉴 4: 조항 변경 이력 추적 - 정밀 경계 매칭 및 본칙/부칙 자동 분리"""
    conn = get_connection()
    try:
        clean_ref = normalize_article_query(ref)
        variants = get_hang_variants(clean_ref) or [clean_ref]
        conds, params_art = _build_exact_ref_sql(variants, "ref_no")
        q = f"""
            SELECT id, reg_date, ref_no, article_title, content, unique_key
            FROM regulation_history
            WHERE regulation_name=? AND {conds}
            ORDER BY reg_date, id
        """
        df = pd.read_sql(sql_ph(q), conn, params=[target] + params_art)
        df = _filter_df_by_exact_ref(df, clean_ref, col_name="ref_no")
        return _attach_occurrence_rank(df, date_col="reg_date", ref_col="ref_no")
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu5_detail(target, date, ref):
    """메뉴 5: 특정 시점 조항 상세 조회 - 정밀 경계 매칭(제20조와 제20조의2 정확 분리)"""
    conn = get_connection()
    try:
        clean_ref = normalize_article_query(ref)
        variants = get_hang_variants(clean_ref) or [clean_ref]
        conds, params_art = _build_exact_ref_sql(variants, "ref_no")
        q = f"""
            SELECT ref_no AS "조항", article_title AS "조명", content AS "내용"
            FROM regulation_history
            WHERE regulation_name=? AND reg_date=? AND {conds}
            ORDER BY id
        """
        df = pd.read_sql(sql_ph(q), conn, params=[target, date] + params_art)
        return _filter_df_by_exact_ref(df, clean_ref, col_name="조항")
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu6_keyword_search(target, keyword, latest):
    """메뉴 6: 통합 키워드 검색 - 복합 검색(AND/OR/NOT) 및 원문자/숫자 항 상호 호환(NOT 포함)"""
    conn = get_connection()
    try:
        or_groups, not_terms = parse_search_query(keyword)
        if not or_groups and not not_terms:
            return pd.DataFrame()

        like_op = "ILIKE" if is_postgres() else "LIKE"
        not_like_op = "NOT ILIKE" if is_postgres() else "NOT LIKE"
        p = []
        or_clauses = []
        for and_terms in or_groups:
            and_clauses = []
            for term in and_terms:
                vars_list = get_hang_variants(term) or [term]
                term_sub = []
                for v in vars_list:
                    term_sub.append(f"(content {like_op} ? OR article_title {like_op} ? OR ref_no {like_op} ?)")
                    p.extend([f"%{v}%", f"%{v}%", f"%{v}%"])
                and_clauses.append(f"({' OR '.join(term_sub)})")
            if and_clauses:
                or_clauses.append(f"({' AND '.join(and_clauses)})")

        not_clauses = []
        for n_term in not_terms:
            n_vars = get_hang_variants(n_term) or [n_term]
            for nv in n_vars:
                not_clauses.append(f"(content {not_like_op} ? AND article_title {not_like_op} ? AND ref_no {not_like_op} ?)")
                p.extend([f"%{nv}%", f"%{nv}%", f"%{nv}%"])

        where_parts = []
        if or_clauses:
            where_parts.append(f"({' OR '.join(or_clauses)})")
        if not_clauses:
            where_parts.append(f"({' AND '.join(not_clauses)})")

        where_expr = " AND ".join(where_parts) if where_parts else "1=1"

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
    """메뉴 7: 관심 조항 번호에 따른 원문 내용 조회 (정밀 경계 매칭 및 본칙/부칙 구분)"""
    conn = get_connection()
    try:
        clean_art = normalize_article_query(target_art)
        variants = get_hang_variants(clean_art) or [clean_art]
        col_expr = "h.ref_no" if latest_only else "ref_no"
        art_condition, params_art = _build_exact_ref_sql(variants, col_expr)

        if latest_only:
            query = f"""
                WITH LatestDate AS (
                    SELECT MAX(reg_date) as max_date 
                    FROM regulation_history 
                    WHERE regulation_name = ?
                )
                SELECT h.id, h.reg_date, h.ref_no, h.article_title, h.content
                FROM regulation_history h
                JOIN LatestDate ld ON h.regulation_name = ? AND h.reg_date = ld.max_date
                WHERE {art_condition}
                ORDER BY h.id
            """
            params = [target_reg, target_reg] + params_art
        else:
            query = f"""
                SELECT id, reg_date, ref_no, article_title, content
                FROM regulation_history
                WHERE regulation_name = ? AND {art_condition}
                ORDER BY reg_date DESC, id
            """
            params = [target_reg] + params_art

        df = pd.read_sql(sql_ph(query), conn, params=params)
        df = _filter_df_by_exact_ref(df, clean_art, col_name="ref_no")
        df = _attach_occurrence_rank(df, date_col="reg_date", ref_col="ref_no")
        return clean_art, df
    finally:
        conn.close()

@st.cache_data(ttl=600)
def get_menu7_reference_analysis(target_reg: str, target_art: str, latest_only: bool):
    """
    메뉴 7: 고정밀 조항 인용 및 역참조 분석 (캐싱).
    - 가지조/가지호 경계 보호 (제20조 vs 제20조의2, 제2호 vs 제2호의2)
    - 연속 조항 열거 문맥 상속 ('규정 제6조제2항, 제25조')
    - 공백 없는 타 규정명('유가증권시장업무규정') 및 동일 시장 내 약칭('업무규정') 인식
    - '같은 규정' 선행 법규 복원 및 조문 범위 인용('제23조 내지 제25조', '제23조부터 제27조까지') 지원
    """
    clean_art = normalize_article_query(target_art)
    variants = get_hang_variants(clean_art) or [clean_art]
    is_rule = "시행세칙" in target_reg
    partner_reg_name = (
        target_reg.replace(" 시행세칙", "").replace("시행세칙", "").strip()
        if is_rule else f"{target_reg} 시행세칙"
    )

    is_single_article = parse_article_num_key(clean_art) is not None
    prefix_tbl = "h." if latest_only else ""

    art_likes = [f"%{v}%" for v in variants]
    art_cond = " OR ".join([f"{prefix_tbl}content LIKE ?" for _ in art_likes])
    if is_single_article:
        cand_cond = f"(({art_cond}) OR {prefix_tbl}content LIKE ? OR {prefix_tbl}content LIKE ?)"
        cand_params = art_likes + ["%부터%조까지%", "%내지%조%"]
    else:
        cand_cond = f"({art_cond})"
        cand_params = art_likes

    target_nospace = re.sub(r"\s+", "", target_reg)
    target_short = target_reg.split()[-1]
    ext_name_likes = [f"%{target_reg}%", f"%{target_nospace}%"]
    if not is_rule and len(target_short) >= 3 and target_short != target_reg:
        ext_name_likes.append(f"%{target_short}%")
    ext_name_cond = " OR ".join([f"{prefix_tbl}content LIKE ?" for _ in ext_name_likes])

    where_clause = f"""
        ({prefix_tbl}regulation_name = ? AND {cand_cond}) OR
        ({prefix_tbl}regulation_name = ? AND {cand_cond}) OR
        ({prefix_tbl}regulation_name != ? AND {prefix_tbl}regulation_name != ? AND {cand_cond} AND ({ext_name_cond}))
    """
    params = (
        [target_reg] + cand_params +
        [partner_reg_name] + cand_params +
        [target_reg, partner_reg_name] + cand_params + ext_name_likes
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
            ORDER BY regulation_name, reg_date DESC, id
        """

    conn = get_connection()
    try:
        df_cand = pd.read_sql(sql_ph(full_query), conn, params=params)
    finally:
        conn.close()

    clause_pat = build_clause_pattern(clean_art)
    results_internal, results_partner, results_external = [], [], []

    for _, row in df_cand.iterrows():
        curr_reg = row["regulation_name"]
        content = row["content"] or ""
        spans_by_cat, via_range = find_article_citations_in_text(
            content=content,
            clean_art=clean_art,
            curr_reg=curr_reg,
            target_reg=target_reg,
            partner_reg=partner_reg_name,
            clause_pat=clause_pat,
        )
        row_dict = row.to_dict()
        if spans_by_cat["internal"]:
            results_internal.append({"row": row_dict, "spans": spans_by_cat["internal"], "is_range": via_range})
        if spans_by_cat["partner"]:
            results_partner.append({"row": row_dict, "spans": spans_by_cat["partner"], "is_range": via_range})
        if spans_by_cat["external"]:
            results_external.append({"row": row_dict, "spans": spans_by_cat["external"], "is_range": via_range})

    return {
        "clean_art": clean_art,
        "partner_reg_name": partner_reg_name,
        "internal": results_internal,
        "partner": results_partner,
        "external": results_external,
    }


# =========================================================
# 5. UI 네비게이션 · 세션 상태 · 메인 앱 렌더링
# =========================================================

QUICK_KEYWORDS = [
    "공매도",
    "유동성공급",
    "매매거래정지",
    "단일가매매",
    "가격제한폭 | 변동성",
    "상장폐지 | 정리매매",
]

def _navigate_to(
    menu_key: str,
    reg: str = None,
    art: str = None,
    date: str = None,
    kw: str = None,
    auto_run: bool = True,
):
    """어느 메뉴에서든 다른 메뉴로 규정·조항·검색어를 자동 입력하여 즉시 이동"""
    st.session_state["_nav_pending"] = {
        "menu_key": menu_key,
        "reg": reg,
        "art": art,
        "date": date,
        "kw": kw,
        "auto_run": auto_run,
    }
    st.rerun()

def _record_recent_view(reg: str, art: str, menu_key: str):
    """최근 조회 조항 기록 (사이드바 빠른 복귀용, 최대 6개 유지)"""
    if not reg or not art:
        return
    history = list(st.session_state.get("recent_views", []))
    entry = {"reg": reg, "art": art, "menu_key": menu_key}
    history = [h for h in history if not (h["reg"] == reg and h["art"] == art and h["menu_key"] == menu_key)]
    history.insert(0, entry)
    st.session_state["recent_views"] = history[:6]

def _apply_pending_navigation(reg_names: list):
    """위젯 생성 전 보류 중인 네비게이션 요청을 session_state에 안전하게 반영"""
    pending = st.session_state.pop("_nav_pending", None)
    if not pending:
        return
    mkey = pending.get("menu_key")
    if mkey in MENU_NAMES:
        st.session_state["active_menu"] = MENU_NAMES[mkey]

    reg = pending.get("reg")
    art = pending.get("art")
    date = pending.get("date")
    kw = pending.get("kw")
    auto_run = pending.get("auto_run", True)
    valid_reg = reg if (reg and reg in reg_names) else None

    if mkey == "2" and valid_reg:
        st.session_state["m2_reg"] = valid_reg
    elif mkey == "3" and valid_reg:
        st.session_state["m3_reg"] = valid_reg
        dates = get_regulation_dates(valid_reg)
        chosen_date = date if (date and date in dates) else (dates[0] if dates else None)
        if chosen_date:
            st.session_state["m3_date"] = chosen_date
            if auto_run:
                st.session_state["m3_active"] = {"reg": valid_reg, "date": chosen_date}
                st.session_state["m3_page"] = 1
    elif mkey == "4" and valid_reg:
        st.session_state["m4_reg"] = valid_reg
        if art:
            st.session_state["m4_ref"] = art
            if auto_run:
                st.session_state["m4_active"] = {"reg": valid_reg, "ref": art}
    elif mkey == "5" and valid_reg:
        st.session_state["m5_reg"] = valid_reg
        dates = get_regulation_dates(valid_reg)
        chosen_date = date if (date and date in dates) else (dates[0] if dates else None)
        if chosen_date:
            st.session_state["m5_date"] = chosen_date
        if art:
            st.session_state["m5_ref"] = art
            if auto_run and chosen_date:
                st.session_state["m5_active"] = {"reg": valid_reg, "date": chosen_date, "ref": art}
    elif mkey == "6":
        if kw is not None:
            st.session_state["m6_kw"] = kw
            if auto_run and kw.strip():
                st.session_state["m6_active"] = {
                    "target": st.session_state.get("m6_target", "전체 규정 (All)"),
                    "keyword": kw.strip(),
                    "latest": st.session_state.get("m6_latest", True),
                }
                st.session_state["m6_page"] = 1
    elif mkey == "7" and valid_reg:
        st.session_state["m7_reg"] = valid_reg
        if art:
            st.session_state["m7_art"] = art
            if auto_run:
                st.session_state["m7_active"] = {
                    "reg": valid_reg,
                    "art": art,
                    "latest": st.session_state.get("m7_latest", True),
                }

def _inject_custom_css():
    """가독성과 다크/라이트 모드 호환성을 높이는 경량 CSS 스타일"""
    st.markdown(
        """
        <style>
        .rule-card-meta { font-size: 0.85rem; opacity: 0.78; margin-bottom: 4px; }
        .rule-range-badge {
            display: inline-block; font-size: 0.75rem; font-weight: 600;
            background-color: rgba(255, 165, 0, 0.18); color: #b45309;
            padding: 1px 7px; border-radius: 10px; margin-left: 6px;
        }
        .rule-addendum-badge {
            display: inline-block; font-size: 0.75rem; font-weight: 600;
            background-color: rgba(99, 102, 241, 0.16); color: #4338ca;
            padding: 1px 7px; border-radius: 10px; margin-left: 6px;
        }
        .rule-status-new {
            background-color: #e6ffed; color: #1a7f37;
            padding: 10px 14px; border-radius: 8px; border: 1px solid #aceebb; line-height: 1.65;
        }
        .rule-status-del {
            background-color: #ffeef0; color: #cf222e;
            padding: 10px 14px; border-radius: 8px; border: 1px solid #ffcecb;
            text-decoration: line-through; line-height: 1.65;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

def _render_pagination(total_items: int, page_size: int, state_key: str):
    """재사용 가능한 페이지네이션 컨트롤 (start_idx, end_idx 반환)"""
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    cur_page = st.session_state.get(state_key, 1)
    if cur_page < 1 or cur_page > total_pages:
        cur_page = 1
        st.session_state[state_key] = 1

    if total_pages > 1:
        p1, p2, p3 = st.columns([1, 2, 1])
        with p1:
            if st.button("◀ 이전 페이지", key=f"{state_key}_prev", disabled=(cur_page <= 1), width="stretch"):
                st.session_state[state_key] = cur_page - 1
                st.rerun()
        with p2:
            st.markdown(
                f"<div style='text-align:center; padding-top:6px;'>"
                f"<strong>{cur_page} / {total_pages} 페이지</strong> (총 {total_items:,}건 중 {(cur_page - 1) * page_size + 1}~{min(cur_page * page_size, total_items)}건)"
                f"</div>",
                unsafe_allow_html=True,
            )
        with p3:
            if st.button("다음 페이지 ▶", key=f"{state_key}_next", disabled=(cur_page >= total_pages), width="stretch"):
                st.session_state[state_key] = cur_page + 1
                st.rerun()

    start_idx = (cur_page - 1) * page_size
    end_idx = min(start_idx + page_size, total_items)
    return start_idx, end_idx

def _render_sidebar(db_stats: dict):
    """좌측 사이드바: DB 연결 현황 · 참고 자료 관리 · 메뉴 선택 · 최근 조회 기록"""
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

                    if st.button("🔄 Supabase 다시 연결", type="primary", width="stretch"):
                        _resolve_db_engine_cached.clear()
                        _connect_postgres_cached.clear()
                        ensure_db_initialized.clear()
                        clear_db_cache()
                        st.rerun()
                st.caption("ℹ️ 현재는 로컬 DB(`regulation_master.db`)를 읽어 정상 검색이 가능합니다. (새로 추가한 데이터는 로컬에만 반영됨)")
            else:
                st.warning("📁 **로컬 DB (SQLite)**")
                st.caption("⚠️ SUPABASE_DB_URL이 설정되지 않아 로컬 SQLite 모드로 동작합니다. 영구 저장을 위해 Secrets에 URL을 등록해주세요.")

        sqlite_mb = get_sqlite_file_size()
        st.caption(f"📊 실데이터: **{db_stats['reg_count']}개 규정** / **{db_stats['row_count']:,}건 조항**")
        if sqlite_mb > 0:
            st.caption(f"💾 로컬 백업 DB: **{sqlite_mb} MB**")
        st.markdown("---")

        # =====================================================
        # 기능 선택 (최상단 바로 아래 — 접근성 최우선)
        # =====================================================
        st.header("🔍 기능 선택")
        if "active_menu" not in st.session_state:
            st.session_state["active_menu"] = MENU_NAMES["1"]
        menu = st.radio("메뉴 선택", list(MENU_NAMES.values()), key="active_menu")

        # =====================================================
        # 최근 조회 기록 (원클릭 복귀)
        # =====================================================
        recent_views = st.session_state.get("recent_views", [])
        if recent_views:
            st.markdown("---")
            st.caption("🕒 **최근 조회 조항 (클릭하여 이동)**")
            for idx, rv in enumerate(recent_views):
                icon = "🕰️" if rv["menu_key"] == "4" else ("🔎" if rv["menu_key"] == "5" else "🔗")
                short_r = rv["reg"].replace("유가증권시장 ", "유가 ").replace("코스닥시장 ", "코스닥 ").replace(" 시행세칙", " 세칙")
                if st.button(f"{icon} {short_r} {rv['art']}", key=f"recent_jump_{idx}", width="stretch"):
                    _navigate_to(rv["menu_key"], reg=rv["reg"], art=rv["art"], auto_run=True)

        st.markdown("---")

        # =====================================================
        # 참고 자료 관리 메뉴 (접기 가능) — 원본 파일 보관 및 DB 적재
        # =====================================================
        with st.expander("📁 참고 자료 관리 (업로드 · 동기화 · 백업)", expanded=False):
            st.caption("ℹ️ '규정' 폴더의 파일들은 원본 참고 자료 및 백업용입니다. 시스템의 모든 검색과 목록은 DB 실데이터를 기준으로 실시간 동작합니다.")

            # --- (1) 파일 업로드 → 자동 DB 등록 (원스톱) ---
            st.markdown("**(1) 신규 파일 업로드 → DB 자동 등록**")

            if is_postgres():
                st.info("💡 **Supabase 클라우드 저장**: 업로드 시 파싱된 모든 조항이 클라우드 DB에 즉시 적재되어 영구 보존됩니다.")
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
                    - `_전문_` : 규정명과 일자를 구분 (권장)
                    - `.hwp`, `.txt`, `.csv` 파일 형식 모두 지원
                    """
                )

            overwrite_opt = st.checkbox("🔄 기존 동일 개정일자 존재 시 덮어쓰기", value=False)
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
                            st.info(f"⏭️ [{res['reg_name']}] {res['reg_date']} — 이미 등록되어 건너뜀")
                        else:
                            errs += 1
                            st.error(f"❌ {res['filename']} — {res.get('message', '알 수 없는 오류')}")
                        progress_bar.progress((i + 1) / len(uploaded_files))
                    progress_bar.empty()
                    st.success(f"처리 완료! (신규: {ins} / 갱신: {upd} / 건너뜀: {skip} / 오류: {errs})")
                    if ins > 0 or upd > 0:
                        st.button("🔄 최신 데이터 즉시 반영 (새로고침)", on_click=st.rerun)

            recent_df = get_recent_uploads(5)
            if not recent_df.empty:
                with st.expander("📋 최근 업로드 이력 (DB 영구 기록)"):
                    st.dataframe(recent_df, hide_index=True, width="stretch")

            st.markdown("---")

            # --- (2) 규정 폴더 HWP 자동 검사 및 DB 적재 ---
            st.markdown("**(2) 규정 폴더 HWP 자동 검사 및 DB 적재**")
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
                        st.info("ℹ️ 모든 HWP 파일이 이미 DB에 등록되어 있습니다.")

            st.markdown("---")

            # --- (3) 개별 수동 도구 ---
            st.markdown("**(3) 개별 수동 도구 (단계별 변환)**")
            if st.button("📄 HWP -> TXT 변환"):
                if not HAS_PYHWP:
                    st.error("pyhwp 라이브러리가 설치되어 있지 않습니다. 'pip install pyhwp'를 실행해주세요.")
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

            st.markdown("---")

            # --- (4) DB 특정 개정본 삭제 ---
            st.markdown("**(4) DB 특정 개정본 삭제 관리**")
            reg_names_for_del = get_regulation_names()
            if reg_names_for_del:
                del_reg_name = st.selectbox("삭제 대상 규정 선택", reg_names_for_del, key="del_reg_select")
                del_dates = get_regulation_dates(del_reg_name)
                if del_dates:
                    del_reg_date = st.selectbox("삭제 대상 개정일자 선택", del_dates, key="del_date_select")
                    target_count = get_clause_count(del_reg_name, del_reg_date)
                    st.warning(f"선택 항목: **[{del_reg_name}] {del_reg_date}** (총 {target_count:,}건 조항)")

                    del_files_opt = st.checkbox(
                        "로컬 참고 파일(.hwp, .txt, .csv)도 함께 삭제",
                        value=False,
                        key="del_files_checkbox",
                    )
                    del_confirm = st.checkbox(
                        f"⚠️ [{del_reg_name}] ({del_reg_date}) DB 삭제에 동의합니다.",
                        key="del_confirm_checkbox",
                    )
                    if st.button("🗑️ 선택한 개정본 DB에서 삭제", type="secondary", disabled=not del_confirm):
                        with st.spinner("DB에서 삭제 중입니다..."):
                            del_res = delete_regulation_revision(del_reg_name, del_reg_date, delete_local_files=del_files_opt)
                        if del_res.get("status") == "success":
                            msg = f"✅ [{del_res['reg_name']}] {del_res['reg_date']} {del_res['deleted_rows']:,}건 삭제 완료."
                            if del_res["deleted_files"]:
                                msg += f" (로컬 파일 {len(del_res['deleted_files'])}개 삭제)"
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(f"삭제 실패: {del_res.get('message', '알 수 없는 오류')}")
                else:
                    st.info("해당 규정에 등록된 개정일자가 없습니다.")
            else:
                st.info("DB에 등록된 규정이 없습니다.")

            st.markdown("---")

            # --- (5) 로컬 DB 최적화 (VACUUM) ---
            st.markdown("**(5) 로컬 SQLite DB 최적화 (VACUUM)**")
            st.caption(f"현재 로컬 DB 크기: **{sqlite_mb} MB**")
            if st.button("🧹 로컬 DB 압축 최적화"):
                with st.spinner("SQLite VACUUM 및 ANALYZE 실행 중..."):
                    opt_res = optimize_sqlite_db()
                if opt_res.get("status") == "success":
                    st.success(f"✅ {opt_res['before_mb']} MB ➜ **{opt_res['after_mb']} MB** (절감: {opt_res['saved_mb']} MB)")
                    st.rerun()
                else:
                    st.error(f"최적화 실패: {opt_res.get('message')}")

            st.markdown("---")

            # --- (6) 데이터 백업 및 내보내기 ---
            st.markdown("**(6) 데이터 백업 및 내보내기**")
            if st.button("📥 DB 전체 엑셀 다운로드 준비"):
                with st.spinner("엑셀 파일 생성 중... (데이터 양에 따라 수 초 소요될 수 있습니다)"):
                    if is_postgres() or os.path.exists(DB_FILE):
                        excel_data = export_db_to_excel()
                        if excel_data:
                            st.download_button(
                                label="💾 전체 백업 엑셀 다운로드",
                                data=excel_data,
                                file_name="regulation_db_dump.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            )
                        else:
                            st.error("엑셀 백업 생성 중 오류가 발생했습니다.")

    return menu

# =========================================================
# 6. 메뉴별 렌더링 함수 (Menu 1 ~ Menu 7)
# =========================================================

def _render_menu1(reg_names: list):
    st.subheader("📂 시스템 DB 등록 규정 목록 (실시간 실데이터)")
    st.caption("※ 실제 데이터베이스(DB)에 적재된 전체 규정 및 개정본 현황을 실시간 집계합니다.")
    df_summary = get_menu1_summary()
    if df_summary.empty:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다. 사이드바의 '참고 자료 관리'에서 파일을 등록해주세요.")
        return

    m1, m2, m3 = st.columns(3)
    m1.metric("📚 등록 규정 수", f"{len(df_summary):,}개")
    m2.metric("📅 총 개정본 수", f"{int(df_summary['개정본 수'].sum()):,}개")
    m3.metric("📄 총 등록 조항 수", f"{int(df_summary['총 조항(레코드) 수'].sum()):,}건")

    st.dataframe(df_summary, width="stretch", hide_index=True)

    st.markdown("---")
    st.markdown("##### ⚡ 선택한 규정 바로 열기")
    c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment="bottom")
    with c1:
        default_idx = reg_names.index(PREFERRED_REG_NAME) if PREFERRED_REG_NAME in reg_names else 0
        quick_reg = st.selectbox("규정 선택", reg_names, index=default_idx, key="m1_quick_reg")
    with c2:
        if st.button("📖 최신 전문 보기", type="primary", width="stretch", key="m1_go_m3"):
            _navigate_to("3", reg=quick_reg, auto_run=True)
    with c3:
        if st.button("⚖️ 개정 신구대조", width="stretch", key="m1_go_m2"):
            _navigate_to("2", reg=quick_reg, auto_run=False)

def _render_menu2(reg_names: list, default_reg_index: int):
    st.subheader("📅 규정별 개정 히스토리 및 개정일 간 신구대조표")
    st.caption("※ 선택한 규정의 개정본 목록을 확인하고, **두 개정일 사이의 전체 변경 조항 신구대조표**를 자동 생성합니다.")
    if not reg_names:
        st.info("데이터가 없습니다.")
        return

    if "m2_reg" not in st.session_state or st.session_state["m2_reg"] not in reg_names:
        st.session_state["m2_reg"] = reg_names[default_reg_index]
    target = st.selectbox("규정 선택", reg_names, key="m2_reg")
    df_dates = get_menu2_history(target)
    dates = df_dates["개정일자"].tolist() if not df_dates.empty else []

    tab_list, tab_diff = st.tabs([
        f"📅 개정일자 목록 ({len(dates)}개)",
        "⚖️ 두 개정일 간 전체 신구대조표 비교",
    ])

    with tab_list:
        st.write(f"**{target}** 개정일 목록 (DB 실데이터 기준 총 **{len(df_dates)}개** 개정본):")
        st.dataframe(df_dates, width="stretch", hide_index=True)
        if dates:
            c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
            with c1:
                open_d = st.selectbox("전문 조회할 개정일자", dates, key="m2_open_date")
            with c2:
                if st.button("📖 해당 버전 전문 보기", type="primary", width="stretch", key="m2_open_btn"):
                    _navigate_to("3", reg=target, date=open_d, auto_run=True)

    with tab_diff:
        if len(dates) < 2:
            st.info("신구대조표를 비교하려면 해당 규정에 최소 2개 이상의 개정본이 등록되어 있어야 합니다.")
            return

        c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        with c1:
            old_date = st.selectbox("이전 개정일 (구법)", dates, index=min(1, len(dates) - 1), key=f"m2_old_{target}")
        with c2:
            new_date = st.selectbox("비교 개정일 (신법)", dates, index=0, key=f"m2_new_{target}")
        with c3:
            run_diff = st.button("⚖️ 비교 실행", type="primary", width="stretch", key="m2_run_diff")

        if run_diff:
            if old_date == new_date:
                st.warning("서로 다른 두 개정일자를 선택해주세요.")
            else:
                st.session_state["m2_diff_active"] = {"reg": target, "old": old_date, "new": new_date}
                st.session_state["m2_diff_page"] = 1

        active = st.session_state.get("m2_diff_active")
        if active and active["reg"] == target and active["old"] != active["new"]:
            with st.spinner("두 개정본의 전체 조문을 비교하여 신구대조표를 생성 중입니다..."):
                diff_res = get_revision_diff(active["reg"], active["old"], active["new"])

            counts = diff_res["counts"]
            all_items = diff_res["items"]

            st.markdown(f"#### 📊 **[{active['reg']}]** `{format_date_kr(active['old'])}` ➜ `{format_date_kr(active['new'])}` 개정 요약")
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("🆕 신설", f"{counts['신설']:,}건")
            mc2.metric("✏️ 변경", f"{counts['변경']:,}건")
            mc3.metric("🗑️ 삭제", f"{counts['삭제']:,}건")
            mc4.metric("─ 유지 (동일)", f"{counts['유지']:,}건")

            if not all_items:
                st.info("두 개정본 사이에 실질적인 조문 내용 변경이 없습니다. (단순 띄어쓰기·마침표 차이 자동 무시)")
                return

            f1, f2, f3 = st.columns([2, 2, 1.5], vertical_alignment="bottom")
            with f1:
                sel_statuses = st.multiselect(
                    "변경 유형 필터",
                    ["신설", "변경", "삭제"],
                    default=["신설", "변경", "삭제"],
                    key="m2_diff_statuses",
                )
            with f2:
                diff_kw = st.text_input("결과 내 조항/키워드 필터", placeholder="예: 제20조의2 또는 공매도", key="m2_diff_kw")
            with f3:
                excel_diff = export_revision_diff_to_excel(all_items, active["reg"], active["old"], active["new"])
                safe_r = active["reg"].replace(" ", "_")
                st.download_button(
                    label="📥 신구대조표 엑셀(.xlsx)",
                    data=excel_diff,
                    file_name=f"신구대조표_{safe_r}_{active['old']}_{active['new']}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
                )

            filtered_items = [x for x in all_items if x["status"] in sel_statuses]
            if diff_kw and diff_kw.strip():
                kw_l = diff_kw.strip()
                filtered_items = [
                    x for x in filtered_items
                    if kw_l in x["label"] or kw_l in x["title"] or kw_l in x["old_content"] or kw_l in x["new_content"]
                ]

            st.caption(f"표시 대상: **{len(filtered_items):,}건** (전체 변경 {len(all_items):,}건 중)")
            s_idx, e_idx = _render_pagination(len(filtered_items), 25, "m2_diff_page")

            for idx, item in enumerate(filtered_items[s_idx:e_idx], start=s_idx):
                status = item["status"]
                badge_map = {"신설": ("🆕 신설", "blue"), "변경": ("✏️ 변경", "orange"), "삭제": ("🗑️ 삭제", "red")}
                b_txt, b_col = badge_map[status]
                title_part = f" ({item['title']})" if item["title"] else ""
                with st.container(border=True):
                    hc1, hc2, hc3 = st.columns([6, 1.2, 1.2])
                    with hc1:
                        st.markdown(f":{b_col}[**{b_txt}**] **📌 {item['label']}{title_part}**")
                    with hc2:
                        if st.button("🕰️ 이력", key=f"m2d_h_{idx}", width="stretch"):
                            _navigate_to("4", reg=active["reg"], art=extract_base_article(item["ref_no"]), auto_run=True)
                    with hc3:
                        if st.button("🔗 역참조", key=f"m2d_r_{idx}", width="stretch"):
                            _navigate_to("7", reg=active["reg"], art=extract_base_article(item["ref_no"]), auto_run=True)

                    if status == "변경":
                        diff_html = generate_inline_diff_html(item["old_content"], item["new_content"])
                        st.markdown(render_diff_box_html(diff_html), unsafe_allow_html=True)
                    elif status == "신설":
                        st.markdown(f"<div class='rule-status-new'>{html.escape(item['new_content'])}</div>", unsafe_allow_html=True)
                    else:
                        st.markdown(f"<div class='rule-status-del'>{html.escape(item['old_content'])}</div>", unsafe_allow_html=True)

def _render_menu3(reg_names: list, default_reg_index: int):
    st.subheader("📖 규정 전문 조회")
    st.caption("선택한 규정 및 개정일자의 전체 조문을 리더 뷰/테이블 뷰로 조회하고 즉석 필터링·다운로드합니다.")
    if not reg_names:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")
        return

    if "m3_reg" not in st.session_state or st.session_state["m3_reg"] not in reg_names:
        st.session_state["m3_reg"] = reg_names[default_reg_index]

    c1, c2, c3 = st.columns([2.5, 1.5, 1], vertical_alignment="bottom")
    with c1:
        target = st.selectbox("규정", reg_names, key="m3_reg")
    dates = get_regulation_dates(target)
    if dates and st.session_state.get("m3_date") not in dates:
        st.session_state["m3_date"] = dates[0]
    with c2:
        date = st.selectbox("날짜", dates, key="m3_date") if dates else st.selectbox("날짜", [], key="m3_date_empty")
    with c3:
        if st.button("조회", type="primary", width="stretch", key="m3_query_btn") and date:
            st.session_state["m3_active"] = {"reg": target, "date": date}
            st.session_state["m3_page"] = 1

    active = st.session_state.get("m3_active")
    if not active:
        return

    a_reg, a_date = active["reg"], active["date"]
    df = get_menu3_full_text(a_reg, a_date)
    if df.empty:
        st.warning("해당 일자의 조문 데이터가 없습니다.")
        return

    # 전문 내 즉석 검색 필터
    fc1, fc2, fc3 = st.columns([2.5, 1.25, 1.25], vertical_alignment="bottom")
    with fc1:
        in_filter = st.text_input(
            "🔎 전문 내 빠른 필터 (조항 번호 또는 키워드)",
            placeholder="예: 제20조의2 또는 공매도 (입력 즉시 필터링)",
            key="m3_in_filter",
        )
    filtered_df = df
    filter_terms = []
    if in_filter and in_filter.strip():
        kw_s = in_filter.strip()
        filter_terms = get_hang_variants(kw_s) or [kw_s]
        mask = filtered_df.apply(
            lambda r: any(t in str(r["조항"]) or t in str(r["조명"]) or t in str(r["내용"]) for t in filter_terms),
            axis=1,
        )
        filtered_df = filtered_df[mask].reset_index(drop=True)

    with fc2:
        excel_bytes = export_dataframe_to_excel(filtered_df, sheet_name=a_reg[:30])
        st.download_button(
            label=f"📥 엑셀(.xlsx) ({len(filtered_df):,}건)",
            data=excel_bytes,
            file_name=f"{a_reg}_{a_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
    with fc3:
        full_txt = "\n\n".join([f"[{r['조항']}] {r['조명']}\n{r['내용']}" for _, r in filtered_df.iterrows()])
        st.download_button(
            label="📥 텍스트(.txt)",
            data=full_txt.encode("utf-8-sig"),
            file_name=f"{a_reg}_{a_date}.txt",
            mime="text/plain",
            width="stretch",
        )

    st.success(f"**[{a_reg}] {format_date_kr(a_date)}** — 총 {len(filtered_df):,}개 조항 (전체 {len(df):,}건)")

    tab_reader, tab_table = st.tabs(["📄 문서 리더 뷰 (가독성)", "📊 테이블 뷰"])
    with tab_reader:
        if filtered_df.empty:
            st.warning("필터 조건과 일치하는 조항이 없습니다.")
        else:
            s_idx, e_idx = _render_pagination(len(filtered_df), 30, "m3_page")
            for idx, row in filtered_df.iloc[s_idx:e_idx].iterrows():
                title_part = f" ({row['조명']})" if row["조명"] else ""
                with st.container(border=True):
                    rc1, rc2, rc3 = st.columns([6, 1.1, 1.1])
                    with rc1:
                        st.markdown(f"**📌 {row['조항']}{title_part}**")
                    with rc2:
                        if st.button("🕰️ 이력", key=f"m3_h_{idx}", width="stretch"):
                            _navigate_to("4", reg=a_reg, art=extract_base_article(row["조항"]), auto_run=True)
                    with rc3:
                        if st.button("🔗 역참조", key=f"m3_r_{idx}", width="stretch"):
                            _navigate_to("7", reg=a_reg, art=extract_base_article(row["조항"]), auto_run=True)
                    body_md = highlight_terms(str(row["내용"]), filter_terms, "red") if filter_terms else str(row["내용"])
                    st.markdown(body_md)
    with tab_table:
        st.dataframe(filtered_df, width="stretch", height=620, hide_index=True)

def _render_menu4(reg_names: list, default_reg_index: int):
    st.subheader("🕰️ 조항 변경 이력 추적")
    st.caption("조항 번호를 입력하여 역대 개정 이력과 개정 전·후 신구대조표(Inline Diff)를 조회합니다. (Enter 키 즉시 조회 · `20-2`, `20` 등 축약 입력 지원)")
    if not reg_names:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")
        return

    if "m4_reg" not in st.session_state or st.session_state["m4_reg"] not in reg_names:
        st.session_state["m4_reg"] = reg_names[default_reg_index]
    if "m4_ref" not in st.session_state:
        st.session_state["m4_ref"] = DEFAULT_ART_NO

    with st.form("history_form", border=False):
        c1, c2 = st.columns(2)
        with c1:
            target = st.selectbox("규정", reg_names, key="m4_reg")
        with c2:
            ref = st.text_input("조항 번호", key="m4_ref", placeholder="예: 제20조의2, 20-2, 제25조")
        search_btn = st.form_submit_button("히스토리 검색", type="primary")

    if search_btn and ref and ref.strip():
        st.session_state["m4_active"] = {"reg": target, "ref": ref.strip()}
        _record_recent_view(target, normalize_article_query(ref.strip()), "4")

    active = st.session_state.get("m4_active")
    if not active:
        return

    a_reg, a_ref = active["reg"], active["ref"]
    norm_ref = normalize_article_query(a_ref)
    df = get_menu4_history(a_reg, norm_ref)
    if df.empty:
        st.warning(f"**[{a_reg}]**에서 '{norm_ref}' 조항의 이력 결과가 없습니다. (입력값: `{a_ref}`)")
        return

    # 상단 액션 바
    ac1, ac2, ac3 = st.columns([2.5, 1.25, 1.25], vertical_alignment="center")
    with ac1:
        changes_only = st.checkbox(
            "✏️ 실제 변경·신설 시점만 모아보기 (유지 버전 숨김)",
            value=True,
            key="m4_changes_only",
        )
    with ac2:
        if st.button("🔗 이 조항 역참조 분석", width="stretch", key="m4_jump_m7"):
            _navigate_to("7", reg=a_reg, art=norm_ref, auto_run=True)
    with ac3:
        export_cols = [c for c in ["reg_date", "ref_no", "article_title", "content"] if c in df.columns]
        excel_m4 = export_dataframe_to_excel(
            df[export_cols].rename(columns={"reg_date": "개정일자", "ref_no": "조항", "article_title": "조명", "content": "내용"}),
            sheet_name=f"{norm_ref}_이력",
        )
        st.download_button(
            label="📥 변경 이력 엑셀(.xlsx)",
            data=excel_m4,
            file_name=f"변경이력_{a_reg.replace(' ', '_')}_{norm_ref}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

    # 본칙(occ_idx=0) 우선, 자연 법령 순서 정렬
    grouped_keys = sorted(
        df.groupby(["occ_idx", "ref_no"]).groups.keys(),
        key=lambda k: (k[0], natural_ref_sort_key(k[1])),
    )

    for occ_idx, r_no in grouped_keys:
        group = df[(df["occ_idx"] == occ_idx) & (df["ref_no"] == r_no)].sort_values("reg_date")
        latest_title = ""
        for t in reversed(group["article_title"].tolist()):
            if t:
                latest_title = t
                break

        # 이력 사전 계산 (타임라인 요약 생성용)
        timeline = []
        prev = None
        change_cnt = 0
        for _, row in group.iterrows():
            content = row["content"] or ""
            if prev is None:
                badge, color = "🆕 신설", "blue"
            elif is_substantive_change(prev, content):
                badge, color = "✏️ 변경", "orange"
                change_cnt += 1
            else:
                badge, color = "─ 유지", "grey"
            timeline.append((row["reg_date"], badge, color, prev, content))
            prev = content

        addendum_tag = f" [부칙{'' if occ_idx == 1 else f' #{occ_idx}'}]" if occ_idx > 0 else ""
        title_str = f" ({latest_title})" if latest_title else ""
        summary_hint = f"총 {len(timeline)}개 버전 · 실질 변경 {change_cnt}회"

        with st.expander(f"📌 {r_no}{addendum_tag}{title_str}  —  {summary_hint}", expanded=(occ_idx == 0)):
            # 간략 타임라인 배지
            milestones = [f"{b.split()[0]} {format_date_kr(d)}" for d, b, _, _, _ in timeline if b != "─ 유지"]
            st.caption("📍 **개정 연혁**: " + " → ".join(milestones))

            rendered_any = False
            for reg_d, badge, color, prev_txt, curr_txt in timeline:
                if changes_only and badge == "─ 유지":
                    continue
                rendered_any = True
                st.markdown(f":{color}[**[{format_date_kr(reg_d)}] {badge}**]")
                if badge == "✏️ 변경":
                    diff_html = generate_inline_diff_html(prev_txt, curr_txt)
                    st.markdown(render_diff_box_html(diff_html), unsafe_allow_html=True)
                    with st.expander("📄 개정 후 원문 전문 보기", expanded=False):
                        st.caption(curr_txt)
                elif badge == "🆕 신설":
                    st.markdown(f"> {curr_txt}")
                else:
                    st.caption(curr_txt)
                st.divider()
            if not rendered_any:
                st.caption("표시할 변경 내역이 없습니다.")

def _render_menu5(reg_names: list, default_reg_index: int):
    st.subheader("🔎 특정 시점 조항 상세 조회")
    st.caption("특정 규정의 지정 일자 버전에 수록된 조항의 상세 내용을 정확히 조회합니다. (`제20조` 조회 시 `제20조의2` 자동 제외)")
    if not reg_names:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")
        return

    if "m5_reg" not in st.session_state or st.session_state["m5_reg"] not in reg_names:
        st.session_state["m5_reg"] = reg_names[default_reg_index]
    if "m5_ref" not in st.session_state:
        st.session_state["m5_ref"] = DEFAULT_ART_NO

    c1, c2 = st.columns(2)
    with c1:
        target = st.selectbox("규정", reg_names, key="m5_reg")
    dates = get_regulation_dates(target)
    if dates and st.session_state.get("m5_date") not in dates:
        st.session_state["m5_date"] = dates[0]
    with c2:
        date = st.selectbox("날짜", dates, key="m5_date") if dates else st.selectbox("날짜", [], key="m5_date_empty")

    with st.form("detail_form", border=False):
        c3, c4 = st.columns([4, 1], vertical_alignment="bottom")
        with c3:
            ref = st.text_input("조항 번호", key="m5_ref", placeholder="예: 제20조의2, 20-2, 제25조")
        with c4:
            search_btn = st.form_submit_button("조회", type="primary", width="stretch")

    if search_btn and ref and ref.strip() and date:
        st.session_state["m5_active"] = {"reg": target, "date": date, "ref": ref.strip()}
        _record_recent_view(target, normalize_article_query(ref.strip()), "5")

    active = st.session_state.get("m5_active")
    if not active:
        return

    a_reg, a_date, a_ref = active["reg"], active["date"], active["ref"]
    norm_ref = normalize_article_query(a_ref)
    df = get_menu5_detail(a_reg, a_date, norm_ref)
    if df.empty:
        st.warning(f"**[{a_reg} / {format_date_kr(a_date)}]**에서 '{norm_ref}' 조항을 찾을 수 없습니다. (입력값: `{a_ref}`)")
        return

    # 빠른 연계 액션 바
    ac1, ac2, ac3, ac4 = st.columns([1.8, 1.2, 1.2, 1.2], vertical_alignment="center")
    with ac1:
        st.success(f"**[{a_reg}] {norm_ref}** ({format_date_kr(a_date)}) — {len(df)}건")
    with ac2:
        if st.button("🕰️ 변경 이력", width="stretch", key="m5_go_m4"):
            _navigate_to("4", reg=a_reg, art=norm_ref, auto_run=True)
    with ac3:
        if st.button("🔗 역참조 분석", width="stretch", key="m5_go_m7"):
            _navigate_to("7", reg=a_reg, art=norm_ref, auto_run=True)
    with ac4:
        st.download_button(
            label="📥 엑셀 다운로드",
            data=export_dataframe_to_excel(df, sheet_name=norm_ref[:30]),
            file_name=f"{a_reg.replace(' ', '_')}_{a_date}_{norm_ref}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

    tab_card, tab_tbl, tab_copy = st.tabs(["📄 법령 카드 뷰", "📊 테이블 뷰", "📋 원문 복사"])
    with tab_card:
        for _, row in df.iterrows():
            title_s = f" ({row['조명']})" if row["조명"] else ""
            with st.container(border=True):
                st.markdown(f"**📌 {row['조항']}{title_s}**")
                st.markdown(f"> {row['내용']}")
    with tab_tbl:
        st.dataframe(df, width="stretch", hide_index=True)
    with tab_copy:
        plain_txt = "\n\n".join([f"[{r['조항']}] {r['조명']}\n{r['내용']}" for _, r in df.iterrows()])
        st.code(plain_txt, language=None)

def _render_menu6(reg_names: list):
    st.subheader("🔍 통합 키워드 검색")
    st.caption("💡 **복합 검색 연산자 지원**: 공백(AND) · `|` (OR) · `-` (NOT 제외)  |  예: `공매도 호가` / `거래정지 | 중단` / `정리매매 -세칙`")
    if not reg_names:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")
        return

    # 자주 쓰는 실무 키워드 빠른 칩
    st.caption("⚡ **자주 쓰는 실무 키워드 (원클릭 검색)**")
    chip_cols = st.columns(len(QUICK_KEYWORDS))
    for i, qkw in enumerate(QUICK_KEYWORDS):
        with chip_cols[i]:
            if st.button(qkw, key=f"m6_chip_{i}", width="stretch"):
                _navigate_to("6", kw=qkw, auto_run=True)

    target_options = ["전체 규정 (All)"] + reg_names
    if "m6_target" not in st.session_state or st.session_state["m6_target"] not in target_options:
        st.session_state["m6_target"] = "전체 규정 (All)"
    if "m6_latest" not in st.session_state:
        st.session_state["m6_latest"] = True
    if "m6_kw" not in st.session_state:
        st.session_state["m6_kw"] = ""

    with st.form("keyword_search_form", border=False):
        c1, c2 = st.columns([1, 2])
        with c1:
            target = st.selectbox("대상", target_options, key="m6_target")
            latest = st.checkbox("최신 규정만", key="m6_latest")
        with c2:
            keyword = st.text_input("검색어", key="m6_kw", placeholder="예: 공매도 호가 / 거래정지 | 중단 / 정리매매 -세칙")
            btn = st.form_submit_button("검색", type="primary")

    if btn:
        if keyword and keyword.strip():
            st.session_state["m6_active"] = {
                "target": target,
                "keyword": keyword.strip(),
                "latest": latest,
            }
            st.session_state["m6_page"] = 1
            st.session_state["m6_reg_filter"] = "전체 보기"
        else:
            st.warning("검색어를 입력해주세요.")

    active = st.session_state.get("m6_active")
    if not active:
        return

    a_target, a_kw, a_latest = active["target"], active["keyword"], active["latest"]
    df = get_menu6_keyword_search(a_target, a_kw, a_latest)
    if df.empty:
        st.warning(f"'{a_kw}'에 대한 검색 결과가 없습니다.")
        return

    # 규정별 분포 요약 및 즉석 필터
    reg_counts = df["regulation_name"].value_counts()
    st.success(f"검색어 **`{a_kw}`** — 총 **{len(df):,}건** 검색됨 ({len(reg_counts)}개 규정)")

    # 엑셀 / CSV 다운로드 (항상 전체 검색결과 기준)
    col_dl1, col_dl2, col_flt = st.columns([1.2, 1.2, 2], vertical_alignment="bottom")
    safe_kw = re.sub(r'[^\w가-힣]', '_', a_kw)[:30]
    with col_dl1:
        excel_bytes = export_dataframe_to_excel(df, sheet_name="통합키워드검색")
        st.download_button(
            label=f"📥 전체 엑셀(.xlsx) ({len(df):,}건)",
            data=excel_bytes,
            file_name=f"검색결과_{safe_kw}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
    with col_dl2:
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="📥 전체 CSV 다운로드",
            data=csv_bytes,
            file_name=f"검색결과_{safe_kw}.csv",
            mime="text/csv",
            width="stretch",
        )
    with col_flt:
        filter_opts = ["전체 보기"] + [f"{r} ({cnt}건)" for r, cnt in reg_counts.items()]
        if st.session_state.get("m6_reg_filter") not in filter_opts:
            st.session_state["m6_reg_filter"] = "전체 보기"
        sel_reg_flt = st.selectbox("규정별 결과 필터", filter_opts, key="m6_reg_filter")

    display_df = df
    if sel_reg_flt != "전체 보기":
        chosen_r = sel_reg_flt.rsplit(" (", 1)[0]
        display_df = df[df["regulation_name"] == chosen_r].reset_index(drop=True)

    # 복합 검색어의 모든 긍정 단어(AND/OR)를 개별 하이라이트
    or_groups, _ = parse_search_query(a_kw)
    kw_variants = []
    for grp in or_groups:
        for term in grp:
            kw_variants.extend(get_hang_variants(term) or [term])
    if not kw_variants:
        kw_variants = get_hang_variants(a_kw)

    s_idx, e_idx = _render_pagination(len(display_df), 30, "m6_page")
    for idx, row in display_df.iloc[s_idx:e_idx].iterrows():
        with st.container(border=True):
            hc1, hc2, hc3 = st.columns([6, 1.1, 1.1])
            with hc1:
                hl_title = highlight_terms(str(row["article_title"] or ""), kw_variants, "red")
                st.markdown(
                    f"**📌 [{row['regulation_name']}] {row['ref_no']} {hl_title}** "
                    f":grey[[{format_date_kr(row['reg_date'])}]]"
                )
            with hc2:
                if st.button("🕰️ 이력", key=f"m6_h_{idx}", width="stretch"):
                    _navigate_to("4", reg=row["regulation_name"], art=extract_base_article(row["ref_no"]), auto_run=True)
            with hc3:
                if st.button("🔗 역참조", key=f"m6_r_{idx}", width="stretch"):
                    _navigate_to("7", reg=row["regulation_name"], art=extract_base_article(row["ref_no"]), auto_run=True)
            st.markdown(highlight_terms(str(row["content"] or ""), kw_variants, "red"))

def _render_citation_cards(items: list, color: str, show_reg_name: bool, key_prefix: str, filter_kw: str = ""):
    """메뉴 7 역참조 결과 카드 리스트 렌더링 (결과 내 필터 및 페이지네이션 + 원클릭 연계)"""
    filtered = items
    if filter_kw and filter_kw.strip():
        fk = filter_kw.strip()
        filtered = [
            it for it in items
            if fk in str(it["row"].get("content", ""))
            or fk in str(it["row"].get("ref_no", ""))
            or fk in str(it["row"].get("article_title", ""))
            or fk in str(it["row"].get("regulation_name", ""))
        ]

    if not filtered:
        st.caption("해당하는 인용 결과가 없습니다.")
        return

    s_idx, e_idx = _render_pagination(len(filtered), 30, f"{key_prefix}_page")
    for idx, item in enumerate(filtered[s_idx:e_idx], start=s_idx):
        row = item["row"]
        spans = item["spans"]
        is_range = item.get("is_range", False)
        reg_prefix = f"[{row['regulation_name']}] " if show_reg_name else ""
        range_badge = " <span class='rule-range-badge'>범위 인용</span>" if is_range else ""
        with st.container(border=True):
            hc1, hc2, hc3 = st.columns([6, 1.1, 1.1])
            with hc1:
                st.markdown(
                    f"**📌 {reg_prefix}{row['ref_no']} {row['article_title'] or ''}** "
                    f":grey[[시행일자: {format_date_kr(row['reg_date'])}]]{range_badge}",
                    unsafe_allow_html=True,
                )
            with hc2:
                if st.button("🕰️ 이력", key=f"{key_prefix}_h_{idx}", width="stretch"):
                    _navigate_to("4", reg=row["regulation_name"], art=extract_base_article(row["ref_no"]), auto_run=True)
            with hc3:
                if st.button("🔗 역참조", key=f"{key_prefix}_r_{idx}", width="stretch"):
                    _navigate_to("7", reg=row["regulation_name"], art=extract_base_article(row["ref_no"]), auto_run=True)
            st.markdown(highlight_spans(row["content"], spans, color))

def _render_menu7(reg_names: list, default_reg_index: int):
    st.subheader("🔗 조항 인용 및 역참조 분석")
    st.caption(
        "특정 조항의 **역참조(내부·파트너·타 규정에서 인용하는 내역)**와 **정방향 참조(본 조항이 인용 중인 타 조항)**를 정밀 분석합니다. "
        "가지조항 경계 보호, 연속 조항 열거(`규정 제6조, 제25조`), 범위 인용(`제23조 내지 제25조`)을 모두 지원합니다."
    )
    if not reg_names:
        st.info("데이터베이스에 등록된 규정 데이터가 없습니다.")
        return

    if "m7_reg" not in st.session_state or st.session_state["m7_reg"] not in reg_names:
        st.session_state["m7_reg"] = reg_names[default_reg_index]
    if "m7_art" not in st.session_state:
        st.session_state["m7_art"] = DEFAULT_ART_NO
    if "m7_latest" not in st.session_state:
        st.session_state["m7_latest"] = True

    with st.form("ref_analysis_form", border=False):
        col1, col2 = st.columns(2)
        with col1:
            target_reg = st.selectbox("관심 규정", reg_names, key="m7_reg")
        with col2:
            target_art = st.text_input("관심 조항 번호", key="m7_art", placeholder="예: 제20조의2, 20-2, 제25조, 제26조제1항제2호")
        latest_only = st.checkbox("최신 규정 내용에서만 찾기 (권장)", key="m7_latest")
        search_btn = st.form_submit_button("인용 분석 시작", type="primary")

    if search_btn and target_art and target_art.strip():
        st.session_state["m7_active"] = {
            "reg": target_reg,
            "art": target_art.strip(),
            "latest": latest_only,
        }
        st.session_state["m7_int_page"] = 1
        st.session_state["m7_part_page"] = 1
        st.session_state["m7_ext_page"] = 1
        _record_recent_view(target_reg, normalize_article_query(target_art.strip()), "7")

    active = st.session_state.get("m7_active")
    if not active:
        return

    a_reg, a_art, a_latest = active["reg"], active["art"], active["latest"]

    # 1. 관심 조항 원문 조회
    clean_art, df_target = get_target_article_content(a_reg, a_art, a_latest)
    outgoing_list = []
    if not df_target.empty:
        combined_text = "\n".join(df_target["content"].fillna("").tolist())
        outgoing_list = extract_outgoing_citations(combined_text, curr_reg=a_reg, all_reg_names=reg_names)

    # 2. 역참조 분석 실행 (캐시)
    analysis = get_menu7_reference_analysis(a_reg, clean_art, a_latest)
    partner_reg_name = analysis["partner_reg_name"]
    results_internal = analysis["internal"]
    results_partner = analysis["partner"]
    results_external = analysis["external"]

    # 관심 조항 원문 박스
    st.markdown(f"### 📖 [관심 조항 원문] {a_reg} {clean_art}")
    if not df_target.empty:
        main_rows = df_target[df_target["occ_idx"] == 0]
        add_rows = df_target[df_target["occ_idx"] > 0]
        first_row = main_rows.iloc[0] if not main_rows.empty else df_target.iloc[0]
        title_suffix = f" ({first_row['article_title']})" if first_row["article_title"] else ""

        with st.container(border=True):
            th1, th2 = st.columns([5.5, 1.5], vertical_alignment="center")
            with th1:
                st.markdown(
                    f"#### 📌 **[{a_reg}] {clean_art}{title_suffix}** "
                    f":grey[[시행일자: {format_date_kr(first_row['reg_date'])}]]"
                )
            with th2:
                if st.button("🕰️ 변경 이력 보기", width="stretch", key="m7_target_hist"):
                    _navigate_to("4", reg=a_reg, art=clean_art, auto_run=True)
            st.markdown("---")
            for _, row in main_rows.iterrows():
                st.markdown(f"**{row['ref_no']}**")
                st.markdown(f"> {row['content']}")
            if not add_rows.empty:
                with st.expander(f"📎 동일 조항 번호의 부칙 조문 ({len(add_rows)}건)", expanded=False):
                    for _, row in add_rows.iterrows():
                        st.markdown(f"**{row['ref_no']}** :grey[({row['article_title'] or '부칙'})]")
                        st.markdown(f"> {row['content']}")
    else:
        st.warning(f"⚠️ 선택하신 규정({a_reg})에서 '{clean_art}' 조항의 원문을 찾을 수 없습니다. (조항 번호를 확인해주세요)")

    st.markdown("---")

    # 분석 결과 요약 메트릭 + 엑셀 다운로드 + 결과 내 필터
    st.markdown(f"### 🔗 [{clean_art}] 인용 및 역참조 분석 결과")
    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("🏠 내부 참조", f"{len(results_internal):,}건")
    mc2.metric(f"🤝 파트너 ({'규정' if '시행세칙' in a_reg else '세칙'})", f"{len(results_partner):,}건")
    mc3.metric("🌏 타 규정 참조", f"{len(results_external):,}건")
    mc4.metric("↗️ 정방향 인용", f"{len(outgoing_list):,}건")

    ac1, ac2 = st.columns([2.5, 1.5], vertical_alignment="bottom")
    with ac1:
        res_filter = st.text_input(
            "🔎 인용 결과 내 키워드 필터",
            placeholder="분석 결과 안에서 특정 키워드나 조항만 추려보기",
            key="m7_res_filter",
        )
    with ac2:
        if not df_target.empty or results_internal or results_partner or results_external or outgoing_list:
            excel_m7_bytes = export_menu7_to_excel(
                df_target=df_target[[c for c in ["reg_date", "ref_no", "article_title", "content"] if c in df_target.columns]] if not df_target.empty else df_target,
                results_internal=results_internal,
                results_partner=results_partner,
                results_external=results_external,
                target_reg=a_reg,
                clean_art=clean_art,
                partner_reg_name=partner_reg_name,
                outgoing_list=outgoing_list,
            )
            st.download_button(
                label="📥 전체 분석 결과 엑셀(.xlsx)",
                data=excel_m7_bytes,
                file_name=f"인용분석_{a_reg.replace(' ', '_')}_{clean_art}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
                help="관심 조항 원문, 내부/파트너/타규정 역참조, 정방향 인용을 시트별로 분리 저장합니다.",
            )

    # 4개 탭으로 깔끔하게 분리
    tab_int, tab_part, tab_ext, tab_out = st.tabs([
        f"🏠 내부 참조 ({len(results_internal)})",
        f"🤝 {partner_reg_name} ({len(results_partner)})",
        f"🌏 타 규정 참조 ({len(results_external)})",
        f"↗️ 정방향 인용 ({len(outgoing_list)})",
    ])

    with tab_int:
        st.caption(f"**[{a_reg}]** 내부에서 `{clean_art}`을(를) 직접·연속·범위 인용하는 조항입니다. (타 규정·법령 인용 자동 제외)")
        _render_citation_cards(results_internal, "red", False, "m7_int", res_filter)

    with tab_part:
        st.caption(f"**[{partner_reg_name}]**에서 `{clean_art}`을(를) 지배 접두어 문맥으로 인용하는 조항입니다. (예: `규정 제6조, 제25조` 연속 열거 포함)")
        _render_citation_cards(results_partner, "blue", False, "m7_part", res_filter)

    with tab_ext:
        st.caption(f"제3의 규정에서 **[{a_reg}]**의 `{clean_art}`을(를) 인용하는 조항입니다. (공백 없는 표기 및 동일 시장 내 약칭 포함)")
        _render_citation_cards(results_external, "green", True, "m7_ext", res_filter)

    with tab_out:
        st.caption(f"**[{a_reg} {clean_art}]** 본문 안에서 참조·인용하고 있는 타 규정 및 조항 목록입니다. DB에 등록된 규정은 클릭하여 바로 역참조 분석할 수 있습니다.")
        if not outgoing_list:
            st.caption("본 조항에서 추출된 정방향 인용 조항이 없습니다.")
        else:
            for idx, item in enumerate(outgoing_list):
                with st.container(border=True):
                    oc1, oc2, oc3 = st.columns([4.5, 1.5, 1.5], vertical_alignment="center")
                    with oc1:
                        target_hint = f" ➜ **{item['resolved_reg']}**" if item["resolved_reg"] else " *(외부 법령/미등록)*"
                        st.markdown(f"**↗️ `{item['full_label']}`**{target_hint}")
                    with oc2:
                        if item["resolved_reg"]:
                            if st.button("🔗 역참조 이동", key=f"m7_out_r_{idx}", width="stretch"):
                                _navigate_to("7", reg=item["resolved_reg"], art=item["base_art"], auto_run=True)
                    with oc3:
                        if item["resolved_reg"]:
                            if st.button("🕰️ 이력 보기", key=f"m7_out_h_{idx}", width="stretch"):
                                _navigate_to("4", reg=item["resolved_reg"], art=item["base_art"], auto_run=True)


def render_app():
    """메인 Streamlit 애플리케이션 엔트리포인트"""
    st.set_page_config(page_title="금융 규정 검색 시스템", layout="wide", page_icon="⚡")
    _inject_custom_css()

    # 1. 앱 기동 시 DB 초기화 및 누락 HWP 자동 동기화 1회 실행
    ensure_db_initialized(is_postgres())
    db_stats = get_db_stats()
    reg_names = get_regulation_names()
    default_reg_index = reg_names.index(PREFERRED_REG_NAME) if PREFERRED_REG_NAME in reg_names else 0

    # 2. 보류 중인 원클릭 네비게이션 요청 반영 (위젯 생성 전!)
    _apply_pending_navigation(reg_names)

    # 3. 사이드바 및 타이틀 렌더링
    menu = _render_sidebar(db_stats)
    st.title(f"⚡ {menu}")

    # 4. 선택된 메뉴 본문 렌더링
    if menu == MENU_NAMES["1"]:
        _render_menu1(reg_names)
    elif menu == MENU_NAMES["2"]:
        _render_menu2(reg_names, default_reg_index)
    elif menu == MENU_NAMES["3"]:
        _render_menu3(reg_names, default_reg_index)
    elif menu == MENU_NAMES["4"]:
        _render_menu4(reg_names, default_reg_index)
    elif menu == MENU_NAMES["5"]:
        _render_menu5(reg_names, default_reg_index)
    elif menu == MENU_NAMES["6"]:
        _render_menu6(reg_names)
    elif menu == MENU_NAMES["7"]:
        _render_menu7(reg_names, default_reg_index)


if __name__ == "__main__":
    render_app()
