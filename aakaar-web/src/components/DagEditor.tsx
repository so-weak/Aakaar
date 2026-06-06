import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addEdge,
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
import type {
  Connection,
  Edge,
  Node,
  NodeProps,
  OnSelectionChangeParams,
} from "@xyflow/react";
import { useQuery } from "@tanstack/react-query";
import dagre from "dagre";
import {
  AlertTriangle,
  Box,
  CheckCircle2,
  LayoutGrid,
  MonitorSmartphone,
  Plus,
  Save,
  Search,
  Server,
  Settings2,
  Trash2,
  X,
} from "lucide-react";

import { agents as agentsApi, placement as placementApi } from "@/api";
import type { Dag, DagNode, NodeKind, PlacementIssue } from "@/api/types";
import { useTheme } from "@/theme/ThemeProvider";

// "server"/null both mean "run on the API host". This sentinel is the
// canonical empty value the selector + node renderer share.
const SERVER_TARGET = "server";

/** Normalize a node's target to either a real remote target or null (server). */
function normalizeTarget(target: string | null | undefined): string | null {
  if (!target) return null;
  const t = target.trim();
  if (!t || t.toLowerCase() === SERVER_TARGET) return null;
  return t;
}

// ---------------------------------------------------------------------------
// Props
//
// availableRefs is the set of registry refs a node can point at, grouped by
// kind so the palette can render the same colored chips DagViewer uses.
//
// onSave receives a freshly-rebuilt Dag {id, version, nodes, edges} assembled
// from the live canvas state.
// ---------------------------------------------------------------------------

export interface AvailableRef {
  ref: string;
  kind: NodeKind;
  description?: string;
}

export interface DagEditorProps {
  dag: Dag;
  availableRefs: AvailableRef[];
  onSave: (dag: Dag) => void;
  onCancel?: () => void;
}

// ---------------------------------------------------------------------------
// Retry policy shape
//
// The backend Dag/DagNode types in "@/api/types" do not yet model a retry
// policy. We carry it on the node's `inputs` under a reserved key so that
// nothing in the shared type contract changes; integration can later promote
// it to a first-class field on DagNode if desired.
// ---------------------------------------------------------------------------

// TODO(integration): when DagNode gains a typed retry policy field, read/write
// it directly instead of stashing it in inputs under RETRY_KEY.
const RETRY_KEY = "_retry";

interface RetryPolicy {
  max_attempts: number;
  backoff_ms: number;
}

// ---------- node data + custom renderer ------------------------------------
// Mirrors DagViewer's DagNode so the editor canvas matches the read-only view.

interface EditorNodeData extends Record<string, unknown> {
  label: string;
  ref: string;
  kind: NodeKind;
  outputsAs: string | null;
  inputs: Record<string, unknown>;
  retry: RetryPolicy | null;
  // Placement target — null means "server" (the API host). Any other value
  // is a remote agent alias or pool label.
  target: string | null;
  // Set by the live placement check: a human-readable reason this node can't
  // currently be placed on its target (e.g. no online agent). null when fine.
  placementIssue: string | null;
}

const KIND_STYLES: Record<
  NodeKind,
  { ring: string; chip: string; chipText: string; dot: string; palette: string }
> = {
  capability: {
    ring: "ring-emerald-300/45",
    chip: "bg-emerald-300/15",
    chipText: "text-emerald-300",
    dot: "bg-emerald-300",
    palette: "hover:border-emerald-300/60 hover:ring-emerald-300/30",
  },
  action: {
    ring: "ring-signal-cyan/45",
    chip: "bg-signal-cyan/15",
    chipText: "text-signal-cyan",
    dot: "bg-signal-cyan",
    palette: "hover:border-signal-cyan/60 hover:ring-signal-cyan/30",
  },
  control: {
    ring: "ring-signal-pink/45",
    chip: "bg-signal-pink/15",
    chipText: "text-signal-pink",
    dot: "bg-signal-pink",
    palette: "hover:border-signal-pink/60 hover:ring-signal-pink/30",
  },
};

const KIND_ORDER: NodeKind[] = ["capability", "action", "control"];

