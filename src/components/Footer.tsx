import { Icon } from "@iconify/react/offline";
import { LinkButton } from "./ui/LinkButton";

interface FooterProps {
  disclaimer: string;
  repoUrl: string;
  liveUrl: string;
}

export function Footer({ disclaimer, repoUrl, liveUrl }: FooterProps) {
  return (
    <footer className="relative border-t border-border/60">
      <div className="grid-lines pointer-events-none absolute inset-0 opacity-30" />
      <div className="relative mx-auto flex w-full max-w-6xl flex-col gap-6 px-5 py-10 sm:flex-row sm:items-end sm:justify-between sm:px-8">
        <div className="max-w-xl">
          <p className="font-display text-base font-semibold">
            Weather<span className="temp-text">Edge</span>
          </p>
          <p className="mt-1 text-sm text-muted">Station-aligned forecasting across fifteen US cities + prediction-market quant research.</p>
          <p className="mt-4 text-xs leading-relaxed text-muted">{disclaimer}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <LinkButton href={liveUrl} variant="ghost" size="sm" className="gap-1.5">
            <Icon icon="solar:square-top-down-bold" className="size-4" /> Live dashboard
          </LinkButton>
          <LinkButton href={repoUrl} variant="outline" size="sm" className="gap-1.5">
            <Icon icon="solar:code-square-bold" className="size-4" /> GitHub
          </LinkButton>
        </div>
      </div>
    </footer>
  );
}
