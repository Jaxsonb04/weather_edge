import { buttonVariants } from "@heroui/styles";
import type { ReactNode } from "react";

interface LinkButtonProps {
  href: string;
  children: ReactNode;
  variant?: "primary" | "secondary" | "tertiary" | "outline" | "ghost";
  size?: "sm" | "md" | "lg";
  className?: string;
  /** external links open in a new tab; internal (hash) links navigate in place */
  external?: boolean;
}

/** A real anchor styled as a HeroUI Button via the buttonVariants slot fn —
    the framework-blessed way to make a link look/behave like a button. */
export function LinkButton({ href, children, variant = "outline", size = "md", className, external = true }: LinkButtonProps) {
  return (
    <a
      href={href}
      {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
      className={buttonVariants({ variant, size, className })}
    >
      {children}
    </a>
  );
}
