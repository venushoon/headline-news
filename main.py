import os
import re
import io
import csv
import json
import time
import datetime
import pytz
import feedparser
import firebase_admin
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ==========================================
# 0. 무료 한도 가드 & 공통 설정
# ==========================================
# 💡 무료 범위 보장 장치 (신문 발행은 Gemini 무료 등급 안에서만 동작):
#  - 하루 실행당 Gemini 호출 횟수 상한. 기사 선정(1)
#    + 재작성(기사 2개 × 학년 2개 × 최대 재시도 2회 = 최대 8) = 최대 9회.
#    모델 전환·재시도까지 전부 이 횟수에 포함되며, 상한에 닿으면 중단한다.
#  - 오늘 문서가 이미 있으면 Gemini를 한 번도 부르지 않고 종료한다
#    (워크플로우의 '확인 실행'이 공짜로 동작하는 이유).
#  - 원문 본문 수집은 일반 HTTP라 API 비용이 없고, 프롬프트에 넣는
#    본문 길이를 잘라 입력 토큰 증가도 제한한다.
#  - 모델은 1-1절의 후보 목록(고정 1순위 + 허용 목록)을 순서대로 쓴다.
MAX_GEMINI_CALLS = 16  # 2026-11-18: 12→16 (503 재시도가 예산을 태워 뒤 기사가 굶는 사례; flash-lite 무료 한도는 모델당 500회/일이라 여유 충분)
_gemini_call_count = 0

class QuotaExceededError(Exception):
    """모든 후보 모델을 쓸 수 없거나 자체 호출 상한에 도달했을 때 발생."""

def _classify_gemini_error(e):
    """Gemini 오류를 대응 방식별로 분류한다."""
    msg = str(e)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
        return "quota"
    if "404" in msg or "NOT_FOUND" in msg:
        return "not_found"
    if "503" in msg or "UNAVAILABLE" in msg:
        return "unavailable"
    return "other"

def call_gemini(contents, config=None):
    """모든 Gemini 호출의 단일 통로.

    - 호출 횟수를 세어 실행당 상한(MAX_GEMINI_CALLS)을 지킨다.
    - 503(일시 과부하): 같은 모델로 한 번 더 시도한다.
    - 429(쿼터)·404(모델 없음): 다음 후보 모델로 내려가 시도한다.
      후보가 더 없으면 QuotaExceededError로 실행을 중단한다(부분 발행 방지).
    """
    global _gemini_call_count, _model_index
    retried_unavailable = False
    while True:
        if _gemini_call_count >= MAX_GEMINI_CALLS:
            raise QuotaExceededError(
                f"실행당 Gemini 호출 상한({MAX_GEMINI_CALLS}회)에 도달해 중단합니다.")
        _gemini_call_count += 1
        model = MODEL_CANDIDATES[_model_index]
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=config)
        except Exception as e:
            kind = _classify_gemini_error(e)
            if kind in ("quota", "not_found"):
                if _model_index + 1 < len(MODEL_CANDIDATES):
                    _model_index += 1
                    print(f"  🔀 모델 {model} 사용 불가({kind}) → 다음 후보 "
                          f"{MODEL_CANDIDATES[_model_index]}로 전환합니다.")
                    retried_unavailable = False
                    continue
                raise QuotaExceededError(
                    f"사용 가능한 후보 모델이 없습니다. 마지막 오류: {str(e)[:200]}") from e
            if kind == "unavailable" and not retried_unavailable:
                retried_unavailable = True
                print(f"  ⚠️ 모델 {model} 일시 과부하(503) — 같은 모델로 재시도합니다.")
                time.sleep(5)
                continue
            raise

BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36"
}

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
# 1-1. 모델 후보 목록 (고정 1순위 + 허용 목록 폴백)
# ==========================================
# 💡 정책 (2026-11-15):
#  - 1순위는 무료로 검증된 모델로 고정한다. 새 모델이 나와도 자동 승격하지 않는다.
#  - 429(쿼터)·404(모델 없음)일 때만 아래 허용 목록 순서대로 내려간다.
#  - 자동 감지는 허용 패턴(gemini-X.Y-flash-lite 로 정확히 끝나는 이름)에 맞는
#    새 모델을 후보 맨 뒤에 보충하는 용도로만 쓴다. -tts/-image 같은 변종은
#    JSON 모드 미지원·별도 쿼터 문제로 반드시 제외한다.
#  - 무료 여부는 API로 판정할 수 없으므로, 이 목록 자체가 허용 범위다.
#    모델을 1순위로 올리려면 사람이 검증한 뒤 이 목록의 순서를 직접 바꾼다.
MODEL_ALLOWLIST = [
    "gemini-3.5-flash-lite",  # 1순위 — 검증됨 (2026-11-14~15 실제 발행에 사용)
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
]

def build_model_candidates(client):
    """허용 목록 순서 + 감지된 신규 lite 모델(맨 뒤)로 후보 목록을 구성한다.

    모델 목록 조회가 되면 허용 목록 중 실제 존재하는 것만 남기고,
    조회에 실패하면 허용 목록을 그대로 쓴다.
    """
    try:
        available = {m.name.replace("models/", "") for m in client.models.list()}
    except Exception as e:
        print(f"⚠️ 모델 목록 조회 실패, 허용 목록 그대로 사용: {e}")
        return list(MODEL_ALLOWLIST)

    candidates = [name for name in MODEL_ALLOWLIST if name in available]
    if not candidates:
        print("⚠️ 허용 목록 모델이 조회 목록에 없어 허용 목록 그대로 사용합니다.")
        candidates = list(MODEL_ALLOWLIST)

    extras = []
    for name in available:
        match = re.fullmatch(r"gemini-(\d+)\.(\d+)-flash-lite", name)
        if match and name not in candidates:
            extras.append(((int(match.group(1)), int(match.group(2))), name))
    extras.sort(reverse=True)
    if extras:
        print(f"ℹ️ 감지된 신규 lite 모델을 후보 맨 뒤에 보충: {[n for _, n in extras]}")
    return candidates + [name for _, name in extras]

MODEL_CANDIDATES = build_model_candidates(client)
_model_index = 0
print(f"🧠 모델 후보 목록: {MODEL_CANDIDATES}")

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
        5: {"name": "문화와 역사 (전통/문화유산)", "query": "문화재 OR 역사 OR 전통문화 OR 유산 OR 박물관"},
        6: {"name": "인물과 도전 (위인/성장)", "query": "위인 OR 도전 OR 성장 OR 봉사활동 OR 스포츠정신"}
    }
    return themes[weekday]["name"], themes[weekday]["query"]

def _clean_snippet(html_text):
    """구글 뉴스 RSS 요약은 HTML 조각 + 언론사명이 섞여 있어 텍스트만 남긴다."""
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&amp;", "&")
    return " ".join(text.split())

def fetch_news_by_query(query, limit=10):
    encoded_query = urllib.parse.quote(query)
    rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=ko&gl=KR&ceid=KR:ko"

    feed = feedparser.parse(rss_url)
    articles = []

    for entry in feed.entries[:limit]:
        source_name = ""
        if hasattr(entry, 'source'):
            source_name = entry.source.get('title', '') if hasattr(entry.source, 'get') else ""
        articles.append({
            "title": entry.title,
            "link": entry.link,
            "description": _clean_snippet(entry.description if hasattr(entry, 'description') else ""),
            "source_name": source_name,
            "content": "",
            "original_url": ""
        })
    return articles

# ==========================================
# 2-1. 원문 본문 수집 (일반 HTTP — API 비용 없음)
# ==========================================
# 구글 뉴스 RSS 링크는 원문으로 바로 리다이렉트되지 않는 중간 주소라,
# 기사 페이지에서 서명(signature)·타임스탬프를 읽어 batchexecute로
# 원문 URL을 복원한 뒤, 원문 페이지의 <p> 문단들을 추출한다.
_GARTURLREQ_CTX = [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                    None, None, None, None, None, 0, 1],
                   "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0]