function EditorNode({ data, selected }: NodeProps<Node<EditorNodeData>>) {
  const styles = KIND_STYLES[data.kind];
  const remoteTarget = normalizeTarget(data.target);
  const hasIssue = !!data.placementIssue;
  return (
    <div
      className={[
        "min-w-[210px] rounded-card border bg-ink-950/95 px-3 py-2.5 text-left brand-shadow-cyan-md",
        "ring-1 ring-inset transition",
        selected ? "border-accent-300 ring-accent-300/70" : "border-ink-700",
        selected ? "" : hasIssue ? "ring-amber-400/70" : styles.ring,
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
      {data.outputsAs ? (
        <div className="mt-1 truncate font-mono text-[10px] text-ink-400">
          {"→ "}
          {data.outputsAs}
        </div>
      ) : null}
      {remoteTarget ? (
        <div
          className="mt-1.5 inline-flex max-w-full items-center gap-1 rounded-control bg-signal-cyan/10 px-1.5 py-0.5 font-mono text-[10px] text-signal-cyan ring-1 ring-inset ring-signal-cyan/30"
          title={`Runs on ${remoteTarget}`}
        >
          <MonitorSmartphone size={11} className="shrink-0" />
          <span className="truncate">{remoteTarget}</span>
        </div>
      ) : null}
      {hasIssue ? (
        <div
          className="mt-1.5 flex items-start gap-1 rounded-control bg-amber-300/10 px-1.5 py-0.5 text-[10px] text-amber-200 ring-1 ring-inset ring-amber-400/30"
          title={data.placementIssue ?? undefined}
        >
          <AlertTriangle size={11} className="mt-px shrink-0" />
          <span className="line-clamp-2">{data.placementIssue}</span>
        </div>
      ) : null}
      <Handle
        type="source"
        position={Position.Bottom}
        className="!h-2.5 !w-2.5 !border-2 !border-ink-950 !bg-signal-cyan"
      />
    </div>
  );
}

const NODE_TYPES = { editor: EditorNode };

// ---------- layout helper (same approach as DagViewer) ---------------------

function layout(
  nodes: Node<EditorNodeData>[],
  edges: Edge[],
): Node<EditorNodeData>[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 60, marginx: 20, marginy: 20 });

  for (const n of nodes) g.setNode(n.id, { width: 220, height: 64 });
  for (const e of edges) g.setEdge(e.source, e.target);

  dagre.layout(g);

  return nodes.map((n) => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: { x: pos.x - 110, y: pos.y - 32 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    };
  });
}

// ---------- helpers --------------------------------------------------------

/** Pretty-print inputs for the JSON textarea, hiding the reserved retry key. */
function inputsToJson(inputs: Record<string, unknown>): string {
  const clone = { ...inputs };
  delete clone[RETRY_KEY];
  if (Object.keys(clone).length === 0) return "{}";
  return JSON.stringify(clone, null, 2);
}

/** Split a stored inputs object into the user-facing inputs + retry policy. */
function splitInputs(inputs: Record<string, unknown>): {
  inputs: Record<string, unknown>;
  retry: RetryPolicy | null;
} {
  const clone = { ...inputs };
  const raw = clone[RETRY_KEY];
  delete clone[RETRY_KEY];
  let retry: RetryPolicy | null = null;
  if (raw && typeof raw === "object") {
    const r = raw as Record<string, unknown>;
    const max = Number(r.max_attempts);
    const backoff = Number(r.backoff_ms);
    if (Number.isFinite(max) || Number.isFinite(backoff)) {
      retry = {
        max_attempts: Number.isFinite(max) ? max : 1,
        backoff_ms: Number.isFinite(backoff) ? backoff : 0,
      };
    }
  }
  return { inputs: clone, retry };
}

/** Re-attach retry policy into the inputs object for persistence. */
function mergeInputs(
  inputs: Record<string, unknown>,
  retry: RetryPolicy | null,
): Record<string, unknown> {
  const out = { ...inputs };
  delete out[RETRY_KEY];
  if (retry) out[RETRY_KEY] = { ...retry };
  return out;
}

/** Generate a unique node id from a ref, avoiding collisions with `taken`. */
function nextId(ref: string, taken: Set<string>): string {
  const base =
    ref
      .split(/[.:/]/)
      .pop()
      ?.replace(/[^a-zA-Z0-9_]/g, "_")
      .toLowerCase() || "node";
  if (!taken.has(base)) return base;
  let i = 2;
  while (taken.has(`${base}_${i}`)) i += 1;
  return `${base}_${i}`;
}

// ---------- editor ---------------------------------------------------------

