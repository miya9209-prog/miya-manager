# 미야언니 관리프로그램

별도 운영용 Streamlit 앱입니다.

기능
- 자동 상품DB 생성
- 반자동 DB 업로드 준비
- 미야언니 V2 통계 분석
- DB 품질 점검 대시보드

## 실행
```bash
pip install -r requirements.txt
streamlit run app.py
```

## 반영 흐름
1. 자동 상품DB 생성에서 `misharp_miya_db.csv` 생성
2. 반자동 DB 업로드 준비에서 현재 DB와 비교
3. 다운로드한 `misharp_miya_db.csv`를 미야언니 V2 레포에 덮어쓰기
4. GitHub 커밋 후 Streamlit 재배포
