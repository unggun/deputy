import { useGraph } from "@/lib/api";
import { FetchError } from "@/components/FetchError";
import { StateBadge } from "@/components/StateBadge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

/** The live transition table, rendered beside the board it governs.
 *
 * Served from graph.TRANSITIONS (via /api/graph) rather than a checked-in
 * copy, so it cannot drift. Rendered as a plain table instead of a mermaid
 * diagram: a diagram-layout engine is a lot of dependency weight for an
 * 11-state graph, and a table sorts, reads and greps better anyway. The
 * mermaid source still rides along in the API response for pasting into
 * something that does render it -- it's tucked under the details toggle
 * below rather than thrown away.
 */
export function GraphTab() {
  const { data, isError, error } = useGraph();
  // An empty transition table would read as "the runtime has no rules",
  // which is never true -- so say the fetch failed instead.
  if (isError) return <FetchError what="the state graph" error={error} />;
  const transitions = data?.transitions ?? [];

  return (
    <div className="space-y-4 pt-4">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>From</TableHead>
            <TableHead>To</TableHead>
            <TableHead>Reason</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {transitions.map((t, i) => (
            <TableRow key={i}>
              <TableCell><StateBadge state={t.from} /></TableCell>
              <TableCell><StateBadge state={t.to} /></TableCell>
              <TableCell className="whitespace-normal text-sm text-muted-foreground">{t.label}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {data?.mermaid && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">Mermaid source</summary>
          <pre className="mt-2 overflow-x-auto rounded border p-3 font-mono text-xs">{data.mermaid}</pre>
        </details>
      )}
    </div>
  );
}
