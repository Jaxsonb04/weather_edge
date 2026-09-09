/** Adapted from Magic UI's Border Beam (MIT). See LICENSE.md in this folder.
 * A single selection sweep, with no perpetual work or reduced-motion animation. */
import { motion, useReducedMotion, type MotionStyle } from "motion/react";

export function BorderBeam() {
  const reducedMotion = useReducedMotion();
  if (reducedMotion) return null;
  return (
    <span aria-hidden="true" className="magic-border-beam">
      <motion.span
        className="magic-border-beam-light"
        style={{ offsetPath: "rect(0 auto auto 0 round 16px)" } as MotionStyle}
        initial={{ offsetDistance: "0%", opacity: 0 }}
        animate={{ offsetDistance: "100%", opacity: [0, 0.8, 0.8, 0] }}
        transition={{ duration: 3.6, ease: "linear" }}
      />
    </span>
  );
}
