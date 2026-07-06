import NumberFlow, { type Format } from "@number-flow/react";

interface AnimatedNumberProps {
  value: number;
  /** NumberFlow format options — e.g. { style: "percent", maximumFractionDigits: 1 } */
  format?: Format;
  prefix?: string;
  suffix?: string;
  className?: string;
}

/** Tabular, animated counting number. NumberFlow honors prefers-reduced-motion. */
export function AnimatedNumber({ value, format, prefix, suffix, className }: AnimatedNumberProps) {
  return (
    <NumberFlow
      value={value}
      format={format}
      prefix={prefix}
      suffix={suffix}
      className={className}
      style={{ fontVariantNumeric: "tabular-nums" }}
    />
  );
}
