# KRX Regulation Search Dashboard (한국거래소 규정 검색 시스템)

이 프로젝트는 한국거래소(KRX)의 규정 및 시행세칙을 효율적으로 관리하고 검색하기 위한 **Python Streamlit 기반의 대시보드**입니다. 
HWP 형식의 규정 파일을 텍스트와 CSV로 변환하여 DB를 구축하고, 웹 인터페이스를 통해 조항별 히스토리 추적, 통합 검색, 상호 인용 분석 기능을 제공합니다.

## 🌐 웹에서 바로 실행하기 (Live Demo)
복잡한 설치 과정 없이 아래 링크를 클릭하면 웹 브라우저에서 바로 규정 검색 시스템을 사용할 수 있습니다.
👉 **[규정 검색 시스템 바로가기](https://rulesearch-9hkfiouvzz6s9eecftwc7d.streamlit.app/)**

---

## 📌 주요 기능 (Features)

* **규정 DB 구축**: 규정 파일(HWP/TXT/CSV)을 파싱하여 SQLite DB에 저장
* **규정 목록 및 전문 조회**: 등록된 규정 목록 확인 및 날짜별 전문 조회
* **개정 히스토리 관리**: 규정별 개정 일자 및 조항 변경 이력 추적
* **통합 키워드 검색**: 전체 규정 또는 최신 규정 대상 키워드 검색 (하이라이팅 지원)
* **조항 상세 분석**: 특정 시점의 조항 상세 내용 조회
* **인용(역참조) 분석**: 특정 조항이 내부, 파트너 규정(세칙), 타 규정에서 어떻게 인용되고 있는지 분석

## 🛠 설치 방법 (Installation) - 로컬 실행용

*웹에서 바로 사용하실 분은 위 **[Live Demo]** 링크를 이용하세요. 직접 코드를 수정하거나 로컬에서 실행하고 싶다면 아래 절차를 따르세요.*

### 1. 저장소 클론 (Clone)
```bash
git clone [https://github.com/keimania/rule_search.git](https://github.com/keimania/rule_search.git)
cd rule_search
```

### 2. 가상환경 설정 (선택 사항)

가상환경을 사용하면 프로젝트 패키지를 독립적으로 관리할 수 있습니다.

```bash
# 가상환경 생성
python -m venv venv

# 가상환경 활성화 (Windows)
venv\Scripts\activate

# 가상환경 활성화 (Mac/Linux)
source venv/bin/activate
```

### 3. 필수 패키지 설치 (Install Dependencies)

`requirements.txt` 파일을 사용하여 필요한 패키지를 한 번에 설치합니다.

```bash
pip install -r requirements.txt
```

---

## 🚀 사용 방법 (Usage)

### 1. 데이터 준비 (전처리)

규정 원문 파일(.hwp)이 있다면 아래 순서대로 데이터를 가공해야 합니다. 이미 `.csv` 파일이 `규정/` 폴더에 준비되어 있다면 이 단계는 건너뛰어도 됩니다.

1. **파일 위치**: 프로젝트 폴더 내 `규정/` 폴더에 `.hwp` 파일을 위치시킵니다.
2. **HWP → TXT 변환**:
* *사전 준비*: `hwp5txt` 도구가 필요하며, `hwp_to_txt.py` 파일 내부의 `exe_path` 변수를 본인의 환경에 맞게 수정해야 합니다.


```bash
python hwp_to_txt.py
```


3. **TXT → CSV 변환**:
* 텍스트 파일을 파싱하여 DB 적재용 CSV 포맷으로 변환합니다.


```bash
python 규정_txt_to_csv.py
```


### 2. 대시보드 실행

Streamlit 앱을 실행합니다.

```bash
streamlit run app.py
```

### 3. DB 업데이트

1. 웹 브라우저가 열리면 좌측 사이드바의 **"🔄 DB 업데이트 (증분)"** 버튼을 클릭합니다.
2. `규정/` 폴더에 있는 CSV 파일들이 `regulation_master.db`에 적재됩니다.
3. 업데이트가 완료되면 메뉴를 선택하여 기능을 사용합니다.

---

## 📂 프로젝트 구조 (Project Structure)

```
rule_search/
├── app.py                  # Streamlit 메인 애플리케이션
├── hwp_to_txt.py           # HWP 파일을 TXT로 변환하는 스크립트
├── 규정_txt_to_csv.py       # TXT 파일을 파싱하여 CSV로 변환하는 스크립트
├── regulation_master.db    # 규정 데이터가 저장되는 SQLite DB (자동 생성됨)
├── requirements.txt        # 의존성 패키지 목록
├── README.md               # 프로젝트 설명서
└── 규정/                    # 규정 데이터 폴더 (HWP, TXT, CSV 저장)

```

## ⚠️ 주의 사항

* **hwp_to_txt.py 경로 설정**: `hwp_to_txt.py` 파일을 실행하기 전에, 코드 내부의 `exe_path` 변수가 로컬 환경의 `hwp5txt.exe` 실제 경로와 일치하는지 반드시 확인해야 합니다.
* **파일명 규칙**: 파싱 로직의 정확성을 위해 규정 파일명은 `규정명_전문_YYYYMMDD` 형식을 권장합니다.