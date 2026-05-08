# DART 분기실적 분석 대시보드

삼성전자 등 상장사의 분기 재무실적 + 네이버 컨센서스를 시각화하는 Streamlit 대시보드.

## 기능
- DART API 기반 분기 매출/영업이익/ROE 추이
- 주가 오버레이
- 듀퐁 분석 (ROE 분해)
- 네이버 컨센서스 자동 로드 + 다중 분기 추정치 표시

## 설치 및 실행

### 의존성 설치
pip install -r requirements.txt

### API 키 설정
.streamlit/secrets.toml 파일 생성 후 아래 내용 입력:
DART_API_KEY = "발급받은_DART_API_키"

DART API 키 발급: https://opendart.fss.or.kr

### 실행
streamlit run 0_분기실적분석.py