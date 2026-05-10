/**
 * Telegram Mini App auth context: initData (inside Telegram) and/or tg_user_sess (bot-signed URL).
 * Used by main.jsx before React mounts so session from query is stored immediately.
 */

export function telegramInitData() {
  return window.Telegram?.WebApp?.initData || "";
}

/** True when Telegram's WebApp script bridged (initData may still be empty briefly). */
export function isTelegramWebAppShell() {
  return Boolean(window.Telegram?.WebApp);
}

/** Best-effort: expand viewport and signal readiness — helps some clients (notably iOS) populate initData sooner. */
export function primeTelegramWebAppForInitData() {
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
}

export function persistTelegramUserSession(sess) {
  const v = (sess || "").trim();
  if (!v) return;
  try {
    sessionStorage.setItem("telegramUserSession", v);
    localStorage.setItem("telegramUserSession", v);
  } catch {
    /* ignore */
  }
}

/** Persist bot-signed user session from the Mini App URL; survives payment browser hops. */
export function captureTelegramUserSessionFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const sess = params.get("tg_user_sess");
    if (!sess) return;
    persistTelegramUserSession(sess);
    params.delete("tg_user_sess");
    const qs = params.toString();
    const clean = `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`;
    window.history.replaceState({}, "", clean);
  } catch {
    /* ignore */
  }
}

export function storedTelegramUserSession() {
  try {
    return sessionStorage.getItem("telegramUserSession") || localStorage.getItem("telegramUserSession") || "";
  } catch {
    return "";
  }
}

/** @returns {boolean} Whether we have initData or a stored bot session for API calls. */
export function hasTelegramAuthContext() {
  return Boolean(telegramInitData().trim()) || Boolean(storedTelegramUserSession().trim());
}

/** Max wait for initData before POST /api/mini-app/session when no tg_user_sess yet (cold start). */
const MINI_APP_SESSION_INIT_WAIT_MS = 900;
const MINI_APP_SESSION_INIT_WAIT_IN_TG_MS = 6_000;

function miniAppSessionInitWaitMs() {
  if (storedTelegramUserSession().trim()) return 0;
  return isTelegramWebAppShell() ? MINI_APP_SESSION_INIT_WAIT_IN_TG_MS : MINI_APP_SESSION_INIT_WAIT_MS;
}

/**
 * @param {{ maxInitDataWaitMs?: number }} [opts]
 */
export async function bootstrapMiniAppSession(opts = {}) {
  captureTelegramUserSessionFromUrl();
  primeTelegramWebAppForInitData();
  let initData = telegramInitData();
  if (!initData && !storedTelegramUserSession().trim()) {
    const cap = typeof opts.maxInitDataWaitMs === "number" ? opts.maxInitDataWaitMs : undefined;
    const defaultMs = miniAppSessionInitWaitMs();
    const ms = cap !== undefined ? Math.min(cap, defaultMs) : defaultMs;
    initData = await waitForTelegramInitData(ms);
  }
  const base = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
  const path = "/api/mini-app/session";
  const url = base ? `${base}${path}` : path;
  const postSession = async () => {
    const id = telegramInitData() || initData || "";
    const fetchUrl = appendTelegramContextQuery(url, id);
    const sess = storedTelegramUserSession();
    return fetch(fetchUrl, {
      method: "POST",
      headers: jsonHeaders({}, { initData: id, userSession: sess }),
      body: JSON.stringify({
        telegram_init_data: id,
        telegram_user_session: sess,
      }),
    });
  };
  try {
    let res = await postSession();
    if (!res.ok && res.status === 401 && isTelegramWebAppShell() && !storedTelegramUserSession().trim()) {
      await waitForTelegramInitData(2_500);
      initData = telegramInitData() || initData;
      res = await postSession();
    }
    if (!res.ok) return;
    const data = await res.json();
    const tok = typeof data?.tg_user_sess === "string" ? data.tg_user_sess.trim() : "";
    if (tok) persistTelegramUserSession(tok);
  } catch {
    /* ignore */
  }
}

const TELEGRAM_INIT_FALLBACK_MS = 900;

export function waitForTelegramInitData(maxMs = TELEGRAM_INIT_FALLBACK_MS, intervalMs = 16) {
  return new Promise((resolve) => {
    const tg = window.Telegram?.WebApp;
    if (tg?.expand && !telegramInitData()) {
      try {
        tg.expand();
      } catch {
        /* ignore */
      }
    }
    const start = Date.now();
    let done = false;
    let intervalId = 0;
    const finish = (value) => {
      if (done) return;
      done = true;
      window.clearInterval(intervalId);
      try {
        tg?.offEvent?.("viewport_changed", check);
      } catch {
        /* ignore */
      }
      try {
        tg?.offEvent?.("theme_changed", check);
      } catch {
        /* ignore */
      }
      resolve(value);
    };
    const check = () => {
      const d = telegramInitData();
      if (d) {
        finish(d);
        return;
      }
      if (Date.now() - start >= maxMs) {
        finish("");
      }
    };
    intervalId = window.setInterval(check, intervalMs);
    check();
    tg?.onEvent?.("viewport_changed", check);
    tg?.onEvent?.("theme_changed", check);
  });
}

export function appendTelegramContextQuery(url, initData) {
  const params = [];
  if (initData && !url.includes("tg_init_data=")) {
    params.push(`tg_init_data=${encodeURIComponent(initData)}`);
  }
  const sess = storedTelegramUserSession();
  if (sess && !url.includes("tg_user_sess=")) {
    params.push(`tg_user_sess=${encodeURIComponent(sess)}`);
  }
  if (!params.length) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}${params.join("&")}`;
}

/**
 * @param {Record<string, string>} [extra]
 * @param {{ initData?: string; userSession?: string }} [ctx]
 */
export function jsonHeaders(extra = {}, ctx = {}) {
  const initData = ctx.initData !== undefined ? ctx.initData : telegramInitData();
  const userSession = ctx.userSession !== undefined ? ctx.userSession : storedTelegramUserSession();
  return {
    "Content-Type": "application/json",
    ...(initData ? { "X-Telegram-Init-Data": initData } : {}),
    ...(initData ? { Authorization: `TMA ${initData}` } : {}),
    ...(userSession ? { "X-Telegram-User-Sess": userSession } : {}),
    ...extra,
  };
}

/** Open a t.me link from inside the Mini App when possible. */
export function openTelegramDeepLink(url) {
  const tg = window.Telegram?.WebApp;
  if (typeof tg?.openTelegramLink === "function") {
    try {
      tg.openTelegramLink(url);
      return;
    } catch {
      /* fall through */
    }
  }
  window.open(url, "_blank", "noopener,noreferrer");
}
