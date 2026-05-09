/**
 * Single retry on transient failures (network / 5xx / 429).
 * Does not retry most 4xx (client errors).
 * Waits `retryDelayMs` plus up to 50% random jitter before retrying.
 */
export async function fetchWithRetry(
  url,
  options = {},
  { retries = 1, retryDelayMs = 400 } = {},
) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, options);
      if (res.ok) return res;
      if (res.status >= 400 && res.status < 500 && res.status !== 429) {
        return res;
      }
      if (attempt >= retries) return res;
    } catch (e) {
      lastErr = e;
      if (attempt >= retries) throw e;
    }
    const jitter = Math.floor(Math.random() * Math.max(1, retryDelayMs * 0.5));
    await new Promise((r) => setTimeout(r, retryDelayMs + jitter));
  }
  throw lastErr ?? new Error("fetchWithRetry: exhausted");
}
