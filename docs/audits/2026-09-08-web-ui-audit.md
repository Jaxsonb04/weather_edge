# WeatherEdge Web UI Audit — September 8, 2026

## Result

The closed-position ledger now uses vertical, expandable records below 768px.
City, target date, bracket, side, realized P&L, and outcome remain visible; a tap
reveals entry/exit, quantity, resolution time, ROI, entry edge, quality, settled
high, and the published outcome explanation. Desktop retains HeroUI Pro's Data
Grid with pinned bracket and P&L columns, essential/detail views, and accessible
horizontal navigation. Existing account boundaries and publication gates remain
intact. The changes are local and have not been deployed.

## Findings and fixes

| Location | Finding | Resolution |
| --- | --- | --- |
| `src/components/strategy/LedgerTable.tsx` | A 960px grid inside a 333px phone viewport hid P&L behind 12 columns; the outer scrolling wrapper duplicated Pro's own scroll container. | Mobile Accordion records; one desktop scroller with visible next/previous controls, arrow-key scrolling, and Pro pinned columns. |
| `src/components/strategy/ProfileDashboard.tsx` | The city breakdown occupied most of a phone screen before the first closed trade. Heading notes squeezed the section title. | Expandable city breakdown; notes wrap below titles on narrow screens. |
| `src/components/strategy/ProfileExplorer.tsx` | Profile selectors consumed unnecessary mobile height. | Compact mobile selection rows, retained identity/P&L/outcome counts, and a subtle selection sweep. |
| `src/components/hero/Hero.tsx` | The long headline, introduction, and wrapping actions pushed the forecast past the first phone screen. | Shorter operational headline and introduction, compact navigation labels, balanced spacing. Coverage and SFO flagship context remain explicit. |
| `src/index.css` | The above-fold Pro TrendChip depended on a lazy below-fold stylesheet and could initially display as a tall, oversized block. | Load its stylesheet with the initial component styles. |
| `src/components/ui/Reveal.tsx` / `src/index.css` | Existing reveals used a 700ms, 22px transition and retained `will-change` across many containers. | 360ms, 12px reveals without persistent layer hints; existing reduced-motion fallback retained. |
| `src/components/strategy/LedgerTable.tsx` | A zero-P&L record without an explicit outcome tone fell back to “Win.” | Fallback now reads “Flat”; a published tone remains authoritative. |

## Motion and component composition

- Adapted Magic UI's [Border Beam](https://magicui.design/docs/components/border-beam)
  for forecast cards and the selected paper profile. The sweep runs once for
  3.6 seconds, uses the WeatherEdge gold/thermal palette, ignores pointer input,
  and disappears under reduced motion.
- Adapted Magic UI's [Dot Pattern](https://magicui.design/docs/components/dot-pattern)
  for the three route headers. A single SVG pattern supplies the texture without
  per-dot resize measurements, random animation, or hundreds of motion nodes.
- Retained HeroUI Pro Data Grid, Segment, TrendChip, charts, KPI, and Widget
  composition. Used the installed Data Grid's documented `pinned`,
  `contentClassName`, and `scrollContainerClassName` APIs instead of nesting
  another scrolling container. Mobile disclosure uses HeroUI Accordion.
- No new package dependency. The Magic UI MIT notice is retained in source and
  distributed in `public/licenses/magicui.txt`.

## Initial audit verification

- Production SPA build passed.
- 176 frontend tests passed across 40 files. Four added ledger tests exercise
  mobile disclosure, absent values, outcome semantics, and Pro column behavior.
  Running these tests against the original ledger failed as expected; restoring
  the implementation passed the complete suite.
- Lint, icon integrity, and Git diff checks passed; lint retains the two existing
  CityGrid Fast Refresh warnings.
- 29 automated browser checks passed with no page or console errors. Coverage:
  Overview, Methodology, and Strategy Lab at 320, 390, 768, 1024, and 1440px;
  light and dark themes at 390/1440px; iPhone touch emulation at phone widths.
- Verified mobile tap-to-expand, published detail values, show more/fewer,
  city breakdown, research profile switching, and mobile navigation. Verified
  desktop detail columns, numeric sorting, pinned P&L visibility, button and
  keyboard scrolling, disabled boundary controls, and exactly one scroller.
- Page width stayed within the viewport. Mobile rendered no desktop ledger;
  visible trade triggers were at least 104px high. Essential desktop columns
  fit at 1440px; expanded detail columns remained independently scrollable.
