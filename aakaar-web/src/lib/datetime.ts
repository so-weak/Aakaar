// Render timestamps in IST regardless of where the user's browser is.
// The backend stores everything as UTC (timezone-aware datetimes); the
// frontend formats explicitly with timeZone="Asia/Kolkata" so the displayed
// time doesn't drift when an admin is travelling.

const IST_TZ = "Asia/Kolkata";

const dtFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST_TZ,
  dateStyle: "medium",
  timeStyle: "short",
});

const dateFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST_TZ,
  dateStyle: "medium",
});

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  timeZone: IST_TZ,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function _toDate(value: string | Date | null | undefined): Date | null {
  if (value == null) return null;
  if (value instanceof Date) return value;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

/** "8 May 2026, 12:39 pm IST" — for headers / row dates with a time. */
export function formatISTDateTime(value: string | Date | null | undefined): string {
  const d = _toDate(value);
  if (!d) return "—";
  return dtFmt.format(d) + " IST";
}

/** "8 May 2026" — for created_at-style date-only cells. */
export function formatISTDate(value: string | Date | null | undefined): string {
  const d = _toDate(value);
  if (!d) return "—";
  return dateFmt.format(d);
}

/** "12:39:42" — for the run-event timeline column. */
export function formatISTTime(value: string | Date | null | undefined): string {
  const d = _toDate(value);
  if (!d) return "—";
  return timeFmt.format(d);
}
