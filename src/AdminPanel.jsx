import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  ChevronDown,
  ChevronUp,
  Copy,
  KeyRound,
  Loader2,
  Receipt,
  RefreshCw,
  ShieldCheck,
  ToggleLeft,
  ToggleRight,
  UserCircle,
  Users,
} from "lucide-react";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

function apiUrl(path) {
  return API_BASE ? `${API_BASE}${path}` : path;
}

function telegramInitData() {
  return window.Telegram?.WebApp?.initData || "";
}

/** Persist tg_sess from bot URL — authenticates as Telegram when initData is empty */
function captureTgSessFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const sess = params.get("tg_sess");
    if (sess) {
      sessionStorage.setItem("adminTgSess", sess);
      params.delete("tg_sess");
      const qs = params.toString();
      const clean = `${window.location.pathname}${qs ? `?${qs}` : ""}${window.location.hash}`;
      window.history.replaceState({}, "", clean);
    }
  } catch {
    /* ignore */
  }
}

function storedTgSess() {
  try {
    return sessionStorage.getItem("adminTgSess") || "";
  } catch {
    return "";
  }
}

/** Telegram often fills initData slightly after load; fetching immediately yields admin_auth_required. */
function waitForTelegramInitData(maxMs = 15000, intervalMs = 40) {
  return new Promise((resolve) => {
    const start = Date.now();
    let done = false;
    let intervalId = 0;
    const finish = (value) => {
      if (done) return;
      done = true;
      window.clearInterval(intervalId);
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
    window.Telegram?.WebApp?.onEvent?.("viewport_changed", check);
  });
}

function storedAdminSecret() {
  return window.localStorage.getItem("adminApiSecret") || "";
}

function adminHeaders(secret = storedAdminSecret()) {
  const initData = telegramInitData();
  const headers = {
    "Content-Type": "application/json",
  };
  if (initData) {
    headers["X-Telegram-Init-Data"] = initData;
    /* Telegram Web Apps: some proxies strip custom headers; Authorization TMA is widely forwarded */
    headers.Authorization = `TMA ${initData}`;
  } else if (secret.trim()) {
    headers.Authorization = `Bearer ${secret.trim()}`;
  }
  return headers;
}

/** Append initData / tg_sess as query params (some clients strip headers; initData may be empty). */
function withAdminAuthQuery(path) {
  let url = apiUrl(path);
  const initData = telegramInitData();
  const tgSess = storedTgSess();
  const parts = [];
  if (initData && !url.includes("tg_init_data=")) {
    parts.push(`tg_init_data=${encodeURIComponent(initData)}`);
  }
  if (tgSess && !url.includes("tg_sess=")) {
    parts.push(`tg_sess=${encodeURIComponent(tgSess)}`);
  }
  if (!parts.length) {
    return url;
  }
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}${parts.join("&")}`;
}

async function adminFetch(path, options = {}) {
  const res = await fetch(withAdminAuthQuery(path), {
    ...options,
    headers: {
      ...adminHeaders(),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

function StatCard({ icon: Icon, label, value, tone = "blue" }) {
  const tones = {
    blue: "bg-blue-50 text-blue-700 border-blue-100",
    emerald: "bg-emerald-50 text-emerald-700 border-emerald-100",
    amber: "bg-amber-50 text-amber-700 border-amber-100",
    slate: "bg-slate-50 text-slate-700 border-slate-100",
  };
  return (
    <div className={`rounded-2xl border p-4 ${tones[tone]}`}>
      <div className="mb-3 flex items-center gap-2 text-sm font-bold">
        <Icon size={18} />
        {label}
      </div>
      <div className="text-3xl font-black">{value}</div>
    </div>
  );
}

function formatDate(value) {
  if (!value) return "-";
  try {
    return new Intl.DateTimeFormat("he-IL", {
      dateStyle: "short",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

const FORM_FIELD_LABELS = {
  hebrew_full_name: "שם בעברית",
  english_full_name: "שם באנגלית",
  id_number: "תעודת זהות",
  expiration_date: "תוקף",
  expiry_option: "בחירת תוקף / חבילה",
  telegram_user_id: "מזהה טלגרם",
  username: "משתמש",
  first_name: "שם פרטי",
};

function FormSnapshotCard({ title, data }) {
  if (!data || typeof data !== "object") return null;
  const entries = Object.entries(data).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (!entries.length) return null;
  return (
    <div className="mt-2 rounded-xl border border-slate-200 bg-slate-50 p-3 text-right">
      {title ? <div className="mb-2 text-xs font-bold text-slate-600">{title}</div> : null}
      <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {entries.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-xs text-slate-500">{FORM_FIELD_LABELS[k] || k}</dt>
            <dd className="font-mono text-sm font-semibold break-all text-slate-900 ltr">{String(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** pdf_generated: preview (watermark) = לא שולם; final = שולם (אחרי קוד תשלום) */
function pdfPaymentBadge(meta) {
  const ps = meta.payment_status;
  if (ps === "paid_final" || ps === "paid") {
    return { label: "שולם", className: "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200" };
  }
  if (ps === "preview_unpaid" || ps === "preview") {
    return { label: "לא שולם", className: "bg-amber-100 text-amber-900 ring-1 ring-amber-200" };
  }
  if (meta.watermark === true) {
    return { label: "לא שולם", className: "bg-amber-100 text-amber-900 ring-1 ring-amber-200" };
  }
  if (meta.watermark === false) {
    return { label: "שולם", className: "bg-emerald-100 text-emerald-800 ring-1 ring-emerald-200" };
  }
  return { label: "לא ידוע", className: "bg-slate-100 text-slate-600" };
}

export default function AdminPanel() {
  const [summary, setSummary] = useState(null);
  const [events, setEvents] = useState([]);
  const [codes, setCodes] = useState([]);
  const [secret, setSecret] = useState(storedAdminSecret());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [newCode, setNewCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [tgWaiting, setTgWaiting] = useState(false);
  const [hasInitData, setHasInitData] = useState(false);
  const [showAccessCode, setShowAccessCode] = useState(false);
  const [debugInfo, setDebugInfo] = useState(null);
  const [hasTgSess, setHasTgSess] = useState(false);
  const [users, setUsers] = useState([]);
  const [usersTotal, setUsersTotal] = useState(0);
  const [detailEventId, setDetailEventId] = useState(null);

  const isTelegram = Boolean(
    typeof window !== "undefined" && window.Telegram?.WebApp,
  );
  const maintenanceEnabled = Boolean(summary?.control?.maintenance_mode);

  const topTypes = useMemo(
    () => summary?.activity?.by_type?.slice(0, 5) || [],
    [summary],
  );

  const redeemStats = summary?.activity?.redeem_stats || {};
  const totalEv = summary?.activity?.total_events || 1;

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [summaryData, eventsData, codesData, usersData] = await Promise.all([
        adminFetch("/api/admin/summary"),
        adminFetch("/api/admin/events?limit=120"),
        adminFetch("/api/admin/payment-codes"),
        adminFetch("/api/admin/users?limit=200"),
      ]);
      setSummary(summaryData);
      setEvents(eventsData.items || []);
      setCodes(codesData.items || []);
      setUsers(usersData.items || []);
      setUsersTotal(usersData.total ?? 0);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;

    (async () => {
      captureTgSessFromUrl();
      setHasTgSess(Boolean(storedTgSess()));
      window.Telegram?.WebApp?.ready?.();
      window.Telegram?.WebApp?.expand?.();

      if (window.Telegram?.WebApp) {
        setTgWaiting(true);
        await waitForTelegramInitData();
        if (cancelled) return;
        setTgWaiting(false);
        setHasInitData(Boolean(telegramInitData()));
      } else {
        setHasInitData(false);
      }

      await load();
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const fetchDebug = async () => {
    try {
      const res = await fetch(apiUrl("/api/admin/debug"));
      const data = await res.json();
      setDebugInfo(data);
    } catch (e) {
      setDebugInfo({ error: String(e) });
    }
  };

  const saveSecret = () => {
    window.localStorage.setItem("adminApiSecret", secret.trim());
    load();
  };

  const issueCode = async () => {
    setBusy(true);
    setError("");
    try {
      const data = await adminFetch("/api/admin/codes/issue", { method: "POST", body: "{}" });
      setNewCode(data.code);
      await load();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleMaintenance = async () => {
    setBusy(true);
    setError("");
    try {
      await adminFetch("/api/admin/maintenance", {
        method: "POST",
        body: JSON.stringify({ enabled: !maintenanceEnabled }),
      });
      await load();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const copyText = async (text) => {
    await navigator.clipboard?.writeText(text);
  };

  return (
    <div className="min-h-screen bg-slate-950 p-4 text-slate-100 md:p-8" dir="rtl">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="flex flex-col gap-4 rounded-3xl border border-white/10 bg-white/5 p-5 shadow-2xl shadow-black/20 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2 text-sm font-bold text-blue-300">
              <ShieldCheck size={18} />
              פאנל ניהול
            </div>
            <h1 className="text-2xl font-black md:text-3xl">Analytics & Control</h1>
            <p className="mt-1 text-sm text-slate-400">
              מעקב פעילות, קודי תשלום ובקרת מצב השירות.
            </p>
          </div>
          <button
            type="button"
            onClick={load}
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-blue-600 px-4 py-2 text-sm font-bold text-white transition hover:bg-blue-500 disabled:opacity-60"
            disabled={loading}
          >
            {loading ? <Loader2 size={18} className="animate-spin" /> : <RefreshCw size={18} />}
            רענון
          </button>
        </header>

        {tgWaiting ? (
          <section className="rounded-2xl border border-blue-400/30 bg-blue-500/10 p-4 text-sm text-blue-100">
            <div className="flex items-center gap-2 font-bold">
              <Loader2 size={18} className="animate-spin" />
              ממתינים לטלגרם (initData)…
            </div>
            <p className="mt-2 text-xs text-blue-200/80">
              אם פתחת מכפתור &quot;ניהול&quot; או מ־/admin, הקישור כבר כולל זיהוי מסונכרן — ייטען מיד אחרי ההמתנה.
            </p>
          </section>
        ) : null}

        {hasTgSess && summary && !error ? (
          <section className="rounded-2xl border border-emerald-400/25 bg-emerald-500/10 p-3 text-sm text-emerald-100">
            <span className="font-bold">מסונכרן עם טלגרם</span>
            <span className="mr-2 text-emerald-200/90">
              — נכנסת דרך קישור מהבוט (זיהוי ללא הדבקה ידנית).
            </span>
          </section>
        ) : null}

        {/* Access code section — always available */}
        <section className="rounded-2xl border border-amber-400/20 bg-amber-400/10 p-4">
          <button
            type="button"
            className="flex w-full items-center justify-between gap-2 font-bold text-amber-200"
            onClick={() => setShowAccessCode((v) => !v)}
          >
            <span className="flex items-center gap-2">
              <AlertTriangle size={18} />
              {hasInitData && !error ? "כניסה חלופית (קוד גיבוי)" : "כניסה עם קוד גיבוי בלבד"}
            </span>
            <span className="text-xs text-amber-200/70">{showAccessCode ? "▲ סגור" : "▼ פתח"}</span>
          </button>

          {showAccessCode || (!hasInitData && !hasTgSess) || error === "admin_auth_required" ? (
            <div className="mt-3 space-y-3">
              <p className="text-sm text-amber-100/80">
                בתוך טלגרם: פתחו את הפאנל מכפתור &quot;ניהול&quot; בתחתית או שלחו{" "}
                <code className="rounded bg-black/30 px-1">/admin</code> — הקישור מזהה אתכם אוטומטית.
                {isTelegram && !hasInitData && !hasTgSess
                  ? " אם עדיין יש שגיאה, השתמשו בקוד הגיבוי שהבוט שלח."
                  : ""}
              </p>
              <div className="flex flex-col gap-2 sm:flex-row">
                <input
                  value={secret}
                  onChange={(e) => setSecret(e.target.value)}
                  type="text"
                  placeholder="קוד גיבוי (אם פותחים בדפדפן)"
                  dir="ltr"
                  className="min-w-0 flex-1 rounded-xl border border-white/10 bg-slate-900 px-3 py-2 text-sm font-mono text-white outline-none focus:border-amber-400"
                />
                <button
                  type="button"
                  onClick={saveSecret}
                  className="rounded-xl bg-amber-400 px-4 py-2 text-sm font-bold text-slate-950"
                >
                  התחבר
                </button>
              </div>
              {storedAdminSecret() ? (
                <p className="text-xs text-amber-200/60">הקוד נשמר בדפדפן. לניקוי — מחק ולחץ התחבר.</p>
              ) : null}
              <div className="pt-1">
                <button
                  type="button"
                  onClick={fetchDebug}
                  className="text-xs text-amber-300/60 underline"
                >
                  הצג מידע דיאגנוסטי
                </button>
                {debugInfo ? (
                  <pre className="mt-2 max-h-36 overflow-auto rounded-lg bg-black/30 p-2 text-xs text-amber-100/80 ltr">
                    {JSON.stringify(debugInfo, null, 2)}
                  </pre>
                ) : null}
              </div>
            </div>
          ) : null}
        </section>

        {error ? (
          <div className="rounded-2xl border border-red-400/20 bg-red-500/10 p-4 text-sm text-red-100">
            <div className="mb-1 font-bold">שגיאה: {error}</div>
            {error === "admin_auth_required" || error.includes("auth") ? (
              <p className="text-xs text-red-200/70">
                פתחו שוב מכפתור &quot;ניהול&quot; או <code className="rounded bg-black/20 px-1">/admin</code>, או הדביקו קוד גיבוי מהבוט.
              </p>
            ) : null}
          </div>
        ) : null}

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          <StatCard icon={Activity} label="אירועים" value={summary?.activity?.total_events ?? "-"} />
          <StatCard
            icon={Users}
            label="משתמשים מזוהים"
            value={summary?.activity?.unique_users ?? "-"}
            tone="emerald"
          />
          <StatCard
            icon={Receipt}
            label="מימושי קודים"
            value={redeemStats.total_redemptions ?? "-"}
            tone="blue"
          />
          <StatCard
            icon={UserCircle}
            label="משתמשים שמימשו קוד"
            value={redeemStats.distinct_redeemers ?? "-"}
            tone="emerald"
          />
          <StatCard
            icon={KeyRound}
            label="קודים פנויים"
            value={summary?.payment_codes?.unused ?? "-"}
            tone="amber"
          />
          <StatCard
            icon={maintenanceEnabled ? ToggleRight : ToggleLeft}
            label="מצב תחזוקה"
            value={maintenanceEnabled ? "פעיל" : "כבוי"}
            tone={maintenanceEnabled ? "amber" : "slate"}
          />
        </section>

        <section className="rounded-2xl border border-white/10 bg-white p-5 text-slate-900">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <h2 className="flex items-center gap-2 text-lg font-black">
              <UserCircle size={20} />
              משתמשים ({usersTotal})
            </h2>
            <p className="text-xs text-slate-500">
              סיכום לפי מזהה טלגרם · רכישות ישירות בעתיד יתווספו כאן
            </p>
          </div>
          <div className="overflow-x-auto rounded-xl border border-slate-100">
            <table className="min-w-[720px] w-full text-sm">
              <thead className="bg-slate-50 text-right text-xs font-bold text-slate-600">
                <tr>
                  <th className="px-3 py-2">מזהה</th>
                  <th className="px-3 py-2">שם</th>
                  <th className="px-3 py-2">@</th>
                  <th className="px-3 py-2">אירועים</th>
                  <th className="px-3 py-2">מימוש קוד</th>
                  <th className="px-3 py-2">יצירת PDF</th>
                  <th className="px-3 py-2">הורדות</th>
                  <th className="px-3 py-2">בוט</th>
                  <th className="px-3 py-2">נראה לאחרונה</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.telegram_user_id} className="border-t border-slate-100">
                    <td className="px-3 py-2 font-mono text-xs ltr">{u.telegram_user_id}</td>
                    <td className="px-3 py-2">{u.first_name || "—"}</td>
                    <td className="px-3 py-2 font-mono text-xs ltr">{u.username ? `@${u.username}` : "—"}</td>
                    <td className="px-3 py-2">{u.event_count}</td>
                    <td className="px-3 py-2">{u.redeem_count}</td>
                    <td className="px-3 py-2">{u.pdf_generated_count}</td>
                    <td className="px-3 py-2">{u.pdf_download_count}</td>
                    <td className="px-3 py-2">{u.bot_events_count}</td>
                    <td className="px-3 py-2 whitespace-nowrap text-xs">{formatDate(u.last_seen_ts)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!users.length ? <p className="mt-3 text-sm text-slate-500">אין משתמשים מזוהים באירועים עדיין.</p> : null}
        </section>

        <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="rounded-2xl border border-white/10 bg-white p-5 text-slate-900 lg:col-span-2">
            <div className="mb-4 flex items-center justify-between gap-3">
              <h2 className="flex items-center gap-2 text-lg font-black">
                <BarChart3 size={20} />
                סוגי פעילות
              </h2>
            </div>
            <div className="space-y-3">
              {topTypes.length ? (
                topTypes.map((item) => (
                  <div key={item.event_type}>
                    <div className="mb-1 flex justify-between text-sm font-bold">
                      <span>{item.event_type}</span>
                      <span>{item.count}</span>
                    </div>
                    <div className="h-2 rounded-full bg-slate-100">
                      <div
                        className="h-2 rounded-full bg-blue-600"
                        style={{
                          width: `${Math.max(
                            6,
                            Math.round((item.count / Math.max(1, totalEv)) * 100),
                          )}%`,
                        }}
                      />
                    </div>
                  </div>
                ))
              ) : (
                <p className="text-sm text-slate-500">אין פעילות להצגה עדיין.</p>
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-white/10 bg-white p-5 text-slate-900">
            <h2 className="mb-4 text-lg font-black">בקרה מהירה</h2>
            <div className="space-y-3">
              <button
                type="button"
                onClick={issueCode}
                disabled={busy}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-600 px-4 py-3 text-sm font-bold text-white transition hover:bg-emerald-500 disabled:opacity-60"
              >
                <KeyRound size={18} />
                הנפק קוד תשלום
              </button>
              {newCode ? (
                <button
                  type="button"
                  onClick={() => copyText(newCode)}
                  className="flex w-full items-center justify-between gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 font-mono text-sm font-bold text-emerald-800"
                >
                  <span>{newCode}</span>
                  <Copy size={16} />
                </button>
              ) : null}
              <button
                type="button"
                onClick={toggleMaintenance}
                disabled={busy}
                className={`flex w-full items-center justify-center gap-2 rounded-xl px-4 py-3 text-sm font-bold text-white transition disabled:opacity-60 ${
                  maintenanceEnabled ? "bg-slate-700 hover:bg-slate-600" : "bg-amber-600 hover:bg-amber-500"
                }`}
              >
                {maintenanceEnabled ? <ToggleLeft size={18} /> : <ToggleRight size={18} />}
                {maintenanceEnabled ? "כבה מצב תחזוקה" : "הפעל מצב תחזוקה"}
              </button>
            </div>
          </div>
        </section>

        <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="rounded-2xl border border-white/10 bg-white p-5 text-slate-900">
            <h2 className="mb-4 text-lg font-black">פעילות ופרטים</h2>
            <div className="max-h-[640px] overflow-auto">
              {events.map((event) => {
                const meta = event.meta || {};
                const hasDetail =
                  (meta.form && Object.keys(meta.form).length) ||
                  (meta.redemption && Object.keys(meta.redemption).length) ||
                  meta.code_last4 ||
                  meta.reason;
                const open = detailEventId === event.id;
                const payBadge = event.event_type === "pdf_generated" ? pdfPaymentBadge(meta) : null;
                return (
                  <div key={event.id} className="border-b border-slate-100 py-3 last:border-b-0">
                    <button
                      type="button"
                      className={`flex w-full items-start justify-between gap-2 text-right ${hasDetail ? "" : "cursor-default"}`}
                      onClick={() => {
                        if (!hasDetail) return;
                        setDetailEventId(open ? null : event.id);
                      }}
                    >
                      <span className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-bold">{event.event_type}</span>
                          {payBadge ? (
                            <span
                              className={`rounded-full px-2 py-0.5 text-xs font-black ${payBadge.className}`}
                            >
                              {payBadge.label}
                            </span>
                          ) : null}
                          {hasDetail ? (
                            <span className="rounded bg-blue-100 px-1.5 py-0.5 text-xs font-bold text-blue-800">
                              פרטים
                            </span>
                          ) : null}
                        </div>
                        <div className="mt-1 text-xs text-slate-500">
                          {event.source}
                          {event.telegram_user_id ? ` · ${event.telegram_user_id}` : ""}
                          {event.username ? ` · @${event.username}` : ""}
                        </div>
                      </span>
                      <span className="flex shrink-0 items-center gap-1 text-xs text-slate-500">
                        {formatDate(event.ts)}
                        {hasDetail ? (open ? <ChevronUp size={16} /> : <ChevronDown size={16} />) : null}
                      </span>
                    </button>
                    {open && hasDetail ? (
                      <div className="mt-2 space-y-2">
                        {meta.code_last4 ? (
                          <p className="text-xs text-slate-600 ltr">
                            קוד (4 ספרות אחרונות): <strong>{meta.code_last4}</strong>
                          </p>
                        ) : null}
                        {meta.reason ? (
                          <p className="text-xs text-amber-700">
                            סיבה: <strong>{meta.reason}</strong>
                          </p>
                        ) : null}
                        <FormSnapshotCard title="טופס / נתונים" data={meta.form} />
                        <FormSnapshotCard title="מימוש קוד (צילום)" data={meta.redemption} />
                        {event.event_type === "pdf_generated" ? (
                          <p className="text-xs text-slate-600">
                            <span className="font-bold text-slate-800">סטטוס תשלום:</span>{" "}
                            {payBadge ? (
                              <span className={`font-bold ${payBadge.label === "שולם" ? "text-emerald-700" : "text-amber-800"}`}>
                                {payBadge.label}
                                {payBadge.label === "לא שולם"
                                  ? " — תצוגת דוגמה (עם סימון דוגמה)"
                                  : " — PDF סופי (אחרי אישור תשלום)"}
                              </span>
                            ) : null}
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                );
              })}
              {!events.length ? <p className="text-sm text-slate-500">אין אירועים עדיין.</p> : null}
            </div>
          </div>

          <div className="rounded-2xl border border-white/10 bg-white p-5 text-slate-900">
            <h2 className="mb-4 text-lg font-black">קודי תשלום · פירוט מימוש</h2>
            <div className="max-h-[640px] space-y-4 overflow-auto">
              {codes.map((code) => (
                <div key={`${code.code}-${code.created_at}`} className="rounded-xl border border-slate-100 bg-slate-50/80 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <button
                      type="button"
                      onClick={() => copyText(code.code)}
                      className="flex items-center gap-2 font-mono font-bold text-blue-700"
                    >
                      {code.code}
                      <Copy size={14} />
                    </button>
                    <span
                      className={`rounded-full px-2 py-1 text-xs font-bold ${
                        code.used ? "bg-slate-200 text-slate-700" : "bg-emerald-100 text-emerald-700"
                      }`}
                    >
                      {code.used ? "נוצל" : "פנוי"}
                    </span>
                  </div>
                  <div className="mt-2 text-xs text-slate-500">
                    נוצר: {formatDate(code.created_at)}
                    {code.redeemed_at ? ` · נוצל: ${formatDate(code.redeemed_at)}` : ""}
                  </div>
                  {code.used && code.redemption ? (
                    <FormSnapshotCard title="פרטי המשתמש והטופס בעת המימוש" data={code.redemption} />
                  ) : null}
                  {code.used && !code.redemption ? (
                    <p className="mt-2 text-xs text-amber-700">נוצל לפני שמירת פירוט טופס — מימושים חדשים יכללו נתונים.</p>
                  ) : null}
                </div>
              ))}
              {!codes.length ? <p className="text-sm text-slate-500">אין קודים עדיין.</p> : null}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
