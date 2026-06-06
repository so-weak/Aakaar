import { useEffect, useMemo } from "react";
import {
  Background,
  ConnectionMode,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";
import type { Edge, Node, NodeProps } from "@xyflow/react";
import dagre from "dagre";
import { MonitorSmartphone } from "lucide-react";

import type { Dag, NodeKind, RunEvent, RunStatus } from "@/api/types";
import { useTheme } from "@/theme/ThemeProvider";

/**
 * Compact, status-aware ReactFlow viewer for a live (or finished) run.
 *
 * The detailed RunDetail page renders a full timeline; this is the
 * abstracted view used in the operator console — every node is one
 * tile, colored by per-node status. Multiple LiveDagViewer instances
 * tile cleanly in a grid (no minimap, smaller nodes, no controls by
 * default).
 *
 * Per-node status is derived from the event stream:
 *   succeeded — saw NODE_COMPLETED for this node id
 *   failed    — saw NODE_FAILED
 *   paused    — last RUN_PAUSED for this node has no following SIGNAL_RECEIVED
 *   running   — heuristic: predecessors all succeeded, no terminal event yet
 *   pending   — otherwise
 */

export type NodeStatus =
  | "succeeded"
  | "failed"
  | "paused"
  | "running"
  | "pending";

interface LiveDagNodeData extends Record<string, unknown> {
  label: string;
  ref: string;
  kind: NodeKind;
  status: NodeStatus;
  // The remote agent this node ran on, if a provenance log event named one.
  agent: string | null;
}

const STATUS_STYLES: Record<NodeStatus, { ring: string; chip: string; glow: string }> = {
  succeeded: {
    ring: "ring-emerald-300/70",
    chip: "bg-emerald-300/20 text-emerald-200",
    glow: "shadow-[0_0_0_1px_rgb(110_231_183/0.35)]",
  },
  failed: {
    ring: "ring-rose-400/70",
    chip: "bg-rose-400/20 text-rose-200",
    glow: "shadow-[0_0_0_1px_rgb(251_113_133/0.45)]",
  },
  paused: {
    ring: "ring-amber-300/70",
    chip: "bg-amber-300/25 text-amber-100",
    glow: "shadow-[0_0_18px_rgb(252_211_77/0.35)]",
  },
  running: {
    ring: "ring-signal-cyan/80",
    chip: "bg-signal-cyan/25 text-signal-cyan",
    glow: "shadow-[0_0_22px_rgb(22_217_255/0.45)] animate-pulse",
  },
  pending: {
    ring: "ring-ink-700",
    chip: "bg-ink-800/60 text-ink-500",
    glow: "",
  },
};

function LiveDagNode({ data }: NodeProps<Node<LiveDagNodeData>>) {
  const styles = STATUS_STYLES[data.status];
  return (
    <div
      className={[
        "min-w-[140px] rounded-md border border-ink-700 bg-ink-950/95 px-2 py-1.5 text-left",
        "ring-2 ring-inset",
        styles.ring,
        styles.glow,
      ].join(" ")}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!h-1.5 !w-1.5 !border-0 !bg-ink-600"
      />
      <div className="flex items-center justify-between gap-1.5">
        <div className="truncate font-mono text-[10px] font-semibold uppercase tracking-wide text-ink-300">
          {data.label}
        </div>
        <span
          className={[
            "rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider",
            styles.chip,
          ].join(" ")}
        >
          {data.status}
        </span>
      </div>
      <div className="mt-0.5 truncate font-mono text-[10px] text-ink-100">
        {data.ref}
      </div>
      {data.agent ? (
        <div
          className="mt-1 inline-flex max-w-full items-center gap-1 rounded bg-signal-cyan/15 px-1 py-0.5 font-mono text-[9px] text-signal-cyan"
          title={`Ran on ${data.agent}`}
        >
          <MonitorSmartphone size={9} className="shrink-0" />
          <span className="truncate">{data.agent}</span>
        </div>
      ) : null}
      <Handle
        type="source"
        position={Position.Bottom}
        className="!h-1.5 !w-1.5 !border-0 !bg-ink-600"
      />
    </div>
  );
}

const NODE_TYPES = { live: LiveDagNode };

// ---------- status derivation -------------------------------------------

export function deriveNodeStatuses(
  dag: Dag,
  events: RunEvent[],
  runStatus: RunStatus,
): Record<string, NodeStatus> {
  const out: Record<string, NodeStatus> = {};
  for (const n of dag.nodes) out[n.id] = "pending";

  // Last terminal event per node decides succeeded/failed; track pause state.
  const pausedNodes = new Set<string>();
  for (const e of events) {
    if (!e.node_id) continue;
    if (e.kind === "node_completed") {
      out[e.node_id] = "succeeded";
      pausedNodes.delete(e.node_id);
    } else if (e.kind === "node_failed") {
      out[e.node_id] = "failed";
      pausedNodes.delete(e.node_id);
    } else if (e.kind === "run_paused") {
      pausedNodes.add(e.node_id);
    } else if (e.kind === "signal_received") {
      pausedNodes.delete(e.node_id);
    }
  }
  for (const id of pausedNodes) {
    if (out[id] === "pending") out[id] = "paused";
  }

  if (runStatus === "running" || runStatus === "paused") {
    // Heuristic: any pending node whose predecessors are all "succeeded"
    // is the current step (or one of them, in parallel branches).
    const preds: Record<string, string[]> = {};
    for (const n of dag.nodes) preds[n.id] = [];
    for (const e of dag.edges) {
      if (preds[e.to] !== undefined) preds[e.to].push(e.from);
    }
    for (const n of dag.nodes) {
      if (out[n.id] !== "pending") continue;
      const ps = preds[n.id] ?? [];
      if (ps.length === 0 || ps.every((p) => out[p] === "succeeded")) {
        out[n.id] = "running";
      }
    }
  }

  return out;
}

/**
 * Map node_id -> agent alias, read from run-provenance events: a remote node
 * emits a "log" event whose payload names the agent it ran on
 * ({ message, agent }). The last such event per node wins.
 */
export function deriveNodeAgents(events: RunEvent[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const e of events) {
    if (e.kind !== "log" || !e.node_id) continue;
    const agent = (e.payload as { agent?: unknown })?.agent;
    if (typeof agent === "string" && agent) out[e.node_id] = agent;
  }
  return out;
}

// ---------- layout -------------------------------------------------------

function layout(
  nodes: Node<LiveDagNodeData>[],
  edges: Edge[],
  compact: boolean,
): { nodes: Node<LiveDagNodeData>[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: "TB",
    nodesep: compact ? 22 : 36,
    ranksep: compact ? 32 : 50,
    marginx: 12,
    marginy: 12,
  });

  const nodeWidth = compact ? 150 : 220;
  const nodeHeight = compact ? 50 : 64;
  for (const n of nodes) g.setNode(n.id, { width: nodeWidth, height: nodeHeight });
  for (const e of edges) g.setEdge(e.source, e.target);

  dagre.layout(g);

  const out = nodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: { x: pos.x - nodeWidth / 2, y: pos.y - nodeHeight / 2 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    };
  });
  return { nodes: out, edges };
}

