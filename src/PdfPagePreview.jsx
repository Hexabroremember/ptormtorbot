import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

/**
 * Renders page 1 of a PDF from a blob: URL in-page (canvas).
 *
 * Mobile WebKit / Telegram WebViews often distort PDF.js output when canvas CSS
 * width/height fight `max-w-full` (aspect ratio breaks → stretched text / broken spacing).
 * Touch devices also hit Safari bugs combining devicePixelRatio scaling with pdf.js.
 *
 * Strategy: match CSS aspect-ratio to the pdf viewport, use width:100% + maxWidth,
 * and skip HiDPI backing-store scale on coarse pointers / narrow screens.
 */
export function PdfPagePreview({ pdfBlobUrl, ariaLabel, labels }) {
  const wrapRef = useRef(null);
  const canvasRef = useRef(null);
  const pdfRef = useRef(null);
  const paintGen = useRef(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!pdfBlobUrl) return undefined;
    let cancelled = false;
    let resizeTimer = 0;

    const paintPage = async () => {
      const pdf = pdfRef.current;
      const wrap = wrapRef.current;
      const canvas = canvasRef.current;
      if (!pdf || !wrap || !canvas || cancelled) return;

      const gen = ++paintGen.current;

      try {
        const page = await pdf.getPage(1);
        const baseViewport = page.getViewport({ scale: 1 });

        const rect = wrap.getBoundingClientRect();
        const cw = Math.max(160, Math.floor(rect.width) - 4);
        const scale = Math.min(cw / baseViewport.width, 2.8);
        const viewport = page.getViewport({ scale });

        const coarse =
          typeof window !== "undefined" &&
          Boolean(window.matchMedia?.("(pointer: coarse)")?.matches);
        const narrow =
          typeof window !== "undefined" &&
          Boolean(window.matchMedia?.("(max-width: 768px)")?.matches);
        const useHiDpi = !coarse && !narrow;
        const dpr = useHiDpi ? Math.min(window.devicePixelRatio || 1, 2) : 1;

        const bw = Math.max(1, Math.floor(viewport.width * dpr));
        const bh = Math.max(1, Math.floor(viewport.height * dpr));

        if (gen !== paintGen.current) return;

        canvas.width = bw;
        canvas.height = bh;

        const ctx = canvas.getContext("2d", { alpha: false });
        if (!ctx) throw new Error("Canvas rendering unsupported");

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, bw, bh);
        ctx.scale(dpr, dpr);

        await page.render({
          canvasContext: ctx,
          viewport,
        }).promise;

        if (gen !== paintGen.current) return;

        canvas.style.width = "100%";
        canvas.style.height = "auto";
        canvas.style.maxWidth = `${viewport.width}px`;
        canvas.style.aspectRatio = `${viewport.width} / ${viewport.height}`;
        canvas.style.margin = "0 auto";
        canvas.style.display = "block";

        setError(null);
      } catch (e) {
        if (!cancelled && gen === paintGen.current) {
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled && gen === paintGen.current) {
          setLoading(false);
        }
      }
    };

    const schedulePaint = () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        requestAnimationFrame(() => paintPage());
      }, 120);
    };

    const loadPdf = async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
        const task = pdfjs.getDocument({
          url: pdfBlobUrl,
          withCredentials: false,
        });
        const pdf = await task.promise;
        if (cancelled) {
          await pdf.destroy?.().catch(() => {});
          return;
        }
        pdfRef.current = pdf;
        await paintPage();
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          setLoading(false);
        }
      }
    };

    setLoading(true);
    setError(null);
    loadPdf();

    const wrap = wrapRef.current;
    let ro;
    if (wrap && typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => {
        if (!pdfRef.current || cancelled) return;
        schedulePaint();
      });
      ro.observe(wrap);
    }

    return () => {
      cancelled = true;
      window.clearTimeout(resizeTimer);
      paintGen.current += 1;
      ro?.disconnect();
      pdfRef.current?.destroy?.().catch(() => {});
      pdfRef.current = null;
    };
  }, [pdfBlobUrl]);

  return (
    <div ref={wrapRef} className="relative w-full overflow-x-auto rounded-lg bg-white">
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
        className={loading ? "hidden" : ""}
        aria-label={ariaLabel}
      />
    </div>
  );
}
