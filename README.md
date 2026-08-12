# 📰 어린이 헤드라인 뉴스 (Headline News)

문해력과 시민 의식을 키우는, 초등학생을 위한 AI 자동 생성 뉴스 서비스입니다.

매일 아침 최신 뉴스를 수집해 **초등학교 3~4학년군 / 5~6학년군** 눈높이에 맞게 AI가 다시 쓰고, 2022 개정 교육과정 성취기준과 연계해 학교 현장에서 바로 활용할 수 있도록 제작되었습니다.

🔗 **서비스 바로가기**: https://venushoon.github.io/headline-news/

---

## ✨ 주요 기능

- **AI 기사 재구성**: 원문 뉴스를 Google Gemini API로 학년별(3~4, 5~6학년군) 눈높이에 맞춰 팩트 기반으로 재작성
- **교육과정 연계**: 2022 개정 교육과정 성취기준을 사전 검증해, 기준에 부합하는 기사만 선별
- **매일 자동 발행**: GitHub Actions 크론으로 매일 아침 6시(KST) 자동 수집·생성·업로드
- **지난 기사 아카이브**: 월별 아코디언 형태로 지난 기사를 편하게 탐색
- **학습 보조 기능**
  - 낱말 도우미 (어려운 단어 뜻풀이)
  - 생각해 보기 (사고력 확장 질문)
  - 국어사전 바로 검색 (네이버 국어사전 연동)
  - 글자 크기 조절, 기사 검색
  - 인쇄/PDF 저장 (학습지 형태로 출력 가능)
- **반응형 디자인**: PC와 모바일 환경 모두 지원

---

## 🛠 기술 스택

| 영역 | 기술 |
|---|---|
| 프론트엔드 | React (Vite), Tailwind CSS |
| 백엔드 | Python |
| AI | Google Gemini API (`google-genai`) |
| 데이터베이스 | Firebase Firestore |
| 뉴스 수집 | RSS (feedparser) |
| 배포 | GitHub Pages (프론트엔드) |
| 자동화 | GitHub Actions (매일 크론 실행) |

---

## 📂 프로젝트 구조

```
headline-news/
├── src/
│   ├── App.jsx           # 메인 화면 컴포넌트
│   ├── App.css           # 스타일
│   ├── firebase.js       # Firebase 클라이언트 설정
│   ├── main.jsx
│   └── assets/
├── .github/
│   └── workflows/
│       └── daily-news.yml   # 매일 아침 자동 실행 워크플로우
├── main.py                # 뉴스 수집 + AI 재작성 + Firestore 업로드 스크립트
├── requirements.txt
├── vite.config.js
└── package.json
```

---

## ⚙️ 동작 구조

```
GitHub Actions (매일 06:00 KST)
        │
        ▼
   main.py 실행
   ├─ RSS로 최신 뉴스 수집
   ├─ 2022 개정 교육과정 성취기준 검증
   ├─ Gemini API로 학년별 기사 재작성
   └─ Firestore에 날짜별 문서로 저장 (daily_briefings/YYYY-MM-DD)
        │
        ▼
Firestore (daily_briefings 컬렉션)
        │
        ▼
GitHub Pages (React 앱)
   └─ 브라우저에서 Firestore를 직접 조회해 화면에 표시
```

- 백엔드(`main.py`)와 프론트엔드는 완전히 분리된 두 시스템입니다.
- GitHub Actions는 하루 한 번 스크립트를 실행하고 종료되며, GitHub Pages는 정적 파일을 상시 호스팅합니다.

---

## 🚀 로컬 개발

### 프론트엔드
```bash
npm install
npm run dev
```

### 백엔드 (뉴스 수집 스크립트 수동 실행)
```bash
pip install -r requirements.txt
python3 main.py
```

> `.env`와 `serviceAccountKey.json` 파일이 로컬에 있어야 하며, 두 파일 모두 `.gitignore`에 포함되어 저장소에 올라가지 않습니다.

### 배포
```bash
npm run deploy
```

---

## 🔐 환경 변수 및 보안

다음 파일은 민감 정보를 담고 있어 저장소에 포함되지 않습니다.

- `.env` — Gemini API 키 등
- `serviceAccountKey.json` — Firebase 서비스 계정 키

GitHub Actions 실행 시에는 저장소의 **Secrets**(`GEMINI_API_KEY`, `FIREBASE_ADMIN_KEY`)를 통해 안전하게 주입됩니다.

---

## ⚠️ 안내

본문은 인공지능(AI)이 원문 기사를 어린이 눈높이로 다시 쓴 글로, 원문과 다르거나 일부 내용이 생략될 수 있습니다. 정확한 내용은 기사 하단의 원문 링크에서 확인해 주세요.

---

## 📄 라이선스

© 어린이 헤드라인 뉴스