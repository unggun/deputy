import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const qc = new QueryClient();

export default function App() {
  const [tab, setTab] = useState("overview");
  return (
    <QueryClientProvider client={qc}>
      <div className="min-h-screen bg-background text-foreground p-4 md:p-6">
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="board">Board</TabsTrigger>
            <TabsTrigger value="spend">Spend</TabsTrigger>
            <TabsTrigger value="throughput">Throughput</TabsTrigger>
            <TabsTrigger value="graph">Graph</TabsTrigger>
          </TabsList>
          <TabsContent value="overview">{/* Task 7 */}</TabsContent>
          <TabsContent value="board">{/* Task 7 */}</TabsContent>
          <TabsContent value="spend">{/* Task 8 */}</TabsContent>
          <TabsContent value="throughput">{/* Task 8 */}</TabsContent>
          <TabsContent value="graph">{/* Task 9 */}</TabsContent>
        </Tabs>
      </div>
    </QueryClientProvider>
  );
}
