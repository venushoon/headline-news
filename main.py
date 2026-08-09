import os
import json
import datetime
import pytz
import feedparser
import firebase_admin
import urllib.request
import urllib.parse
import ssl
import warnings
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ==========================================
# 0. 시스템 환경 최적화
# ==========================================
warnings.filterwarnings("ignore")
ssl._create_default_https_context = ssl._create_unverified_context

# ==========================================
# 1. 환경 설정 및 API 키 세팅
# ==========================================
load_dotenv() 

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("\n⚠️ [오류] .env 파일에서 'GEMINI_API_KEY'를 찾을 수 없습니다.\n")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# Firebase Admin SDK 초기화
if not firebase_admin._apps:
    try:
        cred = credentials.Certificate("serviceAccountKey.json") 
        firebase_admin.initialize_app(cred)
    except FileNotFoundError:
        print("\n⚠️ [오류] serviceAccountKey.json 파일이 없습니다.\n")
        exit(1)

db = firestore.client(database_id="headline-news-d6a13")

# ==========================================
# 2. 요일별 테마 및 뉴스 검색 로직
# ==========================================
def get_today_theme_and_query():
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.datetime.now(kst)
    weekday = now.weekday() 
    
    themes = {
        0: {"name": "우리 사회 (사회/문화)", "query": "사회 OR 문화 OR 교육"},
        1: {"name": "지구촌 소식 (세계/국제)", "query": "국제 OR 세계 OR 글로벌"},
        2: {"name": "푸른 지구 (환경/기후)", "query": "기후 OR 환경 OR 생태계 OR 탄소"},
        3: {"name": "신기한 기술 (과학/IT)", "query": "과학 OR 우주 OR 인공지능 OR 기술"},
        4: {"name": "생활과 경제 (경제/안전)", "query": "경제 OR 생활 OR 안전"},
        5: {"name": "주말 종합 (주요 뉴스)", "query": "주요뉴스 OR 헤드라인"},
        6: {"name": "주말 종합 (주요 뉴스)", "query": "주요뉴스 OR 헤드라인"}
    }
    return themes[weekday]["name"], themes[weekday]["query"]

def fetch_news_by_query(query, limit=3):
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"
    
    feed = feedparser.parse(rss_url)
    articles = []
    
    for entry in feed.entries[:limit]:
        articles.append({
            "title": entry.title,
            "link": entry.link,
            "description": entry.description if hasattr(entry, 'description') else ""
        })
    return articles

# ==========================================
# 3. 성취기준 CSV 불러오기 
# ==========================================
def fetch_standards_csv():
    url = "https://raw.githubusercontent.com/venushoon/headline-news/refs/heads/main/2026list.csv"
    try:
        print("🌐 깃허브에서 성취기준(2026list.csv)을 불러오는 중...")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        return ""

# ==========================================
# 💡 [신규 추가] 성취기준 토큰 다이어트(최적화) 로직
# ==========================================
def filter_standards_by_theme(csv_data, theme_name):
    if not csv_data:
        return ""
    
    # 테마별 연관성이 높은 교과 및 핵심 키워드 매핑
    theme_keywords = {
        "우리 사회 (사회/문화)": ["사회", "도덕", "국어", "역사", "인권", "민주"],
        "지구촌 소식 (세계/국제)": ["사회", "도덕", "세계", "지리", "문화", "국제"],
        "푸른 지구 (환경/기후)": ["과학", "사회", "도덕", "실과", "환경", "기후", "생태", "탄소", "자연"],
        "신기한 기술 (과학/IT)": ["과학", "실과", "수학", "정보", "기술", "인공지능", "우주"],
        "생활과 경제 (경제/안전)": ["사회", "실과", "수학", "안전", "체육", "경제", "소비", "생활"],
        "주말 종합 (주요 뉴스)": ["국어", "사회", "도덕"]
    }
    
    keywords = theme_keywords.get(theme_name, [])
    if not keywords:
        return csv_data # 매핑된 키워드가 없으면 원본 그대로 반환
        
    lines = csv_data.strip().split('\n')
    if len(lines) <= 1:
        return csv_data
        
    header = lines[0]
    filtered_lines = [header]
    
    for line in lines[1:]:
        # 현재 테마와 관련된 키워드가 포함된 성취기준 라인만 살려둠
        if any(keyword in line for keyword in keywords):
            filtered_lines.append(line)
            
    # 혹시라도 너무 많이 걸러졌을 경우를 대비한 최후의 100줄 컷
    if len(filtered_lines) > 100:
        filtered_lines = filtered_lines[:100]
        
    return '\n'.join(filtered_lines)

