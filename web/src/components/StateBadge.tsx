import { Badge } from "@/components/ui/badge";
import { STATE_STATUS, STATUS } from "@/lib/palette";

const ICON: Record<string, string> = {
  good: "✓", warning: "⚠", serious: "▲", critical: "✕",
};

export function StateBadge({ state }: { state: string }) {
  const tone = STATE_STATUS[state] ?? null;
  // The word is always present. Color is a second channel, never the only one.
  return (
    <Badge variant="outline" className="gap-1 font-mono text-xs"
           style={tone ? { borderColor: STATUS[tone], color: STATUS[tone] } : undefined}>
      {tone && <span aria-hidden>{ICON[tone]}</span>}
      {state}
    </Badge>
  );
}
