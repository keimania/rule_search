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
        
        # 5. 파이썬 내부 모듈(hwp5txt)을 직접 호출 (subprocess 미사용)
        # hwp5txt.main()이 인자를 읽을 수 있도록 sys.argv를 임시로 세팅합니다.
        original_argv = sys.argv
        sys.argv = ['hwp5txt', '--output', txt_path, hwp_path]
        
        try:
            hwp5.hwp5txt.main()
        except SystemExit as e:
            # hwp5txt.main()은 작업 완료 시 sys.exit()을 호출하는 구조이므로, 
            # 이를 예외로 잡아내어 스크립트가 완전히 종료되는 것을 방지합니다.
            if e.code == 0 or e.code is None:
                print("  └─ 성공")
            else:
                print(f"  └─ 실패 (에러 코드: {e.code})")
        except Exception as e:
            print(f"  └─ 알 수 없는 오류: {e}")
        finally:
            # 다음 파일 변환을 위해 sys.argv를 원래 상태로 복구
            sys.argv = original_argv

    print("\n모든 작업이 완료되었습니다.")

if __name__ == "__main__":
    convert_all_hwp_to_txt()