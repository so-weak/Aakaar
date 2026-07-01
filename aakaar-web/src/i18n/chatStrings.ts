// Chat surface strings — full i18n for the rebuilt chat window.
//
// The flat label table in labels.ts holds the app's mythic *vocabulary* nouns
// (Sutra, Yantra, Samvada …). This module holds the chat window's sentences,
// hints, prompt-starters, dialog copy, and aria announcements — content that is
// too large and too structured (arrays, {n} templates) for that flat map.
//
// English is demystified (plain: workflow / plan / chat / run). The other five
// carry the same mythic vocabulary already used across the app, in their script.
// Placeholders: {n} counts, {x} an example step, {v} a version number.

import { useLang } from "@/i18n/LanguageProvider";
import type { LangCode } from "@/i18n/labels";

export interface Starter {
  title: string;
  prompt: string;
}

export interface ChatStrings {
  // shell / header / rail ------------------------------------------------
  newLabel: string;
  headerSubtitle: string;
  onePerAutomation: string;
  statusPlanning: string;
  statusSaved: string;
  statusUnsaved: string;
  statusRunning: string;
  noChatsYet: string;
  ariaHideConversations: string;
  ariaShowConversations: string;
  ariaHidePlan: string;
  ariaShowPlan: string;
  subNoPlan: string;
  subSteps: string; // "{n} steps in the current plan"
  loadingWorkspace: string;
  loadingChats: string;
  noActiveChat: string;

  // empty / starters -----------------------------------------------------
  startersHeading: string;
  startersIntro: string;
  tryExample: string;
  starters: Starter[];
  composerFootnote: string;

  // composer + palette ---------------------------------------------------
  placeholder: string;
  hint: string;
  advanced: string;
  ariaSend: string;
  ariaStop: string;
  paletteCommands: string;
  paletteInsertCap: string;
  paletteNoMatches: string;
  cmdRunSub: string;
  cmdSaveSub: string;
  cmdPlanSub: string;

  // activity strip -------------------------------------------------------
  draftingPlan: string;
  runningWith: string; // "Running · {x} · {n}/{total}"
  runningCount: string; // "Running · {n}/{total} steps"
  runState: string; // "Run {status} · {n}/{total} steps"
  openLive: string;

  // dag bubble -----------------------------------------------------------
  drafted: string; // "I drafted a {n}-step workflow"
  plannerReasoning: string;
  liveAction: string;
  makerChecker: string; // "This plan has live actions (e.g. {x}). Running it may need a checker's approval."
  reviewPlan: string;
  runDraft: string;
  copy: string;
  copied: string;
  showFewer: string;
  moreSteps: string; // "+{n} more steps"
  buildingPlan: string;

  // clarify --------------------------------------------------------------
  clarifyHeading: string;
  typeAnswer: string;
  sendAnswers: string;
  answerAll: string; // "Answer all {n} to continue"
  answersSent: string;

  // missing --------------------------------------------------------------
  missingHeading: string;
  clickToCopy: string;
  openAccess: string;

  // error ----------------------------------------------------------------
  errorHeading: string;
  errorFootnote: string;
  errRate: string;
  errAuth: string;
  errPlanner: string;
  errServer: string;
  errValidation: string;
  errNetwork: string;
  errGeneric: string;

  // approval -------------------------------------------------------------
  approvalHeading: string;
  approvalBody: string;
  viewApprovals: string;

  // dock empty plan ------------------------------------------------------
  planAppearHeading: string;
  planAppearBody: string;

  // draft header ---------------------------------------------------------
  saveWorkflow: string;
  saveVersion: string; // "Save v{v}"
  savedVersion: string; // "Saved · v{v}"
  drift: string;
  workflowNamePlaceholder: string;
  ariaDelete: string;

  // save dialog ----------------------------------------------------------
  saveTitleFirst: string;
  saveTitleUpdate: string;
  saveDescFirst: string;
  saveDescUpdate: string; // "...v{v}."
  nameLabel: string;
  namePlaceholder: string;
  cancel: string;
  save: string;
  saving: string;
  confirmUpdate: string;

  // delete dialog --------------------------------------------------------
  deleteTitle: string;
  deleteBodyKept: string;
  deleteBodyLost: string;
  deleting: string;
  deleteConfirm: string;
  deleteFailed: string;

  // run dialog -----------------------------------------------------------
  runTitle: string;
  runDesc: string; // "Running v{v} from the planner."
  runsOn: string;
  runsOnServer: string;
  modeLabel: string;
  modeLive: string;
  modeLiveSub: string;
  modeDry: string;
  modeDrySub: string;
  execInputs: string;
  startRun: string;
  starting: string;

  // aria-live announcements ---------------------------------------------
  annPlanReady: string; // "Plan ready with {n} steps."
  annReplied: string;
  annStopped: string;
  annCouldntBuild: string;
  annRunStarted: string;
  annInvalidJson: string;
  annNeedsApproval: string;
}

// ---------------------------------------------------------------------------
// English (demystified — the source of truth)
// ---------------------------------------------------------------------------

const en: ChatStrings = {
  newLabel: "New",
  headerSubtitle:
    "Describe an outcome; the planner turns it into a workflow. Persists across reloads.",
  onePerAutomation: "One per automation",
  statusPlanning: "Planning",
  statusSaved: "Saved workflow",
  statusUnsaved: "Unsaved changes",
  statusRunning: "Running now",
  noChatsYet: "No chats yet.",
  ariaHideConversations: "Hide conversations",
  ariaShowConversations: "Show conversations",
  ariaHidePlan: "Hide plan panel",
  ariaShowPlan: "Show plan panel",
  subNoPlan: "Tell the planner what you want to accomplish",
  subSteps: "{n} step(s) in the current plan",
  loadingWorkspace: "Loading workspace…",
  loadingChats: "Loading chats…",
  noActiveChat: "No active chat.",

  startersHeading: "What would you like to automate?",
  startersIntro:
    "Explain it as you would to a teammate. Start with the outcome; the planner will ask for any details it needs.",
  tryExample: "Try an example",
  starters: [
    {
      title: "Download and file a report",
      prompt:
        "Every weekday morning, download the previous day's settlement report and save it in the finance folder.",
    },
    {
      title: "Fill the visible form",
      prompt:
        "On the workstation screen, fill Customer Name with Aarya Traders, Account Number with 12345, and Amount with 1000. Then press Enter.",
    },
    {
      title: "Monitor a workstation",
      prompt:
        "Check CPU, memory, and disk usage on the remote workstation. Alert me when any value crosses 85%.",
    },
    {
      title: "Add an approval step",
      prompt:
        "Prepare disputed transactions for submission, show them to an operator for approval, then submit only the approved items.",
    },
    {
      title: "Route by condition",
      prompt:
        "If the downloaded file is empty, alert me and stop. Otherwise upload it to the portal and confirm success.",
    },
  ],
  composerFootnote:
    "Plain language is enough. Say what you see, what should be filled, and what happens next.",

  placeholder: "Tell me what should happen, in your own words…",
  hint: "Enter to send · Shift+Enter for a new line",
  advanced: "Advanced",
  ariaSend: "Send message",
  ariaStop: "Stop planning",
  paletteCommands: "Commands",
  paletteInsertCap: "Insert a capability",
  paletteNoMatches: "No matches",
  cmdRunSub: "Save (if needed) and run this workflow",
  cmdSaveSub: "Save the current plan as a workflow",
  cmdPlanSub: "Open the plan view",

  draftingPlan: "Drafting the plan…",
  runningWith: "Running · {x} · {n}/{total}",
  runningCount: "Running · {n}/{total} steps",
  runState: "Run {status} · {n}/{total} steps",
  openLive: "Open Live →",

  drafted: "I drafted a {n}-step workflow",
  plannerReasoning: "Planner reasoning",
  liveAction: "live action",
  makerChecker:
    "This plan has live actions (e.g. {x}). Running it may need a checker's approval.",
  reviewPlan: "Review plan",
  runDraft: "Run draft",
  copy: "Copy",
  copied: "Copied",
  showFewer: "Show fewer steps",
  moreSteps: "+{n} more step(s)",
  buildingPlan: "Building your plan…",

  clarifyHeading: "A few details will help",
  typeAnswer: "Type your answer…",
  sendAnswers: "Send answers",
  answerAll: "Answer all {n} to continue",
  answersSent: "Answers sent",

  missingHeading: "I need access to a tool",
  clickToCopy: "Click to copy",
  openAccess: "Open access settings",

  errorHeading: "I couldn't build the plan",
  errorFootnote: "Your message is back in the composer — edit and try again.",
  errRate: "You're sending messages too quickly. Wait a moment and try again.",
  errAuth: "Your session may have expired. Refresh the page and try again.",
  errPlanner:
    "The planning service is temporarily unavailable. Try again — or rephrase your request if it persists.",
  errServer: "Something went wrong on the server. Please try again.",
  errValidation: "Your request couldn't be processed. Try rephrasing it more clearly.",
  errNetwork: "Network error — check your connection and try again.",
  errGeneric: "Something went wrong. Try rephrasing or simplifying your request.",

  approvalHeading: "This run needs a checker",
  approvalBody:
    "A maker-checker approval was opened. Another admin must approve it before the run starts.",
  viewApprovals: "View approvals",

  planAppearHeading: "Your plan will appear here",
  planAppearBody:
    "Describe the result you want in chat. The planner will ask for missing details and build it.",

  saveWorkflow: "Save workflow",
  saveVersion: "Save v{v}",
  savedVersion: "Saved · v{v}",
  drift: "drift",
  workflowNamePlaceholder: "Workflow name",
  ariaDelete: "Delete conversation",

  saveTitleFirst: "Save workflow",
  saveTitleUpdate: "Save a new workflow version",
  saveDescFirst:
    "Give this plan a clear name so your team can find and run it later.",
  saveDescUpdate:
    "This preserves the current workflow and creates version v{v}.",
  nameLabel: "Workflow name",
  namePlaceholder: "e.g. Daily settlement download",
  cancel: "Cancel",
  save: "Save",
  saving: "Saving…",
  confirmUpdate: "Confirm update",

  deleteTitle: "Delete conversation?",
  deleteBodyKept: "The saved workflow will remain available in Workflows.",
  deleteBodyLost: "Any unsaved plan in this conversation will also be lost.",
  deleting: "Deleting…",
  deleteConfirm: "Delete conversation",
  deleteFailed: "The conversation could not be deleted.",

  runTitle: "Run workflow",
  runDesc: "Running v{v} from the planner.",
  runsOn: "Runs on",
  runsOnServer: "No workstation steps — this runs entirely on the Aakaar server.",
  modeLabel: "Mode",
  modeLive: "Live",
  modeLiveSub: "Perform every step for real",
  modeDry: "Dry run",
  modeDrySub: "Simulate side-effecting steps",
  execInputs: "Execution inputs (JSON)",
  startRun: "Start run",
  starting: "Starting…",

  annPlanReady: "Plan ready with {n} steps.",
  annReplied: "The planner replied.",
  annStopped: "Planning stopped.",
  annCouldntBuild: "The planner couldn't build the plan.",
  annRunStarted: "Run started. Watch progress in the Live tab.",
  annInvalidJson: "Execution inputs must be valid JSON.",
  annNeedsApproval: "This run needs a checker's approval before it starts.",
};

