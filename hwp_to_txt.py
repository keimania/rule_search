import os
import glob
import subprocess

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

    # 2. hwp5txt 실행 파일 경로 설정
    exe_path = r"C:\Users\100186\AppData\Roaming\Python\Python314\Scripts\hwp5txt.exe"

    for hwp_path in hwp_files:
        # 3. 출력할 파일 경로 생성
        # os.path.splitext를 사용하면 '규정/파일이름'까지 추출됩니다.
        file_path_without_ext = os.path.splitext(hwp_path)[0]
        txt_path = f"{file_path_without_ext}.txt"

        # 4. 명령어 구성
        command = [exe_path, "--output", txt_path, hwp_path]

        try:
            # 파일명만 출력하기 위해 os.path.basename 사용
            file_name = os.path.basename(hwp_path)
            print(f"변환 중: {file_name} -> {os.path.basename(txt_path)}")
            
            # 5. 명령어 실행
            subprocess.run(command, check=True)
            print("  └─ 성공")
            
        except subprocess.CalledProcessError as e:
            print(f"  └─ 실패 (에러 코드: {e.returncode})")
        except FileNotFoundError:
            print(f"  └─ 실패: '{exe_path}' 경로를 확인해주세요.")
            break
        except Exception as e:
            print(f"  └─ 알 수 없는 오류: {e}")

    print("\n모든 작업이 완료되었습니다.")

if __name__ == "__main__":
    convert_all_hwp_to_txt()