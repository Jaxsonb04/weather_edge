import { useEffect, useRef, useState, type ReactNode } from "react";

interface RevealProps {
  children: ReactNode;
  className?: string;
  /** extra delay in seconds to stagger sibling reveals */
  delay?: number;
  /** start the reveal as soon as mounted (above-the-fold), not on scroll */
  immediate?: boolean;
}

/** Scroll-into-view rise+fade via a CSS transition toggled by an
    IntersectionObserver. Both the observer and React effects run even when the
    tab is backgrounded, so content always settles visible (unlike rAF-gated JS
    animation). Degrades to instant under prefers-reduced-motion (see index.css). */
export function Reveal({ children, className, delay = 0, immediate = false }: RevealProps) {
  const ref = useRef<HTMLDivElement>(null);
  // Above-the-fold content must not depend on requestAnimationFrame. Browsers
  // can defer frames for background tabs, leaving the initial screen blank.
  const [shown, setShown] = useState(immediate);

  useEffect(() => {
    if (immediate) return;
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true);
          io.disconnect();
        }
      },
      { rootMargin: "0px 0px -72px 0px", threshold: 0.08 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [immediate]);

  return (
    <div
      ref={ref}
      className={`reveal ${shown ? "is-in" : ""} ${className ?? ""}`}
      style={delay ? { transitionDelay: `${delay}s` } : undefined}
    >
      {children}
    </div>
  );
}
