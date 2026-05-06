import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
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
    },
  },
});