- Reduced motion removed the beam and gave reveals a zero-second transition.
- Browser-observed initial assets: **219.81 KiB gzip JavaScript** and
  **17.25 KiB gzip CSS**, within the existing 300/40 KiB budgets.
- Screenshots, browser checks, motion measurements, build/test logs, and the
  resource list are in ignored `.local/ui-audit-2026-09-08/`.

## Data and operational boundary

The initial read-only public inspection reproduced the reported phone problem.
Local stale runtime state was cleared using the canonical cleanup script.
Visual verification used five freshly downloaded public JSON artifacts, each
SHA-256 matched to the captured manifest published at
`2026-09-09T04:12:29+00:00`. These are an AWS-published snapshot, not ongoing
local runtime data. They were copied only into ignored build/operator storage.

No AWS service, timer, deployment, model, account, strategy policy, execution
flag, provider, or billing setting changed. Live Stability and Research ROI
remain separate paper accounts. No current production-health assertion follows
from this frontend audit. Deployment of the revised SPA remains a separate step.
Existing session-memory edits and the two pre-existing audit documents were
preserved.

## Menu and overview follow-up

The mobile menu now uses a controlled HeroUI Modal with the same 12px blur and
backdrop colors as the HeroUI Pro command palette. Its panel drops into place
over 380ms with staggered navigation rows and a 160ms exit. Opening it no longer
pushes the page down. A two-line menu mark collapses to one horizontal line;
the panel's accessible close button uses the same plain line with a 44px target.
Route descriptions, active-page styling, and a compact external-link footer
complete the navigation panel. The panel fits short landscape screens and
closes when the viewport reaches desktop width.

React Aria supplies focus containment, background isolation, scroll locking,
Escape, backdrop dismissal, and focus restoration. Menu and search use one
application overlay state. Route focus waits until the exiting overlay releases
the main region; subsequent pointer or keyboard input cancels that handoff.
This fixes a browser-observed race where the modal restored the menu trigger
after a page selection instead of leaving focus on the new route heading.

The overview's previous immediate reveals began in their final state, so their
transition delays did not produce an entrance. Its title, copy, actions, and
forecast instrument now use finite 680ms CSS entrances staggered over 280ms.
The Magic UI dot texture fades in and a short thermal accent line draws once.
One status chip replaces the crowded badge group, with a shorter introduction
and a separate paper-risk note. Reduced motion shows the content immediately;
keyboard focus also cancels the entrance on the focused content. These are
presentation changes only, with no authentication or trading behavior added.

Follow-up verification passed:

- Production build and **178 tests across 40 files**, including modal dismissal,
  native link navigation, delayed route focus, and cancellation after user input.
- **31 browser checks** with no page or console errors: 320, 390, 768, 844, and
  1440px layouts; 390/1440px light and dark themes; 844×390 landscape; iPhone
  touch emulation; actual entrance/exit animation state and settled styles.
- Modal bounds, no page shift or horizontal overflow, background isolation,
  scroll locking, Tab/Shift-Tab containment, Escape/focus restoration, both
  dismissal controls, all three destinations, menu-to-search switching,
  desktop resize, and reduced motion.
- Lint and icon checks passed with the same two pre-existing CityGrid warnings.
  Browser-observed initial resources are **223.09 KiB gzip JavaScript** and
  **18.83 KiB gzip CSS**, within the 300/40 KiB budgets.
- Desktop, phone, light/dark, landscape, and intermediate entrance screenshots
  were inspected. Evidence and the reproducible browser script are in ignored
  `.local/ui-menu-overview-2026-09-08/`. Browser checks used the five public
  artifacts matching the manifest published at `2026-09-09T04:32:29+00:00`;
  publication-age gating remained active as the local snapshot aged.

## Deployment snapshot

The guarded `trading/deploy/aws/deploy_web_app.sh` workflow built the SPA,
synced `dist/` to the configured production web root, and triggered the
operational publisher. The public Pages shell returned HTTP 200 and served the
new asset bundle. A desktop production canary loaded the overview in 1.3s;
the iPhone canary confirmed the menu's 12px blur, modal focus containment and
restoration, scroll lock, no overflow, and plain-line close control. Both had
no page or console errors. No backend, paper-account, execution, provider, or
billing policy changed.
