import { useState } from "react";

/** A shell command, with a copy button. The dashboard is read-only: deciding
 *  happens at the terminal, and this is the shortest way there. */
export function Command({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // No clipboard (an insecure origin, a denied permission): the command
      // is still on screen to select by hand.
    }
  };
  return (
    <div className="flex items-center gap-2 rounded bg-muted px-2 py-1">
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-xs">
        {text}</code>
      <button onClick={copy} className="shrink-0 text-xs text-muted-foreground"
              aria-label={`copy: ${text}`}>
        {copied ? "copied" : "copy"}</button>
    </div>
  );
}
