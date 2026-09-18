import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// Tailwind's dark-mode variant here is class-based (`.dark`), while the
// chart palette in @/lib/palette picks its hex values off the OS media
// query directly. Those two must never drift apart -- a dark-palette bar
// rendered on a light card was validated for contrast against a surface it
// is not actually sitting on. This is the one place that sets the class;
// @/lib/useColorMode is the one place components read the same signal.
const mq = window.matchMedia("(prefers-color-scheme: dark)");
const applyColorScheme = (dark: boolean) =>
  document.documentElement.classList.toggle("dark", dark);
applyColorScheme(mq.matches);
mq.addEventListener("change", (e) => applyColorScheme(e.matches));

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
