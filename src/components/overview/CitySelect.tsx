import { Label, ListBox, Select } from "@heroui/react";
import type { City } from "../../lib/data";

interface CitySelectProps {
  cities: City[];
  selected: string;
  onSelect: (slug: string) => void;
}

/** Compact secondary affordance for switching the active city — the coverage
    grid above is the primary selector, this is the keyboard/quick-jump path. */
export function CitySelect({ cities, selected, onSelect }: CitySelectProps) {
  return (
    <Select
      variant="secondary"
      className="w-full sm:w-[13.5rem]"
      value={selected}
      onChange={(v) => v != null && onSelect(String(v))}
      placeholder="Jump to a city"
    >
      <Label className="sr-only">Active city</Label>
      <Select.Trigger>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      <Select.Popover>
        <ListBox>
          {cities.map((c) => {
            const slug = c.slug ?? c.series_ticker;
            return (
              <ListBox.Item key={slug} id={slug} textValue={c.name ?? slug}>
                {c.name ?? slug}
                <ListBox.ItemIndicator />
              </ListBox.Item>
            );
          })}
        </ListBox>
      </Select.Popover>
    </Select>
  );
}
