import { ItemCard, ItemCardGroup } from "@heroui-pro/react";
import { Icon } from "@iconify/react";
import type { StrategyLab } from "../../lib/strategy";

/** Glossary the lab publishes so the numbers are read with the right caveats. */
export function ResearchNotes({ s }: { s: StrategyLab }) {
  const notes = s.research_notes ?? [];
  if (!notes.length) return null;
  return (
    <ItemCardGroup layout="grid" columns={2}>
      <ItemCardGroup.Header>
        <ItemCardGroup.Title>Reading the lab</ItemCardGroup.Title>
        <ItemCardGroup.Description>How to interpret these figures honestly</ItemCardGroup.Description>
      </ItemCardGroup.Header>
      {notes.map((n) => (
        <ItemCard key={n.term} variant="secondary">
          <ItemCard.Icon>
            <Icon icon="solar:notebook-bold" className="size-4 text-accent" />
          </ItemCard.Icon>
          <ItemCard.Content>
            <ItemCard.Title>{n.term}</ItemCard.Title>
            <ItemCard.Description>{n.note}</ItemCard.Description>
          </ItemCard.Content>
        </ItemCard>
      ))}
    </ItemCardGroup>
  );
}