def decode_google_news_url(google_link):
    """구글 뉴스 중간 링크 → 원문 URL. 실패 시 None."""
    try:
        art_id = google_link.split("/articles/")[1].split("?")[0]
        page_url = (f"https://news.google.com/rss/articles/{art_id}"
                    "?hl=ko&gl=KR&ceid=KR:ko")
        req = urllib.request.Request(page_url, headers=BROWSER_UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        sg = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        if not (sg and ts):
            return None
        inner = ["garturlreq", _GARTURLREQ_CTX, art_id, int(ts.group(1)), sg.group(1)]
        envelope = [["Fbv4je", json.dumps(inner, separators=(",", ":")), None, "0"]]
        body = "f.req=" + urllib.parse.quote(json.dumps([envelope], separators=(",", ":")))
        req2 = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body.encode(),
            headers={**BROWSER_UA,
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        with urllib.request.urlopen(req2, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        if text.startswith(")]}'"):
            text = text.split("\n", 1)[1]
        for row in json.loads(text.strip()):
            if (isinstance(row, list) and len(row) >= 3
                    and isinstance(row[2], str) and "garturlres" in row[2]):
                payload = json.loads(row[2])
                if payload and payload[0] == "garturlres":
                    return payload[1]
    except Exception:
        return None
    return None

class _ParagraphExtractor(HTMLParser):
    """기사 본문으로 보이는 <p> 문단만 모은다 (스크립트·메뉴 등 제외)."""
    SKIP_TAGS = {"script", "style", "noscript", "header", "footer",
                 "nav", "aside", "form", "iframe", "svg", "button"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_p = False
        self._buf = []
        self.paragraphs = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip += 1
        if tag == "p" and self._skip == 0:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip > 0:
            self._skip -= 1
        if tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._buf).split())
            if len(text) >= 40:  # 캡션·버튼류 짧은 문장은 본문으로 보지 않음
                self.paragraphs.append(text)

    def handle_data(self, data):
        if self._in_p and self._skip == 0:
            self._buf.append(data)

def _decode_html_bytes(raw, content_type_charset):
    """charset 헤더 → 메타 태그 → utf-8 순으로 시도, 깨지면 euc-kr로 재시도."""
    charset = content_type_charset
    if not charset:
        m = re.search(rb'charset=["\']?\s*([a-zA-Z0-9\-_]+)', raw[:4096])
        if m:
            charset = m.group(1).decode("ascii", errors="ignore")
    for enc in [charset, "utf-8", "euc-kr"]:
        if not enc:
            continue
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.count("\ufffd") <= 2:
            return text
    return raw.decode("utf-8", errors="replace")

def fetch_original_text(url, max_chars=2800):
    """원문 페이지에서 본문 텍스트 추출. 실패/과소 시 빈 문자열."""
    try:
        req = urllib.request.Request(url, headers=BROWSER_UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(2_000_000)
            charset = resp.headers.get_content_charset()
        html = _decode_html_bytes(raw, charset)
        parser = _ParagraphExtractor()
        parser.feed(html)
        text = "\n".join(parser.paragraphs)
        return text[:max_chars] if len(text) >= 400 else ""
    except Exception:
        return ""

# 본문 확보를 시도할 후보 수 상한 (HTTP라 무료지만 실행 시간 보호)
MAX_FULLTEXT_ATTEMPTS = 6

def enrich_articles_with_fulltext(articles):
    """
    후보 기사에 원문 본문을 채운다.
    본문을 얻은 기사가 앞으로 오도록 정렬하되 순서는 안정적으로 유지.
    반환: (정렬된 기사 목록, 본문 확보 개수)
    """
    attempted = 0
    got = 0
    for art in articles:
        if attempted >= MAX_FULLTEXT_ATTEMPTS:
            break
        attempted += 1
        original_url = decode_google_news_url(art["link"])
        if not original_url:
            continue
        art["original_url"] = original_url
        text = fetch_original_text(original_url)
        if text:
            art["content"] = text
            got += 1
    articles.sort(key=lambda a: 0 if a.get("content") else 1)  # stable sort
    return articles, got

# ==========================================
# 3. 성취기준 CSV 불러오기 
# ==========================================
def fetch_standards_csv():
    url = "https://raw.githubusercontent.com/venushoon/headline-news/refs/heads/main/2026list.csv"
    try:
        print("🌐 깃허브에서 성취기준(2026list.csv)을 불러오는 중...")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        # 실패를 조용히 넘기면 성취기준 없는 기사가 그대로 발행되므로 크게 알린다
        print(f"⚠️⚠️ [경고] 성취기준 CSV 로드 실패 — 오늘 기사는 성취기준 매칭 없이 생성됩니다: {e}")
        return ""

# ==========================================
# 💡 성취기준 토큰 다이어트(최적화) 로직
# ==========================================
def filter_standards_by_theme(csv_data, theme_name):
    if not csv_data:
        return ""
    
    theme_keywords = {
        "우리 사회 (사회/문화)": ["사회", "도덕", "국어", "역사", "인권", "민주"],
        "지구촌 소식 (세계/국제)": ["사회", "도덕", "세계", "지리", "문화", "국제"],
        "푸른 지구 (환경/기후)": ["과학", "사회", "도덕", "실과", "환경", "기후", "생태", "탄소", "자연"],
        "신기한 기술 (과학/IT)": ["과학", "실과", "수학", "정보", "기술", "인공지능", "우주"],
        "생활과 경제 (경제/안전)": ["사회", "실과", "수학", "안전", "체육", "경제", "소비", "생활"],
        "문화와 역사 (전통/문화유산)": ["사회", "도덕", "국어", "역사", "문화", "전통", "예술"],
        "인물과 도전 (위인/성장)": ["국어", "도덕", "사회", "체육", "인물", "봉사"]
    }
    
    keywords = theme_keywords.get(theme_name, [])
    if not keywords:
        return csv_data

    # 💡 CSV는 셀 안에 줄바꿈이 들어간 멀티라인 형식이라 줄 단위로 자르면
    # 레코드가 깨진다. 반드시 csv 모듈로 레코드 단위 파싱/필터/재직렬화한다.
    try:
        reader = csv.reader(io.StringIO(csv_data))
        rows = [row for row in reader if row]
    except Exception:
        return csv_data
    if len(rows) <= 1:
        return csv_data

    header, body_rows = rows[0], rows[1:]
    filtered = [row for row in body_rows
                if any(keyword in " ".join(row) for keyword in keywords)]
    filtered = filtered[:100]  # 상한은 '줄'이 아니라 '레코드(성취기준)' 100개

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(filtered)
    return out.getvalue()

# ==========================================
# 💡 1단계: 규칙 기반 홍보성/저품질 기사 제거
# ==========================================
BLACKLIST_KEYWORDS = [
    "채용", "모집", "접수", "부고", "인사", "동정", "포토", "화보",
    "공연", "행사", "축제", "전시회", "박람회", "할인", "이벤트",
    "특가", "세일", "쿠폰", "경품", "당첨", "판매", "출시 기념",
    "브리핑", "인터뷰 전문", "속보", "단독", "[포토]", "[영상]",
    "뉴스 모음", "주요 뉴스", "종합뉴스", "와이드", "이슈 정리",
    "한주 정리", "주간 브리핑", "톱뉴스", "헤드라인 모음"
]

def filter_low_quality_articles(articles):
    """
    제목에 홍보성/저품질 키워드가 포함된 기사를 1차로 걸러냄.
    """
    filtered = []
    for art in articles:
        title = art.get("title", "")
        if any(keyword in title for keyword in BLACKLIST_KEYWORDS):
            continue
        filtered.append(art)
    return filtered if len(filtered) >= 2 else articles

# ==========================================
# 💡 2단계: AI를 이용한 교육 가치 기준 기사 선정 (1회 호출)
# ==========================================
def select_best_articles_for_kids(articles, theme_name, pick_count=2):
    """
    후보 기사 목록 전체를 한 번의 API 호출로 AI에게 보내,
    초등학생의 문해력/시민의식 함양에 가장 적합한 기사를 pick_count개 선정.
    실패 시 후보 상위 pick_count개로 안전하게 폴백.
    """
    if len(articles) <= pick_count:
        return articles

    def _candidate_line(i, art):
        # 원문 본문이 확보된 기사는 본문 앞부분까지 보여줘 선정 정확도를 높인다
        body_hint = (art.get('content') or art.get('description') or '')[:300]
        mark = "원문확보" if art.get('content') else "요약만"
        return f"{i}. 제목: {art['title']}\n   요약: {art['description'][:150]}\n   본문({mark}): {body_hint}"

    candidate_list = "\n".join(
        _candidate_line(i, art) for i, art in enumerate(articles)
    )

    prompt = f"""
    너는 초등학생을 위한 어린이 신문의 '수석 편집장'이야.
    오늘 신문의 핵심 테마는 '{theme_name}'이고, 아래는 오늘 수집된 뉴스 후보 목록이야.

    [선정 기준]
    1. 초등학생의 문해력과 시민 의식 함양에 도움이 되는 기사 (단순 지역 행사/공연/광고성 안내보다는, 사회 현상·정책·과학·환경 등 배경지식이 되는 기사를 우선)
    2. 사실관계가 명확하고 원문만으로 충분히 이해 가능한 기사 (단신, 속보성 짧은 기사는 지양)
    3. 자극적이거나 폭력적, 선정적인 내용은 제외
    4. 최대한 오늘 테마('{theme_name}')와 관련성이 높은 기사
    5. 본문이 '원문확보'로 표시된 기사를 우선 선정 (요약만 있는 기사는 사실이 부족해 기사로 다시 쓰기 어렵다)

    [뉴스 후보 목록]
    {candidate_list}

    위 기준에 따라 가장 적합한 기사 {pick_count}개를 선정해서, 아래 JSON 형식으로만 출력해.
    {{
        "selected_indices": [숫자, 숫자]
    }}
    """

    try:
        response = call_gemini(
            prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        result = json.loads(response.text)
        indices = result.get("selected_indices", [])
        selected = [articles[i] for i in indices if 0 <= i < len(articles)]
        if len(selected) >= 1:
            print(f"✅ AI가 선정한 기사 {len(selected)}개 (후보 {len(articles)}개 중)")
            return selected[:pick_count]
        else:
            raise ValueError("선정된 기사가 없음")
    except QuotaExceededError:
        raise  # 쿼터 문제는 폴백으로 숨기지 않고 실행 자체를 중단한다
    except Exception as e:
        print(f"⚠️ 기사 선정 AI 호출 실패, 상위 {pick_count}개로 대체: {e}")
        return articles[:pick_count]

# ==========================================
# 💡 응답 검증 및 정규화
# ==========================================
def normalize_article_body(data):
    """
    AI가 body를 ["문장1", "문장2"] 형태(문자열 배열)로 반환하는 경우가 있어,
    프론트엔드가 기대하는 [{"p": "문장1"}, {"p": "문장2"}] 형태로 통일함.
    """
    if not data or not isinstance(data.get("body"), list):
        return data
    normalized = []
    for item in data["body"]:
        if isinstance(item, dict) and "p" in item:
            normalized.append({"p": item["p"]})
        elif isinstance(item, str):
            normalized.append({"p": item})
    data["body"] = normalized
    return data

def is_valid_article(data, expected_paragraphs=None, level_check=None):
    """
    AI 응답이 화면에 정상적으로 표시될 수 있는 최소 요건을 갖췄는지 검증.
    expected_paragraphs가 주어지면 유효 문단 수가 정확히 그 개수와 같아야 하고,
    level_check가 주어지면 문장 길이·총 분량의 학년별 하드 bound도 검사한다
    (규칙을 어기면 재시도로 다시 받기 위함).
    """
    if not data:
        return False
    if not data.get("title") or not data.get("lede"):
        return False
    body = data.get("body")
    if not body or not isinstance(body, list):
        return False
    valid_paragraphs = [b for b in body if isinstance(b, dict) and b.get("p") and len(b["p"].strip()) > 5]
    if expected_paragraphs is not None and len(valid_paragraphs) != expected_paragraphs:
        return False
    if expected_paragraphs is None and len(valid_paragraphs) < 2:
        return False
    if level_check:
        total, avg_sent, max_sent = _body_metrics([b["p"] for b in valid_paragraphs])
        if max_sent > level_check["max_sent"] or avg_sent > level_check["max_avg_sent"]:
            return False
        if total < level_check["min_total"] or total > level_check["max_total"]:
            return False
    return True

def _body_metrics(paragraph_texts):
    """본문 문단들에서 (총 글자수, 평균 문장 길이, 최장 문장 길이)를 잰다."""
    total = sum(len(t) for t in paragraph_texts)
    sents = []
    for t in paragraph_texts:
        sents += [s.strip() for s in re.split(r"[.!?]+", t) if s.strip()]
    if not sents:
        return total, 0, 0
    lens = [len(s) for s in sents]
    return total, sum(lens) / len(lens), max(lens)

def _fails_only_on_min_total(data, expected_paragraphs, check):
    """
    구조·문단 수·문장 길이는 모두 합격인데 총 분량 하한만 미달한 경우인지.
    마지막 시도에서 이 경우만 채택해, 짧은 원문 기사가 통째로 빠지는 것을 막는다
    (2026-11-18 한·호주 5~6학년판 누락 사례). 단, 하한의 70% 미만이면
    퇴화한 출력으로 보고 채택하지 않는다.
    """
    if not data or not data.get("title") or not data.get("lede"):
        return False
    body = data.get("body")
    if not body or not isinstance(body, list):
        return False
    valid = [b for b in body if isinstance(b, dict) and b.get("p") and len(b["p"].strip()) > 5]
    if len(valid) != expected_paragraphs:
        return False
    total, avg_sent, max_sent = _body_metrics([b["p"] for b in valid])
    if max_sent > check["max_sent"] or avg_sent > check["max_avg_sent"] or total > check["max_total"]:
        return False
    return int(check["min_total"] * 0.7) <= total < check["min_total"]

# ==========================================
# 4. 제미나이(Gemini) AI 기사 '변환' 로직
# ==========================================
# 학년별 본문 구성·난이도 기준 (2026-11-15: 3~4학년 3문단, 5~6학년 4문단 + 난이도 차등)
# level_rules는 프롬프트에 그대로 들어가고, check는 검증에서 기계적으로 거르는 하드 bound다
# (프롬프트 목표보다 넓게 잡아 재시도 폭증을 막는다. 특히 분량 하한은 짧은 원문에서
#  '사실 엄수' 규칙과 충돌하므로, 퇴화한 출력만 거르는 수준으로 낮게 둔다 —
#  2026-11-15 CJ 기사 5~6학년판이 원문 분량 한계로 2회 다 탈락해 건너뛰어진 사례.)
GRADE_SPECS = {
    "3-4": {
        "count": 3,
        "roles": ["육하원칙 요약", "배경/설명", "결과/전망"],
        "sentences": "각 문단은 최소 2문장 이상",
        "level_rules": [
            "한 문장은 40자 안팎으로 짧게 쓰고, 60자를 넘기지 않는다. 한 문장에는 하나의 정보만 담는다.",
            "사람 이름과 직함을 줄줄이 나열하지 않는다. 꼭 필요한 인물만 쓴다.",
            "어려운 한자어·외래어·전문용어는 쉬운 말로 바꾸고, 바꿀 수 없으면 바로 뒤에 괄호로 짧게 뜻을 덧붙인다. (예: '사업 재편(회사가 하는 일을 다시 정리하는 것)')",
            "기관·단체의 긴 공식 이름은 짧게 줄여 쓴다.",
        ],
        "total_range": (450, 600),
        "check": {"max_sent": 100, "max_avg_sent": 65, "min_total": 300, "max_total": 800},
    },
    "5-6": {
        "count": 4,
        "roles": ["육하원칙 요약", "배경", "자세한 내용 (관련 인물·추가 사실)", "결과/전망"],
        "sentences": "각 문단은 2~3문장",
        "level_rules": [
            "한 문장은 60~80자까지 쓸 수 있고, 원인과 결과를 접속 표현으로 연결해도 좋다.",
            "어려운 용어는 문맥 속에서 풀어 설명하거나 괄호로 뜻을 덧붙인다.",
            "사실의 배경과 원인, 앞으로 미칠 영향까지 포함해 한 단계 깊게 설명한다.",
        ],
        "total_range": (650, 850),
        "check": {"max_sent": 140, "max_avg_sent": 95, "min_total": 420, "max_total": 1100},
    },
}

def rewrite_article_for_kids(article_data, theme_name, grade_text, grade_value, standards_csv, max_retries=2):
    spec = GRADE_SPECS.get(grade_value, GRADE_SPECS["3-4"])
    target_count = spec["count"]
    ordinal = ["첫 번째", "두 번째", "세 번째", "네 번째", "다섯 번째"]
    roles_text = " → ".join(spec["roles"])
    body_example = ",\n            ".join(
        '{"p": "' + f"{ordinal[i]} 문단 ({role})" + '"}'
        for i, role in enumerate(spec["roles"]))
    level_rules_block = "\n".join(f"       - {r}" for r in spec["level_rules"])
    total_min, total_max = spec["total_range"]
    prompt = f"""
    너는 초등학생의 문해력 향상을 돕는 '어린이 신문 수석 편집장'이야. 
    오늘 신문의 핵심 테마는 '{theme_name}'야.
    아래 제공된 어른들을 위한 뉴스 원문을 바탕으로, 원문의 사실(Fact)을 절대 왜곡하거나 지어내지 말고, 내용 그대로 {grade_text} 수준에 맞게 어휘와 문장만 변환(다시 쓰기)해 줘.

    [작성 규칙]
    1. 팩트 엄수: 원문에 없는 내용을 상상해서 덧붙이거나 사실 관계를 바꾸는 일(할루시네이션)은 절대 금지.
    2. 문체: 실제 신문 기사와 같은 객관적인 평어체(~다., ~밝혔다, ~전망이다 등) 사용. 대화체나 존댓말 금지.
    3. 분량 및 구조: body는 반드시 {target_count}개의 문단(p)으로 구성하며, 문단의 역할은 순서대로 [{roles_text}] 이다. {spec['sentences']}으로 작성할 것. body를 비워두거나 문단 개수를 어기는 것은 절대 금지. 특히, 첫 번째 문단(body[0])은 반드시 '누가, 언제, 어디서, 무엇을, 어떻게, 왜(육하원칙)' 했는지가 명확하게 드러나는 핵심 요약(리드문)으로 작성할 것.
    4. 성취기준 매칭: [성취기준 후보]로 제공된 CSV 데이터를 검토해 보고 이 기사와 완벽하게 일치하면 standards 배열에 추가, 없으면 빈 배열([])로 둘 것.
    5. 난이도 ({grade_text} 기준 — 반드시 지킬 것):
{level_rules_block}
       - 본문 전체 분량은 {total_min}~{total_max}자 사이로 맞춘다.

    [성취기준 후보 (CSV 형식)]
    {standards_csv}

    [원문 기사 정보]
    - 제목: {article_data['title']}
    - 요약문: {article_data['description']}
    - 원문 본문: {article_data.get('content') or '(원문 본문을 확보하지 못했습니다. 제목과 요약문에 적힌 사실만으로, 있는 내용을 풀어서 다시 쓰세요. 요약에 없는 숫자·인물·장소·이유를 새로 지어내면 안 됩니다.)'}

    [출력 JSON 형식] (반드시 이 JSON 형식만 출력할 것. body는 절대 비우지 말 것)
    {{
        "title": "기사 제목",
        "lede": "한 줄 리드",
        "body": [
            {body_example}
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
            "name": "{article_data.get('source_name') or '구글 뉴스'}",
            "url": "{article_data.get('original_url') or article_data['link']}"
        }},
        "grade": "{grade_value}"
    }}
    """

    for attempt in range(1, max_retries + 1):
        response = None
        try:
            response = call_gemini(
                prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=4096  # 응답이 중간에 잘려 본문이 비는 것을 방지
                )
            )
            data = json.loads(response.text)
            data = normalize_article_body(data)

            if is_valid_article(data, expected_paragraphs=target_count, level_check=spec["check"]):
                return data
            elif attempt == max_retries and _fails_only_on_min_total(data, target_count, spec["check"]):
                total_chars = sum(len(b["p"]) for b in data["body"] if isinstance(b, dict) and b.get("p"))
                print(f"  ⚠️ 마지막 시도: 총 분량만 하한({spec['check']['min_total']}자)에 미달(총 {total_chars}자) — 기사 누락 방지를 위해 채택합니다.")
                return data
            else:
                print(f"  ⚠️ 본문이 비었거나 문단 수(목표 {target_count}개)·난이도 기준과 다른 응답 (시도 {attempt}/{max_retries}) - 재시도합니다.")
                print(f"  🔎 [디버그] 실제 응답 데이터: {json.dumps(data, ensure_ascii=False)[:500]}")

        except QuotaExceededError:
            raise  # 쿼터 초과 시 재시도로 호출을 더 태우지 않고 즉시 중단
        except Exception as e:
            print(f"  ⚠️ AI 생성 또는 JSON 파싱 에러 발생 ({grade_text}용, 시도 {attempt}/{max_retries}): {e}")
            try:
                finish_reason = response.candidates[0].finish_reason if response else "알 수 없음"
                print(f"  🔎 [디버그] finish_reason: {finish_reason}")
                print(f"  🔎 [디버그] 원본 응답 일부: {response.text[:500] if response and response.text else '(응답 없음)'}")
            except Exception:
                pass

    print(f"  ❌ {max_retries}회 시도 후에도 정상 기사를 생성하지 못해 이 기사({grade_text})는 건너뜁니다.")
    return None

# ==========================================
# 5. 메인 실행 파트
# ==========================================
def main():
    print("📰 어린이 헤드라인 뉴스 AI 자동 생성 스크립트 시작...")
    print(f"🧠 사용 모델: {MODEL_CANDIDATES[_model_index]} "
          f"(후보 {len(MODEL_CANDIDATES)}개, 쿼터·모델 오류 시 순서대로 전환)")

    kst = pytz.timezone('Asia/Seoul')
    today_str = datetime.datetime.now(kst).strftime("%Y-%m-%d")

    # 💡 무료 한도 보호 + 중복 발행 방지: 오늘 문서가 이미 있으면
    # Gemini를 한 번도 호출하지 않고 종료한다. 워크플로우의 '확인 실행'이
    # 이 동작을 이용해, 첫 실행이 지연/실패한 날에만 대신 발행한다.
    # 수동 강제 재발행이 필요하면 FORCE_RUN=1 환경변수를 준다.
    if os.environ.get("FORCE_RUN") != "1":
        existing = db.collection('daily_briefings').document(today_str).get()
        if existing.exists:
            print(f"✅ {today_str} 신문이 이미 발행돼 있어 건너뜁니다. (기사 생성 호출 0회)")
            return

    try:
        run_pipeline(today_str)
    except QuotaExceededError as e:
        # 쿼터 초과 상태에서는 부분 발행도 하지 않는다 (기존 상태 유지)
        print(f"\n⛔ 무료 쿼터 보호로 오늘 발행을 중단합니다: {e}")
        print("오늘 문서는 만들지 않았으니, 다음 정기 실행에서 다시 시도합니다.")
        return
    print(f"📊 이번 실행 Gemini 호출 횟수: {_gemini_call_count}회 (상한 {MAX_GEMINI_CALLS}회)")

def run_pipeline(today_str):
    theme_name, search_query = get_today_theme_and_query()
    print(f"✅ 오늘의 테마: {theme_name} (검색어: {search_query})")

    standards_csv = fetch_standards_csv()
    filtered_standards = filter_standards_by_theme(standards_csv, theme_name)
    
    print("🔍 구글 뉴스에서 관련 기사를 검색 중입니다...")
    raw_candidates = fetch_news_by_query(search_query, limit=10)
    
    if not raw_candidates:
        print("⚠️ 기사를 찾지 못했습니다.")
        return

    print(f"📋 후보 기사 {len(raw_candidates)}개 수집 완료. 홍보성/저품질 기사 필터링 중...")
    filtered_candidates = filter_low_quality_articles(raw_candidates)
    print(f"✅ 필터링 후 {len(filtered_candidates)}개 후보 남음.")

    print("🌐 원문 본문 수집 중... (일반 HTTP라 API 비용 없음)")
    filtered_candidates, fulltext_count = enrich_articles_with_fulltext(filtered_candidates)
    print(f"✅ 원문 본문 확보: {fulltext_count}개 / 시도 {min(len(filtered_candidates), MAX_FULLTEXT_ATTEMPTS)}개")

    print("🧠 AI가 초등학생 교육 가치 기준으로 최종 기사를 선정 중...")
    raw_articles = select_best_articles_for_kids(filtered_candidates, theme_name, pick_count=2)

    if not raw_articles:
        print("⚠️ 선정된 기사가 없습니다.")
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