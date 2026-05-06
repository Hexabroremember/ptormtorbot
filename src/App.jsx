import React, { useState, useEffect } from 'react';
import { 
  User, 
  CreditCard, 
  CheckCircle2, 
  Globe, 
  ChevronLeft, 
  ChevronRight, 
  IdCard, 
  Languages, 
  Coins,
  Loader2,
  FileText,
  Lock,
  QrCode,
  Bitcoin,
  Apple,
  CircleDollarSign,
  Ticket,
  Download,
} from 'lucide-react';

function formatDateDdMmYyyy(d) {
  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const yyyy = d.getFullYear();
  return `${dd}/${mm}/${yyyy}`;
}

/** תאריך התפוגה בטופס לפי תקופה נבחרת (מהיום). לצמיתות → המחרוזת «לצמיתות». */
function computeExpirationForPdf(expiryOptionId) {
  if (!expiryOptionId) return "";
  if (expiryOptionId === "1500") return "לצמיתות";
  const yearsById = { "300": 1, "500": 3, "900": 5, "1200": 10 };
  const years = yearsById[expiryOptionId] ?? 1;
  const d = new Date();
  d.setFullYear(d.getFullYear() + years);
  return formatDateDdMmYyyy(d);
}

/** מילים = רצפים מופרדים ברווחים (בדיוק 2). מזהה: 8–10 ספרות בלבד (לאחר נירמול). */
function validateStep1(formData, lang) {
  const t = content[lang];
  const name = formData.fullName.trim();
  const nameEn = formData.fullNameEn.trim();
  const idDigits = formData.idNumber.replace(/\D/g, "");
  if (!name || !nameEn || !formData.idNumber.trim() || !formData.expiryOption) {
    return t.validationCompleteStep1;
  }
  const wordsHe = name.split(/\s+/).filter(Boolean);
  const wordsEn = nameEn.split(/\s+/).filter(Boolean);
  if (wordsHe.length !== 2) return t.validationFullNameTwoWords;
  if (wordsEn.length !== 2) return t.validationEnglishTwoWords;
  if (!/^[A-Za-z\s'\-]+$/.test(nameEn)) return t.validationEnglishOnly;
  if (idDigits.length < 8 || idDigits.length > 10) return t.validationIdDigits;
  return null;
}

const content = {
  he: {
    title: "הנפקת תעודה דיגיטלית",
    next: "המשך",
    back: "חזרה",
    payment: "מעבר לתשלום מאובטח",
    step1Title: "פרטי המבוטח ותוקף",
    step2Title: "תצוגת דוגמה לטופס פטור מתור",
    step3Title: "בחירת אמצעי תשלום",
    summary: "סיכום הזמנה",
    total: "סה\"כ לתשלום:",
    loadingMsg: "מנפיק דוגמה לטופס פטור מתור...",
    paymentMethods: {
      creditCard: "כרטיס אשראי",
      applePay: "Apple Pay",
      crypto: "קריפטו (BTC/USDT/ETH)",
      withdrawalCode: "קוד משיכה",
    },
    cryptoMsg: "אנא שלח את הסכום המדויק לכתובת הארנק הבאה:",
    labels: {
      fullName: "שם מלא",
      fullNameEn: "שם מלא באנגלית",
      idNumber: "מספר זהות",
      expiryDate: "תקופת תוקף ועלות הנפקה",
      expiryPrintedHint: "תאריך שיודפס בטופס (לפי הבחירה)",
    },
    placeholders: {
      fullName: "ישראל ישראלי",
      fullNameEn: "ISRAEL ISRAELI",
      idNumber: "למשל 123456789",
    },
    pickExpiryHint: "בחרו תקופה כדי לראות את התאריך שיודפס בטופס",
    validationCompleteStep1: "נא למלא את כל השדות ולבחור תקופת תוקף לפני ההמשך.",
    validationFullNameTwoWords: "שם מלא בעברית חייב להכיל בדיוק שתי מילים (שם פרטי ושם משפחה).",
    validationEnglishTwoWords: "השם באנגלית חייב להכיל בדיוק שתי מילים.",
    validationEnglishOnly: "השם באנגלית חייב להכיל אותיות אנגלית בלבד.",
    validationIdDigits: "מספר הזהות חייב להכיל 8–10 ספרות.",
    paymentApprovalTitle: "קוד אישור תשלום",
    paymentApprovalHint:
      "שילמת דרך העברה בנקאית, קופה או דרך אחרת? הזינו כאן את הקוד החד־פעמי שקיבלתם מהמנהל.",
    paymentApprovalPlaceholder: "הזינו את הקוד",
    paymentApprovalSubmit: "אשר קוד",
    paymentApprovedBadge: "התשלום אושר — אפשר להוריד את הקובץ הסופי ללא סימן מים.",
    paymentCodeInvalid: "הקוד שגוי או לא קיים.",
    paymentCodeUsed: "הקוד כבר נוצל. צריך קוד חדש מהמנהל.",
    paymentDownloadFinal: "הורד PDF סופי (ללא סימן מים)",
    paymentDownloading: "מוריד…",
  },
  ar: {
    title: "إصدار شهادة رقمية",
    next: "استمرار",
    back: "رجوع",
    payment: "الانتقال إلى الدفع الآمن",
    step1Title: "تفاصيل المؤمن عليه",
    step2Title: "معاينة نموذج الإعفاء",
    step3Title: "اختيار طريقة الدفع",
    summary: "ملخص الطلب",
    total: "المبلغ الإجمالي:",
    loadingMsg: "جاري إصدار إعفاء من الطابور...",
    paymentMethods: {
      creditCard: "بطاقة ائتمان",
      applePay: "Apple Pay",
      crypto: "كريبتو (BTC/USDT/ETH)",
      withdrawalCode: "رمز السحب",
    },
    cryptoMsg: "يرجى إرسال المبلغ المحدد إلى عنوان المحفظة التالي:",
    labels: {
      fullName: "الاسم الكامل",
      fullNameEn: "الاسم الكامل بالإنجليزية",
      idNumber: "رقم الهوية",
      expiryDate: "فترة صلاحية الشهادة",
      expiryPrintedHint: "التاريخ في النموذج (حسب الاختيار)",
    },
    placeholders: {
      fullName: "مثال: اسم تجريبي",
      fullNameEn: "ISRAEL ISRAELI",
      idNumber: "مثال 123456789",
    },
    pickExpiryHint: "اختر المدة لعرض التاريخ الذي سيُطبع",
    validationCompleteStep1: "يرجى تعبئة جميع الحقول واختيار مدة الصلاحية قبل المتابعة.",
    validationFullNameTwoWords: "الاسم بالعبرية يجب أن يتكون من كلمتين بالضبط (اسم أول واسم عائلة).",
    validationEnglishTwoWords: "الاسم بالإنجليزية يجب أن يتكون من كلمتين بالضبط.",
    validationEnglishOnly: "الاسم الإنجليزي يجب أن يحتوي على أحرف إنجليزية فقط.",
    validationIdDigits: "رقم الهوية يجب أن يحتوي على 8–10 أرقام.",
    paymentApprovalTitle: "رمز تأكيد الدفع",
    paymentApprovalHint:
      "دفعت بالتحويل أو بطريقة أخرى؟ أدخل الرمز لمرة واحدة الذي استلمته من المسؤول.",
    paymentApprovalPlaceholder: "أدخل الرمز",
    paymentApprovalSubmit: "تأكيد الرمز",
    paymentApprovedBadge: "تم تأكيد الدفع — يمكنك تنزيل الملف النهائي بدون علامة مائية.",
    paymentCodeInvalid: "الرمز غير صالح أو غير موجود.",
    paymentCodeUsed: "تم استخدام هذا الرمز مسبقًا. اطلب رمزًا جديدًا.",
    paymentDownloadFinal: "تنزيل PDF النهائي (بدون علامة مائية)",
    paymentDownloading: "جاري التنزيل…",
  },
};

const App = () => {
  const [currentStep, setCurrentStep] = useState(1);
  const [language, setLanguage] = useState('he');
  const [loadingProgress, setLoadingProgress] = useState(0);
  const [cryptoSelected, setCryptoSelected] = useState(false);

  const [paymentApproved, setPaymentApproved] = useState(false);
  const [paymentCodeInput, setPaymentCodeInput] = useState("");
  const [paymentCodeError, setPaymentCodeError] = useState(null);
  const [paymentCodeSubmitting, setPaymentCodeSubmitting] = useState(false);
  const [finalPdfDownloading, setFinalPdfDownloading] = useState(false);

  const [pdfPreviewUrl, setPdfPreviewUrl] = useState(null);
  const [pdfGenerating, setPdfGenerating] = useState(false);
  const [pdfError, setPdfError] = useState(null);

  const [step1Error, setStep1Error] = useState(null);

  const [formData, setFormData] = useState({
    fullName: "",
    fullNameEn: "",
    idNumber: "",
    expiryOption: "",
    birthDate: "",
    idIssueDate: "",
  });

  const TELEGRAM_LINK = "https://t.me/m/h_K7ZBosMzdh";

  /** Full-screen step 2 animation until the watermarked PDF is loaded or the request fails. */
  const step2AwaitingPdf = currentStep === 2 && !pdfPreviewUrl && !pdfError;

  useEffect(() => {
    if (currentStep === 2) {
      setLoadingProgress(0);
    }
  }, [currentStep]);

  useEffect(() => {
    if (!step2AwaitingPdf) {
      if (pdfPreviewUrl || pdfError) {
        setLoadingProgress(100);
      }
      return undefined;
    }
    const id = setInterval(() => {
      setLoadingProgress((p) => (p >= 92 ? p : p + 0.9));
    }, 70);
    return () => clearInterval(id);
  }, [step2AwaitingPdf, pdfPreviewUrl, pdfError]);

  const buildPdfApiUrl = () => {
    const base = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
    return base ? `${base}/generate-pdf` : "/generate-pdf";
  };

  const buildRedeemApiUrl = () => {
    const base = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
    return base ? `${base}/redeem-payment-code` : "/redeem-payment-code";
  };

  useEffect(() => {
    if (currentStep !== 2) {
      setPdfPreviewUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return null;
      });
      setPdfError(null);
      setPdfGenerating(false);
    }
  }, [currentStep]);

  useEffect(() => {
    if (currentStep !== 2) return undefined;

    let cancelled = false;

    const parseError = async (res) => {
      try {
        const err = await res.json();
        if (typeof err.detail === "string") return err.detail;
        if (Array.isArray(err.detail)) {
          return err.detail
            .map((d) => (typeof d === "string" ? d : d.msg || JSON.stringify(d)))
            .join(" ");
        }
      } catch {
        /* ignore */
      }
      return `HTTP ${res.status}`;
    };

    (async () => {
      const step1Err = validateStep1(formData, language);
      if (step1Err) {
        setPdfGenerating(false);
        setPdfError(step1Err);
        return;
      }

      setPdfGenerating(true);
      setPdfError(null);
      try {
        const idDigits = formData.idNumber.replace(/\D/g, "");
        const res = await fetch(buildPdfApiUrl(), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            hebrew_full_name: formData.fullName.trim(),
            english_full_name: formData.fullNameEn.trim().toUpperCase(),
            id_number: idDigits,
            expiration_date: computeExpirationForPdf(formData.expiryOption),
            watermark: true,
          }),
        });
        if (!res.ok) throw new Error(await parseError(res));
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        setPdfPreviewUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return url;
        });
      } catch (e) {
        if (!cancelled) setPdfError(e.message || String(e));
      } finally {
        if (!cancelled) setPdfGenerating(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    currentStep,
    formData.fullName,
    formData.fullNameEn,
    formData.idNumber,
    formData.expiryOption,
    language,
  ]);

  const steps = [
    { id: 1, label: language === 'he' ? 'פרטי המבוטח' : 'تفاصيل المؤمن عليه', icon: User },
    { id: 2, label: language === 'he' ? 'טופס פטור מתור' : 'نموذج الإعفاء', icon: FileText },
    { id: 3, label: language === 'he' ? 'תשלום' : 'دفع', icon: CreditCard },
  ];

  const expiryOptions = [
    { id: '300', label: language === 'he' ? 'שנה' : 'سنة واحدة', value: 'שנה / 1 Year' },
    { id: '500', label: language === 'he' ? '3 שנים' : '3 سنوات', value: '3 שנים / 3 Years' },
    { id: '900', label: language === 'he' ? '5 שנים' : '5 سنوات', value: '5 שנים / 5 Years' },
    { id: '1200', label: language === 'he' ? '10 שנים' : '10 سنوات', value: '10 שנים / 10 Years' },
    { id: '1500', label: language === 'he' ? 'לצמיתות' : 'دائم', value: 'לצמיתות / Permanent' },
  ];

  const t = content[language];

  const handleInputChange = (field, value) => {
    setStep1Error(null);
    setFormData((prev) => ({ ...prev, [field]: value }));
  };

  const handleNext = () => {
    if (currentStep === 1) {
      const err = validateStep1(formData, language);
      if (err) {
        setStep1Error(err);
        return;
      }
      setStep1Error(null);
    }
    if (currentStep < 3) setCurrentStep(currentStep + 1);
  };

  const handleBack = () => {
    if (currentStep > 1) {
      setCurrentStep(currentStep - 1);
      setCryptoSelected(false);
    }
  };

  const handlePaymentAction = (method) => {
    if (method === 'crypto') {
      setCryptoSelected(true);
    } else {
      window.open(TELEGRAM_LINK, '_blank');
    }
  };

  const parseJsonDetail = async (res) => {
    try {
      const err = await res.json();
      const d = err.detail;
      if (typeof d === "string") return d;
      if (Array.isArray(d)) {
        return d.map((x) => (typeof x === "string" ? x : x.msg || JSON.stringify(x))).join(" ");
      }
    } catch {
      /* ignore */
    }
    return "";
  };

  const handleRedeemPaymentCode = async () => {
    setPaymentCodeError(null);
    const trimmed = paymentCodeInput.trim();
    if (!trimmed) {
      setPaymentCodeError(t.paymentCodeInvalid);
      return;
    }
    setPaymentCodeSubmitting(true);
    try {
      const res = await fetch(buildRedeemApiUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: trimmed }),
      });
      if (res.ok) {
        setPaymentApproved(true);
        setPaymentCodeInput("");
        return;
      }
      const detail = await parseJsonDetail(res);
      if (detail === "code_already_used") {
        setPaymentCodeError(t.paymentCodeUsed);
      } else {
        setPaymentCodeError(t.paymentCodeInvalid);
      }
    } catch (e) {
      setPaymentCodeError(e.message || String(e));
    } finally {
      setPaymentCodeSubmitting(false);
    }
  };

  const handleDownloadFinalPdf = async () => {
    const err = validateStep1(formData, language);
    if (err) {
      setPaymentCodeError(err);
      return;
    }
    setFinalPdfDownloading(true);
    setPaymentCodeError(null);
    try {
      const idDigits = formData.idNumber.replace(/\D/g, "");
      const res = await fetch(buildPdfApiUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          hebrew_full_name: formData.fullName.trim(),
          english_full_name: formData.fullNameEn.trim().toUpperCase(),
          id_number: idDigits,
          expiration_date: computeExpirationForPdf(formData.expiryOption),
          watermark: false,
        }),
      });
      if (!res.ok) throw new Error((await parseJsonDetail(res)) || `HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "FormPDFPreview.pdf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setPaymentCodeError(e.message || String(e));
    } finally {
      setFinalPdfDownloading(false);
    }
  };

  const renderStepContent = () => {
    switch (currentStep) {
      case 1:
        return (
          <div className="space-y-6 animate-in fade-in slide-in-from-bottom-2 duration-500">
            <h2 className="text-xl font-bold text-slate-800 border-b pb-2">{t.step1Title}</h2>
            {step1Error ? (
              <div className="p-3 rounded-xl bg-red-50 border border-red-200 text-red-800 text-sm">
                {step1Error}
              </div>
            ) : null}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-1">
                <label className="text-sm font-semibold text-slate-600 block">{t.labels.fullName}</label>
                <div className="relative">
                  <User className="absolute left-3 top-3 text-slate-400" size={18} />
                  <input 
                    type="text" 
                    placeholder={t.placeholders.fullName}
                    value={formData.fullName || ''}
                    onChange={(e) => handleInputChange('fullName', e.target.value)}
                    className="w-full p-2.5 pr-3 pl-10 rounded-xl border border-slate-200 focus:ring-2 focus:ring-blue-500 outline-none transition-all placeholder:text-slate-400"
                  />
                </div>
              </div>
              <div className="space-y-1">
                <label className="text-sm font-semibold text-slate-600 block">{t.labels.fullNameEn}</label>
                <div className="relative">
                  <Languages className="absolute left-3 top-3 text-slate-400" size={18} />
                  <input 
                    type="text" 
                    dir="ltr"
                    placeholder={t.placeholders.fullNameEn}
                    value={formData.fullNameEn || ''}
                    onChange={(e) => handleInputChange('fullNameEn', e.target.value)}
                    className="w-full p-2.5 pr-3 pl-10 rounded-xl border border-slate-200 focus:ring-2 focus:ring-blue-500 outline-none transition-all text-left font-mono placeholder:text-slate-400"
                  />
                </div>
              </div>
              <div className="space-y-1">
                <label className="text-sm font-semibold text-slate-600 block">{t.labels.idNumber}</label>
                <div className="relative">
                  <IdCard className="absolute left-3 top-3 text-slate-400" size={18} />
                  <input 
                    type="text" 
                    placeholder={t.placeholders.idNumber}
                    inputMode="numeric"
                    value={formData.idNumber || ''}
                    onChange={(e) => handleInputChange('idNumber', e.target.value)}
                    className="w-full p-2.5 pr-3 pl-10 rounded-xl border border-slate-200 focus:ring-2 focus:ring-blue-500 outline-none transition-all placeholder:text-slate-400"
                  />
                </div>
              </div>
              <div className="md:col-span-2 space-y-3 pt-2">
                <label className="text-sm font-semibold text-slate-600 flex items-center gap-2">
                  <Coins size={16} className="text-amber-500" />
                  {t.labels.expiryDate}
                </label>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">
                  {expiryOptions.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      onClick={() => handleInputChange('expiryOption', option.id)}
                      className={`p-3 rounded-xl border-2 transition-all flex flex-col items-center justify-center gap-1 ${
                        formData.expiryOption === option.id
                        ? 'bg-blue-600 border-blue-600 text-white shadow-md'
                        : 'bg-white border-slate-100 text-slate-600 hover:border-blue-200'
                      }`}
                    >
                      <span className="text-xs font-bold">{option.label}</span>
                      <div className="flex items-baseline gap-0.5 font-mono">
                        <span className="text-[10px] opacity-80">₪</span>
                        <span className="text-lg font-bold">{Number(option.id).toLocaleString()}</span>
                      </div>
                    </button>
                  ))}
                </div>
                <div className="mt-3 p-3 rounded-xl bg-slate-50 border border-slate-100 text-sm text-slate-700">
                  <span className="font-semibold text-slate-600">{t.labels.expiryPrintedHint}: </span>
                  <span
                    className={
                      !formData.expiryOption
                        ? "text-slate-400 italic"
                        : formData.expiryOption === "1500"
                          ? "font-bold"
                          : "font-mono"
                    }
                    dir={formData.expiryOption === "1500" || !formData.expiryOption ? undefined : "ltr"}
                  >
                    {formData.expiryOption
                      ? computeExpirationForPdf(formData.expiryOption)
                      : t.pickExpiryHint}
                  </span>
                </div>
              </div>
            </div>
          </div>
        );
      case 2:
        if (step2AwaitingPdf) {
          return (
            <div className="flex flex-col items-center justify-center py-12 space-y-6 animate-in fade-in duration-500">
              <div className="relative w-24 h-24">
                <Loader2 className="w-24 h-24 text-blue-600 animate-spin opacity-20" />
                <div className="absolute inset-0 flex items-center justify-center">
                  <FileText className="w-10 h-10 text-blue-600 animate-pulse" />
                </div>
              </div>
              <div className="text-center space-y-2">
                <p className="text-lg font-bold text-slate-800">{t.loadingMsg}</p>
                <p className="text-sm text-slate-500">אנא המתן, המערכת מאחזרת נתונים מהמרכז הדיגיטלי</p>
              </div>
              <div className="w-full max-w-xs bg-slate-100 h-2 rounded-full overflow-hidden">
                <div 
                  className="bg-blue-600 h-full transition-all duration-300 ease-out"
                  style={{ width: `${loadingProgress}%` }}
                ></div>
              </div>
              <span className="text-xs font-mono text-blue-600 font-bold">{Math.round(loadingProgress)}%</span>
            </div>
          );
        }
        return (
          <div className="space-y-6 animate-in fade-in slide-in-from-bottom-2 duration-500">
            <h2 className="text-xl font-bold text-slate-800 border-b pb-2">{t.step2Title}</h2>
            <div className="max-w-xl mx-auto space-y-6">
              <div className="p-4 bg-emerald-50 border-r-4 border-emerald-500 rounded-lg flex gap-3 text-emerald-900 text-sm shadow-sm">
                <CheckCircle2 size={20} className="shrink-0 text-emerald-600 mt-0.5" />
                <div>
                  <p>הטופס הופק בהצלחה! אנא עיין בדוגמה לפני מעבר לתשלום.</p>
                  <p className="text-xs text-emerald-800/90 mt-1">תצוגה בלבד — הורדת הקובץ אינה זמינה בשלב זה.</p>
                </div>
              </div>

              {pdfError && (
                <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-800 text-sm">
                  {pdfError}
                  <p className="mt-2 text-xs text-red-600">
                    ודא שהשרת רץ (למשל uvicorn על פורט 8000) ושקובץ watermark קיים בשרת.
                  </p>
                </div>
              )}

              <div className="relative w-full max-w-md mx-auto rounded-xl border border-slate-200 bg-slate-50 shadow-inner overflow-hidden min-h-[520px]">
                {pdfPreviewUrl ? (
                  <iframe
                    title="PDF preview"
                    src={pdfPreviewUrl}
                    className="w-full min-h-[520px] border-0 bg-white"
                  />
                ) : null}
              </div>
            </div>
          </div>
        );
      case 3: {
        const approvalCard = (
          <div className="rounded-2xl border border-emerald-100 bg-emerald-50/50 p-5 space-y-4">
            <div className="flex items-start gap-3">
              <Ticket className="text-emerald-600 shrink-0 mt-0.5" size={22} />
              <div className="flex-1 space-y-1">
                <h3 className="font-bold text-slate-800">{t.paymentApprovalTitle}</h3>
                <p className="text-sm text-slate-600">{t.paymentApprovalHint}</p>
              </div>
            </div>
            {paymentApproved ? (
              <div className="space-y-4 pt-1">
                <div className="flex gap-2 items-start p-3 rounded-xl bg-white border border-emerald-200 text-emerald-900 text-sm">
                  <CheckCircle2 className="shrink-0 text-emerald-600" size={20} />
                  <span>{t.paymentApprovedBadge}</span>
                </div>
                <button
                  type="button"
                  onClick={handleDownloadFinalPdf}
                  disabled={finalPdfDownloading}
                  className="w-full flex items-center justify-center gap-2 py-3 rounded-xl bg-emerald-600 text-white font-bold hover:bg-emerald-700 disabled:opacity-60 transition-all"
                >
                  {finalPdfDownloading ? (
                    <Loader2 className="animate-spin" size={20} />
                  ) : (
                    <Download size={20} />
                  )}
                  {finalPdfDownloading ? t.paymentDownloading : t.paymentDownloadFinal}
                </button>
              </div>
            ) : (
              <div className="flex flex-col sm:flex-row gap-2">
                <input
                  type="text"
                  dir="ltr"
                  autoComplete="off"
                  placeholder={t.paymentApprovalPlaceholder}
                  value={paymentCodeInput}
                  onChange={(e) => {
                    setPaymentCodeInput(e.target.value);
                    setPaymentCodeError(null);
                  }}
                  className="flex-1 px-4 py-2.5 rounded-xl border border-slate-200 font-mono text-center tracking-wide focus:ring-2 focus:ring-emerald-500 outline-none placeholder:text-slate-400"
                />
                <button
                  type="button"
                  onClick={handleRedeemPaymentCode}
                  disabled={paymentCodeSubmitting}
                  className="px-6 py-2.5 rounded-xl bg-emerald-600 text-white font-bold hover:bg-emerald-700 disabled:opacity-60 flex items-center justify-center gap-2 shrink-0"
                >
                  {paymentCodeSubmitting ? <Loader2 className="animate-spin" size={18} /> : null}
                  {t.paymentApprovalSubmit}
                </button>
              </div>
            )}
            {paymentCodeError ? (
              <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                {paymentCodeError}
              </div>
            ) : null}
          </div>
        );

        if (cryptoSelected) {
          return (
            <div className="space-y-6 animate-in zoom-in-95 duration-500 max-w-lg mx-auto">
              <div className="p-6 bg-slate-900 text-white rounded-3xl shadow-xl text-center">
                <div className="flex justify-center gap-3 mb-4">
                  <div className="bg-amber-500 w-12 h-12 rounded-full flex items-center justify-center text-slate-900">
                    <Bitcoin size={24} />
                  </div>
                  <div className="bg-blue-500 w-12 h-12 rounded-full flex items-center justify-center text-white">
                    <CircleDollarSign size={24} />
                  </div>
                  <div className="bg-slate-700 w-12 h-12 rounded-full flex items-center justify-center text-white">
                    <Coins size={24} />
                  </div>
                </div>
                <h3 className="text-xl font-bold mb-2">{t.paymentMethods.crypto}</h3>
                <p className="text-sm text-slate-400 mb-6">{t.cryptoMsg}</p>

                <div className="bg-white/10 p-4 rounded-xl border border-white/10 break-all font-mono text-xs mb-6">
                  bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh
                </div>

                <div className="bg-white p-4 rounded-xl inline-block mb-6">
                  <QrCode size={140} className="text-slate-900" />
                </div>

                <div className="flex justify-center gap-4">
                  <button
                    type="button"
                    onClick={() => setCryptoSelected(false)}
                    className="px-6 py-2 rounded-lg bg-white/10 text-white font-bold hover:bg-white/20 transition-all"
                  >
                    {t.back}
                  </button>
                  <button
                    type="button"
                    className="px-6 py-2 rounded-lg bg-blue-600 text-white font-bold hover:bg-blue-700 transition-all"
                  >
                    שלחתי את התשלום
                  </button>
                </div>
              </div>
              {approvalCard}
            </div>
          );
        }
        return (
          <div className="space-y-8 animate-in fade-in duration-500 max-w-2xl mx-auto">
            <div className="text-center">
              <h2 className="text-2xl font-bold text-slate-800">{t.step3Title}</h2>
              <div className="mt-4 p-4 bg-blue-50 rounded-2xl border border-blue-100 flex flex-col items-center">
                <span className="text-sm text-blue-600 font-bold">{t.summary}</span>
                <div className="flex items-baseline gap-1 mt-1">
                  <span className="text-3xl font-black text-slate-900">
                    ₪{Number(formData.expiryOption).toLocaleString()}
                  </span>
                </div>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {/* Credit Card */}
              <button
                type="button"
                onClick={() => handlePaymentAction('credit')}
                className="flex items-center gap-4 p-5 bg-white border-2 border-slate-100 rounded-2xl hover:border-blue-500 hover:shadow-md transition-all text-right group"
              >
                <div className="p-3 bg-slate-50 rounded-xl text-slate-400 group-hover:text-blue-600 group-hover:bg-blue-50 transition-colors">
                  <CreditCard size={24} />
                </div>
                <div>
                  <div className="font-bold text-slate-800">{t.paymentMethods.creditCard}</div>
                  <div className="text-xs text-slate-400">Visa / Mastercard / Amex</div>
                </div>
              </button>

              {/* Apple Pay */}
              <button
                type="button"
                onClick={() => handlePaymentAction('apple')}
                className="flex items-center gap-4 p-5 bg-white border-2 border-slate-100 rounded-2xl hover:border-blue-500 hover:shadow-md transition-all text-right group"
              >
                <div className="p-3 bg-slate-50 rounded-xl text-slate-400 group-hover:text-black group-hover:bg-slate-100 transition-colors">
                  <Apple size={24} />
                </div>
                <div>
                  <div className="font-bold text-slate-800">{t.paymentMethods.applePay}</div>
                  <div className="text-xs text-slate-400">תשלום מהיר ומאובטח</div>
                </div>
              </button>

              {/* Withdrawal Code */}
              <button
                type="button"
                onClick={() => handlePaymentAction('code')}
                className="flex items-center gap-4 p-5 bg-white border-2 border-slate-100 rounded-2xl hover:border-blue-500 hover:shadow-md transition-all text-right group"
              >
                <div className="p-3 bg-slate-50 rounded-xl text-slate-400 group-hover:text-emerald-600 group-hover:bg-emerald-50 transition-colors">
                  <QrCode size={24} />
                </div>
                <div>
                  <div className="font-bold text-slate-800">{t.paymentMethods.withdrawalCode}</div>
                  <div className="text-xs text-slate-400">באמצעות קוד כספומט / SMS</div>
                </div>
              </button>

              {/* Crypto */}
              <button
                type="button"
                onClick={() => handlePaymentAction('crypto')}
                className="flex items-center gap-4 p-5 bg-white border-2 border-slate-100 rounded-2xl hover:border-blue-500 hover:shadow-md transition-all text-right group"
              >
                <div className="p-3 bg-slate-50 rounded-xl text-slate-400 group-hover:text-amber-600 group-hover:bg-amber-50 transition-colors flex gap-1">
                  <Bitcoin size={18} />
                  <CircleDollarSign size={18} />
                </div>
                <div>
                  <div className="font-bold text-slate-800">{t.paymentMethods.crypto}</div>
                  <div className="text-xs text-slate-400">BTC / USDT / ETH</div>
                </div>
              </button>
            </div>

            {approvalCard}

            <div className="flex justify-center items-center gap-2 text-xs text-slate-400 border-t pt-6">
              <Lock size={14} />
              חיבור מוצפן SSL • תשלום מאובטח
            </div>
          </div>
        );
      }
      default:
        return null;
    }
  };

  return (
    <div className="min-h-screen bg-slate-50 font-sans p-4 md:p-8" dir={language === 'ar' || language === 'he' ? 'rtl' : 'ltr'}>
      <header className="max-w-4xl mx-auto mb-8 flex justify-between items-center">
        <div className="flex items-center gap-4">
          <div className="p-2.5 bg-blue-600 rounded-xl text-white shadow-lg shadow-blue-100">
            <CreditCard size={24} />
          </div>
          <h1 className="text-xl md:text-2xl font-bold text-slate-800 tracking-tight">
            {t.title}
          </h1>
        </div>
        
        <button 
          type="button"
          onClick={() => setLanguage(language === 'he' ? 'ar' : 'he')}
          className="flex items-center gap-2 px-4 py-2 bg-white border border-slate-200 rounded-full text-xs font-bold text-slate-700 hover:bg-slate-50 transition-all shadow-sm"
        >
          <Globe size={16} className="text-blue-600" />
          {language === 'he' ? 'العربية' : 'עברית'}
        </button>
      </header>

      <main className="max-w-4xl mx-auto">
        <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 mb-6">
          <div className="flex items-center justify-between relative px-4 md:px-10">
            <div className="absolute top-1/2 left-4 md:left-10 right-4 md:right-10 h-0.5 bg-slate-100 -translate-y-1/2 z-0" />
            
            {steps.map((step) => {
              const isActive = step.id === currentStep;
              const isCompleted = step.id < currentStep;
              const StepIcon = step.icon;
              
              return (
                <div key={step.id} className="relative z-10 flex flex-col items-center">
                  <div 
                    className={`w-10 h-10 rounded-full flex items-center justify-center transition-all duration-500 shadow-sm ${
                      isActive 
                        ? 'bg-blue-600 text-white ring-4 ring-blue-50 scale-110 shadow-lg' 
                        : isCompleted 
                        ? 'bg-emerald-500 text-white' 
                        : 'bg-white border-2 border-slate-100 text-slate-300'
                    }`}
                  >
                    {isCompleted ? <CheckCircle2 size={18} strokeWidth={3} /> : <StepIcon size={18} />}
                  </div>
                  <span className={`mt-3 text-[10px] md:text-xs font-bold ${
                    isActive ? 'text-blue-700' : 'text-slate-400'
                  }`}>
                    {step.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        <div className="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden mb-8">
          <div className="p-6 md:p-10 min-h-[400px]">
            {renderStepContent()}
          </div>
          
          <div className="bg-slate-50 border-t border-slate-100 p-6 flex justify-between items-center">
            <button 
              type="button"
              onClick={handleBack}
              disabled={currentStep === 1 || step2AwaitingPdf}
              className={`flex items-center gap-2 px-6 py-2 rounded-xl font-bold transition-all text-sm ${
                currentStep === 1 || step2AwaitingPdf
                ? 'opacity-0 cursor-default pointer-events-none'
                : 'text-slate-500 hover:bg-slate-200 active:scale-95'
              }`}
            >
              <ChevronRight size={18} className={language === 'he' || language === 'ar' ? '' : 'rotate-180'} />
              {t.back}
            </button>

            {currentStep < 3 && (
              <button 
                type="button"
                onClick={handleNext}
                disabled={step2AwaitingPdf}
                className={`bg-blue-600 text-white px-8 py-2 rounded-xl font-bold hover:bg-blue-700 transition-all flex items-center gap-2 shadow-lg shadow-blue-100 active:scale-95 ${
                  step2AwaitingPdf ? 'opacity-50 cursor-wait' : ''
                }`}
              >
                {currentStep === 2 ? t.payment : t.next}
                <ChevronLeft size={18} className={language === 'he' || language === 'ar' ? '' : 'rotate-180'} />
              </button>
            )}
          </div>
        </div>
      </main>

      <footer className="max-w-4xl mx-auto text-center text-slate-300 text-[10px] font-bold tracking-widest uppercase">
        Digital Services • V3.5
      </footer>
    </div>
  );
};

export default App;