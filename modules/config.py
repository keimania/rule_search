import re

# pyhwp 라이브러리 내부 모듈 임포트 시도
try:
    import hwp5.hwp5txt
    HAS_PYHWP = True
except ImportError:
    HAS_PYHWP = False

# =========================================================
# 기본 설정 및 경로
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

# 파일명 가이드라인
FILENAME_GUIDE = "규정명_전문_YYYYMMDD.hwp (또는 .txt, .csv)"
FILENAME_EXAMPLE = "유가증권시장 업무규정_전문_20240315.hwp"

# =========================================================
# TXT/조항 파싱용 정규표현식 상수
# =========================================================
ARTICLE_ID_PATTERN = re.compile(r"^(제\d+조(?:의\d+)?)")
HO_PATTERN = re.compile(r"(^|\n)\s*(\d+(?:의\d+)*)\.\s*", re.MULTILINE)
HANG_PATTERN = re.compile(r"(^|\n)\s*([①-⑳])", re.MULTILINE)
MOK_PATTERN = re.compile(r"(^|\n)\s*([가-하])\.\s*", re.MULTILINE)
CHAPTER_PATTERN = re.compile(r"^제(\d+)장\s*(.+)")
SECTION_PATTERN = re.compile(r"^제(\d+)절\s*(.+)")

# =========================================================
# 원문자(①~⑳) ↔ 아라비아 숫자(1~20) 매핑 딕셔너리
# =========================================================
CIRCLED_TO_ARABIC = {chr(9311 + i): str(i) for i in range(1, 21)}
ARABIC_TO_CIRCLED = {str(i): chr(9311 + i) for i in range(1, 21)}
