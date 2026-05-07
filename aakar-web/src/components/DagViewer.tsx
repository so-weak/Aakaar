import { useEffect, useMemo } from "react";
import {
  Background,
  ConnectionMode,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";
import type { Edge, Node, NodeProps } from "@xyflow/react";
import dagre from "dagre";

import type { Dag, NodeKind } from "@/api/types";

// ---------- node data + custom renderer ---------------------------------

interface DagNodeData extends Record<string, unknown> {
  label: string;
  ref: string;
  kind: NodeKind;
}

const KIND_STYLES: Record<NodeKind, { ring: string; chip: string; chipText: string }> = {
  capability: {
    ring: "ring-emerald-300/45",
    chip: "bg-emerald-300/15",
    chipText: "text-emerald-300",
  },
  action: {
    ring: "ring-signal-cyan/45",
    chip: "bg-signal-cyan/15",
    chipText: "text-signal-cyan",
  },
  control: {
    ring: "ring-signal-pink/45",
    chip: "bg-signal-pink/15",
    chipText: "text-signal-pink",
  },
};

function DagNode({ data }: NodeProps<Node<DagNodeData>>) {
  const styles = KIND_STYLES[data.kind];
  return (
    <div
      className={[
        "min-w-[210px] rounded-lg border border-ink-700 bg-ink-950/95 px-3 py-2.5 text-left shadow-[6px_6px_0_rgb(0_0_0/0.35)]",
        "ring-1 ring-inset",
        styles.ring,
      ].join(" ")}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!h-2.5 !w-2.5 !border-2 !border-ink-950 !bg-accent-300"
      />
      <div className="flex items-center justify-between gap-2">
        <div className="truncate font-mono text-[11px] font-semibold uppercase tracking-wide text-ink-300">
          {data.label}
        </div>
        <span
          className={["badge", styles.chip, styles.chipText, "ring-transparent"].join(" ")}
        >
          {data.kind}
        </span>
      </div>
      <div className="mt-1 truncate font-mono text-xs text-ink-50">{data.ref}</div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="!h-2.5 !w-2.5 !border-2 !border-ink-950 !bg-signal-cyan"
      />
    </div>
  );
}

const NODE_TYPES = { dag: DagNode };

// ---------- layout helper ------------------------------------------------

function layout(
  nodes: Node<DagNodeData>[],
  edges: Edge[],
): { nodes: Node<DagNodeData>[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 60, marginx: 20, marginy: 20 });

  for (const n of nodes) g.setNode(n.id, { width: 220, height: 64 });
  for (const e of edges) g.setEdge(e.source, e.target);

  dagre.layout(g);

  const out = nodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: { x: pos.x - 110, y: pos.y - 32 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    };
  });
  return { nodes: out, edges };
}

// ---------- viewer -------------------------------------------------------

function DagViewerInner({ dag }: { dag: Dag }) {
  const initial = useMemo(() => {
    const nodes: Node<DagNodeData>[] = dag.nodes.map((n) => ({
      id: n.id,
      type: "dag",
      position: { x: 0, y: 0 },
      data: { label: n.id, ref: n.ref, kind: n.kind },
    }));
    const edges: Edge[] = dag.edges.map((e, i) => ({
      id: `e-${i}-${e.from}-${e.to}`,
      source: e.from,
      target: e.to,
      animated: true,
      style: { stroke: "rgb(217 251 29 / 0.75)", strokeWidth: 1.7 },
    }));
    return layout(nodes, edges);
  }, [dag]);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node<DagNodeData>>(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initial.edges);

  useEffect(() => {
    setNodes(initial.nodes);
    setEdges(initial.edges);
  }, [initial, setNodes, setEdges]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      proOptions={{ hideAttribution: false }}
      connectionMode={ConnectionMode.Loose}
      minZoom={0.2}
      maxZoom={1.5}
      colorMode="dark"
    >
      <Background color="rgb(244 237 215 / 0.18)" gap={22} size={1} />
      <Controls className="!bg-ink-950 !text-ink-100 [&_button]:!border-ink-700 [&_button]:!bg-ink-950 [&_button]:!text-ink-100 [&_button:hover]:!bg-ink-800" />
      <MiniMap
        pannable
        zoomable
        nodeColor={() => "#d9fb1d"}
        maskColor="rgba(9, 9, 8, 0.72)"
        style={{ backgroundColor: "rgb(22 22 20)" }}
      />
    </ReactFlow>
  );
}

export function DagViewer({ dag }: { dag: Dag }) {
  return (
    <ReactFlowProvider>
      <DagViewerInner dag={dag} />
    </ReactFlowProvider>
  );
}
