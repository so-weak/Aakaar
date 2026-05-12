// i18n label table.
//
// Six languages: English plus the same Sanskrit-rooted vocabulary in five
// Indian scripts. The "label" you see in the UI is whichever translation
// the active language resolves to.
//
// English is *demystified* — plain product nouns (Tenant, Run, Workflow).
// The other five carry the mythic vocabulary in their script.

export const LANG_CODES = [
  "hi-Latn",
  "en",
  "hi-Deva",
  "bn",
  "ta",
  "kn",
] as const;

export type LangCode = (typeof LANG_CODES)[number];

export interface LangMeta {
  code: LangCode;
  /** Short label rendered in the language switcher trigger. */
  label: string;
  /** Native label rendered inside the dropdown row. */
  nativeLabel: string;
  /** Two- or three-character "chip" used as the trigger icon. */
  chip: string;
}

export const LANGUAGES: readonly LangMeta[] = [
  { code: "hi-Latn", label: "Hindi (Latin)", nativeLabel: "Hindi (Latin)", chip: "Aa" },
  { code: "en",      label: "English",       nativeLabel: "English",        chip: "En" },
  { code: "hi-Deva", label: "Hindi",         nativeLabel: "हिन्दी",           chip: "हि" },
  { code: "bn",      label: "Bengali",       nativeLabel: "বাংলা",           chip: "বা" },
  { code: "ta",      label: "Tamil",         nativeLabel: "தமிழ்",           chip: "த" },
  { code: "kn",      label: "Kannada",       nativeLabel: "ಕನ್ನಡ",           chip: "ಕ" },
] as const;

export const DEFAULT_LANG: LangCode = "hi-Latn";

// ---------------------------------------------------------------------------
// label keys
// ---------------------------------------------------------------------------

export type LabelKey =
  // roles
  | "pracharya"
  | "acharya"
  | "sadhaka"
  | "sadhakas"
  // domain / circle
  | "mandala"
  | "mandalas"
  // catalog
  | "vidya"
  | "vidyas"
  // execution
  | "sutra"
  | "sutras"
  | "yajna"
  | "yajnas"
  | "runYajna"
  | "yantra"
  // chat
  | "samvada"
  | "samvadas"
  | "vachana"
  | "sankalpa"
  // storage / audit
  | "kosha"
  | "sakshi"
  | "adhikara"
  // surfaces
  | "darshana"
  | "pratyaksha"
  // threshold
  | "pravesha"
  | "praveshing"
  | "nirgama"
  // status header
  | "status"
  // run statuses
  | "queued"
  | "running"
  | "paused"
  | "succeeded"
  | "failed"
  | "cancelled";

export type LabelMap = Record<LabelKey, string>;

// ---------------------------------------------------------------------------
// translations
// ---------------------------------------------------------------------------

