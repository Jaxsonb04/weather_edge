import type { StrategyLab } from "../../lib/strategy";
import { DetailDisclosure } from "../ui/DetailDisclosure";

/** The Lab's published glossary, folded away until someone needs a definition. */
export function ResearchNotes({ s }: { s: StrategyLab }) {
  const notes = s.research_notes ?? [];
  if (!notes.length) return null;
  return (
    <DetailDisclosure
      id="research-glossary"
      icon="solar:notebook-linear"
      title="Glossary"
      note={`${notes.length} terms used on this page`}
    >
      <dl className="grid min-w-0 gap-x-8 gap-y-4 md:grid-cols-2">
        {notes.map((n) => (
          <div key={n.term} className="min-w-0">
            <dt className="text-sm font-medium text-foreground">{n.term}</dt>
            <dd className="mt-0.5 text-sm leading-relaxed text-muted">
              {n.note.replace(/repeated 15[ -]minute scans/gi, "Repeated scheduled scans")}
            </dd>
          </div>
        ))}
      </dl>
    </DetailDisclosure>
  );
}