// ---------------------------------------------------------------------------
// Hindi (Latin) — romanized, carries the mythic vocabulary
// ---------------------------------------------------------------------------

const hiLatn: ChatStrings = {
  newLabel: "Naya",
  headerSubtitle:
    "Parinaam bataiye; planner use Sutra mein badal dega. Reload ke baad bhi bana rehta hai.",
  onePerAutomation: "Har automation ke liye ek",
  statusPlanning: "Yojana ban rahi hai",
  statusSaved: "Sahejaa gaya Sutra",
  statusUnsaved: "Asahaje badlav",
  statusRunning: "Abhi chal raha hai",
  noChatsYet: "Abhi koi Samvada nahin.",
  ariaHideConversations: "Samvada chhipaayein",
  ariaShowConversations: "Samvada dikhaayein",
  ariaHidePlan: "Yantra panel chhipaayein",
  ariaShowPlan: "Yantra panel dikhaayein",
  subNoPlan: "Planner ko bataiye aap kya karna chahte hain",
  subSteps: "vartamaan Yantra mein {n} charan",
  loadingWorkspace: "Workspace lod ho raha hai…",
  loadingChats: "Samvada lod ho rahe hain…",
  noActiveChat: "Koi sakriya Samvada nahin.",

  startersHeading: "Aap kya automate karna chahenge?",
  startersIntro:
    "Jaise ek saathi ko batayenge waise samjhaaiye. Parinaam se shuru karein; planner zaroori vivaran maang lega.",
  tryExample: "Ek udaharan aazmaayein",
  starters: [
    {
      title: "Report download karke sahejein",
      prompt:
        "Har kaam ke din subah, pichhle din ki settlement report download karein aur finance folder mein sahejein.",
    },
    {
      title: "Dikhta hua form bharein",
      prompt:
        "Workstation screen par Customer Name mein Aarya Traders, Account Number mein 12345, aur Amount mein 1000 bharein. Phir Enter dabaayein.",
    },
    {
      title: "Workstation par nazar rakhein",
      prompt:
        "Remote workstation ka CPU, memory aur disk upyog jaanchein. Koi bhi maan 85% paar kare to mujhe sachet karein.",
    },
    {
      title: "Anumodan charan jodein",
      prompt:
        "Vivaadit transactions jama karne ke liye taiyaar karein, ek operator ko anumodan ke liye dikhaayein, phir keval anumodit item jama karein.",
    },
    {
      title: "Sharat ke anusaar rah chunein",
      prompt:
        "Agar download ki gayi file khaali hai to mujhe sachet karke ruk jaayein. Warna use portal par upload karke safalta pushti karein.",
    },
  ],
  composerFootnote:
    "Saral bhaasha kaafi hai. Bataiye aap kya dekhte hain, kya bharna hai, aur aage kya hoga.",

  placeholder: "Apne shabdon mein bataiye kya hona chahiye…",
  hint: "Bhejne ke liye Enter · nayi line ke liye Shift+Enter",
  advanced: "Unnat",
  ariaSend: "Sandesh bhejein",
  ariaStop: "Yojana roken",
  paletteCommands: "Command",
  paletteInsertCap: "Vidya jodein",
  paletteNoMatches: "Koi milaan nahin",
  cmdRunSub: "Sahejkar (yadi zaroori ho) is Sutra ko chalaayein",
  cmdSaveSub: "Vartamaan Yantra ko Sutra ke roop mein sahejein",
  cmdPlanSub: "Yantra drishya kholein",

  draftingPlan: "Yojana taiyaar ho rahi hai…",
  runningWith: "Chal raha · {x} · {n}/{total}",
  runningCount: "Chal raha · {n}/{total} charan",
  runState: "Yajna {status} · {n}/{total} charan",
  openLive: "Pratyaksha kholein →",

  drafted: "Maine {n}-charan ka Sutra banaya",
  plannerReasoning: "Planner ka tark",
  liveAction: "sajeev kriya",
  makerChecker:
    "Is Yantra mein sajeev kriyaayein hain (jaise {x}). Ise chalane ke liye checker ka anumodan zaroori ho sakta hai.",
  reviewPlan: "Yantra dekhein",
  runDraft: "Draft chalaayein",
  copy: "Copy",
  copied: "Copy ho gaya",
  showFewer: "Kam charan dikhaayein",
  moreSteps: "+{n} aur charan",
  buildingPlan: "Aapki yojana ban rahi hai…",

  clarifyHeading: "Kuchh vivaran madad karenge",
  typeAnswer: "Apna uttar likhein…",
  sendAnswers: "Uttar bhejein",
  answerAll: "Aage badhne ke liye sabhi {n} ka uttar dein",
  answersSent: "Uttar bhej diye gaye",

  missingHeading: "Mujhe ek tool ka adhikar chahiye",
  clickToCopy: "Copy karne ke liye click karein",
  openAccess: "Adhikar settings kholein",

  errorHeading: "Main yojana nahin bana saka",
  errorFootnote: "Aapka sandesh composer mein wapas hai — badalkar phir se try karein.",
  errRate: "Aap bahut tezi se sandesh bhej rahe hain. Thoda ruk kar phir try karein.",
  errAuth: "Aapka session samaapt ho sakta hai. Page refresh karke phir try karein.",
  errPlanner:
    "Planning seva asthaayi roop se uplabdh nahin hai. Phir try karein — ya samasya bani rahe to apna anurodh dobara likhein.",
  errServer: "Server par kuchh galat hua. Kripya phir try karein.",
  errValidation: "Aapka anurodh process nahin ho saka. Ise saaf shabdon mein likhein.",
  errNetwork: "Network truti — apna connection jaanchkar phir try karein.",
  errGeneric: "Kuchh galat hua. Anurodh ko dobara likhein ya saral banaayein.",

  approvalHeading: "Is Yajna ke liye checker zaroori",
  approvalBody:
    "Ek maker-checker anumodan khola gaya. Yajna shuru hone se pehle doosre admin ko ise anumodit karna hoga.",
  viewApprovals: "Anumodan dekhein",

  planAppearHeading: "Aapka Yantra yahan dikhega",
  planAppearBody:
    "Chat mein manchaaha parinaam bataiye. Planner chhoote vivaran maangkar ise banayega.",

  saveWorkflow: "Sutra sahejein",
  saveVersion: "v{v} sahejein",
  savedVersion: "Saheja · v{v}",
  drift: "drift",
  workflowNamePlaceholder: "Sutra ka naam",
  ariaDelete: "Samvada mitaayein",

  saveTitleFirst: "Sutra sahejein",
  saveTitleUpdate: "Naya Sutra sanskaran sahejein",
  saveDescFirst:
    "Is Yantra ko spasht naam dein taaki aapki team ise baad mein dhoondh aur chala sake.",
  saveDescUpdate: "Yah vartamaan Sutra ko surakshit rakhkar sanskaran v{v} banata hai.",
  nameLabel: "Sutra ka naam",
  namePlaceholder: "jaise Dainik settlement download",
  cancel: "Radd karein",
  save: "Sahejein",
  saving: "Sahej rahe hain…",
  confirmUpdate: "Update pushti karein",

  deleteTitle: "Samvada mitaayein?",
  deleteBodyKept: "Saheja gaya Sutra Workflows mein uplabdh rahega.",
  deleteBodyLost: "Is Samvada ka koi bhi asahaje Yantra bhi kho jaayega.",
  deleting: "Mitaaya ja raha hai…",
  deleteConfirm: "Samvada mitaayein",
  deleteFailed: "Samvada mitaaya nahin ja saka.",

  runTitle: "Sutra chalaayein",
  runDesc: "Planner se v{v} chalaya ja raha hai.",
  runsOn: "Ispar chalega",
  runsOnServer: "Koi workstation charan nahin — yah poori tarah Aakaar server par chalega.",
  modeLabel: "Mode",
  modeLive: "Sajeev",
  modeLiveSub: "Har charan vaastav mein karein",
  modeDry: "Dry run",
  modeDrySub: "Prabhaav-daalne waale charan simulate karein",
  execInputs: "Execution inputs (JSON)",
  startRun: "Yajna shuru karein",
  starting: "Shuru ho raha hai…",

  annPlanReady: "{n} charan ke saath yojana taiyaar.",
  annReplied: "Planner ne uttar diya.",
  annStopped: "Yojana rok di gayi.",
  annCouldntBuild: "Planner yojana nahin bana saka.",
  annRunStarted: "Yajna shuru. Pratyaksha tab mein pragati dekhein.",
  annInvalidJson: "Execution inputs valid JSON hone chahiye.",
  annNeedsApproval: "Shuru hone se pehle is Yajna ko checker ke anumodan ki zaroorat hai.",
};