# ==========================================
# 4. 제미나이(Gemini) AI 기사 '변환' 로직
# ==========================================
def rewrite_article_for_kids(article_data, theme_name, grade_text, grade_value, standards_csv):
    prompt = f"""
    너는 초등학생의 문해력 향상을 돕는 '어린이 신문 수석 편집장'이야. 
    오늘 신문의 핵심 테마는 '{theme_name}'야.
    아래 제공된 어른들을 위한 뉴스 원문을 바탕으로, 원문의 사실(Fact)을 절대 왜곡하거나 지어내지 말고, 내용 그대로 {grade_text} 수준에 맞게 어휘와 문장만 변환(다시 쓰기)해 줘.

    [작성 규칙]
    1. 팩트 엄수: 원문에 없는 내용을 상상해서 덧붙이거나 사실 관계를 바꾸는 일(할루시네이션)은 절대 금지.
    2. 문체: 실제 신문 기사와 같은 객관적인 평어체(~다., ~밝혔다, ~전망이다 등) 사용. 대화체나 존댓말 금지.
    3. 분량 및 구조: 본문(body)은 소제목 없이 반드시 3개 또는 4개의 문단(p)으로만 구성. 특히, 첫 번째 문단(body[0])은 반드시 '누가, 언제, 어디서, 무엇을, 어떻게, 왜(육하원칙)' 했는지가 명확하게 드러나는 핵심 요약(리드문)으로 작성할 것.
    4. 성취기준 매칭: [성취기준 후보]로 제공된 CSV 데이터를 검토해 보고 이 기사와 완벽하게 일치하면 standards 배열에 추가, 없으면 빈 배열([])로 둘 것.

    [성취기준 후보 (CSV 형식)]
    {standards_csv}

    [원문 기사 정보]
    - 제목: {article_data['title']}
    - 요약문: {article_data['description']}

    [출력 JSON 형식] (반드시 이 JSON 형식만 출력할 것)
    {{
        "title": "기사 제목",
        "lede": "한 줄 리드",
        "body": [
            {{"p": "첫 번째 문단 (육하원칙 요약)"}},
            {{"p": "두 번째 문단 (배경/설명)"}},
            {{"p": "세 번째 문단 (결과/전망)"}}
        ],
        "vocabularies": [
            {{"word": "단어", "meaning": "뜻풀이"}}
        ],
        "think": "생각해볼 질문",
        "tags": ["태그1", "태그2"],
        "standards": [
            {{"code": "코드", "gloss": "내용"}}
        ],
        "source": {{
            "name": "구글 뉴스",
            "url": "{article_data['link']}"
        }},
        "grade": "{grade_value}"
    }}
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"⚠️ AI 생성 또는 JSON 파싱 에러 발생 ({grade_text}용): {e}")
        return None

# ==========================================
# 5. 메인 실행 파트
# ==========================================
def main():
    print("📰 어린이 헤드라인 뉴스 AI 자동 생성 스크립트 시작...")
    
    kst = pytz.timezone('Asia/Seoul')
    today_str = datetime.datetime.now(kst).strftime("%Y-%m-%d")
    theme_name, search_query = get_today_theme_and_query()
    print(f"✅ 오늘의 테마: {theme_name} (검색어: {search_query})")
    
    standards_csv = fetch_standards_csv()
    
    # 💡 [핵심] 불러온 전체 성취기준을 테마에 맞게 압축하여 토큰 절약
    filtered_standards = filter_standards_by_theme(standards_csv, theme_name)
    
    print("🔍 구글 뉴스에서 관련 기사를 검색 중입니다...")
    raw_articles = fetch_news_by_query(search_query, limit=2)
    
    if not raw_articles:
        print("⚠️ 기사를 찾지 못했습니다.")
        return
        
    final_articles = []
    
    for i, raw_art in enumerate(raw_articles):
        print(f"\n▶️ [{i+1}/{len(raw_articles)}] 원문: {raw_art['title'][:30]}...")
        
        grades_to_generate = [
            {"text": "초등학교 3~4학년", "value": "3-4"},
            {"text": "초등학교 5~6학년", "value": "5-6"}
        ]
        
        for grade in grades_to_generate:
            print(f"  🤖 {grade['text']} 수준으로 팩트 기반 변환 중...")
            
            # 💡 최적화된 성취기준(filtered_standards)을 AI에게 전달
            ai_generated_json = rewrite_article_for_kids(raw_art, theme_name, grade['text'], grade['value'], filtered_standards)
            
            if ai_generated_json:
                final_articles.append(ai_generated_json)
            
    if final_articles:
        print(f"\n☁️ Firebase에 {len(final_articles)}개의 기사(학년별 통합)를 업로드합니다. (문서명: {today_str})")
        doc_ref = db.collection('daily_briefings').document(today_str)
        doc_ref.set({
            "articles": final_articles,
            "theme": theme_name,
            "createdAt": firestore.SERVER_TIMESTAMP
        })
        print("🎉 업로드 완료! React 앱을 새로고침하여 확인해 보세요.")
    else:
        print("⚠️ 업로드할 기사가 생성되지 않았습니다.")

if __name__ == "__main__":
    main()