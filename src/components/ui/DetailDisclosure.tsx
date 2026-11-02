import type { ReactNode } from "react";
import { Accordion } from "@heroui/react/accordion";
import { Icon } from "@iconify/react/offline";

interface DetailDisclosureProps {
  id: string;
  icon: string;
  title: string;
  note?: string;
  children: ReactNode;
  defaultExpanded?: boolean;
  className?: string;
}

/** Progressive disclosure for secondary evidence. HeroUI's Accordion animates
    the panel height itself (and honours reduced motion). */
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
          <Accordion.Trigger className="group flex min-h-14 w-full touch-manipulation items-center gap-3 px-4 py-3 text-left focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--focus)]">
            <Icon icon={icon} className="size-5 shrink-0 text-[color:var(--accent-text)]" aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium text-foreground">{title}</span>
              {note && <span className="mt-0.5 block text-xs text-muted">{note}</span>}
            </span>
            <Accordion.Indicator aria-hidden="true" />
          </Accordion.Trigger>
        </Accordion.Heading>
        <Accordion.Panel>
          <Accordion.Body className="flex flex-col gap-6 px-4 pb-5 pt-2 sm:px-5">{children}</Accordion.Body>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}
