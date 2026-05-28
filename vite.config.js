import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [
    react(),
    {
      name: "telegram-webApp-head-first",
      transformIndexHtml(html) {
        // Ensure telegram-web-app.js is first in <head> (Telegram's requirement)
        html = html.replace(
          /<script[^>]*src="https:\/\/telegram\.org\/js\/telegram-web-app\.js"[^>]*><\/script>\s*/g,
          ""
        );
        return html.replace(
          "<head>",
          '<head>\n    <script src="https://telegram.org/js/telegram-web-app.js"></script>'
        );
      },
    },
  ],
  server: {
    port: 5173,
    strictPort: true,
    // Avoid some Windows / browser cases where "localhost" resolves oddly — use http://127.0.0.1:5173/
    host: "127.0.0.1",
    open: true,
    proxy: {
      // Run FastAPI alongside: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
      "/generate-pdf": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/redeem-payment-code": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