// ---------------------------------------------------------------------------
// Hindi (Devanagari)
// ---------------------------------------------------------------------------

const hiDeva: ChatStrings = {
  newLabel: "नया",
  headerSubtitle:
    "परिणाम बताइए; प्लानर उसे सूत्र में बदल देगा। रीलोड के बाद भी बना रहता है।",
  onePerAutomation: "हर ऑटोमेशन के लिए एक",
  statusPlanning: "योजना बन रही है",
  statusSaved: "सहेजा गया सूत्र",
  statusUnsaved: "असहेजे बदलाव",
  statusRunning: "अभी चल रहा है",
  noChatsYet: "अभी कोई संवाद नहीं।",
  ariaHideConversations: "संवाद छिपाएँ",
  ariaShowConversations: "संवाद दिखाएँ",
  ariaHidePlan: "यंत्र पैनल छिपाएँ",
  ariaShowPlan: "यंत्र पैनल दिखाएँ",
  subNoPlan: "प्लानर को बताइए आप क्या करना चाहते हैं",
  subSteps: "वर्तमान यंत्र में {n} चरण",
  loadingWorkspace: "वर्कस्पेस लोड हो रहा है…",
  loadingChats: "संवाद लोड हो रहे हैं…",
  noActiveChat: "कोई सक्रिय संवाद नहीं।",

  startersHeading: "आप क्या स्वचालित करना चाहेंगे?",
  startersIntro:
    "जैसे किसी साथी को बताएँगे वैसे समझाइए। परिणाम से शुरू करें; प्लानर ज़रूरी विवरण माँग लेगा।",
  tryExample: "एक उदाहरण आज़माएँ",
  starters: [
    {
      title: "रिपोर्ट डाउनलोड करके सहेजें",
      prompt:
        "हर कार्यदिवस सुबह, पिछले दिन की सेटलमेंट रिपोर्ट डाउनलोड करें और फ़ाइनेंस फ़ोल्डर में सहेजें।",
    },
    {
      title: "दिखता हुआ फ़ॉर्म भरें",
      prompt:
        "वर्कस्टेशन स्क्रीन पर Customer Name में Aarya Traders, Account Number में 12345, और Amount में 1000 भरें। फिर Enter दबाएँ।",
    },
    {
      title: "वर्कस्टेशन पर नज़र रखें",
      prompt:
        "रिमोट वर्कस्टेशन का CPU, मेमोरी और डिस्क उपयोग जाँचें। कोई भी मान 85% पार करे तो मुझे सचेत करें।",
    },
    {
      title: "अनुमोदन चरण जोड़ें",
      prompt:
        "विवादित ट्रांज़ैक्शन जमा करने के लिए तैयार करें, एक ऑपरेटर को अनुमोदन के लिए दिखाएँ, फिर केवल अनुमोदित आइटम जमा करें।",
    },
    {
      title: "शर्त के अनुसार राह चुनें",
      prompt:
        "अगर डाउनलोड की गई फ़ाइल खाली है तो मुझे सचेत करके रुक जाएँ। वरना उसे पोर्टल पर अपलोड करके सफलता पुष्टि करें।",
    },
  ],
  composerFootnote:
    "सरल भाषा काफ़ी है। बताइए आप क्या देखते हैं, क्या भरना है, और आगे क्या होगा।",

  placeholder: "अपने शब्दों में बताइए क्या होना चाहिए…",
  hint: "भेजने के लिए Enter · नई लाइन के लिए Shift+Enter",
  advanced: "उन्नत",
  ariaSend: "संदेश भेजें",
  ariaStop: "योजना रोकें",
  paletteCommands: "कमांड",
  paletteInsertCap: "विद्या जोड़ें",
  paletteNoMatches: "कोई मिलान नहीं",
  cmdRunSub: "सहेजकर (यदि ज़रूरी हो) इस सूत्र को चलाएँ",
  cmdSaveSub: "वर्तमान यंत्र को सूत्र के रूप में सहेजें",
  cmdPlanSub: "यंत्र दृश्य खोलें",

  draftingPlan: "योजना तैयार हो रही है…",
  runningWith: "चल रहा · {x} · {n}/{total}",
  runningCount: "चल रहा · {n}/{total} चरण",
  runState: "यज्ञ {status} · {n}/{total} चरण",
  openLive: "प्रत्यक्ष खोलें →",

  drafted: "मैंने {n}-चरण का सूत्र बनाया",
  plannerReasoning: "प्लानर का तर्क",
  liveAction: "सजीव क्रिया",
  makerChecker:
    "इस यंत्र में सजीव क्रियाएँ हैं (जैसे {x})। इसे चलाने के लिए चेकर का अनुमोदन ज़रूरी हो सकता है।",
  reviewPlan: "यंत्र देखें",
  runDraft: "ड्राफ़्ट चलाएँ",
  copy: "कॉपी",
  copied: "कॉपी हो गया",
  showFewer: "कम चरण दिखाएँ",
  moreSteps: "+{n} और चरण",
  buildingPlan: "आपकी योजना बन रही है…",

  clarifyHeading: "कुछ विवरण मदद करेंगे",
  typeAnswer: "अपना उत्तर लिखें…",
  sendAnswers: "उत्तर भेजें",
  answerAll: "आगे बढ़ने के लिए सभी {n} का उत्तर दें",
  answersSent: "उत्तर भेज दिए गए",

  missingHeading: "मुझे एक टूल का अधिकार चाहिए",
  clickToCopy: "कॉपी करने के लिए क्लिक करें",
  openAccess: "अधिकार सेटिंग्स खोलें",

  errorHeading: "मैं योजना नहीं बना सका",
  errorFootnote: "आपका संदेश कम्पोज़र में वापस है — बदलकर फिर से आज़माएँ।",
  errRate: "आप बहुत तेज़ी से संदेश भेज रहे हैं। थोड़ा रुककर फिर आज़माएँ।",
  errAuth: "आपका सत्र समाप्त हो सकता है। पेज रीफ़्रेश करके फिर आज़माएँ।",
  errPlanner:
    "प्लानिंग सेवा अस्थायी रूप से उपलब्ध नहीं है। फिर आज़माएँ — या समस्या बनी रहे तो अपना अनुरोध दोबारा लिखें।",
  errServer: "सर्वर पर कुछ गलत हुआ। कृपया फिर आज़माएँ।",
  errValidation: "आपका अनुरोध प्रोसेस नहीं हो सका। इसे साफ़ शब्दों में लिखें।",
  errNetwork: "नेटवर्क त्रुटि — अपना कनेक्शन जाँचकर फिर आज़माएँ।",
  errGeneric: "कुछ गलत हुआ। अनुरोध को दोबारा लिखें या सरल बनाएँ।",

  approvalHeading: "इस यज्ञ के लिए चेकर ज़रूरी",
  approvalBody:
    "एक मेकर-चेकर अनुमोदन खोला गया। यज्ञ शुरू होने से पहले दूसरे एडमिन को इसे अनुमोदित करना होगा।",
  viewApprovals: "अनुमोदन देखें",

  planAppearHeading: "आपका यंत्र यहाँ दिखेगा",
  planAppearBody:
    "चैट में मनचाहा परिणाम बताइए। प्लानर छूटे विवरण माँगकर इसे बनाएगा।",

  saveWorkflow: "सूत्र सहेजें",
  saveVersion: "v{v} सहेजें",
  savedVersion: "सहेजा · v{v}",
  drift: "drift",
  workflowNamePlaceholder: "सूत्र का नाम",
  ariaDelete: "संवाद मिटाएँ",

  saveTitleFirst: "सूत्र सहेजें",
  saveTitleUpdate: "नया सूत्र संस्करण सहेजें",
  saveDescFirst:
    "इस यंत्र को स्पष्ट नाम दें ताकि आपकी टीम इसे बाद में ढूँढ और चला सके।",
  saveDescUpdate: "यह वर्तमान सूत्र को सुरक्षित रखकर संस्करण v{v} बनाता है।",
  nameLabel: "सूत्र का नाम",
  namePlaceholder: "जैसे दैनिक सेटलमेंट डाउनलोड",
  cancel: "रद्द करें",
  save: "सहेजें",
  saving: "सहेज रहे हैं…",
  confirmUpdate: "अपडेट पुष्टि करें",

  deleteTitle: "संवाद मिटाएँ?",
  deleteBodyKept: "सहेजा गया सूत्र Workflows में उपलब्ध रहेगा।",
  deleteBodyLost: "इस संवाद का कोई भी असहेजा यंत्र भी खो जाएगा।",
  deleting: "मिटाया जा रहा है…",
  deleteConfirm: "संवाद मिटाएँ",
  deleteFailed: "संवाद मिटाया नहीं जा सका।",

  runTitle: "सूत्र चलाएँ",
  runDesc: "प्लानर से v{v} चलाया जा रहा है।",
  runsOn: "इसपर चलेगा",
  runsOnServer: "कोई वर्कस्टेशन चरण नहीं — यह पूरी तरह Aakaar सर्वर पर चलेगा।",
  modeLabel: "मोड",
  modeLive: "सजीव",
  modeLiveSub: "हर चरण वास्तव में करें",
  modeDry: "ड्राई रन",
  modeDrySub: "प्रभाव-डालने वाले चरण सिमुलेट करें",
  execInputs: "Execution inputs (JSON)",
  startRun: "यज्ञ शुरू करें",
  starting: "शुरू हो रहा है…",

  annPlanReady: "{n} चरण के साथ योजना तैयार।",
  annReplied: "प्लानर ने उत्तर दिया।",
  annStopped: "योजना रोक दी गई।",
  annCouldntBuild: "प्लानर योजना नहीं बना सका।",
  annRunStarted: "यज्ञ शुरू। प्रत्यक्ष टैब में प्रगति देखें।",
  annInvalidJson: "Execution inputs मान्य JSON होने चाहिए।",
  annNeedsApproval: "शुरू होने से पहले इस यज्ञ को चेकर के अनुमोदन की ज़रूरत है।",
};

