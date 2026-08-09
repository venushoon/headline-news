import React, { useState, useEffect } from 'react';
import { initializeApp, getApps } from 'firebase/app';
// 💡 서버 사이드 최적화를 위한 query, orderBy, limit 다시 추가
import { getFirestore, collection, getDocs, doc, getDoc, query, orderBy, limit } from 'firebase/firestore';

// 안전한 Firebase 초기화 및 네임드 데이터베이스 연결
let app = null;
let db = null;
let initError = null;

try {
  const firebaseConfig = {
    apiKey: import.meta.env.VITE_FIREBASE_API_KEY || "",
    authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || "",
    projectId: "headline-news-d6a13",
    storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET || "",
    messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || "",
    appId: import.meta.env.VITE_FIREBASE_APP_ID || ""
  };

  if (!firebaseConfig.apiKey || firebaseConfig.apiKey === "YOUR_API_KEY") {
    throw new Error("Firebase 환경변수(VITE_FIREBASE_API_KEY)가 설정되지 않았습니다. .env 파일을 확인해주세요.");
  }

  app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];
  db = getFirestore(app, 'headline-news-d6a13');
} catch (e) {
  initError = e.message;
}

// 로컬 날짜 파싱 헬퍼 함수
const parseLocalDate = (dateStr) => {
  if (!dateStr) return new Date();
  const [y, m, d] = dateStr.split('-').map(Number);
  return new Date(y, m - 1, d);
};

const formatDateStr = (d) => {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
};

// 현재 날짜 가져오기
const getTodayString = () => {
  return formatDateStr(new Date());
};

