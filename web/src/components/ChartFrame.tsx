import { useState, type ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow }
  from "@/components/ui/table";

/** Every chart owes a table view.
 *
 * The validated light palette carries a contrast WARN on three of five slots
 * (2.11-2.74:1 against #fcfcfb). That warning is not dismissable: it obligates
 * relief, and a table is the relief. It is also simply the fastest way to read
 * an exact number off a chart.
 */
export function ChartFrame({ title, columns, rows, children }: {
  title: string;
  columns: string[];
  rows: (string | number)[][];
  children: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-base">{title}</CardTitle>
        <Button variant="ghost" size="sm" onClick={() => setAsTable((v) => !v)}>
          {asTable ? "chart" : "table"}
        </Button>
      </CardHeader>
      <CardContent>
        {asTable ? (
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
