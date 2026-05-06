import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

/**
 * Renders page 1 of a PDF from a blob: URL in-page (canvas).
 * Avoids iframe/window.open — Telegram Mini App and mobile WebViews often break those for blobs.
 */
export function PdfPagePreview({ pdfBlobUrl, ariaLabel, labels }) {
  const wrapRef = useRef(null);
  const canvasRef = useRef(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!pdfBlobUrl) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);

    const run = async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

        const task = pdfjs.getDocument({
          url: pdfBlobUrl,
          withCredentials: false,
        });
        const pdf = await task.promise;
        const page = await pdf.getPage(1);

        if (cancelled) {
          return;
        }

        const canvas = canvasRef.current;
        const wrap = wrapRef.current;
        if (!canvas || !wrap) {
          return;
        }

        const cssWidth = Math.max(wrap.clientWidth - 8, 200);
        const baseViewport = page.getViewport({ scale: 1 });
        const scale = Math.min(cssWidth / baseViewport.width, 2.8);
        const viewport = page.getViewport({ scale });

        const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
        const w = Math.floor(viewport.width * dpr);
        const h = Math.floor(viewport.height * dpr);
        canvas.width = w;
        canvas.height = h;
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;

        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("Canvas rendering unsupported");

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.scale(dpr, dpr);

        await page.render({
          canvasContext: ctx,
          viewport,
        }).promise;
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    run();

    return () => {
      cancelled = true;
    };
  }, [pdfBlobUrl]);

  return (
    <div ref={wrapRef} className="relative w-full overflow-hidden rounded-lg bg-white">
      {loading ? (
        <div className="flex min-h-[200px] items-center justify-center gap-2 text-slate-600">
          <Loader2 className="h-8 w-8 shrink-0 animate-spin text-blue-600" aria-hidden />
          <span className="text-sm font-medium">{labels?.loading ?? "…"}</span>
        </div>
      ) : null}
      {error ? (
        <div className="p-4 text-center text-sm text-red-600">{error}</div>
      ) : null}
      <canvas
        ref={canvasRef}
        className={`mx-auto block max-h-[70vh] max-w-full ${loading ? "hidden" : ""}`}
        aria-label={ariaLabel}
      />
    </div>
  );
}
