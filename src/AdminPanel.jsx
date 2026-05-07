import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Copy,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldCheck,
  ToggleLeft,
  ToggleRight,
  Users,
} from "lucide-react";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

function apiUrl(path) {
  return API_BASE ? `${API_BASE}${path}` : path;
}

function telegramInitData() {
  return window.Telegram?.WebApp?.initData || "";
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

/** Append initData as query fallback when intermediaries drop headers on cross-origin requests */
function withAdminAuthQuery(path) {
  const initData = telegramInitData();
  let url = apiUrl(path);
  if (!initData || url.includes("tg_init_data=")) {
    return url;
  }
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}tg_init_data=${encodeURIComponent(initData)}`;
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

  const isTelegram = Boolean(
    typeof window !== "undefined" && window.Telegram?.WebApp,
  );
  const maintenanceEnabled = Boolean(summary?.control?.maintenance_mode);

  const topTypes = useMemo(
    () => summary?.activity?.by_type?.slice(0, 5) || [],
    [summary],
  );

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [summaryData, eventsData, codesData] = await Promise.all([
        adminFetch("/api/admin/summary"),
        adminFetch("/api/admin/events?limit=80"),
        adminFetch("/api/admin/payment-codes"),
      ]);
      setSummary(summaryData);
      setEvents(eventsData.items || []);
      setCodes(codesData.items || []);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let cancelled = false;

    (async () => {
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
              ממתינים לאימות טלגרם (initData)…
            </div>
          </section>
        ) : null}

        {!isTelegram || (!tgWaiting && !hasInitData) ? (
          <section className="rounded-2xl border border-amber-400/20 bg-amber-400/10 p-4">
            <div className="mb-3 flex items-center gap-2 font-bold text-amber-200">
              <AlertTriangle size={18} />
              {!isTelegram ? "פתיחה מחוץ לטלגרם" : "אימות חלופי"}
            </div>
            <p className="mb-3 text-sm text-amber-100/80">
              {!isTelegram
                ? "אם פתחת בדפדפן, הזן ADMIN_API_SECRET. בתוך טלגרם ההזדהות מתבצעת אוטומטית לאחר טעינת initData."
                : "אם initData לא נטען, אפשר להזין ADMIN_API_SECRET (כמו בדפדפן)."}
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                type="password"
                placeholder="ADMIN_API_SECRET"
                className="min-w-0 flex-1 rounded-xl border border-white/10 bg-slate-900 px-3 py-2 text-sm text-white outline-none focus:border-blue-400"
              />
              <button
                type="button"
                onClick={saveSecret}
                className="rounded-xl bg-amber-400 px-4 py-2 text-sm font-bold text-slate-950"
              >
                שמור והתחבר
              </button>
            </div>
          </section>
        ) : null}

        {error ? (
          <div className="rounded-2xl border border-red-400/20 bg-red-500/10 p-4 text-sm text-red-100">
            {error}
          </div>
        ) : null}

        <section className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <StatCard icon={Activity} label="אירועים" value={summary?.activity?.total_events ?? "-"} />
          <StatCard
            icon={Users}
            label="משתמשים מזוהים"
            value={summary?.activity?.unique_users ?? "-"}
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
                            Math.round((item.count / Math.max(1, summary.activity.total_events)) * 100),
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
            <h2 className="mb-4 text-lg font-black">פעילות אחרונה</h2>
            <div className="max-h-[520px] overflow-auto">
              {events.map((event) => (
                <div key={event.id} className="border-b border-slate-100 py-3 last:border-b-0">
                  <div className="flex items-center justify-between gap-3">
                    <div className="font-bold">{event.event_type}</div>
                    <div className="text-xs text-slate-500">{formatDate(event.ts)}</div>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {event.source}
                    {event.telegram_user_id ? ` · ${event.telegram_user_id}` : ""}
                    {event.username ? ` · @${event.username}` : ""}
                  </div>
                </div>
              ))}
              {!events.length ? <p className="text-sm text-slate-500">אין אירועים עדיין.</p> : null}
            </div>
          </div>

          <div className="rounded-2xl border border-white/10 bg-white p-5 text-slate-900">
            <h2 className="mb-4 text-lg font-black">קודי תשלום</h2>
            <div className="max-h-[520px] overflow-auto">
              {codes.map((code) => (
                <div key={`${code.code}-${code.created_at}`} className="border-b border-slate-100 py-3 last:border-b-0">
                  <div className="flex items-center justify-between gap-3">
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
                        code.used ? "bg-slate-100 text-slate-600" : "bg-emerald-100 text-emerald-700"
                      }`}
                    >
                      {code.used ? "נוצל" : "פנוי"}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    נוצר: {formatDate(code.created_at)}
                    {code.redeemed_at ? ` · נוצל: ${formatDate(code.redeemed_at)}` : ""}
                  </div>
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
