#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
한국거래소 세칙 파싱 스크립트 (Subfolder Version)
현재 폴더 하위의 '규정' 폴더 내 모든 .txt 파일을 읽어
해당 폴더 내에 동일한 이름의 .csv 파일로 변환합니다.
"""

import re
import pandas as pd
from pathlib import Path
import sys

# ----------------------------------------------------------------------
# 1. 원문 읽기 (인코딩 자동 시도)
# ----------------------------------------------------------------------
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
        print(f'[WARN] "{path.name}" 일반 인코딩 실패. utf-8 + ignore 로 강제 디코딩했습니다.')

    return text


# ----------------------------------------------------------------------
# 2. 파싱용 정규표현식 및 유틸 함수 (변경 없음)
# ----------------------------------------------------------------------
ARTICLE_ID_PATTERN = re.compile(r"^(제\d+조(?:의\d+)?)")
HO_PATTERN = re.compile(r"(^|\n)\s*(\d+(?:의\d+)*)\.\s*", re.MULTILINE)
HANG_PATTERN = re.compile(r"(^|\n)\s*([①-⑳])", re.MULTILINE)
MOK_PATTERN = re.compile(r"(^|\n)\s*([가-하])\.\s*", re.MULTILINE)
CHAPTER_PATTERN = re.compile(r"^제(\d+)장\s*(.+)")
SECTION_PATTERN = re.compile(r"^제(\d+)절\s*(.+)")


def clean_text(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\t", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


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
        if mok_matches:
            ho_main = remainder[: mok_matches[0].start(0)].strip()
        else:
            ho_main = remainder.strip()

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


# ----------------------------------------------------------------------
# 6. 전체 문서 파싱 (장/절 컨텍스트 포함)
# ----------------------------------------------------------------------
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

    df = pd.DataFrame(rows_clean, columns=["구분", "장번호", "장명", "절번호", "절명", "참조번호", "조명", "조", "항", "호", "목", "내용"])
    return df


# ----------------------------------------------------------------------
# 7. 통계 집계
# ----------------------------------------------------------------------
def build_stats(df: pd.DataFrame) -> pd.DataFrame:
    hang_set = set()
    ho_set = set()
    for _, row in df.iterrows():
        if row["항"] != "0": hang_set.add((row["참조번호"], row["항"]))
        if row["호"] != "0": ho_set.add((row["참조번호"], row["항"], row["호"]))

    stats_df = pd.DataFrame({
        "구분": ["항 개수", "호 개수", "총 항목 수"],
        "개수": [len(hang_set), len(ho_set), len(df)]
    })
    return stats_df


# ----------------------------------------------------------------------
# 8. 메인 실행부 (폴더 경로 수정)
# ----------------------------------------------------------------------
def main():
    # 수정된 부분: "규정" 폴더를 타겟으로 설정
    target_dir = Path("규정")

    # 규정 폴더 존재 여부 확인
    if not target_dir.is_dir():
        print(f"오류: 현재 위치에 '{target_dir}' 폴더가 없습니다.")
        print(f"      '{Path.cwd() / target_dir}' 경로를 확인해주세요.")
        return

    # '규정' 폴더 내의 모든 txt 파일 검색
    txt_files = list(target_dir.glob("*.txt"))

    if not txt_files:
        print(f"'{target_dir}' 폴더 내에 .txt 파일이 없습니다.")
        return

    print(f"'{target_dir}' 폴더에서 총 {len(txt_files)}개의 txt 파일을 발견했습니다. 변환을 시작합니다...\n")

    for txt_path in txt_files:
        try:
            print(f">> 처리 중: {txt_path.name}")
            
            # 1. 파일 읽기
            text = read_source_text(str(txt_path))
            
            # 2. 파싱
            df = parse_all(text)
            
            # 3. 통계
            stats_df = build_stats(df)

            # 4. 저장 (규정 폴더 내부에 csv 저장)
            output_csv_path = txt_path.with_suffix(".csv")
            # stats_csv_path = txt_path.with_name(f"{txt_path.stem}_stats.csv")

            df.to_csv(output_csv_path, index=False, encoding="utf-8-sig")
            # stats_df.to_csv(stats_csv_path, index=False, encoding="utf-8-sig")

            print(f"   [저장 완료] {output_csv_path.name}")
            
        except Exception as e:
            print(f"   [에러] {txt_path.name} 처리 중 오류 발생: {e}")

    print("\n모든 작업이 완료되었습니다.")


if __name__ == "__main__":
    main()