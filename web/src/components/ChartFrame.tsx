import { useState, type ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";

/** Every chart owes a table view.
 *
 * The validated light palette carries a contrast WARN on three of five slots
 * (2.82 / 2.17 / 2.69:1), measured against the surface these charts are
 * actually drawn on -- the card, `#ffffff`, not the shell background. That
 * warning is not dismissable: it obligates relief, and a table is the relief.
 * It is also simply the fastest way to read an exact number off a chart.
 */
export function ChartFrame({ title, columns, rows, children }: {
  title: string;
  columns: string[];
  rows: (string | number)[][];
  children: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  const empty = rows.length === 0;
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base">{title}</CardTitle>
        {!empty && (
          <Button variant="ghost" size="sm" onClick={() => setAsTable((v) => !v)}>
            {asTable ? "chart" : "table"}
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {empty ? (
          // A blank chart box is indistinguishable from a failed fetch --
          // this dashboard exists to remove exactly that kind of ambiguity.
          <p className="py-8 text-center text-sm text-muted-foreground">
            no data in this window
          </p>
        ) : asTable ? (
          <Table>
            <TableHeader><TableRow>
              {columns.map((c) => <TableHead key={c}>{c}</TableHead>)}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((r, i) => (
                <TableRow key={i}>
                  {r.map((cell, j) => (
                    <TableCell key={j} className="tabular-nums">{cell}</TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : children}
      </CardContent>
    </Card>
  );
}
