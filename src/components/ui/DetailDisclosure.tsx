import type { ReactNode } from "react";
import { Accordion } from "@heroui/react/accordion";
import { Icon } from "@iconify/react/offline";

interface DetailDisclosureProps {
  id: string;
  icon: string;
  title: string;
  note: string;
  children: ReactNode;
  defaultExpanded?: boolean;
  className?: string;
}

/** Shared progressive-disclosure shell for dense operational evidence. */
export function DetailDisclosure({
  id,
  icon,
  title,
  note,
  children,
  defaultExpanded = false,
  className = "",
}: DetailDisclosureProps) {
  return (
    <Accordion
      variant="surface"
      hideSeparator
      {...(defaultExpanded ? { defaultExpandedKeys: [id] } : {})}
      className={`overflow-hidden rounded-2xl ${className}`.trim()}
    >
      <Accordion.Item id={id}>
        <Accordion.Heading>
          <Accordion.Trigger className="group flex min-h-16 w-full touch-manipulation items-center gap-3 px-4 py-3 text-left focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)]">
            <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-surface-secondary text-accent">
              <Icon icon={icon} className="size-4.5" aria-hidden="true" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block font-display text-sm font-semibold text-foreground">{title}</span>
              <span className="mt-0.5 block text-xs leading-relaxed text-muted">{note}</span>
            </span>
            <Accordion.Indicator aria-hidden="true" />
          </Accordion.Trigger>
        </Accordion.Heading>
        <Accordion.Panel>
          <Accordion.Body className="space-y-6 px-4 pb-4 pt-3 sm:px-5">{children}</Accordion.Body>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