export default function App() {
  const [articles, setArticles] = useState([]);
  const [selectedGrade, setSelectedGrade] = useState('3-4');
  const [currentDate, setCurrentDate] = useState('');
  const [viewMode, setViewMode] = useState('today');
  const [archiveList, setArchiveList] = useState([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState('');
  const [fontSize, setFontSize] = useState(16);
  const [searchTerm, setSearchTerm] = useState('');

  // 현재 보고 있는 기사의 인덱스 (1면, 2면)
  const [currentArticleIndex, setCurrentArticleIndex] = useState(0);

  // 국어사전 모달 제어 상태
  const [isDictModalOpen, setIsDictModalOpen] = useState(false);
  const [dictQuery, setDictQuery] = useState('');

  useEffect(() => {
    if (initError) return;
    const dateStr = getTodayString();
    setCurrentDate(dateStr);
    fetchBriefing(dateStr);
  }, []);

  // 날짜, 학년, 검색어가 바뀌면 항상 첫 번째(1면) 기사로 리셋
  useEffect(() => {
    setCurrentArticleIndex(0);
  }, [currentDate, selectedGrade, searchTerm]);

  const fetchBriefing = async (dateStr) => {
    if (!db) return;
    setLoading(true);
    setFetchError('');
    try {
      const docRef = doc(db, 'daily_briefings', dateStr);
      const docSnap = await getDoc(docRef);
      if (docSnap.exists()) {
        setArticles(docSnap.data().articles || []);
      } else {
        setArticles([]);
      }
    } catch (error) {
      console.error("데이터 로드 실패:", error);
      setFetchError(error.message || "데이터를 불러오지 못했습니다.");
      setArticles([]);
    }
    setLoading(false);
  };

  const fetchArchiveList = async () => {
    if (!db) return;
    setLoading(true);
    setFetchError('');
    try {
      // 💡 파이어베이스 읽기 비용(요금) 폭탄 방지를 위한 서버 사이드 쿼리 최적화
      const q = query(
        collection(db, 'daily_briefings'),
        orderBy('__name__', 'desc'), // 문서 ID(날짜) 기준으로 최신순 정렬
        limit(30)                    // 무조건 최신 30개만 다운로드하여 읽기 비용 제한
      );

      const querySnapshot = await getDocs(q);
      const list = [];
      querySnapshot.forEach((docSnap) => {
        list.push({ date: docSnap.id, articles: docSnap.data().articles });
      });

      setArchiveList(list);
      setViewMode('archive');
    } catch (error) {
      console.error("아카이브 로드 실패:", error);
      setFetchError(error.message || "아카이브를 불러오지 못했습니다.");
    }
    setLoading(false);
  };

  const handleDateChange = (days) => {
    if (!currentDate) return;
    const d = parseLocalDate(currentDate);
    d.setDate(d.getDate() + days);
    const newDateStr = formatDateStr(d);

    setCurrentDate(newDateStr);
    fetchBriefing(newDateStr);
  };

  const getDayOfWeek = (dateStr) => {
    if (!dateStr) return '';
    const days = ['일요일', '월요일', '화요일', '수요일', '목요일', '금요일', '토요일'];
    return days[parseLocalDate(dateStr).getDay()];
  };

  const getPrintDateString = (dateStr) => {
    if (!dateStr) return '';
    const d = parseLocalDate(dateStr);
    return `${d.getFullYear()}. ${d.getMonth() + 1}. ${d.getDate()}. ${getDayOfWeek(dateStr)}`;
  };

  const filteredArticles = articles.filter(art => {
    const matchesGrade = art.grade === selectedGrade;
    const queryText = searchTerm.toLowerCase();
    const matchesSearch = searchTerm === '' ||
      (art.title && art.title.toLowerCase().includes(queryText)) ||
      (art.lede && art.lede.toLowerCase().includes(queryText)) ||
      (art.body && art.body.some(b => b.p && b.p.toLowerCase().includes(queryText)));
    return matchesGrade && matchesSearch;
  });

  const handleDictSearch = (e) => {
    e.preventDefault();
    if (dictQuery.trim() === '') return;
    window.open(`https://ko.dict.naver.com/#/search?query=${encodeURIComponent(dictQuery)}`, '_blank');
    setIsDictModalOpen(false);
    setDictQuery('');
  };

  if (initError) {
    return (
      <div className="min-h-screen bg-red-50 flex items-center justify-center p-6 font-sans">
        <div className="bg-white p-8 rounded-lg shadow-md max-w-lg border border-red-200 text-center space-y-4">
          <h2 className="text-xl font-bold text-red-600">⚠️ 환경변수 설정 필요</h2>
          <p className="text-gray-700 text-sm leading-relaxed">{initError}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#f7f5f0] text-gray-900 p-4 md:p-8 relative">
      <style>{`
        .news-typography h2, .news-typography blockquote, .news-typography p {
          font-family: 'Nanum Myeongjo', 'Batang', serif;
          word-break: keep-all; 
        }
        .news-typography p {
          line-height: 1.8;
          text-align: justify; 
          letter-spacing: -0.03em; 
          color: #111;
        }

        .show-on-print { display: none; }
        .print-only-blank { display: none; }
        .think-with-answer {
          break-inside: avoid;
          page-break-inside: avoid;
        }

        @media print {
          @page { margin: 12mm; }
          body { background-color: white !important; }
          .hide-on-print { display: none !important; }
          .show-on-print { display: flex !important; }

          .max-w-4xl { max-width: 100% !important; border: none !important; box-shadow: none !important; padding: 0 !important; }

          /* 💡 인쇄 시에만 낱말 도우미/생각해보기 2단 구성을 해제해 생각해보기가 전체 폭을 사용하도록 함 */
          .grid-cols-1.md\\:grid-cols-2 { display: block !important; }

          /* 💡 한 페이지 안에 들어가도록 간격/줄간격 압축 */
          .news-typography p { line-height: 1.5 !important; margin-bottom: 4px; }
          .space-y-6 > * + * { margin-top: 12px !important; }
          .space-y-4 > * + * { margin-top: 8px !important; }
          .mt-8 { margin-top: 12px !important; }
          .pt-6 { padding-top: 8px !important; }
          blockquote { padding: 6px 12px !important; margin: 4px 0 !important; }

          /* 💡 생각해보기 박스 + 작성란을 한 덩어리로 묶어 페이지 분리 방지 */
          .think-with-answer {
            break-inside: avoid;
            page-break-inside: avoid;
          }

          .print-only-blank {
            display: block !important;
            height: 120px;
            border: 1px solid #333;
            margin-top: 8px;
            page-break-inside: avoid;
          }
          .print-only-blank::before {
            content: "생각해 보기 작성란";
            display: block;
            padding: 4px 10px;
            background: #eee;
            font-size: 10px;
            font-weight: bold;
            color: #333;
            border-bottom: 1px solid #333;
          }
        }
      `}</style>

      {/* 국어사전 검색 모달 (웹 전용) */}
      {isDictModalOpen && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 hide-on-print font-sans">
          <div className="bg-white p-6 rounded-lg shadow-xl w-full max-w-sm mx-4 transform transition-all">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-bold flex items-center gap-2">📖 단어 뜻 찾기</h3>
              <button 
                onClick={() => setIsDictModalOpen(false)}
                className="text-gray-500 hover:text-gray-800 font-bold text-xl"
              >
                &times;
              </button>
            </div>
            <p className="text-sm text-gray-600 mb-4">
              기사를 읽다가 모르는 단어가 있나요?<br/>네이버 국어사전에서 바로 찾아보세요!
            </p>
            <form onSubmit={handleDictSearch} className="flex gap-2">
              <input 
                type="text" 
                value={dictQuery}
                onChange={(e) => setDictQuery(e.target.value)}
                placeholder="모르는 단어 입력..."
                className="flex-1 border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:border-gray-800"
                autoFocus
              />
              <button 
                type="submit"
                className="bg-[#03c75a] text-white px-4 py-2 rounded text-sm font-bold hover:bg-[#02b350] transition"
              >
                검색
              </button>
            </form>
          </div>
        </div>
      )}

      <div className="max-w-4xl mx-auto bg-white p-6 md:p-12 shadow-sm border border-gray-300">

        {/* 웹 화면 전용 헤더 */}
        <header className="border-b-2 border-gray-800 pb-6 mb-8 hide-on-print">
          <div className="flex justify-between items-center text-xs md:text-sm text-gray-600 mb-4 font-sans">
            <div>
              <p className="font-bold">제 {currentDate ? currentDate.replace(/-/g, '') : ''} 호</p>
              <p>매일 아침 발행 · 주간</p>
            </div>

            <div className="text-right">
              <select 
                value={selectedGrade} 
                onChange={(e) => setSelectedGrade(e.target.value)}
                className="border border-gray-300 rounded px-2 py-1 text-xs bg-white focus:outline-none font-sans"
              >
                <option value="3-4">3~4학년군 · 통합</option>
                <option value="5-6">5~6학년군 · 사회 중심</option>
              </select>
            </div>
          </div>

          <div className="text-center my-6">
            <h1 className="text-4xl md:text-6xl font-extrabold tracking-tight" style={{ fontFamily: "'Nanum Myeongjo', 'Batang', serif", whiteSpace: 'nowrap' }}>
              어린이 헤드라인 뉴스
            </h1>
            <p className="text-xs md:text-sm text-gray-500 mt-2 font-sans">
              문해력과 시민 의식을 키우는 오늘의 뉴스 읽기
            </p>
          </div>

          <div className="flex flex-wrap justify-between items-center gap-3 mt-6 pt-4 border-t border-gray-200 font-sans">
            <div className="flex items-center gap-2">
              <button onClick={() => handleDateChange(-1)} className="px-2.5 py-1 bg-gray-100 border border-gray-300 rounded text-xs hover:bg-gray-200 cursor-pointer">◀ 어제</button>
              <span className="font-bold text-sm px-1">{currentDate} ({getDayOfWeek(currentDate)})</span>

              <button 
                onClick={() => {
                  if (viewMode === 'today') {
                    fetchArchiveList();
                  } else {
                    setViewMode('today');
                    fetchBriefing(currentDate);
                  }
                }}
                className="ml-1 px-3 py-1 bg-gray-800 text-white hover:bg-gray-700 rounded text-xs transition cursor-pointer"
              >
                {viewMode === 'today' ? '📚 지난 기사 모아보기' : '📰 오늘의 뉴스'}
              </button>
            </div>

            <div className="flex items-center gap-4">
              <div className="flex items-center gap-1.5 text-xs text-gray-600">
                <span>가</span>
                <input 
                  type="range" 
                  min="14" 
                  max="22" 
                  value={fontSize} 
                  onChange={(e) => setFontSize(Number(e.target.value))}
                  className="w-20 accent-gray-800 cursor-pointer"
                />
                <span>가</span>
              </div>

              <input 
                type="text" 
                placeholder="🔍 기사 검색..." 
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                className="border border-gray-300 rounded px-3 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-gray-800 w-32 md:w-40"
              />

              <button 
                onClick={() => setIsDictModalOpen(true)} 
                title="단어 뜻 찾기 (국어사전)"
                className="p-1.5 hover:bg-gray-100 rounded border border-gray-300 text-sm bg-white cursor-pointer"
              >
                📖
              </button>

              <button 
                onClick={() => window.print()} 
                title="기사 인쇄 및 PDF 저장"
                className="p-1.5 hover:bg-gray-100 rounded border border-gray-300 text-sm bg-white cursor-pointer"
              >
                🖨️
              </button>
            </div>
          </div>
        </header>

        {/* 학습지 형태의 인쇄 전용 헤더 */}
        <div className="show-on-print justify-between items-end border-b-[3px] border-black pb-2 mb-6 font-sans">
          <div className="font-bold text-[15px]">
            어린이 헤드라인 뉴스 읽기 · {getPrintDateString(currentDate)}
          </div>
          <div className="text-[14px]">
            ____학년 ____반 이름: __________________
          </div>
        </div>

        {/* 본문 영역 */}
        <main>
          {loading ? (
            <div className="text-center py-20 text-gray-500 font-sans hide-on-print">뉴스를 불러오는 중입니다...</div>
          ) : fetchError ? (
            <div className="text-center py-20 text-red-500 font-sans hide-on-print">
              ⚠️ 데이터를 불러오는 중 오류가 발생했습니다: {fetchError}
            </div>
          ) : viewMode === 'archive' ? (
            <div className="hide-on-print">
              <h2 className="text-2xl font-bold mb-6 border-b pb-2 font-sans">📚 지난 기사 아카이브</h2>
              {archiveList.length === 0 ? (
                <p className="text-gray-500 font-sans">저장된 기사가 없습니다.</p>
              ) : (
                <div className="space-y-4 font-sans">
                  {archiveList.map((item) => (
                    <div key={item.date} className="p-4 border border-gray-200 rounded hover:bg-gray-50 flex justify-between items-center">
                      <div>
                        <span className="font-bold text-lg">{item.date} ({getDayOfWeek(item.date)})</span>
                        <p className="text-sm text-gray-600 mt-1">등록된 기사 수: {item.articles.length}개</p>
                      </div>
                      <button 
                        onClick={() => {
                          setCurrentDate(item.date);
                          setArticles(item.articles);
                          setViewMode('today');
                        }}
                        className="px-3 py-1.5 bg-gray-800 text-white text-xs rounded hover:bg-gray-700 cursor-pointer"
                      >
                        보기
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : filteredArticles.length === 0 ? (
            <div className="text-center py-20 text-gray-500 font-sans hide-on-print">
              선택하신 학년군({selectedGrade === '3-4' ? '3~4학년군' : '5~6학년군'})에 해당하는 기사가 없습니다. 상단의 학년 선택을 변경해 보세요.
            </div>
          ) : (
            <>
              {/* 기사 탭 네비게이션 (인쇄 시 숨김) */}
              <div className="flex space-x-2 mb-6 border-b-2 border-gray-300 pb-0 hide-on-print font-sans">
                {filteredArticles.map((art, idx) => (
                  <button
                    key={idx}
                    onClick={() => setCurrentArticleIndex(idx)}
                    className={`px-5 py-2 text-sm font-bold rounded-t-lg transition-all ${
                      currentArticleIndex === idx
                        ? 'bg-gray-800 text-white transform translate-y-[2px]'
                        : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                    }`}
                  >
                    {idx + 1}면 기사
                  </button>
                ))}
              </div>

              {/* 단일 기사 렌더링 영역 */}
              {(() => {
                const article = filteredArticles[currentArticleIndex];
                return (
                  <article key={article.title} className="space-y-6 news-typography">

                    {article.standards && article.standards.length > 0 && (
                      <div className="flex flex-wrap gap-2 font-sans">
                        {article.standards.map((std, sIdx) => (
                          <span key={sIdx} className="bg-amber-50 text-amber-800 border border-amber-200 text-xs px-2.5 py-1 rounded font-semibold" title={std.gloss}>
                            [{std.code}] {std.gloss}
                          </span>
                        ))}
                      </div>
                    )}

                    <h2 className="text-2xl md:text-3xl font-bold leading-snug tracking-tight">
                      {article.title}
                    </h2>

                    <blockquote 
                      className="border-l-4 border-gray-800 pl-4 py-2 text-gray-700 bg-gray-50 font-sans" 
                      style={{ fontSize: `${fontSize}px` }}
                    >
                      {article.lede}
                    </blockquote>

                    <div className="space-y-4" style={{ fontSize: `${fontSize}px` }}>
                      {article.body && article.body.map((pObj, pIdx) => (
                        <p 
                          key={pIdx} 
                          className={pIdx === 0 ? "font-bold text-gray-900" : ""}
                        >
                          {pObj.p}
                        </p>
                      ))}
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mt-8 pt-6 border-t border-gray-200 font-sans">
                      {article.vocabularies && article.vocabularies.length > 0 && (
                        <div className="bg-gray-50 p-4 rounded border border-gray-200">
                          <h3 className="font-bold text-sm text-gray-800 mb-2 flex items-center gap-1.5">
                            📖 낱말 도우미
                          </h3>
                          <ul className="space-y-1.5 text-xs md:text-sm">
                            {article.vocabularies.map((v, vIdx) => (
                              <li key={vIdx}>
                                <span className="font-semibold text-gray-900">{v.word}</span> : {v.meaning}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {/* 💡 생각해보기 + 작성란을 하나의 블록으로 묶어 인쇄 시 바로 아래 붙어 나오도록 구성 */}
                      {article.think && (
                        <div className="bg-[#fcf8f5] p-4 rounded border border-[#e6dcd0] think-with-answer">
                          <h3 className="font-bold text-sm text-amber-900 mb-2 flex items-center gap-1.5">
                            🤔 생각해 보기
                          </h3>
                          <p className="text-xs md:text-sm text-gray-700 leading-relaxed">
                            {article.think}
                          </p>
                          <div className="print-only-blank"></div>
                        </div>
                      )}
                    </div>

                    <div className="text-right font-sans pt-2 space-y-1 hide-on-print">
                      {article.source && article.source.url && (
                        <div>
                          <a 
                            href={article.source.url} 
                            target="_blank" 
                            rel="noreferrer" 
                            className="text-xs text-gray-500 hover:underline"
                          >
                            원문 출처: {article.source.name || '구글 뉴스'} ↗
                          </a>
                        </div>
                      )}
                      <p className="text-xs text-gray-400 font-sans">
                        본문은 인공지능(AI)이 원문 기사를 어린이 눈높이로 다시 쓴 글입니다. 그래서 원래 기사와 다르거나 빠진 내용이 있을 수 있습니다. 정확한 내용은 꼭 원문 링크에서 확인하세요.
                      </p>
                    </div>
                  </article>
                );
              })()}

              {/* 하단 페이지 이동 버튼 (인쇄 시 숨김) */}
              <div className="flex justify-between items-center mt-10 px-2 font-sans hide-on-print border-t border-gray-200 pt-6">
                <button
                  onClick={() => setCurrentArticleIndex(prev => Math.max(0, prev - 1))}
                  disabled={currentArticleIndex === 0}
                  className={`px-4 py-2 rounded text-sm font-bold transition-all ${
                    currentArticleIndex === 0 
                      ? 'bg-gray-100 text-gray-400 cursor-not-allowed' 
                      : 'bg-white text-gray-700 border border-gray-300 hover:bg-gray-100'
                  }`}
                >
                  ◀ 이전 기사
                </button>

                <div className="text-gray-500 text-sm font-bold">
                  {currentArticleIndex + 1} / {filteredArticles.length}
                </div>

                <button
                  onClick={() => setCurrentArticleIndex(prev => Math.min(filteredArticles.length - 1, prev + 1))}
                  disabled={currentArticleIndex === filteredArticles.length - 1}
                  className={`px-4 py-2 rounded text-sm font-bold transition-all ${
                    currentArticleIndex === filteredArticles.length - 1 
                      ? 'bg-gray-100 text-gray-400 cursor-not-allowed' 
                      : 'bg-white text-gray-700 border border-gray-300 hover:bg-gray-100'
                  }`}
                >
                  다음 기사 ▶
                </button>
              </div>
            </>
          )}
        </main>

        <footer className="mt-12 pt-6 border-t border-gray-300 text-center text-xs text-gray-500 font-sans hide-on-print">
          <p>© 어린이 헤드라인 뉴스</p>
        </footer>

      </div>
    </div>
  );
}