// ---------------------------------------------------------------------------
// Bengali
// ---------------------------------------------------------------------------

const bn: ChatStrings = {
  newLabel: "নতুন",
  headerSubtitle:
    "ফলাফল বলুন; পরিকল্পক সেটিকে সূত্রে রূপান্তরিত করবে। রিলোডের পরেও থাকে।",
  onePerAutomation: "প্রতি অটোমেশনে একটি",
  statusPlanning: "পরিকল্পনা চলছে",
  statusSaved: "সংরক্ষিত সূত্র",
  statusUnsaved: "অসংরক্ষিত পরিবর্তন",
  statusRunning: "এখন চলছে",
  noChatsYet: "এখনো কোনো সংবাদ নেই।",
  ariaHideConversations: "সংবাদ লুকান",
  ariaShowConversations: "সংবাদ দেখান",
  ariaHidePlan: "যন্ত্র প্যানেল লুকান",
  ariaShowPlan: "যন্ত্র প্যানেল দেখান",
  subNoPlan: "পরিকল্পককে বলুন আপনি কী করতে চান",
  subSteps: "বর্তমান যন্ত্রে {n} ধাপ",
  loadingWorkspace: "ওয়ার্কস্পেস লোড হচ্ছে…",
  loadingChats: "সংবাদ লোড হচ্ছে…",
  noActiveChat: "কোনো সক্রিয় সংবাদ নেই।",

  startersHeading: "আপনি কী স্বয়ংক্রিয় করতে চান?",
  startersIntro:
    "সহকর্মীকে যেমন বলবেন তেমন বোঝান। ফলাফল দিয়ে শুরু করুন; পরিকল্পক প্রয়োজনীয় বিবরণ চাইবে।",
  tryExample: "একটি উদাহরণ চেষ্টা করুন",
  starters: [
    {
      title: "রিপোর্ট ডাউনলোড করে সংরক্ষণ করুন",
      prompt:
        "প্রতি কর্মদিবস সকালে, আগের দিনের সেটেলমেন্ট রিপোর্ট ডাউনলোড করে ফাইন্যান্স ফোল্ডারে সংরক্ষণ করুন।",
    },
    {
      title: "দৃশ্যমান ফর্ম পূরণ করুন",
      prompt:
        "ওয়ার্কস্টেশন স্ক্রিনে Customer Name-এ Aarya Traders, Account Number-এ 12345, এবং Amount-এ 1000 পূরণ করুন। তারপর Enter চাপুন।",
    },
    {
      title: "ওয়ার্কস্টেশন পর্যবেক্ষণ করুন",
      prompt:
        "রিমোট ওয়ার্কস্টেশনের CPU, মেমরি ও ডিস্ক ব্যবহার পরীক্ষা করুন। কোনো মান 85% ছাড়ালে আমাকে সতর্ক করুন।",
    },
    {
      title: "একটি অনুমোদন ধাপ যোগ করুন",
      prompt:
        "বিতর্কিত লেনদেন জমার জন্য প্রস্তুত করুন, একজন অপারেটরকে অনুমোদনের জন্য দেখান, তারপর কেবল অনুমোদিত আইটেম জমা দিন।",
    },
    {
      title: "শর্ত অনুযায়ী পথ বেছে নিন",
      prompt:
        "ডাউনলোড করা ফাইল খালি হলে আমাকে সতর্ক করে থামুন। অন্যথায় সেটি পোর্টালে আপলোড করে সফলতা নিশ্চিত করুন।",
    },
  ],
  composerFootnote:
    "সহজ ভাষাই যথেষ্ট। বলুন আপনি কী দেখছেন, কী পূরণ হবে, এবং এরপর কী ঘটবে।",

  placeholder: "নিজের ভাষায় বলুন কী হওয়া উচিত…",
  hint: "পাঠাতে Enter · নতুন লাইনে Shift+Enter",
  advanced: "উন্নত",
  ariaSend: "বার্তা পাঠান",
  ariaStop: "পরিকল্পনা থামান",
  paletteCommands: "কমান্ড",
  paletteInsertCap: "বিদ্যা যোগ করুন",
  paletteNoMatches: "কোনো মিল নেই",
  cmdRunSub: "সংরক্ষণ করে (প্রয়োজনে) এই সূত্র চালান",
  cmdSaveSub: "বর্তমান যন্ত্রকে সূত্র হিসেবে সংরক্ষণ করুন",
  cmdPlanSub: "যন্ত্র দৃশ্য খুলুন",

  draftingPlan: "পরিকল্পনা প্রস্তুত হচ্ছে…",
  runningWith: "চলছে · {x} · {n}/{total}",
  runningCount: "চলছে · {n}/{total} ধাপ",
  runState: "যজ্ঞ {status} · {n}/{total} ধাপ",
  openLive: "প্রত্যক্ষ খুলুন →",

  drafted: "আমি {n}-ধাপের একটি সূত্র তৈরি করেছি",
  plannerReasoning: "পরিকল্পকের যুক্তি",
  liveAction: "সজীব ক্রিয়া",
  makerChecker:
    "এই যন্ত্রে সজীব ক্রিয়া আছে (যেমন {x})। এটি চালাতে একজন চেকারের অনুমোদন লাগতে পারে।",
  reviewPlan: "যন্ত্র দেখুন",
  runDraft: "খসড়া চালান",
  copy: "কপি",
  copied: "কপি হয়েছে",
  showFewer: "কম ধাপ দেখান",
  moreSteps: "+{n} আরও ধাপ",
  buildingPlan: "আপনার পরিকল্পনা তৈরি হচ্ছে…",

  clarifyHeading: "কিছু বিবরণ সাহায্য করবে",
  typeAnswer: "আপনার উত্তর লিখুন…",
  sendAnswers: "উত্তর পাঠান",
  answerAll: "এগিয়ে যেতে সব {n}টির উত্তর দিন",
  answersSent: "উত্তর পাঠানো হয়েছে",

  missingHeading: "আমার একটি টুলের অধিকার দরকার",
  clickToCopy: "কপি করতে ক্লিক করুন",
  openAccess: "অধিকার সেটিংস খুলুন",

  errorHeading: "আমি পরিকল্পনা তৈরি করতে পারিনি",
  errorFootnote: "আপনার বার্তা কম্পোজারে ফিরে এসেছে — সম্পাদনা করে আবার চেষ্টা করুন।",
  errRate: "আপনি খুব দ্রুত বার্তা পাঠাচ্ছেন। একটু থেমে আবার চেষ্টা করুন।",
  errAuth: "আপনার সেশন শেষ হয়ে থাকতে পারে। পেজ রিফ্রেশ করে আবার চেষ্টা করুন।",
  errPlanner:
    "পরিকল্পনা সেবা সাময়িকভাবে অনুপলব্ধ। আবার চেষ্টা করুন — সমস্যা থাকলে অনুরোধটি নতুন করে লিখুন।",
  errServer: "সার্ভারে কিছু ভুল হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।",
  errValidation: "আপনার অনুরোধ প্রক্রিয়া করা যায়নি। এটি আরও স্পষ্ট করে লিখুন।",
  errNetwork: "নেটওয়ার্ক ত্রুটি — সংযোগ পরীক্ষা করে আবার চেষ্টা করুন।",
  errGeneric: "কিছু ভুল হয়েছে। অনুরোধটি নতুন করে বা সহজ করে লিখুন।",

  approvalHeading: "এই যজ্ঞের জন্য চেকার দরকার",
  approvalBody:
    "একটি মেকার-চেকার অনুমোদন খোলা হয়েছে। যজ্ঞ শুরুর আগে অন্য একজন অ্যাডমিনকে এটি অনুমোদন করতে হবে।",
  viewApprovals: "অনুমোদন দেখুন",

  planAppearHeading: "আপনার যন্ত্র এখানে দেখা যাবে",
  planAppearBody:
    "চ্যাটে কাঙ্ক্ষিত ফলাফল বলুন। পরিকল্পক বাকি বিবরণ চেয়ে এটি তৈরি করবে।",

  saveWorkflow: "সূত্র সংরক্ষণ করুন",
  saveVersion: "v{v} সংরক্ষণ করুন",
  savedVersion: "সংরক্ষিত · v{v}",
  drift: "drift",
  workflowNamePlaceholder: "সূত্রের নাম",
  ariaDelete: "সংবাদ মুছুন",

  saveTitleFirst: "সূত্র সংরক্ষণ করুন",
  saveTitleUpdate: "নতুন সূত্র সংস্করণ সংরক্ষণ করুন",
  saveDescFirst:
    "এই যন্ত্রকে একটি স্পষ্ট নাম দিন যাতে আপনার দল পরে খুঁজে চালাতে পারে।",
  saveDescUpdate: "এটি বর্তমান সূত্র সংরক্ষণ করে সংস্করণ v{v} তৈরি করে।",
  nameLabel: "সূত্রের নাম",
  namePlaceholder: "যেমন দৈনিক সেটেলমেন্ট ডাউনলোড",
  cancel: "বাতিল",
  save: "সংরক্ষণ",
  saving: "সংরক্ষণ হচ্ছে…",
  confirmUpdate: "আপডেট নিশ্চিত করুন",

  deleteTitle: "সংবাদ মুছবেন?",
  deleteBodyKept: "সংরক্ষিত সূত্র Workflows-এ উপলব্ধ থাকবে।",
  deleteBodyLost: "এই সংবাদের কোনো অসংরক্ষিত যন্ত্রও হারিয়ে যাবে।",
  deleting: "মুছে ফেলা হচ্ছে…",
  deleteConfirm: "সংবাদ মুছুন",
  deleteFailed: "সংবাদটি মোছা যায়নি।",

  runTitle: "সূত্র চালান",
  runDesc: "পরিকল্পক থেকে v{v} চালানো হচ্ছে।",
  runsOn: "এতে চলবে",
  runsOnServer: "কোনো ওয়ার্কস্টেশন ধাপ নেই — এটি সম্পূর্ণ Aakaar সার্ভারে চলবে।",
  modeLabel: "মোড",
  modeLive: "সজীব",
  modeLiveSub: "প্রতিটি ধাপ সত্যিকারে করুন",
  modeDry: "ড্রাই রান",
  modeDrySub: "প্রভাব ফেলা ধাপগুলি সিমুলেট করুন",
  execInputs: "Execution inputs (JSON)",
  startRun: "যজ্ঞ শুরু করুন",
  starting: "শুরু হচ্ছে…",

  annPlanReady: "{n} ধাপসহ পরিকল্পনা প্রস্তুত।",
  annReplied: "পরিকল্পক উত্তর দিয়েছে।",
  annStopped: "পরিকল্পনা থামানো হয়েছে।",
  annCouldntBuild: "পরিকল্পক পরিকল্পনা তৈরি করতে পারেনি।",
  annRunStarted: "যজ্ঞ শুরু। প্রত্যক্ষ ট্যাবে অগ্রগতি দেখুন।",
  annInvalidJson: "Execution inputs বৈধ JSON হতে হবে।",
  annNeedsApproval: "শুরুর আগে এই যজ্ঞের জন্য চেকারের অনুমোদন প্রয়োজন।",
};

