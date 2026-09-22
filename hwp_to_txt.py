import os
import glob
import sys

# pyhwp 라이브러리 내부 모듈 임포트
try:
    import hwp5.hwp5txt
except ImportError:
    print("오류: pyhwp 라이브러리가 설치되어 있지 않습니다. 'pip install pyhwp'를 실행해주세요.")
    sys.exit(1)

def convert_all_hwp_to_txt():
    # 1. 대상 폴더 설정 (현재 폴더 내 '규정' 폴더)
    target_folder = "규정"
    
    # 폴더가 존재하지 않을 경우를 대비한 예외 처리
    if not os.path.exists(target_folder):
        print(f"오류: '{target_folder}' 폴더를 찾을 수 없습니다.")
        return

    # '규정/*.hwp' 패턴으로 파일 목록 검색
    search_pattern = os.path.join(target_folder, "*.hwp")
    hwp_files = glob.glob(search_pattern)
    
    if not hwp_files:
        print(f"'{target_folder}' 폴더에 변환할 .hwp 파일이 없습니다.")
        return

    print(f"'{target_folder}' 폴더에서 {len(hwp_files)}개의 파일을 발견했습니다. 변환을 시작합니다...\n")

    for hwp_path in hwp_files:
        # 3. 출력할 파일 경로 생성
        file_path_without_ext = os.path.splitext(hwp_path)[0]
        txt_path = f"{file_path_without_ext}.txt"
        file_name = os.path.basename(hwp_path)

        # 4. 동일한 이름의 txt 파일이 이미 존재하는지 확인
        if os.path.exists(txt_path):
            print(f"건너뜀: {os.path.basename(txt_path)} 파일이 이미 존재합니다.")
            continue # 다음 파일로 넘어감

        print(f"변환 중: {file_name} -> {os.path.basename(txt_path)}")
        
        # 5. pyhwp의 TextTransform 모듈을 사용하여 메모리 스트림 기반으로 안전하게 텍스트 추출 (sys.argv 미사용)
        try:
            import io
            from contextlib import closing
            from hwp5.hwp5txt import TextTransform, Hwp5File

            tt = TextTransform()
            with closing(Hwp5File(hwp_path)) as hwp5file:
                buf = io.BytesIO()
                tt.transform_hwp5_to_text(hwp5file, buf)
                text_content = buf.getvalue().decode('utf-8', errors='ignore')

            with open(txt_path, 'w', encoding='utf-8') as dest:
                dest.write(text_content)
            print("  └─ 성공")
        except Exception as e:
            print(f"  └─ 실패: {e}")

    print("\n모든 작업이 완료되었습니다.")

if __name__ == "__main__":
    convert_all_hwp_to_txt()