import { initializeApp, getApps } from 'firebase/app';
import { getFirestore } from 'firebase/firestore';

// Firebase 초기화의 유일한 출처 — App.jsx는 여기서 db와 initError를 가져다 쓴다.
// (예전에는 App.jsx 안에 초기화가 또 따로 있어 설정이 어긋날 수 있었다)
let db = null;
let initError = null;

try {
  const firebaseConfig = {
    apiKey: import.meta.env.VITE_FIREBASE_API_KEY || "",
    authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || "",
    projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID || "headline-news-d6a13",
    storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET || "",
    messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID || "",
    appId: import.meta.env.VITE_FIREBASE_APP_ID || ""
  };

  if (!firebaseConfig.apiKey || firebaseConfig.apiKey === "YOUR_API_KEY") {
    throw new Error("Firebase 환경변수(VITE_FIREBASE_API_KEY)가 설정되지 않았습니다. .env 파일을 확인해주세요.");
  }

  const app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApps()[0];
  // 파이썬과 동일하게 'headline-news-d6a13' 창고를 콕 집어서 연결합니다.
  db = getFirestore(app, "headline-news-d6a13");
} catch (e) {
  initError = e.message;
}

export { db, initError };