// ---------------------------------------------------------------------------
// Tamil
// ---------------------------------------------------------------------------

const ta: ChatStrings = {
  newLabel: "புதிது",
  headerSubtitle:
    "விளைவைச் சொல்லுங்கள்; திட்டமிடுபவர் அதைச் சூத்திரமாக மாற்றுவார். மீள்ஏற்றத்திற்குப் பிறகும் நிலைத்திருக்கும்.",
  onePerAutomation: "ஒவ்வொரு தானியக்கத்திற்கும் ஒன்று",
  statusPlanning: "திட்டமிடல் நடக்கிறது",
  statusSaved: "சேமித்த சூத்திரம்",
  statusUnsaved: "சேமிக்காத மாற்றங்கள்",
  statusRunning: "இப்போது இயங்குகிறது",
  noChatsYet: "இதுவரை சம்வாதம் இல்லை.",
  ariaHideConversations: "சம்வாதங்களை மறை",
  ariaShowConversations: "சம்வாதங்களைக் காட்டு",
  ariaHidePlan: "யந்திரப் பலகத்தை மறை",
  ariaShowPlan: "யந்திரப் பலகத்தைக் காட்டு",
  subNoPlan: "நீங்கள் என்ன செய்ய விரும்புகிறீர்கள் என்று திட்டமிடுபவரிடம் சொல்லுங்கள்",
  subSteps: "தற்போதைய யந்திரத்தில் {n} படிகள்",
  loadingWorkspace: "பணியிடம் ஏற்றப்படுகிறது…",
  loadingChats: "சம்வாதங்கள் ஏற்றப்படுகின்றன…",
  noActiveChat: "செயலில் சம்வாதம் இல்லை.",

  startersHeading: "எதைத் தானியக்கமாக்க விரும்புகிறீர்கள்?",
  startersIntro:
    "ஒரு சக ஊழியரிடம் சொல்வது போல் விளக்குங்கள். விளைவில் தொடங்குங்கள்; தேவையான விவரங்களை திட்டமிடுபவர் கேட்பார்.",
  tryExample: "ஒரு எடுத்துக்காட்டை முயற்சிக்கவும்",
  starters: [
    {
      title: "அறிக்கையைப் பதிவிறக்கிச் சேமிக்கவும்",
      prompt:
        "ஒவ்வொரு வேலைநாள் காலையிலும், முந்தைய நாளின் செட்டில்மென்ட் அறிக்கையைப் பதிவிறக்கி நிதி கோப்புறையில் சேமிக்கவும்.",
    },
    {
      title: "தெரியும் படிவத்தை நிரப்பவும்",
      prompt:
        "பணிநிலைய திரையில் Customer Name-இல் Aarya Traders, Account Number-இல் 12345, மற்றும் Amount-இல் 1000 நிரப்பவும். பிறகு Enter அழுத்தவும்.",
    },
    {
      title: "பணிநிலையத்தைக் கண்காணிக்கவும்",
      prompt:
        "தொலைநிலை பணிநிலையத்தின் CPU, நினைவகம் மற்றும் வட்டு பயன்பாட்டைச் சரிபார்க்கவும். எந்த மதிப்பும் 85% கடந்தால் என்னை எச்சரிக்கவும்.",
    },
    {
      title: "ஒரு அனுமதி படியைச் சேர்க்கவும்",
      prompt:
        "சர்ச்சைக்குரிய பரிவர்த்தனைகளைச் சமர்ப்பிக்கத் தயார்செய்து, ஒரு இயக்குநருக்கு அனுமதிக்காகக் காட்டி, பிறகு அனுமதிக்கப்பட்டவற்றை மட்டும் சமர்ப்பிக்கவும்.",
    },
    {
      title: "நிபந்தனையின்படி வழி தேர்வு",
      prompt:
        "பதிவிறக்கிய கோப்பு காலியாக இருந்தால் என்னை எச்சரித்து நிறுத்தவும். இல்லையெனில் அதைப் போர்ட்டலில் பதிவேற்றி வெற்றியை உறுதிப்படுத்தவும்.",
    },
  ],
  composerFootnote:
    "எளிய மொழி போதும். நீங்கள் எதைப் பார்க்கிறீர்கள், எது நிரப்பப்பட வேண்டும், அடுத்து என்ன நடக்கும் என்று சொல்லுங்கள்.",

  placeholder: "என்ன நடக்க வேண்டும் என்று உங்கள் சொற்களில் சொல்லுங்கள்…",
  hint: "அனுப்ப Enter · புதிய வரிக்கு Shift+Enter",
  advanced: "மேம்பட்ட",
  ariaSend: "செய்தியை அனுப்பு",
  ariaStop: "திட்டமிடலை நிறுத்து",
  paletteCommands: "கட்டளைகள்",
  paletteInsertCap: "வித்யையைச் சேர்",
  paletteNoMatches: "பொருத்தம் இல்லை",
  cmdRunSub: "சேமித்து (தேவைப்பட்டால்) இந்தச் சூத்திரத்தை இயக்கு",
  cmdSaveSub: "தற்போதைய யந்திரத்தைச் சூத்திரமாகச் சேமி",
  cmdPlanSub: "யந்திரக் காட்சியைத் திற",

  draftingPlan: "திட்டம் தயாராகிறது…",
  runningWith: "இயங்குகிறது · {x} · {n}/{total}",
  runningCount: "இயங்குகிறது · {n}/{total} படிகள்",
  runState: "யக்ஞம் {status} · {n}/{total} படிகள்",
  openLive: "ப்ரத்யக்ஷம் திற →",

  drafted: "{n}-படி சூத்திரம் ஒன்றை உருவாக்கினேன்",
  plannerReasoning: "திட்டமிடுபவரின் காரணம்",
  liveAction: "நேரடிச் செயல்",
  makerChecker:
    "இந்த யந்திரத்தில் நேரடிச் செயல்கள் உள்ளன (எ.கா. {x}). இதை இயக்க ஒரு சரிபார்ப்பாளரின் அனுமதி தேவைப்படலாம்.",
  reviewPlan: "யந்திரத்தைப் பார்",
  runDraft: "வரைவை இயக்கு",
  copy: "நகலெடு",
  copied: "நகலெடுக்கப்பட்டது",
  showFewer: "குறைவான படிகளைக் காட்டு",
  moreSteps: "+{n} மேலும் படிகள்",
  buildingPlan: "உங்கள் திட்டம் உருவாக்கப்படுகிறது…",

  clarifyHeading: "சில விவரங்கள் உதவும்",
  typeAnswer: "உங்கள் பதிலை உள்ளிடவும்…",
  sendAnswers: "பதில்களை அனுப்பு",
  answerAll: "தொடர அனைத்து {n}க்கும் பதிலளிக்கவும்",
  answersSent: "பதில்கள் அனுப்பப்பட்டன",

  missingHeading: "ஒரு கருவிக்கான அணுகல் தேவை",
  clickToCopy: "நகலெடுக்க கிளிக் செய்யவும்",
  openAccess: "அணுகல் அமைப்புகளைத் திற",

  errorHeading: "என்னால் திட்டத்தை உருவாக்க முடியவில்லை",
  errorFootnote: "உங்கள் செய்தி கம்போசரில் திரும்பியுள்ளது — திருத்தி மீண்டும் முயற்சிக்கவும்.",
  errRate: "நீங்கள் மிக விரைவாகச் செய்திகள் அனுப்புகிறீர்கள். சிறிது காத்திருந்து மீண்டும் முயலவும்.",
  errAuth: "உங்கள் அமர்வு முடிந்திருக்கலாம். பக்கத்தைப் புதுப்பித்து மீண்டும் முயலவும்.",
  errPlanner:
    "திட்டமிடல் சேவை தற்காலிகமாகக் கிடைக்கவில்லை. மீண்டும் முயலவும் — தொடர்ந்தால் உங்கள் கோரிக்கையை மறுபடியும் எழுதவும்.",
  errServer: "சர்வரில் ஏதோ தவறு. தயவுசெய்து மீண்டும் முயலவும்.",
  errValidation: "உங்கள் கோரிக்கையைச் செயலாக்க முடியவில்லை. தெளிவாக மறுபடி எழுதவும்.",
  errNetwork: "நெட்வொர்க் பிழை — இணைப்பைச் சரிபார்த்து மீண்டும் முயலவும்.",
  errGeneric: "ஏதோ தவறு. கோரிக்கையை மறுபடி அல்லது எளிமையாக எழுதவும்.",

  approvalHeading: "இந்த யக்ஞத்திற்கு சரிபார்ப்பாளர் தேவை",
  approvalBody:
    "ஒரு மேக்கர்-செக்கர் அனுமதி திறக்கப்பட்டது. யக்ஞம் தொடங்கும் முன் மற்றொரு நிர்வாகி இதை அனுமதிக்க வேண்டும்.",
  viewApprovals: "அனுமதிகளைப் பார்",

  planAppearHeading: "உங்கள் யந்திரம் இங்கே தோன்றும்",
  planAppearBody:
    "விரும்பிய விளைவை அரட்டையில் சொல்லுங்கள். விடுபட்ட விவரங்களைக் கேட்டு திட்டமிடுபவர் இதை உருவாக்குவார்.",

  saveWorkflow: "சூத்திரத்தைச் சேமி",
  saveVersion: "v{v} சேமி",
  savedVersion: "சேமித்தது · v{v}",
  drift: "drift",
  workflowNamePlaceholder: "சூத்திரத்தின் பெயர்",
  ariaDelete: "சம்வாதத்தை நீக்கு",

  saveTitleFirst: "சூத்திரத்தைச் சேமி",
  saveTitleUpdate: "புதிய சூத்திரப் பதிப்பைச் சேமி",
  saveDescFirst:
    "உங்கள் குழு பின்னர் கண்டுபிடித்து இயக்க இந்த யந்திரத்திற்குத் தெளிவான பெயர் கொடுங்கள்.",
  saveDescUpdate: "இது தற்போதைய சூத்திரத்தைப் பாதுகாத்து பதிப்பு v{v} உருவாக்குகிறது.",
  nameLabel: "சூத்திரத்தின் பெயர்",
  namePlaceholder: "எ.கா. தினசரி செட்டில்மென்ட் பதிவிறக்கம்",
  cancel: "ரத்து",
  save: "சேமி",
  saving: "சேமிக்கிறது…",
  confirmUpdate: "புதுப்பிப்பை உறுதிப்படுத்து",

  deleteTitle: "சம்வாதத்தை நீக்கவா?",
  deleteBodyKept: "சேமித்த சூத்திரம் Workflows-இல் கிடைக்கும்.",
  deleteBodyLost: "இந்த சம்வாதத்தின் சேமிக்காத யந்திரமும் இழக்கப்படும்.",
  deleting: "நீக்கப்படுகிறது…",
  deleteConfirm: "சம்வாதத்தை நீக்கு",
  deleteFailed: "சம்வாதத்தை நீக்க முடியவில்லை.",

  runTitle: "சூத்திரத்தை இயக்கு",
  runDesc: "திட்டமிடுபவரிடமிருந்து v{v} இயக்கப்படுகிறது.",
  runsOn: "இதில் இயங்கும்",
  runsOnServer: "பணிநிலைய படிகள் இல்லை — இது முழுவதும் Aakaar சர்வரில் இயங்கும்.",
  modeLabel: "பயன்முறை",
  modeLive: "நேரடி",
  modeLiveSub: "ஒவ்வொரு படியையும் உண்மையாகச் செய்",
  modeDry: "டிரை ரன்",
  modeDrySub: "விளைவு ஏற்படுத்தும் படிகளை உருவகப்படுத்து",
  execInputs: "Execution inputs (JSON)",
  startRun: "யக்ஞத்தைத் தொடங்கு",
  starting: "தொடங்குகிறது…",

  annPlanReady: "{n} படிகளுடன் திட்டம் தயார்.",
  annReplied: "திட்டமிடுபவர் பதிலளித்தார்.",
  annStopped: "திட்டமிடல் நிறுத்தப்பட்டது.",
  annCouldntBuild: "திட்டமிடுபவரால் திட்டத்தை உருவாக்க முடியவில்லை.",
  annRunStarted: "யக்ஞம் தொடங்கியது. ப்ரத்யக்ஷம் தாவலில் முன்னேற்றத்தைப் பார்.",
  annInvalidJson: "Execution inputs செல்லுபடியாகும் JSON ஆக இருக்க வேண்டும்.",
  annNeedsApproval: "தொடங்கும் முன் இந்த யக்ஞத்திற்கு சரிபார்ப்பாளரின் அனுமதி தேவை.",
};

