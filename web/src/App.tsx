import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Overview } from "@/views/Overview";
import { Board } from "@/views/Board";
import { Spend } from "@/views/Spend";
import { Throughput } from "@/views/Throughput";
import { GraphTab } from "@/views/GraphTab";
import { TaskSheet } from "@/views/TaskSheet";

const qc = new QueryClient();

export default function App() {
  const [tab, setTab] = useState("overview");
  // Which task's detail sheet is open. Overview and Board both hand it their
  // row's task_id via onOpen; TaskSheet is mounted outside <Tabs> below so it
  // opens the same way from any view.
  const [openId, setOpenId] = useState<string | null>(null);
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
          <TabsContent value="overview"><Overview onOpen={setOpenId} /></TabsContent>
          <TabsContent value="board"><Board onOpen={setOpenId} /></TabsContent>
          <TabsContent value="spend"><Spend /></TabsContent>
          <TabsContent value="throughput"><Throughput /></TabsContent>
          <TabsContent value="graph"><GraphTab /></TabsContent>
        </Tabs>
        <TaskSheet id={openId} onClose={() => setOpenId(null)} />
      </div>
    </QueryClientProvider>
  );
}
