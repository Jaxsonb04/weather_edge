import { ItemCard } from "@heroui-pro/react/item-card";
import { ItemCardGroup } from "@heroui-pro/react/item-card-group";
import { Icon } from "@iconify/react/offline";
import type { StrategyLab } from "../../lib/strategy";

/** Glossary the lab publishes so the numbers are read with the right caveats. */
export function ResearchNotes({ s }: { s: StrategyLab }) {
  const notes = s.research_notes ?? [];
  if (!notes.length) return null;
  return (
    <ItemCardGroup layout="grid" columns={2}>
      <ItemCardGroup.Header>
        <ItemCardGroup.Title>Glossary</ItemCardGroup.Title>
        <ItemCardGroup.Description>How to read the figures on this page</ItemCardGroup.Description>
      </ItemCardGroup.Header>
      {notes.map((n) => (
        <ItemCard key={n.term} variant="secondary" className="min-w-0">
          <ItemCard.Icon>
            <Icon icon="solar:notebook-bold" className="size-4 text-accent" />
          </ItemCard.Icon>
          <ItemCard.Content className="min-w-0">
            <ItemCard.Title className="whitespace-normal break-words">{n.term}</ItemCard.Title>
            <ItemCard.Description className="whitespace-normal break-words">{n.note}</ItemCard.Description>
          </ItemCard.Content>
        </ItemCard>
      ))}
    </ItemCardGroup>
  );
}
