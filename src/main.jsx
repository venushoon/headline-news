import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css' // 🌟 이 줄이 반드시 있어야 디자인이 입혀집니다!
import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)