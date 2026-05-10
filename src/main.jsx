import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AdminPanel from "./AdminPanel.jsx";
import App from "./App.jsx";
import "./index.css";

// Signal readiness to Telegram before React mounts so initData/session are available sooner.
(() => {
  const tg = window.Telegram?.WebApp;
  if (!tg) return;
  try {
    tg.ready?.();
  } catch {
    /* ignore */
  }
  try {
    tg.expand?.();
  } catch {
    /* ignore */
  }
})();

const Root = window.location.pathname.startsWith("/admin") ? AdminPanel : App;

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
