import os
import re
import sys
import html
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
from concurrent.futures import ThreadPoolExecutor
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
#    + 재작성(기사 2개 × 학년 2개 × 기본 재시도 2회 = 최대 8) = 최대 9회가 기본.
#    문단 수 부족으로만 탈락한 판은 최대 4회까지 더 시도할 수 있으나
#    (EXTENDED_MAX_ATTEMPTS), 전체 호출 상한(16회)이 최종 가드다.
#    모델 전환·재시도까지 전부 이 횟수에 포함되며, 상한에 닿으면 중단한다.
#  - 오늘 문서가 이미 있으면 Gemini를 한 번도 부르지 않고 종료한다
#    (워크플로우의 '확인 실행'이 공짜로 동작하는 이유).
#  - 원문 본문 수집은 일반 HTTP라 API 비용이 없고, 프롬프트에 넣는
#    본문 길이를 잘라 입력 토큰 증가도 제한한다.
#  - 모델은 1-1절의 후보 목록(고정 1순위 + 허용 목록)을 순서대로 쓴다.
MAX_GEMINI_CALLS = 24  # 2026-11-18: 12→16 (503 재시도가 예산을 태워 뒤 기사가 굶는 사례; flash-lite 무료 한도는 모델당 500회/일이라 여유 충분)
EXTENDED_MAX_ATTEMPTS = 4  # 2026-11-21: 문단 수 부족(빈 문단 등)으로만 탈락한 판의 시도 상한 (기본 2회 → 4회; 실행 #128 5~6학년판 누락 사례)
_gemini_call_count = 0
# 503(과부하)은 같은 모델로 지수 백오프 재시도 후 다음 후보로 내려간다 (2026-11-26: 5초×1회 → 5·15·45초)
UNAVAILABLE_BACKOFF_SECONDS = [5, 15, 45]

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
    - 503(일시 과부하): 같은 모델로 한 번 더 시도하고, 그래도 503이면
      다음 후보 모델로 내려가 시도한다 (2026-11-24 실행 #139: 3.5 과부하가
      발행 내내 이어져 기사 3개가 응답 자체를 못 받고 탈락한 사례).
    - 429(쿼터)·404(모델 없음): 다음 후보 모델로 내려가 시도한다.
      후보가 더 없으면 QuotaExceededError로 실행을 중단한다(부분 발행 방지).
    """
    global _gemini_call_count, _model_index
    unavailable_retries = 0
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
                    unavailable_retries = 0
                    continue
                raise QuotaExceededError(
                    f"사용 가능한 후보 모델이 없습니다. 마지막 오류: {str(e)[:200]}") from e
            if kind == "unavailable":
                if unavailable_retries < len(UNAVAILABLE_BACKOFF_SECONDS):
                    wait = UNAVAILABLE_BACKOFF_SECONDS[unavailable_retries]
                    unavailable_retries += 1
                    print(f"  ⚠️ 모델 {model} 일시 과부하(503) — {wait}초 뒤 같은 모델로 재시도합니다 "
                          f"({unavailable_retries}/{len(UNAVAILABLE_BACKOFF_SECONDS)}).")
                    time.sleep(wait)
                    continue
                # 백오프 재시도까지 503이면 과부하가 이어지는 것으로 보고 다음 후보로 전환
                if _model_index + 1 < len(MODEL_CANDIDATES):
                    _model_index += 1
                    print(f"  🔀 모델 {model} 과부하 지속(503) → 다음 후보 "
                          f"{MODEL_CANDIDATES[_model_index]}로 전환합니다.")
                    unavailable_retries = 0
                    continue
                raise QuotaExceededError(
                    f"모든 후보 모델이 과부하(503) 상태입니다. 마지막 오류: {str(e)[:200]}") from e
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

# 외부 서비스 핸들 — import 시점에 키·네트워크를 요구하지 않도록 init_services()에서 채운다
# (테스트·import 가능, 2026-11-26)
client = None
db = None

def init_services():
    """Gemini·Firebase 클라이언트와 모델 후보 목록을 초기화한다 (main()에서 한 번 호출)."""
    global client, db, MODEL_CANDIDATES, _model_index
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\n⚠️ [오류] .env 파일에서 'GEMINI_API_KEY'를 찾을 수 없습니다.\n")
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("serviceAccountKey.json")
            firebase_admin.initialize_app(cred)
        except FileNotFoundError:
            print("\n⚠️ [오류] serviceAccountKey.json 파일이 없습니다.\n")
            sys.exit(1)
    db = firestore.client(database_id="headline-news-d6a13")

    MODEL_CANDIDATES = build_model_candidates(client)
    _model_index = 0
    print(f"🧠 모델 후보 목록: {MODEL_CANDIDATES}")

# ==========================================
# 1-1. 모델 후보 목록 (고정 1순위 + 허용 목록 폴백)
# ==========================================
# 💡 정책 (2026-11-15):
#  - 1순위는 무료로 검증된 모델로 고정한다. 새 모델이 나와도 자동 승격하지 않는다.
#  - 429(쿼터)·404(모델 없음)일 때, 그리고 503 과부하가 재시도 후에도 이어질 때 아래 허용 목록 순서대로 내려간다.
#  - 자동 감지는 허용 패턴(gemini-X.Y-flash-lite 로 정확히 끝나는 이름)에 맞는
#    새 모델을 후보 맨 뒤에 보충하는 용도로만 쓴다. -tts/-image 같은 변종은
#    JSON 모드 미지원·별도 쿼터 문제로 반드시 제외한다.
#  - 무료 여부는 API로 판정할 수 없으므로, 이 목록 자체가 허용 범위다.
#    모델을 1순위로 올리려면 사람이 검증한 뒤 이 목록의 순서를 직접 바꾼다.
# 2026-11-26: gemini-2.5-flash-lite는 신규 사용자에게 404(서비스 종료)라 제거했다
#  (11-26 05:00 실행에서 3.5·3.1이 503일 때 대신하지 못해 발행이 중단됨).
#  gemini-4-hydrogen은 공식 문서상 무료 등급 제공(요금 0, 호출 제한만 있음)이라 마지막 예비로 추가한다.
MODEL_ALLOWLIST = [
    "gemini-3.5-flash-lite",  # 1순위 — 검증됨 (2026-11-14~15 실제 발행에 사용)
    "gemini-3.1-flash-lite",
    "gemini-4-hydrogen",      # 무료 등급 예비 (2026-11-12 GA)
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

MODEL_CANDIDATES = []  # init_services()에서 채워진다
_model_index = 0

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
    text = html.unescape(text)  # &nbsp; &quot; &#39; &lt; 등 모든 엔티티를 한 번에 디코딩
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
            print("  ⚠️ 원문 URL 복원 실패: 기사 페이지에서 서명 값을 찾지 못했습니다(구글 뉴스 형식 변경 가능성).")
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
    except Exception as e:
        print(f"  ⚠️ 원문 URL 복원 실패(구글 뉴스 형식 변경 가능성): {type(e).__name__}: {str(e)[:100]}")
        return None
    print("  ⚠️ 원문 URL 복원 실패: 응답에서 원문 주소를 찾지 못했습니다.")
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
# 프롬프트에 넣는 성취기준 레코드 상한 (입력 토큰 절감, 2026-11-26: 100→40)
MAX_STANDARDS_IN_PROMPT = 40

def _fetch_fulltext_for(art):
    """기사 하나의 원문 본문을 채운다. 성공하면 True."""
    original_url = decode_google_news_url(art["link"])
    if not original_url:
        return False
    art["original_url"] = original_url
    text = fetch_original_text(original_url)
    if text:
        art["content"] = text
        return True
    return False

def enrich_articles_with_fulltext(articles):
    """
    후보 기사에 원문 본문을 채운다 (최대 MAX_FULLTEXT_ATTEMPTS개를 병렬 수집).
    본문을 얻은 기사가 앞으로 오도록 정렬하되 순서는 안정적으로 유지.
    반환: (정렬된 기사 목록, 본문 확보 개수)
    """
    targets = articles[:MAX_FULLTEXT_ATTEMPTS]
    if targets:
        with ThreadPoolExecutor(max_workers=4) as pool:
            got = sum(1 for ok in pool.map(_fetch_fulltext_for, targets) if ok)
    else:
        got = 0
    if targets and got == 0:
        print("⚠️⚠️ [경고] 원문 본문을 한 건도 확보하지 못했습니다 — 요약만으로 기사를 쓰게 되어 "
              "사실 오류 위험이 큽니다. 구글 뉴스 URL 복원 로직 점검이 필요합니다.")
    elif targets and got * 2 < len(targets):
        print(f"⚠️ 원문 확보율이 낮습니다({got}/{len(targets)}).")
    articles.sort(key=lambda a: 0 if a.get("content") else 1)  # stable sort
    return articles, got

# ==========================================
# 💡 요약 인덱스 + 중복 기사 방지 (2026-11-26)
# ==========================================
# daily_briefings/_index 문서에 최근 INDEX_MAX_DAYS일의 날짜별 요약(제목·리드·태그·낱말·원문 URL/제목)을
# 모아 둔다. 화면의 아카이브 목록·검색은 이 문서 1건만 읽고(본문 전체 읽기 대신),
# 발행 때는 최근 DEDUP_DAYS일에 쓴 기사와 겹치는 후보를 걸러내는 데 쓴다.
# (_index는 날짜 형식이 아니라 기존 보안 규칙(daily_briefings/{date})으로 그대로 읽힌다.)
INDEX_DOC_ID = "_index"
INDEX_MAX_DAYS = 120
DEDUP_DAYS = 14
DEDUP_SIMILARITY = 0.5
_index_entries = []
_index_ok = False

def _summarize_articles(articles):
    out = []
    for a in articles:
        src = a.get("source") or {}
        out.append({
            "grade": a.get("grade", ""),
            "title": a.get("title", ""),
            "lede": a.get("lede", ""),
            "tags": [x for x in (a.get("tags") or []) if isinstance(x, str)][:6],
            "words": [v.get("word", "") for v in (a.get("vocabularies") or [])
                      if isinstance(v, dict)][:8],
            "url": src.get("url", ""),
            "ot": src.get("title", ""),
        })
    return out

def _load_index():
    snap = db.collection("daily_briefings").document(INDEX_DOC_ID).get()
    if not snap.exists:
        return None
    return (snap.to_dict() or {}).get("entries", [])

def _save_index(entries):
    db.collection("daily_briefings").document(INDEX_DOC_ID).set({
        "entries": entries[:INDEX_MAX_DAYS],
        "updatedAt": firestore.SERVER_TIMESTAMP,
    })

def _rebuild_index():
    """최근 문서에서 인덱스를 처음부터 다시 만든다 (인덱스가 없거나 뒤처졌을 때만)."""
    print("🗂️ 요약 인덱스를 (재)구성합니다...")
    entries = []
    q = (db.collection("daily_briefings")
         .order_by("__name__", direction=firestore.Query.DESCENDING)
         .limit(INDEX_MAX_DAYS + 1))
    for snap in q.stream():
        if snap.id == INDEX_DOC_ID:
            continue
        data = snap.to_dict() or {}
        entries.append({"date": snap.id, "theme": data.get("theme", ""),
                        "articles": _summarize_articles(data.get("articles") or [])})
    entries = entries[:INDEX_MAX_DAYS]
    _save_index(entries)
    return entries

def ensure_index():
    """인덱스를 읽어 오고, 없거나 최신 문서보다 뒤처졌으면 재구성한다. 실패해도 발행은 계속한다."""
    global _index_entries, _index_ok
    try:
        entries = _load_index()
        q = (db.collection("daily_briefings")
             .order_by("__name__", direction=firestore.Query.DESCENDING)
             .limit(2).select([]))
        ids = [s.id for s in q.stream() if s.id != INDEX_DOC_ID]
        latest = ids[0] if ids else None
        if entries is None or (latest and (not entries or entries[0].get("date") != latest)):
            entries = _rebuild_index()
        _index_entries, _index_ok = entries, True
    except Exception as e:
        _index_entries, _index_ok = [], False
        print(f"⚠️ 요약 인덱스 준비 실패(중복 방지·인덱스 갱신을 건너뜁니다): {type(e).__name__}: {str(e)[:150]}")

def update_index(date_str, theme_name, articles):
    """오늘 발행분을 인덱스 맨 앞에 반영한다."""
    if not _index_ok:
        print("ℹ️ 인덱스 상태를 알 수 없어 갱신을 건너뜁니다. 다음 실행에서 재구성됩니다.")
        return
    entry = {"date": date_str, "theme": theme_name, "articles": _summarize_articles(articles)}
    entries = [entry] + [e for e in _index_entries if e.get("date") != date_str]
    try:
        _save_index(entries)
    except Exception as e:
        print(f"⚠️ 인덱스 갱신 실패: {type(e).__name__}: {str(e)[:150]}")

def _norm_title(title):
    """구글 뉴스 제목 끝의 ' - 언론사'를 떼고 한글·영숫자만 남긴다."""
    base = (title or "").rsplit(" - ", 1)[0]
    return re.sub(r"[^0-9a-z가-힣]", "", base.lower())

def _bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

def _titles_similar(a, b):
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ba, bb = _bigrams(na), _bigrams(nb)
    return len(ba & bb) / max(1, len(ba | bb)) >= DEDUP_SIMILARITY

def _seen_recent():
    """최근 DEDUP_DAYS일에 발행한 기사의 (원문 URL 집합, 제목 목록)."""
    urls, titles = set(), []
    for day in _index_entries[:DEDUP_DAYS]:
        for a in day.get("articles", []):
            if a.get("url"):
                urls.add(a["url"])
            titles += [x for x in (a.get("ot"), a.get("title")) if x]
    return urls, titles

def drop_duplicates(articles, min_keep=2):
    """최근 발행 기사와 URL이 같거나 제목이 비슷한 후보를 걸러낸다.
    남은 기사가 min_keep개 미만이면 중복 후보로 부족분만 채운다(발행 공백 방지)."""
    urls, titles = _seen_recent()
    if not urls and not titles:
        return articles
    kept, dups = [], []
    for art in articles:
        mine = {u for u in (art.get("link"), art.get("original_url")) if u}
        is_dup = bool(mine & urls) or any(_titles_similar(art.get("title", ""), t) for t in titles)
        (dups if is_dup else kept).append(art)
    if dups:
        print(f"♻️ 최근 {DEDUP_DAYS}일 발행 기사와 겹치는 후보 {len(dups)}개 제외: "
              + " / ".join(a.get("title", "")[:25] for a in dups[:3]))
    if len(kept) < min_keep:
        print("⚠️ 새 기사 후보가 부족해 중복 후보로 일부 채웁니다.")
        kept += dups[:min_keep - len(kept)]
    return kept

# ==========================================
# 3. 성취기준 CSV 불러오기 
# ==========================================
def fetch_standards_csv():
    # 저장소 루트에 파일이 동봉돼 있고 워크플로우가 repo를 체크아웃하므로 로컬을 먼저 읽는다.
    # 원격(GitHub Raw)은 로컬 파일이 없을 때만 쓰는 폴백이다.
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "2026list.csv")
    try:
        with open(local_path, encoding="utf-8") as f:
            local_data = f.read()
        if local_data.strip():
            print("📄 로컬 파일에서 성취기준(2026list.csv)을 불러왔습니다.")
            return local_data
    except FileNotFoundError:
        pass
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
    filtered = filtered[:MAX_STANDARDS_IN_PROMPT]  # 상한은 '줄'이 아니라 '레코드(성취기준)' 개수

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(filtered)
    return out.getvalue()

# ==========================================
# 💡 1단계: 규칙 기반 홍보성/저품질 기사 제거
# ==========================================
# 부분 일치로 거르면 '인사말'·'외교 브리핑' 같은 정상 기사까지 탈락하므로,
# 모호하게 짧은 단어는 키워드에서 빼고 머리말([속보] 등) 형태일 때만 거른다 (2026-11-26).
BLACKLIST_KEYWORDS = [
    "채용", "모집", "접수", "부고", "동정", "포토", "화보",
    "공연", "축제", "전시회", "박람회", "할인", "이벤트",
    "특가", "세일", "쿠폰", "경품", "당첨", "출시 기념",
    "인터뷰 전문", "[포토]", "[영상]",
    "뉴스 모음", "주요 뉴스", "종합뉴스", "와이드", "이슈 정리",
    "한주 정리", "주간 브리핑", "톱뉴스", "헤드라인 모음"
]
BLACKLIST_PATTERNS = [
    re.compile(r"^\s*[\[\(【<]\s*(속보|단독|인사|동정|부고|포토|영상|화보)\s*[\]\)】>]"),
    re.compile(r"(행사|판매)\s*(안내|개최|돌입|시작)"),
]

def _is_low_quality_title(title):
    return (any(k in title for k in BLACKLIST_KEYWORDS)
            or any(pt.search(title) for pt in BLACKLIST_PATTERNS))

def filter_low_quality_articles(articles, min_keep=2):
    """
    제목에 홍보성/저품질 키워드가 포함된 기사를 1차로 걸러냄.
    남은 기사가 min_keep개 미만이면 걸러진 기사 중 앞에서부터 필요한 만큼만 채운다
    (전체를 되돌리면 홍보성 기사가 그대로 통과하므로).
    """
    kept, rejected = [], []
    for art in articles:
        (rejected if _is_low_quality_title(art.get("title", "")) else kept).append(art)
    if len(kept) < min_keep:
        kept += rejected[:min_keep - len(kept)]
    return kept

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
        seen = set()
        selected = []
        for i in indices:
            # 정수가 아닌 값은 건너뛰고, 중복 인덱스는 한 번만 (같은 기사가 1면·2면에 중복 발행되는 것 방지)
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(articles) and i not in seen:
                seen.add(i)
                selected.append(articles[i])
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
    if not isinstance(data, dict) or not isinstance(data.get("body"), list):
        return data
    normalized = []
    for item in data["body"]:
        if isinstance(item, dict) and "p" in item:
            normalized.append({"p": item["p"] if isinstance(item["p"], str) else str(item["p"])})
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
    if not data or not isinstance(data, dict):
        return False
    if not data.get("title") or not data.get("lede"):
        return False
    body = data.get("body")
    if not body or not isinstance(body, list):
        return False
    valid_paragraphs = [b for b in body if _is_valid_paragraph(b)]
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

def _meets_minimum_floor(data, expected_paragraphs, check):
    """
    채택 최소 바닥 (2026-11-24 김님 결정: 교육적으로 적합해 선정된 주제는
    글자 수 때문에 버리지 않는다 — 채택 순위를 구조·사실 > 최소 바닥으로 재배열).
    - 구조: 제목·리드·유효 문단 수가 정확해야 한다 (엄격 유지)
    - 최장 문장: 기준의 1.5배 초과만 탈락 (그 수준이면 읽기 자체가 안 됨)
    - 총 분량: 하한의 50% 미만(너무 빈약) 또는 상한 초과만 탈락
    - 평균 문장 길이와 총 분량 하한은 채택 기준이 아니다 (프롬프트 목표로는 유지)
    이전의 '근소 미달 보관 후 채택'(_is_fallback_candidate)은 이 판정에 흡수됐다:
    바닥을 충족하면 보관 없이 바로 채택한다.
    """
    if not data or not isinstance(data, dict) or not data.get("title") or not data.get("lede"):
        return False
    body = data.get("body")
    if not body or not isinstance(body, list):
        return False
    valid = [b for b in body if _is_valid_paragraph(b)]
    if len(valid) != expected_paragraphs:
        return False
    total, avg_sent, max_sent = _body_metrics([b["p"] for b in valid])
    if max_sent > check["max_sent"] * 1.5:
        return False
    if total < int(check["min_total"] * 0.5) or total > check["max_total"]:
        return False
    return True

def _is_valid_paragraph(item):
    """본문 항목 하나가 유효 문단인지 (is_valid_article의 문단 판정과 같은 기준)."""
    if not isinstance(item, dict):
        return False
    text = item.get("p")
    return isinstance(text, str) and len(text.strip()) > 5

def _validation_failure(data, expected_paragraphs, check):
    """
    is_valid_article에서 탈락한 응답의 구체 사유를 계산한다 (검증 기준 자체는 바꾸지 않는다).
    반환: (사유 문장 목록, 문단 수 부족뿐인지 여부, 다음 시도 프롬프트용 피드백 한 줄)

    사유는 (a) 유효 문단 수 부족 — 몇 번째 문단이 비었는지/짧은지 포함,
    (b) 문장 길이 bound 위반(최장·평균), (c) 총 분량 범위 이탈(실제 총 글자수 병기)로 나눈다.
    단, (c)의 하한 미달은 문단이 빠져서 생긴 것일 수 있어, 있는 문단들의 밀도로 환산한
    예상 총량(총량 × 목표 문단 수 ÷ 유효 문단 수)이 하한 이상이면 독립 사유로 세지 않는다
    — 그 경우 탈락의 실질 원인은 문단 부족뿐이다.
    (2026-11-21 실행 #128: 5~6학년판 네 번째 문단이 빈 문자열이라 유효 문단 3개로 탈락.)
    """
    ordinal = ["첫 번째", "두 번째", "세 번째", "네 번째", "다섯 번째", "여섯 번째"]
    def nth(i):
        return ordinal[i] if i < len(ordinal) else f"{i + 1}번째"

    if not data or not data.get("title") or not data.get("lede"):
        return ["제목이나 리드가 비어 있음"], False, (
            "이전 응답의 문제: 제목이나 리드가 비어 있었습니다. 제목과 리드를 반드시 채우세요.")
    body = data.get("body")
    if not body or not isinstance(body, list):
        return ["본문(body)이 없거나 목록이 아님"], False, (
            "이전 응답의 문제: 본문이 비어 있었습니다. 본문을 반드시 채우세요.")

    valid_texts = [b["p"] for b in body if _is_valid_paragraph(b)]
    reasons = []
    feedback_parts = []
    paragraph_only = False

    if expected_paragraphs is not None and len(valid_texts) != expected_paragraphs:
        problems = []
        for idx, item in enumerate(body):
            if _is_valid_paragraph(item):
                continue
            text = item.get("p") if isinstance(item, dict) else None
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{nth(idx)} 문단이 비어 있음")
            else:
                problems.append(f"{nth(idx)} 문단이 너무 짧음({len(text.strip())}자)")
        if len(body) < expected_paragraphs:
            problems.append(f"본문 항목 자체가 {len(body)}개뿐")
        total_now = sum(len(t) for t in valid_texts)
        reasons.append(
            f"유효 문단 {len(valid_texts)}개 (목표 {expected_paragraphs}개) — "
            + ("; ".join(problems) if problems else "문단 수 불일치")
            + f" (현재 총 {total_now}자)")
        if len(valid_texts) < expected_paragraphs:
            paragraph_only = True
            first_bad_idx = next(
                (idx for idx, item in enumerate(body) if not _is_valid_paragraph(item)), None)
            if first_bad_idx is not None:
                feedback_parts.append(
                    f"이전 응답의 문제: {nth(first_bad_idx)} 문단이 비어 있었습니다. "
                    f"반드시 {expected_paragraphs}개 문단을 모두 완성된 문장으로 채우세요.")
            else:
                feedback_parts.append(
                    f"이전 응답의 문제: 문단이 {len(valid_texts)}개뿐이었습니다. "
                    f"반드시 {expected_paragraphs}개 문단을 모두 완성된 문장으로 채우세요.")
        else:
            feedback_parts.append(
                f"이전 응답의 문제: 문단이 {len(valid_texts)}개로 너무 많았습니다. "
                f"반드시 {expected_paragraphs}개 문단으로 맞추세요.")

    sentence_bad = False
    total_bad = False
    if check:
        total, avg_sent, max_sent = _body_metrics(valid_texts)
        if max_sent > check["max_sent"]:
            sentence_bad = True
            reasons.append(f"문장 길이 위반: 최장 문장 {max_sent}자 (기준 {check['max_sent']}자 이하)")
            feedback_parts.append(
                f"한 문장이 너무 깁니다(최장 {max_sent}자). "
                f"한 문장을 {check['max_sent']}자 이하로 짧게 나누어 쓰세요.")
        if avg_sent > check["max_avg_sent"]:
            sentence_bad = True
            reasons.append(f"문장 길이 위반: 평균 문장 {avg_sent:.0f}자 (기준 {check['max_avg_sent']}자 이하)")
            feedback_parts.append(
                f"문장이 전반적으로 깁니다(평균 {avg_sent:.0f}자). 한 문장을 더 짧게 나누어 쓰세요.")
        if total > check["max_total"]:
            total_bad = True
            reasons.append(f"총 분량 초과: 총 {total}자 (기준 {check['min_total']}~{check['max_total']}자)")
            feedback_parts.append(
                f"본문 전체 분량이 너무 많습니다(총 {total}자). {check['max_total']}자 이하로 줄여 쓰세요.")
        elif total < check["min_total"]:
            # 문단이 빠져서 모자란 것인지, 있는 문단 자체가 얇은지 구분한다
            projected = (total * expected_paragraphs / len(valid_texts)) if (
                expected_paragraphs and valid_texts) else 0
            if not (paragraph_only and projected >= check["min_total"]):
                total_bad = True
                reasons.append(f"총 분량 부족: 총 {total}자 (기준 {check['min_total']}~{check['max_total']}자)")
                feedback_parts.append(
                    f"본문 전체 분량이 부족합니다(총 {total}자). "
                    f"각 문단을 조금 더 자세히 풀어 {check['min_total']}자 이상으로 쓰세요.")
    if sentence_bad or total_bad:
        paragraph_only = False
    feedback = " ".join(feedback_parts) if feedback_parts else (
        "이전 응답의 문제: 기사 형식 기준에 맞지 않았습니다. 작성 규칙을 다시 확인해 쓰세요.")
    return reasons, paragraph_only, feedback

# ==========================================
# 💡 숫자 사실 검증 (2026-11-26)
# ==========================================
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

def _numbers_in(text):
    """텍스트의 숫자 토큰(쉼표 제거, 2자리 이상)을 집합으로."""
    out = set()
    for m in _NUM_RE.findall(text or ""):
        n = m.replace(",", "")
        if len(n.replace(".", "")) >= 2:
            out.add(n)
    return out

def find_unsupported_numbers(data, article_data):
    """생성 기사의 숫자 중 원문(본문·요약·제목)에 없는 것을 정렬해 돌려준다.
    원문 근거가 너무 짧으면(100자 미만) 검증하지 않는다."""
    source_text = " ".join([
        article_data.get("title", ""), article_data.get("description", ""),
        article_data.get("content", "")])
    if len(source_text) < 100:
        return []
    out_text = " ".join([data.get("title", ""), data.get("lede", "")]
                        + [b["p"] for b in data.get("body", []) if _is_valid_paragraph(b)])
    return sorted(_numbers_in(out_text) - _numbers_in(source_text))

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
            "name": "언론사명",
            "url": "원문 주소"
        }},
        "grade": "{grade_value}"
    }}
    """

    # 채택은 _meets_minimum_floor(최소 바닥) 기준 — 바닥을 충족하면 바로 채택한다
    last_feedback = None  # 다음 시도 프롬프트 끝에 붙일 직전 탈락 사유 한 줄
    only_paragraph_failures = True  # 지금까지의 검증 탈락이 전부 문단 수 부족뿐인지
    attempt = 0
    # 기본은 max_retries회까지. 탈락이 문단 수 부족뿐이면 EXTENDED_MAX_ATTEMPTS회까지 늘어난다
    # (2026-11-21 실행 #128: 빈 문단으로 2회 다 탈락해 5~6학년판이 누락된 사례).
    while attempt < max_retries or (only_paragraph_failures and attempt < EXTENDED_MAX_ATTEMPTS):
        attempt += 1
        cap = EXTENDED_MAX_ATTEMPTS if attempt > max_retries else max_retries
        if attempt > max_retries:
            # 확장 시도는 호출 상한 여유가 있을 때만 한다. 상한에 닿으면
            # 이 판만 건너뛰고 실행 전체는 계속한다 (쿼터 중단과 다름).
            if _gemini_call_count >= MAX_GEMINI_CALLS:
                print(f"  ⚠️ 호출 상한({MAX_GEMINI_CALLS}회)에 도달해 추가 시도를 멈춥니다.")
                break
            print(f"  ↪ 문단 수 부족만으로 탈락해 시도를 계속합니다 (시도 {attempt}/{EXTENDED_MAX_ATTEMPTS}).")
        # 2회차부터는 직전 탈락 사유를 프롬프트 끝에 붙여 같은 실수를 피하게 한다
        attempt_prompt = prompt if last_feedback is None else (
            prompt + f"\n\n[재작성 시 반드시 고칠 점]\n{last_feedback}")
        response = None
        truncated = False
        try:
            response = call_gemini(
                attempt_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=8192  # 응답이 중간에 잘려 본문이 비는 것을 방지 (thinking 토큰 포함)
                )
            )
            try:
                truncated = "MAX_TOKENS" in str(response.candidates[0].finish_reason)
            except Exception:
                truncated = False
            data = json.loads(response.text)
            data = normalize_article_body(data)
            # 출처는 모델 응답을 믿지 않고 원본 데이터로 직접 주입한다 (URL 변형·JSON 깨짐 방지)
            data["source"] = {
                "name": article_data.get('source_name') or '구글 뉴스',
                "url": article_data.get('original_url') or article_data['link'],
                "title": article_data.get('title', ''),  # 중복 기사 방지용 원문 제목
            }

            if _meets_minimum_floor(data, target_count, spec["check"]):
                unsupported = find_unsupported_numbers(data, article_data)
                if unsupported:
                    print(f"  🔢 원문에 없는 숫자 감지 ({grade_text}, 시도 {attempt}/{cap}): {unsupported[:8]}")
                    if attempt < max_retries:
                        only_paragraph_failures = False
                        last_feedback = ("이전 응답의 문제: 원문에 없는 숫자("
                                         + ", ".join(unsupported[:5])
                                         + ")가 있었습니다. 원문에 적힌 숫자만 쓰고, 없는 숫자는 빼세요.")
                        continue
                    if len(unsupported) >= 2:
                        print("  ⛔ 사실 검증 실패(원문에 없는 숫자 2개 이상) — 이 판은 건너뜁니다.")
                        break
                if not is_valid_article(data, expected_paragraphs=target_count, level_check=spec["check"]):
                    strict_reasons, _, _ = _validation_failure(data, target_count, spec["check"])
                    print(f"  📥 엄격 기준에는 미달이지만 최소 바닥을 충족해 채택합니다: {'; '.join(strict_reasons)}")
                return data

            reasons, paragraph_only, feedback = _validation_failure(
                data, target_count, spec["check"])
            if not paragraph_only:
                only_paragraph_failures = False
            will_retry = attempt < max_retries or (
                only_paragraph_failures and attempt < EXTENDED_MAX_ATTEMPTS)
            print(f"  ⚠️ 검증 탈락 (시도 {attempt}/{cap}): {'; '.join(reasons)}"
                  + (" - 재시도합니다." if will_retry else ""))
            print(f"  🔎 [디버그] 실제 응답 데이터: {json.dumps(data, ensure_ascii=False)[:500]}")
            last_feedback = feedback

        except QuotaExceededError:
            # 확장 시도 중 호출 상한에 닿은 경우만 이 판을 건너뛰고,
            # 그 외(기본 시도 구간·모델 소진)는 기존대로 실행 전체를 중단한다.
            if attempt > max_retries and _gemini_call_count >= MAX_GEMINI_CALLS:
                print(f"  ⚠️ 호출 상한({MAX_GEMINI_CALLS}회)에 도달해 이 기사({grade_text})는 건너뜁니다.")
                break
            raise  # 쿼터 초과 시 재시도로 호출을 더 태우지 않고 즉시 중단
        except Exception as e:
            only_paragraph_failures = False
            if truncated:
                last_feedback = ("이전 응답의 문제: 응답이 너무 길어 중간에 잘렸습니다. "
                                 "각 문단과 낱말 풀이를 더 간결하게 써서 JSON을 끝까지 완성하세요.")
            else:
                last_feedback = ("이전 응답의 문제: JSON 형식이 깨져 읽을 수 없었습니다. "
                                 "지정된 JSON 형식 그대로만 답하세요.")
            print(f"  ⚠️ AI 생성 또는 JSON 파싱 에러 발생 ({grade_text}용, 시도 {attempt}/{cap}): {e}")
            try:
                finish_reason = response.candidates[0].finish_reason if response else "알 수 없음"
                print(f"  🔎 [디버그] finish_reason: {finish_reason}")
                print(f"  🔎 [디버그] 원본 응답 일부: {response.text[:500] if response and response.text else '(응답 없음)'}")
            except Exception:
                pass

    print(f"  ❌ {attempt}회 시도 후에도 최소 바닥을 충족하는 기사를 생성하지 못해 이 기사({grade_text})는 건너뜁니다.")
    return None