// ---------------------------------------------------------------------------
// Kannada
// ---------------------------------------------------------------------------

const kn: ChatStrings = {
  newLabel: "ಹೊಸತು",
  headerSubtitle:
    "ಫಲಿತಾಂಶವನ್ನು ಹೇಳಿ; ಯೋಜಕ ಅದನ್ನು ಸೂತ್ರವಾಗಿ ಪರಿವರ್ತಿಸುತ್ತಾನೆ. ಮರುಲೋಡ್ ನಂತರವೂ ಉಳಿಯುತ್ತದೆ.",
  onePerAutomation: "ಪ್ರತಿ ಸ್ವಯಂಚಾಲನೆಗೆ ಒಂದು",
  statusPlanning: "ಯೋಜನೆ ನಡೆಯುತ್ತಿದೆ",
  statusSaved: "ಉಳಿಸಿದ ಸೂತ್ರ",
  statusUnsaved: "ಉಳಿಸದ ಬದಲಾವಣೆಗಳು",
  statusRunning: "ಈಗ ಚಾಲನೆಯಲ್ಲಿದೆ",
  noChatsYet: "ಇನ್ನೂ ಯಾವುದೇ ಸಂವಾದ ಇಲ್ಲ.",
  ariaHideConversations: "ಸಂವಾದಗಳನ್ನು ಮರೆಮಾಡಿ",
  ariaShowConversations: "ಸಂವಾದಗಳನ್ನು ತೋರಿಸಿ",
  ariaHidePlan: "ಯಂತ್ರ ಫಲಕ ಮರೆಮಾಡಿ",
  ariaShowPlan: "ಯಂತ್ರ ಫಲಕ ತೋರಿಸಿ",
  subNoPlan: "ನೀವು ಏನು ಸಾಧಿಸಬೇಕೆಂದು ಯೋಜಕನಿಗೆ ತಿಳಿಸಿ",
  subSteps: "ಪ್ರಸ್ತುತ ಯಂತ್ರದಲ್ಲಿ {n} ಹಂತಗಳು",
  loadingWorkspace: "ಕಾರ್ಯಸ್ಥಳ ಲೋಡ್ ಆಗುತ್ತಿದೆ…",
  loadingChats: "ಸಂವಾದಗಳು ಲೋಡ್ ಆಗುತ್ತಿವೆ…",
  noActiveChat: "ಸಕ್ರಿಯ ಸಂವಾದ ಇಲ್ಲ.",

  startersHeading: "ನೀವು ಏನನ್ನು ಸ್ವಯಂಚಾಲನೆ ಮಾಡಲು ಬಯಸುತ್ತೀರಿ?",
  startersIntro:
    "ಸಹೋದ್ಯೋಗಿಗೆ ಹೇಳುವಂತೆ ವಿವರಿಸಿ. ಫಲಿತಾಂಶದಿಂದ ಪ್ರಾರಂಭಿಸಿ; ಬೇಕಾದ ವಿವರಗಳನ್ನು ಯೋಜಕ ಕೇಳುತ್ತಾನೆ.",
  tryExample: "ಒಂದು ಉದಾಹರಣೆ ಪ್ರಯತ್ನಿಸಿ",
  starters: [
    {
      title: "ವರದಿಯನ್ನು ಡೌನ್‌ಲೋಡ್ ಮಾಡಿ ಉಳಿಸಿ",
      prompt:
        "ಪ್ರತಿ ಕೆಲಸದ ದಿನ ಬೆಳಿಗ್ಗೆ, ಹಿಂದಿನ ದಿನದ ಸೆಟಲ್‌ಮೆಂಟ್ ವರದಿಯನ್ನು ಡೌನ್‌ಲೋಡ್ ಮಾಡಿ ಹಣಕಾಸು ಫೋಲ್ಡರ್‌ನಲ್ಲಿ ಉಳಿಸಿ.",
    },
    {
      title: "ಕಾಣುವ ನಮೂನೆಯನ್ನು ಭರ್ತಿ ಮಾಡಿ",
      prompt:
        "ವರ್ಕ್‌ಸ್ಟೇಷನ್ ಪರದೆಯಲ್ಲಿ Customer Name-ನಲ್ಲಿ Aarya Traders, Account Number-ನಲ್ಲಿ 12345, ಮತ್ತು Amount-ನಲ್ಲಿ 1000 ಭರ್ತಿ ಮಾಡಿ. ನಂತರ Enter ಒತ್ತಿ.",
    },
    {
      title: "ವರ್ಕ್‌ಸ್ಟೇಷನ್ ಮೇಲ್ವಿಚಾರಣೆ ಮಾಡಿ",
      prompt:
        "ರಿಮೋಟ್ ವರ್ಕ್‌ಸ್ಟೇಷನ್‌ನ CPU, ಮೆಮೊರಿ ಮತ್ತು ಡಿಸ್ಕ್ ಬಳಕೆ ಪರಿಶೀಲಿಸಿ. ಯಾವುದೇ ಮೌಲ್ಯ 85% ಮೀರಿದರೆ ನನಗೆ ಎಚ್ಚರಿಸಿ.",
    },
    {
      title: "ಅನುಮೋದನೆ ಹಂತ ಸೇರಿಸಿ",
      prompt:
        "ವಿವಾದಿತ ವಹಿವಾಟುಗಳನ್ನು ಸಲ್ಲಿಕೆಗೆ ಸಿದ್ಧಪಡಿಸಿ, ಒಬ್ಬ ಆಪರೇಟರ್‌ಗೆ ಅನುಮೋದನೆಗಾಗಿ ತೋರಿಸಿ, ನಂತರ ಅನುಮೋದಿತ ಐಟಂಗಳನ್ನು ಮಾತ್ರ ಸಲ್ಲಿಸಿ.",
    },
    {
      title: "ಷರತ್ತಿನ ಪ್ರಕಾರ ಮಾರ್ಗ ಆಯ್ಕೆ",
      prompt:
        "ಡೌನ್‌ಲೋಡ್ ಮಾಡಿದ ಫೈಲ್ ಖಾಲಿಯಾಗಿದ್ದರೆ ನನಗೆ ಎಚ್ಚರಿಸಿ ನಿಲ್ಲಿಸಿ. ಇಲ್ಲದಿದ್ದರೆ ಅದನ್ನು ಪೋರ್ಟಲ್‌ಗೆ ಅಪ್‌ಲೋಡ್ ಮಾಡಿ ಯಶಸ್ಸನ್ನು ಖಚಿತಪಡಿಸಿ.",
    },
  ],
  composerFootnote:
    "ಸರಳ ಭಾಷೆ ಸಾಕು. ನೀವು ಏನು ನೋಡುತ್ತೀರಿ, ಏನು ಭರ್ತಿ ಆಗಬೇಕು, ಮುಂದೆ ಏನಾಗುತ್ತದೆ ಎಂದು ಹೇಳಿ.",

  placeholder: "ಏನಾಗಬೇಕೆಂದು ನಿಮ್ಮ ಮಾತಿನಲ್ಲಿ ಹೇಳಿ…",
  hint: "ಕಳುಹಿಸಲು Enter · ಹೊಸ ಸಾಲಿಗೆ Shift+Enter",
  advanced: "ಸುಧಾರಿತ",
  ariaSend: "ಸಂದೇಶ ಕಳುಹಿಸಿ",
  ariaStop: "ಯೋಜನೆ ನಿಲ್ಲಿಸಿ",
  paletteCommands: "ಆಜ್ಞೆಗಳು",
  paletteInsertCap: "ವಿದ್ಯೆ ಸೇರಿಸಿ",
  paletteNoMatches: "ಹೊಂದಾಣಿಕೆ ಇಲ್ಲ",
  cmdRunSub: "ಉಳಿಸಿ (ಅಗತ್ಯವಿದ್ದರೆ) ಈ ಸೂತ್ರವನ್ನು ಚಲಾಯಿಸಿ",
  cmdSaveSub: "ಪ್ರಸ್ತುತ ಯಂತ್ರವನ್ನು ಸೂತ್ರವಾಗಿ ಉಳಿಸಿ",
  cmdPlanSub: "ಯಂತ್ರ ನೋಟ ತೆರೆಯಿರಿ",

  draftingPlan: "ಯೋಜನೆ ಸಿದ್ಧವಾಗುತ್ತಿದೆ…",
  runningWith: "ಚಾಲನೆ · {x} · {n}/{total}",
  runningCount: "ಚಾಲನೆ · {n}/{total} ಹಂತಗಳು",
  runState: "ಯಜ್ಞ {status} · {n}/{total} ಹಂತಗಳು",
  openLive: "ಪ್ರತ್ಯಕ್ಷ ತೆರೆಯಿರಿ →",

  drafted: "ನಾನು {n}-ಹಂತಗಳ ಸೂತ್ರ ರಚಿಸಿದೆ",
  plannerReasoning: "ಯೋಜಕನ ತರ್ಕ",
  liveAction: "ಸಜೀವ ಕ್ರಿಯೆ",
  makerChecker:
    "ಈ ಯಂತ್ರದಲ್ಲಿ ಸಜೀವ ಕ್ರಿಯೆಗಳಿವೆ (ಉದಾ. {x}). ಇದನ್ನು ಚಲಾಯಿಸಲು ಪರಿಶೀಲಕರ ಅನುಮೋದನೆ ಬೇಕಾಗಬಹುದು.",
  reviewPlan: "ಯಂತ್ರ ನೋಡಿ",
  runDraft: "ಕರಡು ಚಲಾಯಿಸಿ",
  copy: "ನಕಲಿಸಿ",
  copied: "ನಕಲಿಸಲಾಗಿದೆ",
  showFewer: "ಕಡಿಮೆ ಹಂತ ತೋರಿಸಿ",
  moreSteps: "+{n} ಹೆಚ್ಚಿನ ಹಂತಗಳು",
  buildingPlan: "ನಿಮ್ಮ ಯೋಜನೆ ರಚಿಸಲಾಗುತ್ತಿದೆ…",

  clarifyHeading: "ಕೆಲವು ವಿವರಗಳು ಸಹಾಯ ಮಾಡುತ್ತವೆ",
  typeAnswer: "ನಿಮ್ಮ ಉತ್ತರ ಬರೆಯಿರಿ…",
  sendAnswers: "ಉತ್ತರಗಳನ್ನು ಕಳುಹಿಸಿ",
  answerAll: "ಮುಂದುವರಿಯಲು ಎಲ್ಲ {n} ಕ್ಕೆ ಉತ್ತರಿಸಿ",
  answersSent: "ಉತ್ತರಗಳನ್ನು ಕಳುಹಿಸಲಾಗಿದೆ",

  missingHeading: "ನನಗೆ ಒಂದು ಟೂಲ್‌ನ ಅಧಿಕಾರ ಬೇಕು",
  clickToCopy: "ನಕಲಿಸಲು ಕ್ಲಿಕ್ ಮಾಡಿ",
  openAccess: "ಅಧಿಕಾರ ಸೆಟ್ಟಿಂಗ್ಸ್ ತೆರೆಯಿರಿ",

  errorHeading: "ನನಗೆ ಯೋಜನೆ ರಚಿಸಲಾಗಲಿಲ್ಲ",
  errorFootnote: "ನಿಮ್ಮ ಸಂದೇಶ ಕಂಪೋಸರ್‌ಗೆ ಮರಳಿದೆ — ಸಂಪಾದಿಸಿ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
  errRate: "ನೀವು ತುಂಬಾ ವೇಗವಾಗಿ ಸಂದೇಶ ಕಳುಹಿಸುತ್ತಿದ್ದೀರಿ. ಸ್ವಲ್ಪ ಕಾದು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
  errAuth: "ನಿಮ್ಮ ಸೆಷನ್ ಮುಗಿದಿರಬಹುದು. ಪುಟ ರಿಫ್ರೆಶ್ ಮಾಡಿ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
  errPlanner:
    "ಯೋಜನಾ ಸೇವೆ ತಾತ್ಕಾಲಿಕವಾಗಿ ಲಭ್ಯವಿಲ್ಲ. ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ — ಮುಂದುವರಿದರೆ ನಿಮ್ಮ ವಿನಂತಿಯನ್ನು ಮತ್ತೆ ಬರೆಯಿರಿ.",
  errServer: "ಸರ್ವರ್‌ನಲ್ಲಿ ಏನೋ ತಪ್ಪಾಗಿದೆ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
  errValidation: "ನಿಮ್ಮ ವಿನಂತಿಯನ್ನು ಪ್ರಕ್ರಿಯೆಗೊಳಿಸಲಾಗಲಿಲ್ಲ. ಸ್ಪಷ್ಟವಾಗಿ ಮತ್ತೆ ಬರೆಯಿರಿ.",
  errNetwork: "ನೆಟ್‌ವರ್ಕ್ ದೋಷ — ಸಂಪರ್ಕ ಪರಿಶೀಲಿಸಿ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
  errGeneric: "ಏನೋ ತಪ್ಪಾಗಿದೆ. ವಿನಂತಿಯನ್ನು ಮತ್ತೆ ಅಥವಾ ಸರಳವಾಗಿ ಬರೆಯಿರಿ.",

  approvalHeading: "ಈ ಯಜ್ಞಕ್ಕೆ ಪರಿಶೀಲಕ ಬೇಕು",
  approvalBody:
    "ಒಂದು ಮೇಕರ್-ಚೆಕರ್ ಅನುಮೋದನೆ ತೆರೆಯಲಾಗಿದೆ. ಯಜ್ಞ ಪ್ರಾರಂಭವಾಗುವ ಮೊದಲು ಇನ್ನೊಬ್ಬ ಆಡ್ಮಿನ್ ಇದನ್ನು ಅನುಮೋದಿಸಬೇಕು.",
  viewApprovals: "ಅನುಮೋದನೆಗಳನ್ನು ನೋಡಿ",

  planAppearHeading: "ನಿಮ್ಮ ಯಂತ್ರ ಇಲ್ಲಿ ಕಾಣಿಸುತ್ತದೆ",
  planAppearBody:
    "ಬಯಸಿದ ಫಲಿತಾಂಶವನ್ನು ಚಾಟ್‌ನಲ್ಲಿ ಹೇಳಿ. ಬಿಟ್ಟ ವಿವರಗಳನ್ನು ಕೇಳಿ ಯೋಜಕ ಇದನ್ನು ರಚಿಸುತ್ತಾನೆ.",

  saveWorkflow: "ಸೂತ್ರ ಉಳಿಸಿ",
  saveVersion: "v{v} ಉಳಿಸಿ",
  savedVersion: "ಉಳಿಸಲಾಗಿದೆ · v{v}",
  drift: "drift",
  workflowNamePlaceholder: "ಸೂತ್ರದ ಹೆಸರು",
  ariaDelete: "ಸಂವಾದ ಅಳಿಸಿ",

  saveTitleFirst: "ಸೂತ್ರ ಉಳಿಸಿ",
  saveTitleUpdate: "ಹೊಸ ಸೂತ್ರ ಆವೃತ್ತಿ ಉಳಿಸಿ",
  saveDescFirst:
    "ನಿಮ್ಮ ತಂಡ ನಂತರ ಹುಡುಕಿ ಚಲಾಯಿಸಲು ಈ ಯಂತ್ರಕ್ಕೆ ಸ್ಪಷ್ಟ ಹೆಸರು ನೀಡಿ.",
  saveDescUpdate: "ಇದು ಪ್ರಸ್ತುತ ಸೂತ್ರವನ್ನು ಉಳಿಸಿ ಆವೃತ್ತಿ v{v} ರಚಿಸುತ್ತದೆ.",
  nameLabel: "ಸೂತ್ರದ ಹೆಸರು",
  namePlaceholder: "ಉದಾ. ದೈನಂದಿನ ಸೆಟಲ್‌ಮೆಂಟ್ ಡೌನ್‌ಲೋಡ್",
  cancel: "ರದ್ದು",
  save: "ಉಳಿಸಿ",
  saving: "ಉಳಿಸಲಾಗುತ್ತಿದೆ…",
  confirmUpdate: "ನವೀಕರಣ ಖಚಿತಪಡಿಸಿ",

  deleteTitle: "ಸಂವಾದ ಅಳಿಸಬೇಕೆ?",
  deleteBodyKept: "ಉಳಿಸಿದ ಸೂತ್ರ Workflows-ನಲ್ಲಿ ಲಭ್ಯವಿರುತ್ತದೆ.",
  deleteBodyLost: "ಈ ಸಂವಾದದ ಯಾವುದೇ ಉಳಿಸದ ಯಂತ್ರವೂ ಕಳೆದುಹೋಗುತ್ತದೆ.",
  deleting: "ಅಳಿಸಲಾಗುತ್ತಿದೆ…",
  deleteConfirm: "ಸಂವಾದ ಅಳಿಸಿ",
  deleteFailed: "ಸಂವಾದ ಅಳಿಸಲಾಗಲಿಲ್ಲ.",

  runTitle: "ಸೂತ್ರ ಚಲಾಯಿಸಿ",
  runDesc: "ಯೋಜಕನಿಂದ v{v} ಚಲಾಯಿಸಲಾಗುತ್ತಿದೆ.",
  runsOn: "ಇದರಲ್ಲಿ ಚಲಿಸುತ್ತದೆ",
  runsOnServer: "ವರ್ಕ್‌ಸ್ಟೇಷನ್ ಹಂತಗಳಿಲ್ಲ — ಇದು ಸಂಪೂರ್ಣವಾಗಿ Aakaar ಸರ್ವರ್‌ನಲ್ಲಿ ಚಲಿಸುತ್ತದೆ.",
  modeLabel: "ಮೋಡ್",
  modeLive: "ಸಜೀವ",
  modeLiveSub: "ಪ್ರತಿ ಹಂತವನ್ನು ನಿಜವಾಗಿ ಮಾಡಿ",
  modeDry: "ಡ್ರೈ ರನ್",
  modeDrySub: "ಪರಿಣಾಮ ಬೀರುವ ಹಂತಗಳನ್ನು ಸಿಮ್ಯುಲೇಟ್ ಮಾಡಿ",
  execInputs: "Execution inputs (JSON)",
  startRun: "ಯಜ್ಞ ಪ್ರಾರಂಭಿಸಿ",
  starting: "ಪ್ರಾರಂಭವಾಗುತ್ತಿದೆ…",

  annPlanReady: "{n} ಹಂತಗಳೊಂದಿಗೆ ಯೋಜನೆ ಸಿದ್ಧ.",
  annReplied: "ಯೋಜಕ ಉತ್ತರಿಸಿದ.",
  annStopped: "ಯೋಜನೆ ನಿಲ್ಲಿಸಲಾಗಿದೆ.",
  annCouldntBuild: "ಯೋಜಕನಿಗೆ ಯೋಜನೆ ರಚಿಸಲಾಗಲಿಲ್ಲ.",
  annRunStarted: "ಯಜ್ಞ ಪ್ರಾರಂಭವಾಯಿತು. ಪ್ರತ್ಯಕ್ಷ ಟ್ಯಾಬ್‌ನಲ್ಲಿ ಪ್ರಗತಿ ನೋಡಿ.",
  annInvalidJson: "Execution inputs ಮಾನ್ಯ JSON ಆಗಿರಬೇಕು.",
  annNeedsApproval: "ಪ್ರಾರಂಭವಾಗುವ ಮೊದಲು ಈ ಯಜ್ಞಕ್ಕೆ ಪರಿಶೀಲಕರ ಅನುಮೋದನೆ ಬೇಕು.",
};

const TABLE: Record<LangCode, ChatStrings> = {
  en,
  "hi-Latn": hiLatn,
  "hi-Deva": hiDeva,
  bn,
  ta,
  kn,
};

/** Resolve the chat strings for the active language. */
export function useChatStrings(): ChatStrings {
  const lang = useLang();
  return TABLE[lang] ?? en;
}

/** Replace {key} placeholders in a template with the given values. */
export function fmt(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, k) =>
    k in vars ? String(vars[k]) : `{${k}}`,
  );
}
