import unittest
import pandas as pd
from modules.citation_parser import normalize_art_input, get_hang_variants
from modules.queries import (
    get_menu1_summary,
    get_menu2_history,
    get_menu3_full_text,
    get_menu4_history,
    get_menu5_detail,
    get_menu6_keyword_search,
    get_target_article_content,
    get_menu7_reference_analysis,
    export_dataframe_to_excel,
    export_full_text_to_txt,
    export_history_to_excel,
)
from modules.db_manager import get_regulation_names, get_regulation_dates

class TestUserConvenienceImprovements(unittest.TestCase):
    
    def test_smart_article_normalization(self):
        """다양한 비표준/약어 조항 번호 입력의 표준화 테스트"""
        test_cases = {
            "20": "제20조",
            "20조": "제20조",
            "제20조": "제20조",
            "제 20 조": "제20조",
            "20-2": "제20조의2",
            "20_2": "제20조의2",
            "20의2": "제20조의2",
            "20의 2": "제20조의2",
            "20조의2": "제20조의2",
            "20조의 2": "제20조의2",
            "20조 2": "제20조의2",
            "제20조의2": "제20조의2",
            "제 20 조 의 2": "제20조의2",
            "제20조-2": "제20조의2",
            "20 1항": "제20조제1항",
            "20조 1항": "제20조제1항",
            "20조 제1항": "제20조제1항",
            "제20조 제1항": "제20조제1항",
            "20-2 1항": "제20조의2제1항",
            "제20조의2 제1항": "제20조의2제1항",
            "제1장": "제1장",
            "제1호": "제1호",
            "공매도": "공매도"
        }
        for raw, expected in test_cases.items():
            normalized = normalize_art_input(raw)
            self.assertEqual(normalized, expected, f"Failed for input '{raw}': got '{normalized}', expected '{expected}'")

    def test_get_hang_variants(self):
        """원문자 및 숫자 항 상호 호환 변형 생성 테스트"""
        variants_1 = get_hang_variants("20")
        self.assertIn("제20조", variants_1)

        variants_sub = get_hang_variants("20-2")
        self.assertIn("제20조의2", variants_sub)

        variants_hang = get_hang_variants("제20조제1항")
        self.assertTrue(any("①" in v for v in variants_hang))
        self.assertTrue(any("1" in v for v in variants_hang))

    def test_export_helpers(self):
        """엑셀 및 전문 텍스트 내보내기 헬퍼 테스트"""
        sample_df = pd.DataFrame([
            {"조항": "제1조", "조명": "목적", "내용": "이 규정은 업무를 정함을 목적으로 한다."},
            {"조항": "제2조", "조명": "정의", "내용": "이 규정에서 사용하는 용어의 뜻은 다음과 같다."}
        ])
        
        # 1. 엑셀 내보내기 바이너리 테스트
        excel_bytes = export_dataframe_to_excel(sample_df, "테스트")
        self.assertIsInstance(excel_bytes, bytes)
        self.assertTrue(len(excel_bytes) > 0)

        # 2. 전문 텍스트 내보내기 문자열 테스트
        txt_str = export_full_text_to_txt(sample_df, "유가증권시장 업무규정", "20260129")
        self.assertIn("【규정명】 유가증권시장 업무규정", txt_str)
        self.assertIn("[제1조] 목적", txt_str)
        self.assertIn("이 규정은 업무를 정함을 목적으로 한다.", txt_str)

        # 3. 조항 히스토리 엑셀 내보내기 테스트
        hist_bytes = export_history_to_excel(sample_df, "유가증권시장 업무규정", "제1조")
        self.assertIsInstance(hist_bytes, bytes)
        self.assertTrue(len(hist_bytes) > 0)

    def test_database_queries(self):
        """DB 데이터 쿼리 및 스마트 입력 연동 테스트"""
        names = get_regulation_names()
        self.assertTrue(len(names) > 0, "No regulation names found in DB")
        self.assertIn("유가증권시장 업무규정", names)

        # 메뉴 1 요약 집계
        df_summary = get_menu1_summary()
        self.assertFalse(df_summary.empty)
        self.assertIn("규정명", df_summary.columns)
        self.assertIn("개정본 수", df_summary.columns)

        # 메뉴 2 개정일자 목록
        df_dates = get_menu2_history("유가증권시장 업무규정")
        self.assertFalse(df_dates.empty)
        dates = get_regulation_dates("유가증권시장 업무규정")
        self.assertTrue(len(dates) > 0)

        # 메뉴 3 전문 조회
        df_full = get_menu3_full_text("유가증권시장 업무규정", dates[0])
        self.assertFalse(df_full.empty)
        self.assertIn("조항", df_full.columns)

        # 메뉴 4 약어 입력('20-2')으로 히스토리 검색
        df_hist = get_menu4_history("유가증권시장 업무규정", "20-2")
        self.assertFalse(df_hist.empty, "Failed to retrieve history using smart input '20-2'")

        # 메뉴 5 약어 입력('20-2')으로 상세 조회
        df_detail = get_menu5_detail("유가증권시장 업무규정", dates[0], "20-2")
        self.assertFalse(df_detail.empty, "Failed to retrieve detail using smart input '20-2'")

        # 메뉴 6 키워드 검색
        df_kw = get_menu6_keyword_search("유가증권시장 업무규정", "공매도", True)
        self.assertFalse(df_kw.empty)

        # 메뉴 7 스마트 조항 원문 및 역참조 분석
        clean_art, df_art = get_target_article_content("유가증권시장 업무규정", "20-2", True)
        self.assertEqual(clean_art, "제20조의2")
        self.assertFalse(df_art.empty)

if __name__ == '__main__':
    unittest.main()