# ==========================================
# 5. 메인 실행 파트
# ==========================================
def main():
    init_services()
    print("📰 어린이 헤드라인 뉴스 AI 자동 생성 스크립트 시작...")
    print(f"🧠 사용 모델: {MODEL_CANDIDATES[_model_index]} "
          f"(후보 {len(MODEL_CANDIDATES)}개, 쿼터·모델 오류 시 순서대로 전환)")

    kst = pytz.timezone('Asia/Seoul')
    today_str = datetime.datetime.now(kst).strftime("%Y-%m-%d")

    # 💡 무료 한도 보호 + 중복 발행 방지: 오늘 문서가 이미 있으면
    # Gemini를 한 번도 호출하지 않고 종료한다. 워크플로우의 '확인 실행'이
    # 이 동작을 이용해, 첫 실행이 지연/실패한 날에만 대신 발행한다.
    # 수동 강제 재발행이 필요하면 FORCE_RUN=1 환경변수를 준다.
    ensure_index()

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
        # 정상 종료(0)하면 실행이 '성공'으로 보여 실패를 알아채지 못하므로 비정상 종료한다
        # (워크플로우가 재시도하고, 끝내 실패하면 [발행 실패] 이슈를 만든다)
        sys.exit(1)
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
        sys.exit(1)

    print(f"📋 후보 기사 {len(raw_candidates)}개 수집 완료. 홍보성/저품질 기사 필터링 중...")
    filtered_candidates = filter_low_quality_articles(raw_candidates)
    print(f"✅ 필터링 후 {len(filtered_candidates)}개 후보 남음.")

    filtered_candidates = drop_duplicates(filtered_candidates, min_keep=4)

    print("🌐 원문 본문 수집 중... (일반 HTTP라 API 비용 없음)")
    filtered_candidates, fulltext_count = enrich_articles_with_fulltext(filtered_candidates)
    filtered_candidates = drop_duplicates(filtered_candidates, min_keep=2)  # 원문 URL 기준 재확인
    print(f"✅ 원문 본문 확보: {fulltext_count}개 / 시도 {min(len(filtered_candidates), MAX_FULLTEXT_ATTEMPTS)}개")

    print("🧠 AI가 초등학생 교육 가치 기준으로 최종 기사를 선정 중...")
    raw_articles = select_best_articles_for_kids(filtered_candidates, theme_name, pick_count=2)

    if not raw_articles:
        print("⚠️ 선정된 기사가 없습니다.")
        sys.exit(1)
        
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
        update_index(today_str, theme_name, final_articles)
        print("🎉 업로드 완료! React 앱을 새로고침하여 확인해 보세요.")
    else:
        print("⚠️ 업로드할 기사가 생성되지 않았습니다.")
        # 성공으로 끝내면 [발행 실패] 이슈 자동 생성이 동작하지 않아 조용한 실패가 되므로 비정상 종료한다
        sys.exit(1)

if __name__ == "__main__":
    main()