const T: Record<LabelKey, Record<LangCode, string>> = {
  // roles --------------------------------------------------------------
  pracharya: {
    en: "Principal",
    "hi-Latn": "Pracharya",
    "hi-Deva": "प्राचार्य",
    bn: "প্রাচার্য",
    ta: "ப்ராசார்யா",
    kn: "ಪ್ರಾಚಾರ್ಯ",
  },
  acharya: {
    en: "Admin",
    "hi-Latn": "Acharya",
    "hi-Deva": "आचार्य",
    bn: "আচার্য",
    ta: "ஆசாரியர்",
    kn: "ಆಚಾರ್ಯ",
  },
  sadhaka: {
    en: "User",
    "hi-Latn": "Sadhaka",
    "hi-Deva": "साधक",
    bn: "সাধক",
    ta: "சாதகர்",
    kn: "ಸಾಧಕ",
  },
  sadhakas: {
    en: "Users",
    "hi-Latn": "Sadhakas",
    "hi-Deva": "साधक",
    bn: "সাধকগণ",
    ta: "சாதகர்கள்",
    kn: "ಸಾಧಕರು",
  },

  // domain / circle ----------------------------------------------------
  mandala: {
    en: "Tenant",
    "hi-Latn": "Mandala",
    "hi-Deva": "मण्डल",
    bn: "মণ্ডল",
    ta: "மண்டலம்",
    kn: "ಮಂಡಲ",
  },
  mandalas: {
    en: "Tenants",
    "hi-Latn": "Mandalas",
    "hi-Deva": "मण्डल",
    bn: "মণ্ডলসমূহ",
    ta: "மண்டலங்கள்",
    kn: "ಮಂಡಲಗಳು",
  },

  // catalog ------------------------------------------------------------
  vidya: {
    en: "Capability",
    "hi-Latn": "Vidya",
    "hi-Deva": "विद्या",
    bn: "বিদ্যা",
    ta: "வித்யை",
    kn: "ವಿದ್ಯೆ",
  },
  vidyas: {
    en: "Capabilities",
    "hi-Latn": "Vidyas",
    "hi-Deva": "विद्याएँ",
    bn: "বিদ্যাসমূহ",
    ta: "வித்யைகள்",
    kn: "ವಿದ್ಯೆಗಳು",
  },

  // execution ----------------------------------------------------------
  sutra: {
    en: "Workflow",
    "hi-Latn": "Sutra",
    "hi-Deva": "सूत्र",
    bn: "সূত্র",
    ta: "சூத்திரம்",
    kn: "ಸೂತ್ರ",
  },
  sutras: {
    en: "Workflows",
    "hi-Latn": "Sutras",
    "hi-Deva": "सूत्र",
    bn: "সূত্রসমূহ",
    ta: "சூத்திரங்கள்",
    kn: "ಸೂತ್ರಗಳು",
  },
  yajna: {
    en: "Run",
    "hi-Latn": "Yajna",
    "hi-Deva": "यज्ञ",
    bn: "যজ্ঞ",
    ta: "யக்ஞம்",
    kn: "ಯಜ್ಞ",
  },
  yajnas: {
    en: "Runs",
    "hi-Latn": "Yajnas",
    "hi-Deva": "यज्ञ",
    bn: "যজ্ঞসমূহ",
    ta: "யக்ஞங்கள்",
    kn: "ಯಜ್ಞಗಳು",
  },
  // Action label for the "start a run" button. In English the verb and
  // noun collapse to the same word ("Run") so we don't repeat it; the
  // other languages keep the verb in English and the noun localised so
  // the button is unambiguously an action.
  runYajna: {
    en: "Run",
    "hi-Latn": "Run Yajna",
    "hi-Deva": "यज्ञ चलाएँ",
    bn: "যজ্ঞ চালান",
    ta: "யக்ஞம் தொடங்கு",
    kn: "ಯಜ್ಞ ಆರಂಭಿಸಿ",
  },
  yantra: {
    en: "Plan",
    "hi-Latn": "Yantra",
    "hi-Deva": "यंत्र",
    bn: "যন্ত্র",
    ta: "யந்திரம்",
    kn: "ಯಂತ್ರ",
  },

  // chat ---------------------------------------------------------------
  samvada: {
    en: "Chat",
    "hi-Latn": "Samvada",
    "hi-Deva": "संवाद",
    bn: "সংবাদ",
    ta: "சம்வாதம்",
    kn: "ಸಂವಾದ",
  },
  samvadas: {
    en: "Chats",
    "hi-Latn": "Samvadas",
    "hi-Deva": "संवाद",
    bn: "সংবাদসমূহ",
    ta: "சம்வாதங்கள்",
    kn: "ಸಂವಾದಗಳು",
  },
  vachana: {
    en: "Message",
    "hi-Latn": "Vachana",
    "hi-Deva": "वचन",
    bn: "বচন",
    ta: "வசனம்",
    kn: "ವಚನ",
  },
  sankalpa: {
    en: "Prompt",
    "hi-Latn": "Sankalpa",
    "hi-Deva": "संकल्प",
    bn: "সংকল্প",
    ta: "சங்கல்பம்",
    kn: "ಸಂಕಲ್ಪ",
  },

  // storage / audit ----------------------------------------------------
  kosha: {
    en: "Vault",
    "hi-Latn": "Kosha",
    "hi-Deva": "कोश",
    bn: "কোশ",
    ta: "கோசம்",
    kn: "ಕೋಶ",
  },
  sakshi: {
    en: "Audit",
    "hi-Latn": "Sakshi",
    "hi-Deva": "साक्षी",
    bn: "সাক্ষী",
    ta: "சாக்ஷி",
    kn: "ಸಾಕ್ಷಿ",
  },
  adhikara: {
    en: "Grant",
    "hi-Latn": "Adhikara",
    "hi-Deva": "अधिकार",
    bn: "অধিকার",
    ta: "அதிகாரம்",
    kn: "ಅಧಿಕಾರ",
  },

  // surfaces -----------------------------------------------------------
  darshana: {
    en: "Dashboard",
    "hi-Latn": "Darshana",
    "hi-Deva": "दर्शन",
    bn: "দর্শন",
    ta: "தர்சனம்",
    kn: "ದರ್ಶನ",
  },
  pratyaksha: {
    en: "Live",
    "hi-Latn": "Pratyaksha",
    "hi-Deva": "प्रत्यक्ष",
    bn: "প্রত্যক্ষ",
    ta: "ப்ரத்யக்ஷம்",
    kn: "ಪ್ರತ್ಯಕ್ಷ",
  },

  // threshold ----------------------------------------------------------
  pravesha: {
    en: "Sign in",
    "hi-Latn": "Pravesha",
    "hi-Deva": "प्रवेश",
    bn: "প্রবেশ",
    ta: "ப்ரவேசம்",
    kn: "ಪ್ರವೇಶ",
  },
  praveshing: {
    en: "Signing in…",
    "hi-Latn": "Entering…",
    "hi-Deva": "प्रवेश हो रहा है…",
    bn: "প্রবেশ হচ্ছে…",
    ta: "உள்நுழைகிறது…",
    kn: "ಪ್ರವೇಶಿಸುತ್ತಿದೆ…",
  },
  nirgama: {
    en: "Log out",
    "hi-Latn": "Nirgama",
    "hi-Deva": "निर्गम",
    bn: "নির্গম",
    ta: "வெளியேறு",
    kn: "ನಿರ್ಗಮ",
  },

  // status header ------------------------------------------------------
  status: {
    en: "Status",
    "hi-Latn": "Avastha",
    "hi-Deva": "अवस्था",
    bn: "অবস্থা",
    ta: "நிலை",
    kn: "ಸ್ಥಿತಿ",
  },

  // run statuses (badge labels) ----------------------------------------
  queued: {
    en: "queued",
    "hi-Latn": "Pratiksha",
    "hi-Deva": "प्रतीक्षा",
    bn: "প্রতীক্ষা",
    ta: "ப்ரதீக்ஷை",
    kn: "ಪ್ರತೀಕ್ಷೆ",
  },
  running: {
    en: "running",
    "hi-Latn": "Pravriti",
    "hi-Deva": "प्रवृत्ति",
    bn: "প্রবৃত্তি",
    ta: "ப்ரவ்ருத்தி",
    kn: "ಪ್ರವೃತ್ತಿ",
  },
  paused: {
    en: "paused",
    "hi-Latn": "Aahvaana",
    "hi-Deva": "आह्वान",
    bn: "আহ্বান",
    ta: "ஆஹ்வானம்",
    kn: "ಆಹ್ವಾನ",
  },
  succeeded: {
    en: "succeeded",
    "hi-Latn": "Siddha",
    "hi-Deva": "सिद्ध",
    bn: "সিদ্ধ",
    ta: "ஸித்த",
    kn: "ಸಿದ್ಧ",
  },
  failed: {
    en: "failed",
    "hi-Latn": "Vighna",
    "hi-Deva": "विघ्न",
    bn: "বিঘ্ন",
    ta: "விக்னம்",
    kn: "ವಿಘ್ನ",
  },
  cancelled: {
    en: "cancelled",
    "hi-Latn": "Tyaaga",
    "hi-Deva": "त्याग",
    bn: "ত্যাগ",
    ta: "தியாகம்",
    kn: "ತ್ಯಾಗ",
  },
};

/** Resolve the full label map for a language. */
export function labelsFor(lang: LangCode): LabelMap {
  const out = {} as LabelMap;
  (Object.keys(T) as LabelKey[]).forEach((key) => {
    out[key] = T[key][lang];
  });
  return out;
}

// status keys map 1:1 onto API run.status strings (lowercase English)
export const RUN_STATUS_TO_LABEL_KEY: Record<string, LabelKey> = {
  queued: "queued",
  running: "running",
  paused: "paused",
  succeeded: "succeeded",
  failed: "failed",
  cancelled: "cancelled",
};