// ---------- viewer -------------------------------------------------------

interface LiveDagViewerProps {
  dag: Dag;
  events: RunEvent[];
  runStatus: RunStatus;
  /** Compact mode: smaller nodes, tighter spacing — for tile grids. */
  compact?: boolean;
}

function LiveDagViewerInner({
  dag,
  events,
  runStatus,
  compact = false,
}: LiveDagViewerProps) {
  const { meta } = useTheme();
  const isDark = meta.mode === "dark";
  // Two edge colors per mode:
  //   - `activeEdge`   — the upstream node has succeeded (run animates).
  //   - `inactiveEdge` — pending / future hop, drawn faintly.
  // Mint stays visible in both modes; the dim lime from the dark theme
  // would disappear on white, so light mode swaps it for faint ink.
  const activeEdge = isDark
    ? "rgb(110 231 183 / 0.85)"  // mint — neon-grunge / retro
    : "rgb(4 120 87 / 0.85)";    // emerald-700 on white
  const inactiveEdge = isDark
    ? "rgb(217 251 29 / 0.40)"
    : "rgb(15 23 42 / 0.30)";

  const statuses = useMemo(
    () => deriveNodeStatuses(dag, events, runStatus),
    [dag, events, runStatus],
  );
  const agentsByNode = useMemo(() => deriveNodeAgents(events), [events]);

  const initial = useMemo(() => {
    const nodes: Node<LiveDagNodeData>[] = dag.nodes.map((n) => ({
      id: n.id,
      type: "live",
      position: { x: 0, y: 0 },
      data: {
        label: n.id,
        ref: n.ref,
        kind: n.kind,
        status: statuses[n.id] ?? "pending",
        agent: agentsByNode[n.id] ?? null,
      },
    }));
    const edges: Edge[] = dag.edges.map((e, i) => {
      const fromStatus = statuses[e.from];
      const active = fromStatus === "succeeded";
      return {
        id: `e-${i}-${e.from}-${e.to}`,
        source: e.from,
        target: e.to,
        animated: active && runStatus === "running",
        style: {
          stroke: active ? activeEdge : inactiveEdge,
          strokeWidth: 1.4,
        },
      };
    });
    return layout(nodes, edges, compact);
  }, [dag, statuses, agentsByNode, runStatus, compact, activeEdge, inactiveEdge]);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node<LiveDagNodeData>>(
    initial.nodes,
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initial.edges);

  useEffect(() => {
    setNodes(initial.nodes);
    setEdges(initial.edges);
  }, [initial, setNodes, setEdges]);

  // <Background> dot color, picked off the same `isDark` derived above.
  const dotColor = isDark
    ? "rgb(244 237 215 / 0.10)"
    : "rgb(15 23 42 / 0.12)";

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      fitView
      fitViewOptions={{ padding: compact ? 0.12 : 0.18 }}
      proOptions={{ hideAttribution: true }}
      connectionMode={ConnectionMode.Loose}
      minZoom={0.15}
      maxZoom={1.5}
      colorMode={meta.mode}
      panOnDrag={!compact}
      zoomOnScroll={!compact}
      zoomOnPinch={!compact}
      zoomOnDoubleClick={!compact}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
    >
      <Background color={dotColor} gap={18} size={1} />
    </ReactFlow>
  );
}

export function LiveDagViewer(props: LiveDagViewerProps) {
  return (
    <ReactFlowProvider>
      <LiveDagViewerInner {...props} />
    </ReactFlowProvider>
  );
}
