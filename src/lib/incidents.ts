import type { DayRow } from "./strategy";

/** A dated operational incident the published JSON cannot describe on its own.
    The artifact only shows zero entries for these days; without this note a
    reader would see a quiet market where the engine was actually down. Notes
    are curated from the operator's incident record and appear only while one of
    their days is inside a displayed window. */
export interface PaperIncident {
  id: string;
  /** Inclusive local dates, YYYY-MM-DD. */
  start: string;
  end: string;
  title: string;
  /** One line for chips and chart labels. */
  short: string;
  detail: string;
}

export const PAPER_INCIDENTS: PaperIncident[] = [
  {
    id: "entry-scanner-2026-10",
    start: "2026-10-27",
    end: "2026-11-01",
    title: "Entry scanner outage",
    short: "Scanner outage",
    detail:
      "A malformed market field crashed every paper scan from late Oct 27, so neither account could open positions. Fixed Nov 1, 2026. Exits and settlement kept running.",
  },
];

const overlaps = (incident: PaperIncident, start: string, end: string) =>
  incident.start <= end && incident.end >= start;

/** Incidents touching a [start, end] date window (YYYY-MM-DD, inclusive). */
export function incidentsInWindow(start?: string | null, end?: string | null): PaperIncident[] {
  if (!start || !end) return [];
  return PAPER_INCIDENTS.filter((incident) => overlaps(incident, start.slice(0, 10), end.slice(0, 10)));
}

/** Incidents touching any date of a published day series. */
export function incidentsForDays(days: DayRow[] | undefined): PaperIncident[] {
  const dates = (days ?? []).map((day) => day?.date).filter(Boolean).sort();
  return dates.length ? incidentsInWindow(dates[0], dates.at(-1)) : [];
}

/** The trailing run of days on which a book opened nothing, read from the
    published series. Days on or after `today` are still in progress, so they
    never count: a zero so far is normal until the day is over. Returns null
    when the last finished day had entries, or when the series does not publish
    entry counts at all. */
export function trailingNoEntryRun(
  days: DayRow[] | undefined,
  today?: string | null,
): { start: string; end: string; count: number } | null {
  const rows = [...(days ?? [])]
    .filter((day) => day?.date && (!today || day.date.slice(0, 10) < today.slice(0, 10)))
    .sort((a, b) => a.date.localeCompare(b.date));
  const opened = (day: DayRow) => day.opened ?? day.trades_opened;
  if (!rows.length || rows.every((day) => opened(day) == null)) return null;
  let index = rows.length - 1;
  while (index >= 0 && opened(rows[index]) === 0) index -= 1;
  const run = rows.slice(index + 1);
  if (!run.length) return null;
  return { start: run[0].date, end: run.at(-1)!.date, count: run.length };
}

const monthDay = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

/** "Oct 28" from YYYY-MM-DD, read as a calendar date (not shifted by zone). */
export function shortDay(value: string): string {
  const parsed = new Date(`${value.slice(0, 10)}T12:00:00Z`);
  return Number.isNaN(parsed.getTime()) ? value : monthDay.format(parsed);
}

/** "Oct 28 – Nov 1", or one day when both ends match. */
export function dayRange(start: string, end: string): string {
  return start === end ? shortDay(start) : `${shortDay(start)} – ${shortDay(end)}`;
}
