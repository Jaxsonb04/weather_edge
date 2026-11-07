interface FooterProps {
  disclaimer: string;
  repoUrl: string;
  liveUrl: string;
}

/** One quiet line: what this is, the paper-only disclaimer, and the source link. */
export function Footer({ disclaimer, repoUrl }: FooterProps) {
  return (
    <footer className="border-t border-separator">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-3 px-4 py-8 text-sm sm:flex-row sm:items-center sm:justify-between sm:px-6 lg:px-8">
        <div className="flex min-w-0 items-start gap-3">
          <img
            src={`${import.meta.env.BASE_URL}favicon.svg`}
            alt=""
            aria-hidden="true"
            width={20}
            height={20}
            className="mt-0.5 size-5 shrink-0 rounded-md"
          />
          <p className="max-w-2xl text-xs leading-relaxed text-muted">
            <span className="font-medium text-foreground">WeatherEdge</span> · {disclaimer}
          </p>
        </div>
        <a
          href={repoUrl}
          target="_blank"
          rel="noreferrer"
          className="flex min-h-11 shrink-0 items-center text-xs font-medium text-muted underline-offset-4 sm:min-h-0 transition-colors hover:text-foreground hover:underline focus-visible:rounded focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus)]"
        >
          Source on GitHub
        </a>
      </div>
    </footer>
  );
}
