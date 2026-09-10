/** Adapted from Magic UI's Dot Pattern (MIT). See LICENSE.md in this folder.
 * An SVG pattern keeps the same texture without measuring or animating hundreds of dots. */
import { useId } from "react";

export function DotPattern() {
  const id = useId();
  return (
    <svg aria-hidden="true" className="magic-dot-pattern" width="100%" height="100%">
      <defs>
        <pattern id={id} width="24" height="24" patternUnits="userSpaceOnUse">
          <circle cx="1" cy="1" r="0.8" fill="currentColor" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  );
}
