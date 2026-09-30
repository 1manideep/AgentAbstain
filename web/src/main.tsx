import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { Gallery } from './scene/humans/Gallery.tsx'

// `#/characters` renders the character gallery (screenshots / preset review) instead of the world.
const gallery = window.location.hash.startsWith('#/characters')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {gallery ? <Gallery /> : <App />}
  </StrictMode>,
)