function DagEditorInner({ dag, availableRefs, onSave, onCancel }: DagEditorProps) {
  const { meta } = useTheme();
  const isDark = meta.mode === "dark";

  const edgeStroke = isDark ? "rgb(217 251 29 / 0.75)" : "rgb(15 23 42 / 0.55)";
  const dotColor = isDark ? "rgb(244 237 215 / 0.18)" : "rgb(15 23 42 / 0.18)";
  const miniMapBg = isDark ? "rgb(22 22 20)" : "rgb(248 250 252)";
  const miniMapMask = isDark ? "rgba(9, 9, 8, 0.72)" : "rgba(15, 23, 42, 0.18)";

  // Map a ref -> its kind, so palette adds and "ref" edits resolve a kind.
  const refKind = useMemo(() => {
    const m = new Map<string, NodeKind>();
    for (const r of availableRefs) m.set(r.ref, r.kind);
    return m;
  }, [availableRefs]);

  // Online agents + their pools feed the "Runs on" selector. Stale data is
  // fine here — the placement check is the source of truth for validity.
  const agentsQ = useQuery({
    queryKey: ["agents"],
    queryFn: agentsApi.list,
    staleTime: 30_000,
  });

  // Distinct online agent aliases + distinct pools (online agents only),
  // each tagged so the selector can label them.
  const placementTargets = useMemo(() => {
    const aliases: string[] = [];
    const pools = new Set<string>();
    for (const a of agentsQ.data ?? []) {
      if (!a.online) continue;
      aliases.push(a.alias);
      for (const p of a.pools) pools.add(p);
    }
    return { aliases, pools: Array.from(pools).sort() };
  }, [agentsQ.data]);

  // Build the initial canvas from the incoming dag, once per dag identity.
  const initial = useMemo(() => {
    const nodes: Node<EditorNodeData>[] = dag.nodes.map((n) => {
      const split = splitInputs(n.inputs ?? {});
      return {
        id: n.id,
        type: "editor",
        position: { x: 0, y: 0 },
        data: {
          label: n.id,
          ref: n.ref,
          kind: n.kind,
          outputsAs: n.outputs_as ?? null,
          inputs: split.inputs,
          retry: split.retry,
          target: normalizeTarget(n.target),
          placementIssue: null,
        },
      };
    });
    const edges: Edge[] = dag.edges.map((e, i) => ({
      id: `e-${i}-${e.from}-${e.to}`,
      source: e.from,
      target: e.to,
      style: { stroke: edgeStroke, strokeWidth: 1.7 },
    }));
    return { nodes: layout(nodes, edges), edges };
    // edgeStroke is applied separately on theme change; layout only on dag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dag]);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node<EditorNodeData>>(
    initial.nodes,
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initial.edges);

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeIds, setSelectedEdgeIds] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const idSeq = useRef(0);

  // Reset the canvas when a different dag is passed in.
  useEffect(() => {
    setNodes(initial.nodes);
    setEdges(initial.edges);
    setSelectedNodeId(null);
    setSelectedEdgeIds([]);
  }, [initial, setNodes, setEdges]);

  // Re-tint edges when the theme flips between light/dark.
  useEffect(() => {
    setEdges((es) =>
      es.map((e) => ({ ...e, style: { ...e.style, stroke: edgeStroke } })),
    );
  }, [edgeStroke, setEdges]);

  const selectedNode = useMemo(
    () => nodes.find((n) => n.id === selectedNodeId) ?? null,
    [nodes, selectedNodeId],
  );

  // ---- inline placement check ----------------------------------------------
  //
  // Debounced: whenever the graph shape or any target changes we ask the
  // backend whether each remote-targeted node can actually be placed, then
  // stamp the per-node reason onto data.placementIssue (rendered as a warning
  // badge). We only write when the issue text changes so this doesn't loop.

  // A compact signature of just the placement-relevant graph shape, so the
  // effect re-runs on id/ref/target/edge changes but not on, say, input edits.
  const placementSig = useMemo(
    () =>
      JSON.stringify({
        n: nodes.map((n) => [
          n.id,
          n.data.ref,
          n.data.kind,
          normalizeTarget(n.data.target),
        ]),
        e: edges.map((e) => [e.source, e.target]),
      }),
    [nodes, edges],
  );

  const applyIssues = useCallback(
    (issues: PlacementIssue[]) => {
      const byNode = new Map<string, string>();
      for (const it of issues) {
        if (!byNode.has(it.node_id)) byNode.set(it.node_id, it.reason);
      }
      setNodes((ns) =>
        ns.map((n) => {
          const next = byNode.get(n.id) ?? null;
          if (n.data.placementIssue === next) return n;
          return { ...n, data: { ...n.data, placementIssue: next } };
        }),
      );
    },
    [setNodes],
  );

  useEffect(() => {
    let cancelled = false;
    // Only bother the backend when at least one node is remote-targeted.
    const hasRemote = nodes.some(
      (n) => n.data.kind !== "control" && normalizeTarget(n.data.target),
    );
    if (!hasRemote) {
      applyIssues([]);
      return;
    }
    const handle = window.setTimeout(() => {
      placementApi
        .check(buildDag())
        .then((res) => {
          if (!cancelled) applyIssues(res.issues);
        })
        .catch(() => {
          // A failing check shouldn't block editing; clear stale warnings.
          if (!cancelled) applyIssues([]);
        });
    }, 500);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
    // buildDag closes over nodes/edges; placementSig captures the parts that
    // matter so we don't refire on unrelated input edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placementSig, applyIssues]);

  // ---- selection -----------------------------------------------------------

  const onSelectionChange = useCallback(
    ({ nodes: selNodes, edges: selEdges }: OnSelectionChangeParams) => {
      setSelectedNodeId(selNodes.length === 1 ? selNodes[0].id : null);
      setSelectedEdgeIds(selEdges.map((e) => e.id));
    },
    [],
  );

  // ---- connect (user-drawn edges) ------------------------------------------

  const onConnect = useCallback(
    (conn: Connection) => {
      if (!conn.source || !conn.target || conn.source === conn.target) return;
      setEdges((es) => {
        // Avoid duplicate edges between the same pair.
        if (es.some((e) => e.source === conn.source && e.target === conn.target)) {
          return es;
        }
        return addEdge(
          {
            ...conn,
            id: `e-new-${conn.source}-${conn.target}-${idSeq.current++}`,
            style: { stroke: edgeStroke, strokeWidth: 1.7 },
          },
          es,
        );
      });
    },
    [setEdges, edgeStroke],
  );

  // ---- palette: add node ----------------------------------------------------

  const addNode = useCallback(
    (item: AvailableRef) => {
      setNodes((ns) => {
        const taken = new Set(ns.map((n) => n.id));
        const id = nextId(item.ref, taken);
        // Stagger new nodes so they don't stack perfectly.
        const offset = ns.length * 28;
        const node: Node<EditorNodeData> = {
          id,
          type: "editor",
          position: { x: 80 + (offset % 320), y: 80 + offset * 0.4 },
          selected: true,
          data: {
            label: id,
            ref: item.ref,
            kind: item.kind,
            outputsAs: null,
            inputs: {},
            retry: null,
            target: null,
            placementIssue: null,
          },
        };
        return [...ns.map((n) => ({ ...n, selected: false })), node];
      });
    },
    [setNodes],
  );

  // ---- delete --------------------------------------------------------------

  const deleteSelectedNode = useCallback(() => {
    if (!selectedNodeId) return;
    setNodes((ns) => ns.filter((n) => n.id !== selectedNodeId));
    setEdges((es) =>
      es.filter((e) => e.source !== selectedNodeId && e.target !== selectedNodeId),
    );
    setSelectedNodeId(null);
  }, [selectedNodeId, setNodes, setEdges]);

  const deleteSelectedEdges = useCallback(() => {
    if (selectedEdgeIds.length === 0) return;
    const drop = new Set(selectedEdgeIds);
    setEdges((es) => es.filter((e) => !drop.has(e.id)));
    setSelectedEdgeIds([]);
  }, [selectedEdgeIds, setEdges]);

  const hasSelection = !!selectedNodeId || selectedEdgeIds.length > 0;

  const deleteSelection = useCallback(() => {
    deleteSelectedNode();
    deleteSelectedEdges();
  }, [deleteSelectedNode, deleteSelectedEdges]);

  // Delete / Backspace removes the current selection. We guard against typing
  // inside the config panel inputs by checking the event target.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key !== "Delete" && e.key !== "Backspace") return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      e.preventDefault();
      deleteSelection();
    },
    [deleteSelection],
  );

  // ---- auto-layout ----------------------------------------------------------

  const autoLayout = useCallback(() => {
    setNodes((ns) => layout(ns, edges));
  }, [edges, setNodes]);

  // ---- mutate selected node config -----------------------------------------

  const patchSelected = useCallback(
    (patch: Partial<EditorNodeData>) => {
      if (!selectedNodeId) return;
      setNodes((ns) =>
        ns.map((n) =>
          n.id === selectedNodeId ? { ...n, data: { ...n.data, ...patch } } : n,
        ),
      );
    },
    [selectedNodeId, setNodes],
  );

  // Renaming a node id rewrites the node, its label, and every incident edge.
  const renameSelected = useCallback(
    (rawId: string) => {
      const newId = rawId.trim();
      if (!selectedNodeId || !newId || newId === selectedNodeId) return;
      const collision = nodes.some((n) => n.id === newId);
      if (collision) return; // UI surfaces the duplicate hint; ignore commit
      const oldId = selectedNodeId;
      setNodes((ns) =>
        ns.map((n) =>
          n.id === oldId
            ? { ...n, id: newId, data: { ...n.data, label: newId } }
            : n,
        ),
      );
      setEdges((es) =>
        es.map((e) => ({
          ...e,
          source: e.source === oldId ? newId : e.source,
          target: e.target === oldId ? newId : e.target,
        })),
      );
      setSelectedNodeId(newId);
    },
    [selectedNodeId, nodes, setNodes, setEdges],
  );

  // ---- save -----------------------------------------------------------------

  const buildDag = useCallback((): Dag => {
    const builtNodes: DagNode[] = nodes.map((n) => ({
      id: n.id,
      kind: n.data.kind,
      ref: n.data.ref,
      inputs: mergeInputs(n.data.inputs, n.data.retry),
      outputs_as:
        n.data.outputsAs && n.data.outputsAs.trim() ? n.data.outputsAs.trim() : null,
      // Control nodes always run on the API host; everything else honors the
      // chosen target (null = server).
      target: n.data.kind === "control" ? null : normalizeTarget(n.data.target),
    }));
    const seen = new Set<string>();
    const builtEdges = edges
      .map((e) => ({ from: e.source, to: e.target }))
      .filter((e) => {
        const key = `${e.from}->${e.to}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    return {
      id: dag.id,
      version: dag.version,
      nodes: builtNodes,
      edges: builtEdges,
    };
  }, [nodes, edges, dag.id, dag.version]);

  const handleSave = useCallback(() => {
    onSave(buildDag());
  }, [onSave, buildDag]);

  // ---- palette grouping + filtering ----------------------------------------

  const filteredGroups = useMemo(() => {
    const q = search.trim().toLowerCase();
    const groups: Record<NodeKind, AvailableRef[]> = {
      capability: [],
      action: [],
      control: [],
    };
    for (const r of availableRefs) {
      if (
        q &&
        !r.ref.toLowerCase().includes(q) &&
        !(r.description ?? "").toLowerCase().includes(q)
      ) {
        continue;
      }
      groups[r.kind].push(r);
    }
    return groups;
  }, [availableRefs, search]);

  const noMatches = KIND_ORDER.every((k) => filteredGroups[k].length === 0);

  return (
    <div
      className="flex h-full min-h-0 w-full overflow-hidden"
      onKeyDown={onKeyDown}
      tabIndex={-1}
    >
      {/* ---------- Left palette ------------------------------------------ */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-ink-700/70 bg-ink-950/40">
        <div className="border-b border-ink-700/60 px-3 py-3">
          <div className="panel-title mb-2 flex items-center gap-1.5">
            <Box size={12} />
            Palette
          </div>
          <div className="relative">
            <Search
              size={14}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-500"
            />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search refs"
              aria-label="Search available refs"
              className="input !py-1.5 !pl-8 text-xs"
            />
          </div>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-3 py-3">
          {KIND_ORDER.map((kind) => {
            const items = filteredGroups[kind];
            if (items.length === 0) return null;
            const styles = KIND_STYLES[kind];
            return (
              <div key={kind}>
                <div className="mb-1.5 flex items-center gap-1.5">
                  <span className={["h-2 w-2 rounded-full", styles.dot].join(" ")} />
                  <span className="panel-title">{kind}</span>
                  <span className="font-mono text-[10px] text-ink-500">
                    {items.length}
                  </span>
                </div>
                <div className="space-y-1">
                  {items.map((item) => (
                    <button
                      key={item.ref}
                      type="button"
                      onClick={() => addNode(item)}
                      title={item.description || item.ref}
                      className={[
                        "group flex w-full items-start gap-2 rounded-control border border-ink-700/70 bg-ink-900/50 px-2.5 py-2 text-left ring-1 ring-inset ring-transparent transition",
                        "hover:bg-ink-800/70",
                        styles.palette,
                      ].join(" ")}
                    >
                      <Plus
                        size={13}
                        className="mt-0.5 shrink-0 text-ink-500 transition group-hover:text-accent-200"
                      />
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-xs text-ink-50">
                          {item.ref}
                        </span>
                        {item.description ? (
                          <span className="mt-0.5 block truncate text-[10px] text-ink-400">
                            {item.description}
                          </span>
                        ) : null}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            );
          })}

          {availableRefs.length === 0 ? (
            <p className="px-1 text-xs text-ink-500">No capabilities available.</p>
          ) : noMatches ? (
            <p className="px-1 text-xs text-ink-500">No refs match your search.</p>
          ) : null}
        </div>
      </aside>

      {/* ---------- Canvas ------------------------------------------------- */}
      <div className="relative flex min-w-0 flex-1 flex-col">
        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-2 border-b border-ink-700/60 bg-ink-950/40 px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="stamp">{nodes.length} nodes</span>
            <span className="stamp">{edges.length} edges</span>
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-ghost !min-h-0 !px-2.5 !py-1.5 text-xs"
              onClick={autoLayout}
              title="Auto-arrange nodes with dagre"
            >
              <LayoutGrid size={14} />
              Auto-layout
            </button>
            <button
              type="button"
              className="btn-ghost !min-h-0 !px-2.5 !py-1.5 text-xs"
              onClick={deleteSelection}
              disabled={!hasSelection}
              title="Delete selected node or edge (Del)"
            >
              <Trash2 size={14} />
              Delete
            </button>
            {onCancel ? (
              <button
                type="button"
                className="btn-ghost !min-h-0 !px-2.5 !py-1.5 text-xs"
                onClick={onCancel}
              >
                <X size={14} />
                Cancel
              </button>
            ) : null}
            <button
              type="button"
              className="btn-primary !min-h-0 !px-3 !py-1.5 text-xs"
              onClick={handleSave}
            >
              <Save size={14} />
              Save
            </button>
          </div>
        </div>

        {/* ReactFlow */}
        <div className="relative min-h-0 flex-1">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onSelectionChange={onSelectionChange}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            proOptions={{ hideAttribution: true }}
            connectionMode={ConnectionMode.Loose}
            deleteKeyCode={null}
            minZoom={0.2}
            maxZoom={1.5}
            colorMode={meta.mode}
          >
            <Background color={dotColor} gap={22} size={1} />
            <Controls className="!bg-ink-950 !text-ink-100 [&_button]:!border-ink-700 [&_button]:!bg-ink-950 [&_button]:!text-ink-100 [&_button:hover]:!bg-ink-800" />
            <MiniMap
              pannable
              zoomable
              nodeColor={() => "#d9fb1d"}
              maskColor={miniMapMask}
              style={{ backgroundColor: miniMapBg }}
            />
          </ReactFlow>
        </div>
      </div>

      {/* ---------- Right config panel ------------------------------------ */}
      <NodeConfigPanel
        node={selectedNode}
        allNodes={nodes}
        refKind={refKind}
        agentAliases={placementTargets.aliases}
        pools={placementTargets.pools}
        onRename={renameSelected}
        onPatch={patchSelected}
        onDelete={deleteSelectedNode}
      />
    </div>
  );
}

// ---------- config panel ---------------------------------------------------

interface NodeConfigPanelProps {
  node: Node<EditorNodeData> | null;
  allNodes: Node<EditorNodeData>[];
  refKind: Map<string, NodeKind>;
  // Online agent aliases + distinct pools offered as remote placement targets.
  agentAliases: string[];
  pools: string[];
  onRename: (id: string) => void;
  onPatch: (patch: Partial<EditorNodeData>) => void;
  onDelete: () => void;
}

function NodeConfigPanel({
  node,
  allNodes,
  refKind,
  agentAliases,
  pools,
  onRename,
  onPatch,
  onDelete,
}: NodeConfigPanelProps) {
  // Local drafts so typing in the id / JSON boxes doesn't fight graph state on
  // every keystroke — we commit on blur (id) or on each valid parse (JSON).
  const [idDraft, setIdDraft] = useState("");
  const [jsonDraft, setJsonDraft] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);

  const nodeId = node?.id ?? null;

  // Reset drafts whenever the selected node changes.
  useEffect(() => {
    if (!node) return;
    setIdDraft(node.id);
    setJsonDraft(inputsToJson(node.data.inputs));
    setJsonError(null);
    // Only re-seed when the selected node id changes, not on every patch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId]);

  if (!node) {
    return (
      <aside className="flex w-72 shrink-0 flex-col border-l border-ink-700/70 bg-ink-950/40">
        <div className="border-b border-ink-700/60 px-3 py-3">
          <div className="panel-title flex items-center gap-1.5">
            <Settings2 size={12} />
            Node config
          </div>
        </div>
        <div className="flex flex-1 items-center justify-center px-6 text-center">
          <p className="text-xs text-ink-500">
            Select a node on the canvas to edit its id, ref, inputs, and retry
            policy.
          </p>
        </div>
      </aside>
    );
  }

  const data = node.data;
  const trimmedId = idDraft.trim();
  const idCollision =
    trimmedId !== "" &&
    trimmedId !== node.id &&
    allNodes.some((n) => n.id === trimmedId);

  const commitJson = (value: string) => {
    setJsonDraft(value);
    const trimmed = value.trim();
    if (trimmed === "") {
      setJsonError(null);
      onPatch({ inputs: {} });
      return;
    }
    try {
      const parsed = JSON.parse(trimmed);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setJsonError("Inputs must be a JSON object.");
        return;
      }
      setJsonError(null);
      onPatch({ inputs: parsed as Record<string, unknown> });
    } catch (err) {
      setJsonError(err instanceof Error ? err.message : "Invalid JSON");
    }
  };

  const setRetryEnabled = (enabled: boolean) => {
    onPatch({ retry: enabled ? { max_attempts: 3, backoff_ms: 1000 } : null });
  };

  const patchRetry = (patch: Partial<RetryPolicy>) => {
    const base = data.retry ?? { max_attempts: 3, backoff_ms: 1000 };
    onPatch({ retry: { ...base, ...patch } });
  };

  const kindStyles = KIND_STYLES[data.kind];

  return (
    <aside className="flex w-72 shrink-0 flex-col border-l border-ink-700/70 bg-ink-950/40">
      <div className="flex items-center justify-between border-b border-ink-700/60 px-3 py-3">
        <div className="panel-title flex items-center gap-1.5">
          <Settings2 size={12} />
          Node config
        </div>
        <span
          className={["badge", kindStyles.chip, kindStyles.chipText, "ring-transparent"].join(
            " ",
          )}
        >
          {data.kind}
        </span>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-3 py-3">
        {/* id */}
        <div>
          <label htmlFor="cfg-id" className="panel-title mb-1 block">
            Node id
          </label>
          <input
            id="cfg-id"
            type="text"
            value={idDraft}
            spellCheck={false}
            onChange={(e) => setIdDraft(e.target.value)}
            onBlur={() => {
              if (idCollision || trimmedId === "") {
                setIdDraft(node.id);
              } else {
                onRename(idDraft);
              }
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
            className="input font-mono text-xs"
          />
          {idCollision ? (
            <p className="mt-1 flex items-center gap-1 text-[10px] text-rose-300">
              <AlertTriangle size={11} />
              A node with this id already exists.
            </p>
          ) : null}
        </div>

        {/* ref */}
        <div>
          <label htmlFor="cfg-ref" className="panel-title mb-1 block">
            Ref
          </label>
          <input
            id="cfg-ref"
            type="text"
            list="cfg-ref-options"
            value={data.ref}
            spellCheck={false}
            onChange={(e) => {
              const ref = e.target.value;
              const kind = refKind.get(ref);
              onPatch(kind ? { ref, kind } : { ref });
            }}
            className="input font-mono text-xs"
          />
          <datalist id="cfg-ref-options">
            {[...refKind.keys()].map((r) => (
              <option key={r} value={r} />
            ))}
          </datalist>
          {!refKind.has(data.ref) ? (
            <p className="mt-1 flex items-center gap-1 text-[10px] text-amber-300">
              <AlertTriangle size={11} />
              Ref not in registry. Kind kept as {data.kind}.
            </p>
          ) : null}
        </div>

        {/* inputs (JSON) */}
        <div>
          <label htmlFor="cfg-inputs" className="panel-title mb-1 block">
            Inputs (JSON)
          </label>
          <textarea
            id="cfg-inputs"
            value={jsonDraft}
            spellCheck={false}
            rows={7}
            onChange={(e) => commitJson(e.target.value)}
            className={[
              "input resize-y font-mono text-[11px] leading-relaxed",
              jsonError ? "!border-rose-400 focus:!border-rose-400" : "",
            ].join(" ")}
            aria-invalid={jsonError ? true : undefined}
          />
          {jsonError ? (
            <p className="mt-1 flex items-center gap-1 text-[10px] text-rose-300">
              <AlertTriangle size={11} />
              {jsonError}
            </p>
          ) : (
            <p className="mt-1 flex items-center gap-1 text-[10px] text-emerald-300">
              <CheckCircle2 size={11} />
              Valid JSON object.
            </p>
          )}
        </div>

        {/* outputs_as */}
        <div>
          <label htmlFor="cfg-outputs" className="panel-title mb-1 block">
            Outputs as
          </label>
          <input
            id="cfg-outputs"
            type="text"
            value={data.outputsAs ?? ""}
            spellCheck={false}
            placeholder="e.g. statement_data"
            onChange={(e) =>
              onPatch({ outputsAs: e.target.value === "" ? null : e.target.value })
            }
            className="input font-mono text-xs"
          />
          <p className="mt-1 text-[10px] text-ink-500">
            Binding name later steps reference. Leave blank for none.
          </p>
        </div>

        {/* runs on (placement target) */}
        <div>
          <label htmlFor="cfg-target" className="panel-title mb-1 flex items-center gap-1.5">
            <MonitorSmartphone size={11} />
            Runs on
          </label>
          {data.kind === "control" ? (
            <>
              <select
                id="cfg-target"
                className="input font-mono text-xs"
                value={SERVER_TARGET}
                disabled
                aria-label="Runs on"
              >
                <option value={SERVER_TARGET}>Server (API host)</option>
              </select>
              <p className="mt-1 flex items-center gap-1 text-[10px] text-ink-500">
                <Server size={11} />
                Control nodes always run on the server.
              </p>
            </>
          ) : (
            <>
              <select
                id="cfg-target"
                className="input font-mono text-xs"
                value={normalizeTarget(data.target) ?? SERVER_TARGET}
                onChange={(e) =>
                  onPatch({
                    target:
                      e.target.value === SERVER_TARGET ? null : e.target.value,
                  })
                }
              >
                <option value={SERVER_TARGET}>Server (API host)</option>
                {agentAliases.length > 0 ? (
                  <optgroup label="Agents (online)">
                    {agentAliases.map((alias) => (
                      <option key={`a-${alias}`} value={alias}>
                        {alias}
                      </option>
                    ))}
                  </optgroup>
                ) : null}
                {pools.length > 0 ? (
                  <optgroup label="Pools">
                    {pools.map((pool) => (
                      <option key={`p-${pool}`} value={pool}>
                        {pool}
                      </option>
                    ))}
                  </optgroup>
                ) : null}
                {/* Keep a stale/unknown target selectable so it isn't silently
                    reset to Server when no online agent matches it. */}
                {normalizeTarget(data.target) &&
                !agentAliases.includes(data.target as string) &&
                !pools.includes(data.target as string) ? (
                  <option value={data.target as string}>
                    {data.target} (offline / unknown)
                  </option>
                ) : null}
              </select>
              {normalizeTarget(data.target) ? (
                <p className="mt-1 text-[10px] text-ink-500">
                  Routed to a remote agent. Leave on Server to run on the API host.
                </p>
              ) : (
                <p className="mt-1 text-[10px] text-ink-500">
                  Default. Choose an online agent or pool to run this node remotely.
                </p>
              )}
              {data.placementIssue ? (
                <p className="mt-1 flex items-start gap-1 text-[10px] text-amber-300">
                  <AlertTriangle size={11} className="mt-px shrink-0" />
                  {data.placementIssue}
                </p>
              ) : null}
            </>
          )}
        </div>

        {/* retry policy */}
        <div className="rounded-control border border-ink-700/70 bg-ink-900/40 p-2.5">
          <label className="flex cursor-pointer items-center justify-between gap-2">
            <span className="panel-title">Retry policy</span>
            <input
              type="checkbox"
              checked={data.retry !== null}
              onChange={(e) => setRetryEnabled(e.target.checked)}
              className="h-4 w-4 accent-accent-300"
              aria-label="Enable retry policy"
            />
          </label>

          {data.retry ? (
            <div className="mt-2.5 grid grid-cols-2 gap-2">
              <div>
                <label
                  htmlFor="cfg-retry-attempts"
                  className="mb-1 block font-mono text-[10px] text-ink-400"
                >
                  max_attempts
                </label>
                <input
                  id="cfg-retry-attempts"
                  type="number"
                  min={1}
                  step={1}
                  value={data.retry.max_attempts}
                  onChange={(e) =>
                    patchRetry({
                      max_attempts: Math.max(1, Number(e.target.value) || 1),
                    })
                  }
                  className="input font-mono text-xs"
                />
              </div>
              <div>
                <label
                  htmlFor="cfg-retry-backoff"
                  className="mb-1 block font-mono text-[10px] text-ink-400"
                >
                  backoff_ms
                </label>
                <input
                  id="cfg-retry-backoff"
                  type="number"
                  min={0}
                  step={100}
                  value={data.retry.backoff_ms}
                  onChange={(e) =>
                    patchRetry({ backoff_ms: Math.max(0, Number(e.target.value) || 0) })
                  }
                  className="input font-mono text-xs"
                />
              </div>
            </div>
          ) : (
            <p className="mt-2 text-[10px] text-ink-500">
              No retries. The node fails on first error.
            </p>
          )}
        </div>
      </div>

      <div className="border-t border-ink-700/60 px-3 py-3">
        <button
          type="button"
          className="btn-danger !min-h-0 w-full !py-1.5 text-xs"
          onClick={onDelete}
        >
          <Trash2 size={14} />
          Delete node
        </button>
      </div>
    </aside>
  );
}

export function DagEditor(props: DagEditorProps) {
  return (
    <ReactFlowProvider>
      <DagEditorInner {...props} />
    </ReactFlowProvider>
  );
}
