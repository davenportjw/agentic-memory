/**
 * Interactive Memory Graph Engine (Atom Orbital Constellation & Cards DAG)
 * Modeled on LearnMap's AtomCanvas / Cards dual-view graph architecture.
 * Zero external dependencies; high-DPI HTML5 Canvas with pan, zoom, node dragging,
 * 1-hop neighborhood spotlighting, and collision-culled labels.
 */
(function () {
  "use strict";

  const STORAGE_KEY = "es_memory_graph_prefs_v1";
  const MIN_ZOOM = 0.2;
  const MAX_ZOOM = 3.0;
  const BASE_SHELL_STEP = 108;
  const MIN_ARC_SPACING = 38;
  const COLLISION_PAD = 16;
  const REPULSION_RANGE = 120;
  const ITERATIONS = 160;

  const CARD_W = 216;
  const CARD_H = 68;
  const CARD_COL_GAP = 108;
  const CARD_ROW_GAP = 16;

  const TIER_ORDER = ["ORG", "TEAM", "AGENT", "USER"];
  const LIGHT_NODE_PALETTE = {
    tiers: {
      ORG: "#4f46e5",
      TEAM: "#0e7490",
      AGENT: "#0f766e",
      USER: "#059669",
    },
    proposals: {
      NEW: "#7c3aed",
      MODIFIED: "#4f46e5",
      CONFLICT: "#dc2626",
    },
    kinds: {
      root: "#0e7490",
      session: "#475569",
      fallback: "#64748b",
    },
    edges: {
      extracted_from: "#7c3aed",
      updates: "#4f46e5",
      recalled: "#64748b",
      part_of: "#cbd5e1",
      fallback: "#94a3b8",
    },
  };
  const DARK_NODE_PALETTE = {
    tiers: {
      ORG: "#818cf8",
      TEAM: "#38bdf8",
      AGENT: "#2dd4bf",
      USER: "#34d399",
    },
    proposals: {
      NEW: "#a78bfa",
      MODIFIED: "#818cf8",
      CONFLICT: "#f87171",
    },
    kinds: {
      root: "#22d3ee",
      session: "#94a3b8",
      fallback: "#94a3b8",
    },
    edges: {
      extracted_from: "#a78bfa",
      updates: "#818cf8",
      recalled: "#94a3b8",
      part_of: "#334155",
      fallback: "#64748b",
    },
  };

  function loadPrefs() {
    try {
      const raw = window.localStorage?.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        return {
          visual: parsed.visual === "cards" ? "cards" : "atom",
          showRings: parsed.showRings !== false,
          showHubs: parsed.showHubs !== false,
          labelDensity: ["auto", "minimal", "all"].includes(parsed.labelDensity) ? parsed.labelDensity : "auto",
        };
      }
    } catch (_) {
      // Ignore storage errors in private browsing
    }
    return { visual: "atom", showRings: true, showHubs: true, labelDensity: "auto" };
  }

  function savePrefs(prefs) {
    try {
      window.localStorage?.setItem(STORAGE_KEY, JSON.stringify(prefs));
    } catch (_) {
      // Ignore storage errors
    }
  }

  function clampZoom(k) {
    return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, k));
  }

  function truncateLabel(text, maxChars) {
    const str = String(text || "");
    return str.length > maxChars ? str.slice(0, Math.max(1, maxChars - 1)) + "…" : str;
  }

  function escapeHtmlSafe(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function activeNodePalette(isDark) {
    return isDark ? DARK_NODE_PALETTE : LIGHT_NODE_PALETTE;
  }

  function nodeColor(n, isDark = false) {
    const pal = activeNodePalette(isDark);
    if (n.kind === "root") return pal.kinds.root;
    if (n.kind === "session") return pal.kinds.session;
    if (n.kind === "proposal") return pal.proposals[n.status] || pal.proposals.NEW;
    if (n.kind === "hub" || n.kind === "memory") return pal.tiers[n.scopeType] || pal.tiers.USER;
    return pal.kinds.fallback;
  }

  function edgeBaseColor(type, isDark = false) {
    const pal = activeNodePalette(isDark);
    return pal.edges[type] || pal.edges.fallback;
  }

  function edgeDash(type) {
    if (type === "recalled") return [4, 4];
    if (type === "updates") return [6, 3];
    return [];
  }

  function edgeLabel(type) {
    if (type === "extracted_from") return "extracted";
    if (type === "updates") return "updates";
    if (type === "recalled") return "recalled";
    if (type === "part_of") return "in scope";
    return "linked";
  }

  function normalizeTier(scopeType, fallbackTier) {
    const raw = String(scopeType || fallbackTier || "USER").trim().toUpperCase();
    if (raw === "ORGANIZATION" || raw === "ORG") return "ORG";
    if (raw === "TEAM") return "TEAM";
    if (raw === "AGENT") return "AGENT";
    if (raw === "USER") return "USER";
    return TIER_ORDER.includes(fallbackTier) ? fallbackTier : "USER";
  }

  function buildMemoryGraphData(treePayload, sessionsList, opts) {
    const query = String(opts.searchQuery || "").trim().toLowerCase();
    const statusFilter = String(opts.statusFilter || "ALL").toUpperCase();
    const scopeFilter = String(opts.scopeFilter || "ALL").toUpperCase();
    const showHubs = opts.showHubs !== false;

    const rawSessions = Array.isArray(sessionsList) ? sessionsList.slice(0, 12) : [];
    const rawStaged = Array.isArray(treePayload?.staged_commits) ? treePayload.staged_commits : [];
    const tree = treePayload?.tree || {};

    const rawLive = [];
    TIER_ORDER.forEach((tier) => {
      (tree[tier] || []).forEach((n) => {
        if (String(n.commit_status || "SYNCED").toUpperCase() === "SYNCED") {
          const normTier = normalizeTier(n.scope_type, tier);
          if (scopeFilter === "ALL" || normTier === scopeFilter) {
            rawLive.push({ ...n, _tier: normTier });
          }
        }
      });
    });

    const matchesQuery = (parts) => {
      if (!query) return true;
      return parts.some((p) => String(p || "").toLowerCase().includes(query));
    };

    const liveNodes =
      statusFilter === "STAGED"
        ? []
        : rawLive.filter((n) =>
            matchesQuery([n.memory_key, n.scope_type, n.category, n.natural_language_belief, n.value, n.memory_value])
          );

    const stagedNodes =
      statusFilter === "SYNCED"
        ? []
        : rawStaged.filter((p) => {
            const normTier = normalizeTier(p.scope_type, "USER");
            if (scopeFilter !== "ALL" && normTier !== scopeFilter) return false;
            return matchesQuery([p.memory_key, p.scope_type, p.category, p.commit_status, p.source_session_id, p.after_value]);
          });

    const liveKeys = new Set(liveNodes.map((n) => n.memory_key));
    const proposalSessionIds = new Set(stagedNodes.map((p) => p.source_session_id).filter(Boolean));

    const sessions = rawSessions.filter((s) => {
      if (!query) return true;
      const recalledMatch = (s.triggered_memories || []).some((mk) => liveKeys.has(mk));
      return (
        matchesQuery([s.session_id, s.title, s.summary]) ||
        proposalSessionIds.has(s.session_id) ||
        recalledMatch
      );
    });

    const nodes = [];
    const nodeIds = new Set();
    const edges = [];
    const edgeKeys = new Set();

    const addNode = (node) => {
      if (nodeIds.has(node.id)) return;
      nodeIds.add(node.id);
      nodes.push(node);
    };

    const addEdge = (source, target, type) => {
      if (source === target || !nodeIds.has(source) || !nodeIds.has(target)) return;
      const key = `${source}->${target}:${type}`;
      if (edgeKeys.has(key)) return;
      edgeKeys.add(key);
      edges.push({ id: key, source, target, type });
    };

    const activeTiers = new Set();
    liveNodes.forEach((n) => activeTiers.add(n._tier || normalizeTier(n.scope_type, "USER")));
    stagedNodes.forEach((p) => activeTiers.add(normalizeTier(p.scope_type, "USER")));

    if (showHubs && (liveNodes.length || stagedNodes.length || sessions.length)) {
      addNode({
        id: "root:core",
        kind: "root",
        title: "Memory Core",
        subtitle: `${liveNodes.length} live · ${stagedNodes.length} proposals`,
        badge: "CORE",
        scopeType: "ORG",
      });
      TIER_ORDER.forEach((tier) => {
        if (!activeTiers.has(tier)) return;
        const hubId = `hub:${tier}`;
        addNode({
          id: hubId,
          kind: "hub",
          title: `${tier} Scope`,
          subtitle: "Scope tier",
          badge: tier,
          scopeType: tier,
        });
        addEdge("root:core", hubId, "part_of");
      });
    }

    sessions.forEach((s) => {
      addNode({
        id: `session:${s.session_id}`,
        kind: "session",
        title: s.session_id,
        subtitle: (s.title || s.summary || "Session").slice(0, 64),
        badge: "SESSION",
        sessionId: s.session_id,
        payload: s,
      });
    });

    stagedNodes.forEach((p) => {
      const status = String(p.commit_status || "NEW").toUpperCase();
      const scopeType = normalizeTier(p.scope_type, "USER");
      addNode({
        id: `proposal:${p.staged_id || p.memory_key}`,
        kind: "proposal",
        title: p.memory_key,
        subtitle: p.source_session_id ? `From ${p.source_session_id}` : p.category || "proposal",
        badge: status,
        status,
        scopeType,
        memoryKey: p.memory_key,
        stagedId: p.staged_id || "",
        payload: p,
      });
    });

    liveNodes.forEach((n) => {
      const scopeType = n._tier || normalizeTier(n.scope_type, "USER");
      addNode({
        id: `memory:${n.memory_key}`,
        kind: "memory",
        title: n.memory_key,
        subtitle: n.category || "technical",
        badge: scopeType,
        scopeType,
        memoryKey: n.memory_key,
        payload: n,
      });
      if (showHubs) {
        addEdge(`hub:${scopeType}`, `memory:${n.memory_key}`, "part_of");
      }
    });

    // Connect proposals -> sessions, live memories, and scope hubs
    stagedNodes.forEach((p) => {
      const propId = `proposal:${p.staged_id || p.memory_key}`;
      const scopeType = normalizeTier(p.scope_type, "USER");
      if (p.source_session_id && nodeIds.has(`session:${p.source_session_id}`)) {
        addEdge(`session:${p.source_session_id}`, propId, "extracted_from");
      }
      if (nodeIds.has(`memory:${p.memory_key}`)) {
        addEdge(propId, `memory:${p.memory_key}`, "updates");
      } else if (showHubs && nodeIds.has(`hub:${scopeType}`)) {
        addEdge(`hub:${scopeType}`, propId, "part_of");
      }
    });

    // Connect sessions -> recalled live memories
    sessions.forEach((s) => {
      const sid = `session:${s.session_id}`;
      (s.triggered_memories || []).forEach((mk) => {
        if (nodeIds.has(`memory:${mk}`)) {
          addEdge(sid, `memory:${mk}`, "recalled");
        }
      });
    });

    let rootId = "root:core";
    if (!nodeIds.has(rootId) && nodes.length) {
      // Pick highest-degree node as nucleus when hubs are hidden
      const deg = new Map(nodes.map((n) => [n.id, 0]));
      edges.forEach((e) => {
        deg.set(e.source, (deg.get(e.source) || 0) + 1);
        deg.set(e.target, (deg.get(e.target) || 0) + 1);
      });
      rootId = [...nodes].sort((a, b) => (deg.get(b.id) || 0) - (deg.get(a.id) || 0) || a.id.localeCompare(b.id))[0].id;
    }

    return { nodes, edges, rootId };
  }

  function atomRadius(node, isRoot, degree) {
    if (isRoot) return 17;
    if (node.kind === "hub") return 13;
    const degBoost = Math.sqrt(degree) * 1.9;
    const kindBoost = node.kind === "proposal" ? 1.5 : 0;
    return Math.min(14, Number((7 + degBoost + kindBoost).toFixed(2)));
  }

  /**
   * Deterministic orbital + force-relaxed layout ("Atom" view).
   * Ported from LearnMap's computeAtomLayout (atomLayout.ts).
   */
  function computeAtomLayout(nodes, edges, rootId) {
    if (!nodes.length) {
      return {
        nodes: [],
        edges: [],
        shells: [],
        bounds: { minX: -120, maxX: 120, minY: -120, maxY: 120 },
        maxDepth: 0,
      };
    }

    const nodeMap = new Map(nodes.map((n) => [n.id, n]));
    const validEdges = edges.filter((e) => nodeMap.has(e.source) && nodeMap.has(e.target) && e.source !== e.target);

    const degree = new Map();
    const hierAdj = new Map();
    const allAdj = new Map();
    nodes.forEach((n) => {
      degree.set(n.id, 0);
      hierAdj.set(n.id, []);
      allAdj.set(n.id, []);
    });
    validEdges.forEach((e) => {
      degree.set(e.source, (degree.get(e.source) || 0) + 1);
      degree.set(e.target, (degree.get(e.target) || 0) + 1);
      allAdj.get(e.source).push(e.target);
      allAdj.get(e.target).push(e.source);
      if (e.type === "part_of") {
        hierAdj.get(e.source).push(e.target);
        hierAdj.get(e.target).push(e.source);
      }
    });
    hierAdj.forEach((list) => list.sort());
    allAdj.forEach((list) => list.sort());

    const effectiveRoot = nodeMap.has(rootId) ? rootId : nodes[0].id;
    const depth = new Map([[effectiveRoot, 0]]);
    const parentOf = new Map();

    // Phase 1: BFS over hierarchy edges
    const queue = [effectiveRoot];
    for (let i = 0; i < queue.length; i++) {
      const cur = queue[i];
      const d = depth.get(cur);
      for (const next of hierAdj.get(cur) || []) {
        if (!depth.has(next)) {
          depth.set(next, d + 1);
          parentOf.set(next, cur);
          queue.push(next);
        }
      }
    }

    // Phase 2: BFS over remaining provenance edges
    const q2 = [...queue];
    for (let i = 0; i < q2.length; i++) {
      const cur = q2[i];
      const d = depth.get(cur);
      for (const next of allAdj.get(cur) || []) {
        if (!depth.has(next)) {
          depth.set(next, d + 1);
          parentOf.set(next, cur);
          q2.push(next);
        }
      }
    }

    // Phase 3: Disconnected nodes sit on an outer halo shell
    let maxReached = 0;
    depth.forEach((d) => {
      if (d > maxReached) maxReached = d;
    });
    const orphanDepth = maxReached + 1;
    nodes.forEach((n) => {
      if (!depth.has(n.id)) depth.set(n.id, orphanDepth);
    });

    const byShell = new Map();
    let maxDepth = 0;
    nodes.forEach((n) => {
      const d = depth.get(n.id) ?? 1;
      if (d > maxDepth) maxDepth = d;
      if (!byShell.has(d)) byShell.set(d, []);
      byShell.get(d).push(n);
    });

    const shellRadius = new Map([[0, 0]]);
    const shells = [];
    let prevR = 0;
    for (let d = 1; d <= maxDepth; d++) {
      const bucket = byShell.get(d) || [];
      if (!bucket.length) continue;
      const minCircR = (bucket.length * MIN_ARC_SPACING) / (2 * Math.PI);
      const r = Math.round(Math.max(prevR + BASE_SHELL_STEP, minCircR));
      shellRadius.set(d, r);
      shells.push({ depth: d, radius: r, count: bucket.length });
      prevR = r;
    }

    const angleOf = new Map([[effectiveRoot, 0]]);
    const atomNodes = [];

    for (let d = 0; d <= maxDepth; d++) {
      const bucket = byShell.get(d);
      if (!bucket || !bucket.length) continue;
      if (d === 0) {
        const rootNode = bucket[0];
        const deg = degree.get(rootNode.id) || 0;
        atomNodes.push({
          id: rootNode.id,
          node: rootNode,
          isRoot: true,
          depth: 0,
          degree: deg,
          radius: atomRadius(rootNode, true, deg),
          x: 0,
          y: 0,
        });
        continue;
      }

      bucket.sort((a, b) => {
        const pa = parentOf.get(a.id);
        const pb = parentOf.get(b.id);
        const aa = pa !== undefined ? angleOf.get(pa) ?? 0 : Math.PI * 2;
        const ab = pb !== undefined ? angleOf.get(pb) ?? 0 : Math.PI * 2;
        if (Math.abs(aa - ab) > 1e-6) return aa - ab;
        return a.id.localeCompare(b.id);
      });

      const r = shellRadius.get(d) ?? BASE_SHELL_STEP * d;
      const phaseOffset = (d % 2 === 0 ? 0.5 : 0) * ((Math.PI * 2) / Math.max(1, bucket.length));
      bucket.forEach((n, idx) => {
        const theta = (idx / bucket.length) * Math.PI * 2 - Math.PI / 2 + phaseOffset;
        angleOf.set(n.id, theta);
        const deg = degree.get(n.id) || 0;
        atomNodes.push({
          id: n.id,
          node: n,
          isRoot: false,
          depth: d,
          degree: deg,
          radius: atomRadius(n, false, deg),
          x: Math.cos(theta) * r,
          y: Math.sin(theta) * r,
        });
      });
    }

    const vx = new Float64Array(atomNodes.length);
    const vy = new Float64Array(atomNodes.length);
    const idxById = new Map(atomNodes.map((n, i) => [n.id, i]));
    const linkPairs = [];
    validEdges.forEach((e) => {
      const a = idxById.get(e.source);
      const b = idxById.get(e.target);
      if (a === undefined || b === undefined) return;
      const isHier = e.type === "part_of";
      const dDiff = Math.abs(atomNodes[a].depth - atomNodes[b].depth);
      linkPairs.push({
        a,
        b,
        strength: isHier ? 0.055 : 0.032,
        rest: Math.max(52, dDiff * BASE_SHELL_STEP * 0.92),
      });
    });

    const nCount = atomNodes.length;
    for (let iter = 0; iter < ITERATIONS; iter++) {
      const alpha = 1 - iter / ITERATIONS;
      const step = 0.25 + 0.75 * alpha;

      // 1. Radial orbital shell spring
      for (let i = 0; i < nCount; i++) {
        const a = atomNodes[i];
        if (a.isRoot) continue;
        const targetR = shellRadius.get(a.depth) ?? BASE_SHELL_STEP * a.depth;
        const dist = Math.hypot(a.x, a.y) || 1;
        const pull = (targetR - dist) * 0.18 * step;
        vx[i] += (a.x / dist) * pull;
        vy[i] += (a.y / dist) * pull;
      }

      // 2. Link attraction
      for (let k = 0; k < linkPairs.length; k++) {
        const lp = linkPairs[k];
        const na = atomNodes[lp.a];
        const nb = atomNodes[lp.b];
        const dx = nb.x - na.x;
        const dy = nb.y - na.y;
        const dist = Math.hypot(dx, dy) || 1;
        const delta = (dist - lp.rest) * lp.strength * step;
        const fx = (dx / dist) * delta;
        const fy = (dy / dist) * delta;
        if (!na.isRoot) {
          vx[lp.a] += fx;
          vy[lp.a] += fy;
        }
        if (!nb.isRoot) {
          vx[lp.b] -= fx;
          vy[lp.b] -= fy;
        }
      }

      // 3. Pairwise collision + local repulsion
      for (let i = 0; i < nCount; i++) {
        const na = atomNodes[i];
        for (let j = i + 1; j < nCount; j++) {
          const nb = atomNodes[j];
          let dx = nb.x - na.x;
          let dy = nb.y - na.y;
          let distSq = dx * dx + dy * dy;
          if (distSq > REPULSION_RANGE * REPULSION_RANGE) continue;
          if (distSq < 1e-4) {
            dx = (i % 2 === 0 ? 1 : -1) * 0.5;
            dy = (j % 2 === 0 ? 1 : -1) * 0.5;
            distSq = dx * dx + dy * dy;
          }
          const dist = Math.sqrt(distSq);
          const minDist = na.radius + nb.radius + COLLISION_PAD;
          let push = 0;
          if (dist < minDist) {
            push = ((minDist - dist) * 0.55) / dist;
          } else {
            const falloff = 1 - dist / REPULSION_RANGE;
            push = (falloff * falloff * 3.2 * step) / dist;
          }
          const fx = dx * push;
          const fy = dy * push;
          if (!na.isRoot) {
            vx[i] -= fx;
            vy[i] -= fy;
          }
          if (!nb.isRoot) {
            vx[j] += fx;
            vy[j] += fy;
          }
        }
      }

      // 4. Integrate with damping
      for (let i = 0; i < nCount; i++) {
        const a = atomNodes[i];
        if (a.isRoot) {
          a.x = 0;
          a.y = 0;
          continue;
        }
        vx[i] *= 0.62;
        vy[i] *= 0.62;
        const spd = Math.hypot(vx[i], vy[i]);
        if (spd > 24) {
          vx[i] = (vx[i] / spd) * 24;
          vy[i] = (vy[i] / spd) * 24;
        }
        a.x += vx[i];
        a.y += vy[i];
      }
    }

    // Hard collision pass
    for (let pass = 0; pass < 4; pass++) {
      for (let i = 0; i < nCount; i++) {
        const na = atomNodes[i];
        for (let j = i + 1; j < nCount; j++) {
          const nb = atomNodes[j];
          const dx = nb.x - na.x;
          const dy = nb.y - na.y;
          const dist = Math.hypot(dx, dy) || 0.01;
          const minDist = na.radius + nb.radius + 8;
          if (dist >= minDist) continue;
          const overlap = (minDist - dist) / 2;
          const ux = dx / dist;
          const uy = dy / dist;
          if (na.isRoot) {
            nb.x += ux * overlap * 2;
            nb.y += uy * overlap * 2;
          } else if (nb.isRoot) {
            na.x -= ux * overlap * 2;
            na.y -= uy * overlap * 2;
          } else {
            na.x -= ux * overlap;
            na.y -= uy * overlap;
            nb.x += ux * overlap;
            nb.y += uy * overlap;
          }
        }
      }
    }

    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    atomNodes.forEach((a) => {
      a.x = Math.round(a.x * 10) / 10;
      a.y = Math.round(a.y * 10) / 10;
      if (a.x - a.radius < minX) minX = a.x - a.radius;
      if (a.x + a.radius > maxX) maxX = a.x + a.radius;
      if (a.y - a.radius < minY) minY = a.y - a.radius;
      if (a.y + a.radius > maxY) maxY = a.y + a.radius;
    });

    const outerR = shells.length ? shells[shells.length - 1].radius : 120;
    minX = Math.min(minX, -outerR);
    maxX = Math.max(maxX, outerR);
    minY = Math.min(minY, -outerR);
    maxY = Math.max(maxY, outerR);

    return {
      nodes: atomNodes,
      edges: validEdges,
      shells,
      bounds: { minX, maxX, minY, maxY },
      maxDepth,
    };
  }

  /**
   * Deterministic left-to-right layered DAG layout ("Cards" view).
   * Columns: Sessions -> Proposals -> Scope Hubs (optional) -> Live Memories.
   */
  function computeCardsLayout(nodes, edges) {
    if (!nodes.length) {
      return {
        nodes: [],
        edges: [],
        shells: [],
        bounds: { minX: -120, maxX: 120, minY: -120, maxY: 120 },
        maxDepth: 0,
      };
    }

    const degree = new Map(nodes.map((n) => [n.id, 0]));
    edges.forEach((e) => {
      degree.set(e.source, (degree.get(e.source) || 0) + 1);
      degree.set(e.target, (degree.get(e.target) || 0) + 1);
    });

    const buckets = [
      nodes.filter((n) => n.kind === "session"),
      nodes.filter((n) => n.kind === "proposal"),
      nodes.filter((n) => n.kind === "root"),
      nodes.filter((n) => n.kind === "hub"),
      nodes.filter((n) => n.kind === "memory"),
    ].filter((col) => col.length > 0);

    // Sort memories by tier order then title for clean grouping
    buckets.forEach((col) => {
      col.sort((a, b) => {
        if (a.kind === "root") return -1;
        if (b.kind === "root") return 1;
        const ta = TIER_ORDER.indexOf(a.scopeType || "");
        const tb = TIER_ORDER.indexOf(b.scopeType || "");
        if (ta !== tb) return ta - tb;
        return String(a.title || a.id).localeCompare(String(b.title || b.id));
      });
    });

    const positioned = [];
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;

    const totalCols = buckets.length;
    const totalW = totalCols * CARD_W + Math.max(0, totalCols - 1) * CARD_COL_GAP;

    buckets.forEach((col, colIdx) => {
      const colH = col.length * CARD_H + Math.max(0, col.length - 1) * CARD_ROW_GAP;
      const cx = -totalW / 2 + colIdx * (CARD_W + CARD_COL_GAP) + CARD_W / 2;
      col.forEach((n, rowIdx) => {
        const cy = -colH / 2 + rowIdx * (CARD_H + CARD_ROW_GAP) + CARD_H / 2;
        const item = {
          id: n.id,
          node: n,
          isRoot: n.kind === "root",
          depth: colIdx,
          degree: degree.get(n.id) || 0,
          radius: 14,
          w: CARD_W,
          h: CARD_H,
          x: Math.round(cx),
          y: Math.round(cy),
        };
        positioned.push(item);
        minX = Math.min(minX, item.x - CARD_W / 2);
        maxX = Math.max(maxX, item.x + CARD_W / 2);
        minY = Math.min(minY, item.y - CARD_H / 2);
        maxY = Math.max(maxY, item.y + CARD_H / 2);
      });
    });

    return {
      nodes: positioned,
      edges,
      shells: [],
      bounds: { minX, maxX, minY, maxY },
      maxDepth: buckets.length,
    };
  }

  function computeFitTransform(bounds, width, height, padding) {
    const pad = padding ?? 56;
    const bw = Math.max(120, bounds.maxX - bounds.minX);
    const bh = Math.max(120, bounds.maxY - bounds.minY);
    const availW = Math.max(120, width - pad * 2);
    const availH = Math.max(120, height - pad * 2);
    const k = clampZoom(Math.min(1.25, Math.min(availW / bw, availH / bh)));
    const cx = (bounds.minX + bounds.maxX) / 2;
    const cy = (bounds.minY + bounds.maxY) / 2;
    return {
      x: width / 2 - cx * k,
      y: height / 2 - cy * k,
      k,
    };
  }

  class MemoryGraphController {
    constructor(container, callbacks) {
      this.container = container;
      this.callbacks = callbacks || {};
      this.prefs = loadPrefs();
      this.rawData = { nodes: [], edges: [], rootId: "" };
      this.layout = null;
      this.dragOverrides = new Map();
      this.transform = { x: 300, y: 220, k: 1 };
      this.animFrame = null;
      this.didFit = false;
      this.lastNodeSig = "";
      this.lastClickId = null;
      this.lastClickTs = 0;
      this.size = { w: 0, h: 0 };
      this.hoveredId = null;
      this.selectedId = null;
      this.evolveConfig = {
        generations: 3,
        candidatesPerGeneration: 2,
        alphaQualityWeight: 0.65,
        autoApplyEnabled: true,
        autoApplyMinGain: 0.02,
      };
      this.evolveState = {
        status: "idle", // idle | running | complete | cancelled | error
        step: 0,
        totalSteps: 1,
        message: "",
        baseline: null,
        champion: null,
        activeConfig: null,
        fitnessImprovement: 0,
        tokenReductionPct: 0,
        autoApplied: false,
        autoApplyReason: null,
        detailsOpen: false,
        log: [],
      };
      this.pointer = {
        mode: "idle",
        pointerId: -1,
        startX: 0,
        startY: 0,
        lastX: 0,
        lastY: 0,
        moved: false,
        dragNodeId: null,
      };
      this.mountDom();
      this.bindEvents();
    }

    mountDom() {
      const v = this.prefs.visual;
      const viewLabel = v === "atom" ? "◎ Atom ▾" : "⊞ Cards ▾";
      this.container.innerHTML = `
        <div class="memory-graph-stage" id="memoryGraphStage">
          <div class="graph-toolbar" role="toolbar" aria-label="Graph tools">
            <button type="button" id="esGraphViewBtn" class="graph-tb-btn graph-view-anchor" popovertarget="esGraphViewPopover" title="Switch graph view & options (V)">
              ${viewLabel}
            </button>
            <div id="esGraphViewPopover" popover="auto" class="graph-view-popover">
              <div class="graph-popover-section">
                <button type="button" class="graph-pop-opt ${v === "atom" ? "active" : ""}" data-graph-visual="atom" aria-pressed="${v === "atom"}">
                  <span>◎ Atom</span>
                  <span class="graph-pop-check" aria-hidden="true">${v === "atom" ? "✓" : ""}</span>
                </button>
                <button type="button" class="graph-pop-opt ${v === "cards" ? "active" : ""}" data-graph-visual="cards" aria-pressed="${v === "cards"}">
                  <span>⊞ Cards</span>
                  <span class="graph-pop-check" aria-hidden="true">${v === "cards" ? "✓" : ""}</span>
                </button>
              </div>
              <div class="graph-pop-sep"></div>
              <div class="graph-popover-section graph-popover-options">
                <label class="graph-pop-checkrow">
                  <input type="checkbox" id="esGraphShowRings" ${this.prefs.showRings ? "checked" : ""}>
                  <span>Show orbital rings</span>
                </label>
                <label class="graph-pop-checkrow">
                  <input type="checkbox" id="esGraphShowHubs" ${this.prefs.showHubs ? "checked" : ""}>
                  <span>Show scope hubs</span>
                </label>
                <div class="graph-pop-density">
                  <span class="graph-pop-density-label">Labels</span>
                  <div class="graph-density-seg" role="group" aria-label="Label density">
                    <button type="button" data-label-density="auto" class="${this.prefs.labelDensity === "auto" ? "active" : ""}">Auto</button>
                    <button type="button" data-label-density="minimal" class="${this.prefs.labelDensity === "minimal" ? "active" : ""}">Focus</button>
                    <button type="button" data-label-density="all" class="${this.prefs.labelDensity === "all" ? "active" : ""}">All</button>
                  </div>
                </div>
              </div>
            </div>
            <span class="graph-tb-sep" aria-hidden="true"></span>
            <button type="button" id="esGraphFitToolbarBtn" class="graph-tb-btn" title="Fit to screen (0)">⤢ Fit</button>
            <span class="graph-tb-sep" aria-hidden="true"></span>
            <button type="button" id="esGraphEvolveBtn" class="graph-tb-btn graph-evolve-btn graph-evolve-anchor" popovertarget="esGraphEvolvePopover" title="Auto-tune memory retrieval settings">✦ Evolve ▾</button>
            <div id="esGraphEvolvePopover" popover="auto" class="graph-view-popover graph-evolve-popover">
              <div class="graph-evolve-pop-title">Auto-Tune Retrieval</div>
              <div class="graph-pop-density">
                <span class="graph-pop-density-label">Generations</span>
                <div class="graph-density-seg" role="group" aria-label="Generations">
                  <button type="button" data-evolve-gens="2">2</button>
                  <button type="button" data-evolve-gens="3" class="active">3</button>
                  <button type="button" data-evolve-gens="5">5</button>
                </div>
              </div>
              <div class="graph-pop-density">
                <span class="graph-pop-density-label">Candidates</span>
                <div class="graph-density-seg" role="group" aria-label="Candidates per generation">
                  <button type="button" data-evolve-cands="2" class="active">2</button>
                  <button type="button" data-evolve-cands="3">3</button>
                  <button type="button" data-evolve-cands="4">4</button>
                </div>
              </div>
              <div class="graph-evolve-slider-row">
                <div class="graph-evolve-slider-head">
                  <label for="esGraphEvolveAlpha">Quality focus</label>
                  <span id="esGraphEvolveAlphaVal" class="graph-evolve-mono">65%</span>
                </div>
                <input type="range" id="esGraphEvolveAlpha" min="0.2" max="0.95" step="0.05" value="0.65">
              </div>
              <div class="graph-pop-sep"></div>
              <label class="graph-pop-checkrow graph-evolve-checkrow">
                <input type="checkbox" id="esGraphEvolveAutoApply" checked>
                <span>Auto-apply winner</span>
              </label>
              <div class="graph-evolve-slider-row" id="esGraphEvolveThreshWrap">
                <div class="graph-evolve-slider-head">
                  <label for="esGraphEvolveMinGain">Min score gain</label>
                  <span id="esGraphEvolveMinGainVal" class="graph-evolve-mono">+0.02</span>
                </div>
                <input type="range" id="esGraphEvolveMinGain" min="0" max="0.25" step="0.01" value="0.02">
              </div>
              <button type="button" id="esGraphEvolveStartBtn" class="graph-evolve-start-btn">Start Evolution</button>
            </div>
          </div>

          <div id="memoryGraphEvolveHud" class="graph-evolve-hud hidden" role="status" aria-live="polite">
            <div class="graph-evolve-hud-row">
              <span id="esGraphEvolveDot" class="graph-evolve-dot" aria-hidden="true"></span>
              <span id="esGraphEvolveStatusText" class="graph-evolve-status-text">Ready</span>
              <span id="esGraphEvolveMetrics" class="graph-evolve-metrics"></span>
              <div class="graph-evolve-hud-actions">
                <button type="button" id="esGraphEvolveDetailsBtn" class="graph-evolve-hud-btn" aria-expanded="false">Details ▾</button>
                <button type="button" id="esGraphEvolveApplyBtn" class="graph-evolve-hud-btn graph-evolve-apply-btn hidden">Apply Winner</button>
                <button type="button" id="esGraphEvolveStopBtn" class="graph-evolve-hud-btn graph-evolve-stop-btn hidden">Stop</button>
                <button type="button" id="esGraphEvolveDismissBtn" class="graph-evolve-hud-btn hidden" aria-label="Dismiss progress">✕</button>
              </div>
            </div>
            <progress id="esGraphEvolveProgress" class="graph-evolve-progress" max="100" value="0"></progress>
            <div id="esGraphEvolveLog" class="graph-evolve-log hidden"></div>
          </div>

          <canvas id="memoryGraphCanvas" class="memory-graph-canvas" tabindex="0" role="img" aria-label="Interactive memory provenance graph"></canvas>

          <div id="memoryGraphEmpty" class="memory-graph-empty hidden">
            No matching sessions, proposals, or memories to display.
          </div>

          <div id="memoryGraphTip" class="atom-tip hidden" aria-hidden="true"></div>

          <div class="atom-controls" role="group" aria-label="Zoom controls">
            <button type="button" id="esGraphZoomInBtn" title="Zoom in (+)" aria-label="Zoom in">＋</button>
            <button type="button" id="esGraphZoomOutBtn" title="Zoom out (-)" aria-label="Zoom out">−</button>
            <button type="button" id="esGraphFitBtn" title="Fit to screen (0)" aria-label="Fit to screen">⤢</button>
            <button type="button" id="esGraphResetBtn" class="hidden" title="Reset dragged positions" aria-label="Reset positions">↺</button>
          </div>

          <div class="memory-graph-legend" id="memoryGraphLegend" aria-hidden="true">
            <span><i data-legend-swatch="session"></i>Session</span>
            <span><i data-legend-swatch="proposal"></i>Proposal</span>
            <span><i data-legend-swatch="ORG"></i>ORG</span>
            <span><i data-legend-swatch="TEAM"></i>TEAM</span>
            <span><i data-legend-swatch="AGENT"></i>AGENT</span>
            <span><i data-legend-swatch="USER"></i>USER</span>
          </div>

          <div id="memoryGraphHint" class="memory-graph-hint"></div>
        </div>
      `;

      this.stage = this.container.querySelector("#memoryGraphStage");
      this.canvas = this.container.querySelector("#memoryGraphCanvas");
      this.emptyEl = this.container.querySelector("#memoryGraphEmpty");
      this.tipEl = this.container.querySelector("#memoryGraphTip");
      this.hintEl = this.container.querySelector("#memoryGraphHint");
      this.viewBtn = this.container.querySelector("#esGraphViewBtn");
      this.popoverEl = this.container.querySelector("#esGraphViewPopover");
      this.evolveBtn = this.container.querySelector("#esGraphEvolveBtn");
      this.evolvePopoverEl = this.container.querySelector("#esGraphEvolvePopover");
      this.evolveHudEl = this.container.querySelector("#memoryGraphEvolveHud");
      this.evolveDotEl = this.container.querySelector("#esGraphEvolveDot");
      this.evolveStatusTextEl = this.container.querySelector("#esGraphEvolveStatusText");
      this.evolveMetricsEl = this.container.querySelector("#esGraphEvolveMetrics");
      this.evolveProgressEl = this.container.querySelector("#esGraphEvolveProgress");
      this.evolveLogEl = this.container.querySelector("#esGraphEvolveLog");
      this.evolveDetailsBtn = this.container.querySelector("#esGraphEvolveDetailsBtn");
      this.evolveApplyBtn = this.container.querySelector("#esGraphEvolveApplyBtn");
      this.evolveStopBtn = this.container.querySelector("#esGraphEvolveStopBtn");
      this.evolveDismissBtn = this.container.querySelector("#esGraphEvolveDismissBtn");
      this.evolveStartBtn = this.container.querySelector("#esGraphEvolveStartBtn");
      this.resetBtn = this.container.querySelector("#esGraphResetBtn");
      this.legendEl = this.container.querySelector("#memoryGraphLegend");
      this.syncLegendColors();
    }

    syncLegendColors() {
      if (!this.legendEl) return;
      const isDark = document.documentElement.getAttribute("data-theme") === "dark";
      const pal = activeNodePalette(isDark);
      const map = {
        session: pal.kinds.session,
        proposal: pal.proposals.NEW,
        ORG: pal.tiers.ORG,
        TEAM: pal.tiers.TEAM,
        AGENT: pal.tiers.AGENT,
        USER: pal.tiers.USER,
      };
      this.legendEl.querySelectorAll("[data-legend-swatch]").forEach((el) => {
        const key = el.getAttribute("data-legend-swatch");
        if (key && map[key]) el.style.background = map[key];
      });
    }

    bindEvents() {
      // Repaint canvas and legend immediately when light/dark theme toggles
      if (typeof MutationObserver !== "undefined" && document.documentElement) {
        this.themeObserver = new MutationObserver(() => {
          this.syncLegendColors();
          this.draw();
        });
        this.themeObserver.observe(document.documentElement, {
          attributes: true,
          attributeFilter: ["data-theme"],
        });
      }

      // ResizeObserver for crisp high-DPI canvas
      this.resizeObserver = new ResizeObserver(() => {
        const rect = this.stage.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          const nextW = Math.round(rect.width);
          const nextH = Math.round(rect.height);
          if (nextW !== this.size.w || nextH !== this.size.h) {
            this.size = { w: nextW, h: nextH };
            if (!this.didFit && this.layout?.nodes.length) {
              this.didFit = true;
              this.fitView(false);
            } else {
              this.draw();
            }
          }
        }
      });
      this.resizeObserver.observe(this.stage);

      // Fallback positioning when CSS Anchor Positioning is unsupported
      if (this.popoverEl) {
        this.popoverEl.addEventListener("toggle", (evt) => {
          if (evt.newState === "open" && (!window.CSS || !CSS.supports("anchor-name", "--a"))) {
            const r = this.viewBtn.getBoundingClientRect();
            this.popoverEl.style.position = "fixed";
            this.popoverEl.style.top = `${Math.round(r.bottom + 6)}px`;
            this.popoverEl.style.left = `${Math.max(8, Math.round(r.left))}px`;
            this.popoverEl.style.margin = "0";
          }
        });
      }

      if (this.evolvePopoverEl) {
        this.evolvePopoverEl.addEventListener("toggle", (evt) => {
          if (
            evt.newState === "open" &&
            (!window.CSS || !CSS.supports("position-area", "block-end span-inline-end"))
          ) {
            const r = this.evolveBtn.getBoundingClientRect();
            const popW = this.evolvePopoverEl.offsetWidth || 260;
            const maxLeft = Math.max(8, window.innerWidth - popW - 8);
            this.evolvePopoverEl.style.position = "fixed";
            this.evolvePopoverEl.style.top = `${Math.round(r.bottom + 6)}px`;
            this.evolvePopoverEl.style.left = `${Math.max(8, Math.min(Math.round(r.left), maxLeft))}px`;
            this.evolvePopoverEl.style.margin = "0";
          }
        });
      }

      // Evolve config popover controls
      this.container.querySelectorAll("[data-evolve-gens]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const val = Number(btn.getAttribute("data-evolve-gens") || 3);
          this.evolveConfig.generations = val;
          this.container.querySelectorAll("[data-evolve-gens]").forEach((b) => {
            b.classList.toggle("active", Number(b.getAttribute("data-evolve-gens")) === val);
          });
        });
      });

      this.container.querySelectorAll("[data-evolve-cands]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const val = Number(btn.getAttribute("data-evolve-cands") || 2);
          this.evolveConfig.candidatesPerGeneration = val;
          this.container.querySelectorAll("[data-evolve-cands]").forEach((b) => {
            b.classList.toggle("active", Number(b.getAttribute("data-evolve-cands")) === val);
          });
        });
      });

      const alphaInput = this.container.querySelector("#esGraphEvolveAlpha");
      const alphaValEl = this.container.querySelector("#esGraphEvolveAlphaVal");
      alphaInput?.addEventListener("input", () => {
        const v = Number(alphaInput.value || 0.65);
        this.evolveConfig.alphaQualityWeight = v;
        if (alphaValEl) alphaValEl.textContent = `${Math.round(v * 100)}%`;
      });

      const autoApplyInput = this.container.querySelector("#esGraphEvolveAutoApply");
      const threshWrap = this.container.querySelector("#esGraphEvolveThreshWrap");
      const minGainInput = this.container.querySelector("#esGraphEvolveMinGain");
      const minGainValEl = this.container.querySelector("#esGraphEvolveMinGainVal");

      autoApplyInput?.addEventListener("change", () => {
        this.evolveConfig.autoApplyEnabled = !!autoApplyInput.checked;
        if (threshWrap) threshWrap.classList.toggle("is-disabled", !this.evolveConfig.autoApplyEnabled);
        if (minGainInput) minGainInput.disabled = !this.evolveConfig.autoApplyEnabled;
      });

      minGainInput?.addEventListener("input", () => {
        const v = Number(minGainInput.value || 0);
        this.evolveConfig.autoApplyMinGain = v;
        if (minGainValEl) minGainValEl.textContent = `+${v.toFixed(2)}`;
      });

      this.evolveStartBtn?.addEventListener("click", () => {
        if (this.evolvePopoverEl?.hidePopover) {
          try {
            this.evolvePopoverEl.hidePopover();
          } catch (_) {}
        }
        if (typeof this.callbacks.onStartEvolve === "function") {
          this.callbacks.onStartEvolve({
            generations: this.evolveConfig.generations,
            candidates_per_generation: this.evolveConfig.candidatesPerGeneration,
            alpha_quality_weight: this.evolveConfig.alphaQualityWeight,
            auto_apply_min_gain: this.evolveConfig.autoApplyEnabled
              ? this.evolveConfig.autoApplyMinGain
              : null,
          });
        }
      });

      this.evolveStopBtn?.addEventListener("click", () => {
        if (typeof this.callbacks.onCancelEvolve === "function") {
          this.callbacks.onCancelEvolve();
        }
      });

      this.evolveApplyBtn?.addEventListener("click", () => {
        const champCfg = this.evolveState.champion?.config || null;
        if (champCfg && typeof this.callbacks.onApplyEvolveWinner === "function") {
          this.callbacks.onApplyEvolveWinner(champCfg, this.evolveState.champion);
        }
      });

      this.evolveDetailsBtn?.addEventListener("click", () => {
        this.evolveState.detailsOpen = !this.evolveState.detailsOpen;
        this.renderEvolutionHud();
      });

      this.evolveDismissBtn?.addEventListener("click", () => {
        this.evolveState.status = "idle";
        this.evolveState.activeConfig = null;
        this.evolveState.detailsOpen = false;
        this.renderEvolutionHud();
        this.draw();
      });

      // View mode buttons
      this.container.querySelectorAll("[data-graph-visual]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const nextVisual = btn.getAttribute("data-graph-visual");
          this.setVisual(nextVisual);
          if (this.popoverEl?.hidePopover) {
            try {
              this.popoverEl.hidePopover();
            } catch (_) {}
          }
        });
      });

      // Options inside popover
      this.container.querySelector("#esGraphShowRings")?.addEventListener("change", (e) => {
        this.prefs.showRings = !!e.target.checked;
        savePrefs(this.prefs);
        this.draw();
      });

      this.container.querySelector("#esGraphShowHubs")?.addEventListener("change", (e) => {
        this.prefs.showHubs = !!e.target.checked;
        savePrefs(this.prefs);
        this.dragOverrides.clear();
        this.didFit = false;
      this.lastNodeSig = "";
      this.lastClickId = null;
      this.lastClickTs = 0;
        this.recomputeFromLastInputs();
      });

      this.container.querySelectorAll("[data-label-density]").forEach((btn) => {
        btn.addEventListener("click", () => {
          const density = btn.getAttribute("data-label-density");
          this.prefs.labelDensity = density;
          savePrefs(this.prefs);
          this.syncControlsUi();
          this.draw();
        });
      });

      // Zoom & Fit buttons
      this.container.querySelector("#esGraphFitToolbarBtn")?.addEventListener("click", () => this.fitView(true));
      this.container.querySelector("#esGraphFitBtn")?.addEventListener("click", () => this.fitView(true));
      this.container.querySelector("#esGraphZoomInBtn")?.addEventListener("click", () => this.zoomBy(1.25));
      this.container.querySelector("#esGraphZoomOutBtn")?.addEventListener("click", () => this.zoomBy(1 / 1.25));
      this.resetBtn?.addEventListener("click", () => {
        this.dragOverrides.clear();
        this.syncControlsUi();
        this.draw();
      });

      // Canvas wheel zoom centered on cursor
      this.canvas.addEventListener(
        "wheel",
        (e) => {
          e.preventDefault();
          this.cancelAnim();
          const r = this.canvas.getBoundingClientRect();
          const cx = e.clientX - r.left;
          const cy = e.clientY - r.top;
          const factor = Math.exp(-e.deltaY * 0.0018);
          this.zoomBy(factor, cx, cy);
        },
        { passive: false }
      );

      // Pointer pan / drag / click
      this.canvas.addEventListener("pointerdown", (e) => this.onPointerDown(e));
      this.canvas.addEventListener("pointermove", (e) => this.onPointerMove(e));
      this.canvas.addEventListener("pointerup", (e) => this.onPointerUp(e));
      this.canvas.addEventListener("pointercancel", (e) => this.onPointerUp(e));
      this.canvas.addEventListener("pointerleave", () => {
        if (this.hoveredId !== null) {
          this.hoveredId = null;
          this.hideTip();
          this.canvas.classList.remove("pointer");
          this.draw();
        }
      });
      this.canvas.addEventListener("dblclick", (e) => this.onDoubleClick(e));
      this.canvas.addEventListener("keydown", (e) => this.onKeyDown(e));
    }

    setVisual(nextVisual) {
      const normalized = nextVisual === "cards" ? "cards" : "atom";
      if (this.prefs.visual === normalized) return;
      this.prefs.visual = normalized;
      savePrefs(this.prefs);
      this.dragOverrides.clear();
      this.didFit = false;
      this.lastNodeSig = "";
      this.lastClickId = null;
      this.lastClickTs = 0;
      this.syncControlsUi();
      this.recomputeLayout();
      if (this.size.w > 0 && this.size.h > 0) {
        this.didFit = true;
        this.fitView(true);
      } else {
        this.draw();
      }
    }

    syncControlsUi() {
      const v = this.prefs.visual;
      if (this.viewBtn) {
        this.viewBtn.textContent = v === "atom" ? "◎ Atom ▾" : "⊞ Cards ▾";
      }
      this.container.querySelectorAll("[data-graph-visual]").forEach((btn) => {
        const active = btn.getAttribute("data-graph-visual") === v;
        btn.classList.toggle("active", active);
        btn.setAttribute("aria-pressed", String(active));
        const check = btn.querySelector(".graph-pop-check");
        if (check) check.textContent = active ? "✓" : "";
      });
      this.container.querySelectorAll("[data-label-density]").forEach((btn) => {
        const active = btn.getAttribute("data-label-density") === this.prefs.labelDensity;
        btn.classList.toggle("active", active);
      });
      if (this.resetBtn) {
        this.resetBtn.classList.toggle("hidden", this.dragOverrides.size === 0);
      }
    }

    updateEvolutionProgress(evt) {
      if (!evt || typeof evt !== "object") return;
      const st = this.evolveState;
      const type = String(evt.type || "");

      if (type === "start") {
        st.status = "running";
        st.step = 0;
        st.totalSteps = Math.max(1, Number(evt.total_steps || 1));
        st.message = evt.message || "Starting evolution…";
        st.baseline = null;
        st.champion = null;
        st.activeConfig = null;
        st.fitnessImprovement = 0;
        st.tokenReductionPct = 0;
        st.autoApplied = false;
        st.autoApplyReason = null;
        st.log = [];
      } else if (type === "phase") {
        st.status = "running";
        st.step = Number(evt.step ?? st.step);
        st.totalSteps = Math.max(1, Number(evt.total_steps || st.totalSteps));
        st.message = evt.message || st.message;
        if (evt.config) {
          st.activeConfig = evt.config;
        }
      } else if (type === "baseline") {
        st.status = "running";
        st.step = Number(evt.step ?? 1);
        st.totalSteps = Math.max(1, Number(evt.total_steps || st.totalSteps));
        st.baseline = evt.entry || null;
        st.champion = evt.entry || null;
        st.activeConfig = evt.entry?.config || st.activeConfig;
        st.message = evt.message || "Baseline evaluated";
        st.log.unshift({
          label: "Gen 0 · Baseline",
          cfg: `${evt.entry?.config?.embedding_dimensions || 768}d · ${evt.entry?.config?.injection_format || "FULL_BELIEF"}`,
          score: Number(evt.entry?.pareto_fitness || 0).toFixed(3),
          tokens: `${Math.round(Number(evt.entry?.avg_tokens_used || 0))} tok`,
          elite: false,
          rationale: evt.entry?.config?.mutation_rationale || "Baseline",
        });
      } else if (type === "candidate") {
        st.status = "running";
        st.step = Number(evt.step ?? st.step);
        st.totalSteps = Math.max(1, Number(evt.total_steps || st.totalSteps));
        st.champion = evt.champion || st.champion;
        st.activeConfig = evt.entry?.config || st.activeConfig;
        st.fitnessImprovement = Number(evt.fitness_improvement || 0);
        st.tokenReductionPct = Number(evt.token_reduction_pct || 0);
        st.message = evt.message || st.message;
        const cfg = evt.entry?.config || {};
        st.log.unshift({
          label: `Gen ${evt.generation || 1} · #${evt.candidate || 1}`,
          cfg: `${cfg.embedding_dimensions || 256}d · ${cfg.injection_format || "COMPACT_BULLET"}`,
          score: Number(evt.entry?.pareto_fitness || 0).toFixed(3),
          tokens: `${Math.round(Number(evt.entry?.avg_tokens_used || 0))} tok`,
          elite: !!evt.is_new_elite,
          rationale: cfg.mutation_rationale || "",
        });
      } else if (type === "complete") {
        const res = evt.result || {};
        st.status = "complete";
        st.step = st.totalSteps;
        st.champion = res.champion || st.champion;
        st.activeConfig = res.champion?.config || res.active_config || st.activeConfig;
        st.fitnessImprovement = Number(res.fitness_improvement ?? st.fitnessImprovement);
        st.tokenReductionPct = Number(res.token_reduction_pct ?? st.tokenReductionPct);
        st.autoApplied = !!res.auto_applied;
        st.autoApplyReason = res.auto_apply_reason || null;
        st.message = evt.message || res.auto_apply_reason || "Evolution complete";
      } else if (type === "cancelled") {
        st.status = "cancelled";
        st.message = evt.message || "Evolution stopped";
      } else if (type === "error") {
        st.status = "error";
        st.message = evt.message || "Evolution failed";
      }

      this.renderEvolutionHud();
      this.draw();
    }

    setEvolutionApplied(activeConfig) {
      this.evolveState.autoApplied = true;
      this.evolveState.activeConfig = activeConfig || this.evolveState.activeConfig;
      this.evolveState.message = "Winner applied to active settings";
      this.renderEvolutionHud();
      this.draw();
    }

    renderEvolutionHud() {
      if (!this.evolveHudEl) return;
      const st = this.evolveState;
      const isIdle = st.status === "idle";
      this.evolveHudEl.classList.toggle("hidden", isIdle);
      this.evolveHudEl.setAttribute("data-evolve-status", st.status);
      if (this.legendEl) {
        this.legendEl.classList.toggle("hidden", !isIdle);
      }

      if (this.evolveBtn) {
        this.evolveBtn.classList.toggle("is-running", st.status === "running");
        this.evolveBtn.textContent = st.status === "running" ? "✦ Evolving…" : "✦ Evolve ▾";
      }
      if (this.evolveStartBtn) {
        this.evolveStartBtn.disabled = st.status === "running";
        this.evolveStartBtn.textContent = st.status === "running" ? "Running…" : "Start Evolution";
      }

      if (isIdle) return;

      if (this.evolveStatusTextEl) {
        this.evolveStatusTextEl.textContent = st.message || "Evolving…";
      }

      if (this.evolveMetricsEl) {
        if (st.champion) {
          const score = Number(st.champion.pareto_fitness || 0).toFixed(3);
          const gain = Number(st.fitnessImprovement || 0);
          const gainStr = `${gain >= 0 ? "+" : ""}${gain.toFixed(3)}`;
          const tokPct = Number(st.tokenReductionPct || 0);
          const tokStr = `${tokPct >= 0 ? "-" : "+"}${Math.abs(tokPct).toFixed(0)}% tok`;
          this.evolveMetricsEl.textContent = `Score ${score} (${gainStr}) · ${tokStr}`;
        } else {
          this.evolveMetricsEl.textContent = "";
        }
      }

      if (this.evolveProgressEl) {
        const pct = Math.min(100, Math.max(0, Math.round((st.step / Math.max(1, st.totalSteps)) * 100)));
        if (st.status === "running" && st.step === 0) {
          this.evolveProgressEl.removeAttribute("value");
        } else {
          this.evolveProgressEl.value = pct;
        }
      }

      const isRunning = st.status === "running";
      const canApply = st.status === "complete" && !!st.champion?.config && !st.autoApplied;

      if (this.evolveStopBtn) this.evolveStopBtn.classList.toggle("hidden", !isRunning);
      if (this.evolveApplyBtn) this.evolveApplyBtn.classList.toggle("hidden", !canApply);
      if (this.evolveDismissBtn) this.evolveDismissBtn.classList.toggle("hidden", isRunning);

      if (this.evolveDetailsBtn) {
        const hasLog = st.log.length > 0;
        this.evolveDetailsBtn.classList.toggle("hidden", !hasLog);
        this.evolveDetailsBtn.setAttribute("aria-expanded", String(!!st.detailsOpen));
        this.evolveDetailsBtn.textContent = st.detailsOpen ? "Details ▴" : "Details ▾";
      }

      if (this.evolveLogEl) {
        const showLog = st.detailsOpen && st.log.length > 0;
        this.evolveLogEl.classList.toggle("hidden", !showLog);
        if (showLog) {
          this.evolveLogEl.innerHTML = st.log
            .slice(0, 12)
            .map(
              (item) => `
              <div class="graph-evolve-log-row ${item.elite ? "is-elite" : ""}">
                <span class="graph-evolve-log-gen">${escapeHtmlSafe(item.label)}</span>
                <span class="graph-evolve-log-cfg">${escapeHtmlSafe(item.cfg)}</span>
                <span class="graph-evolve-log-score">${escapeHtmlSafe(item.score)} · ${escapeHtmlSafe(item.tokens)}</span>
                ${item.elite ? `<span class="graph-evolve-elite-tag">Elite</span>` : ""}
                ${item.rationale ? `<div class="graph-evolve-log-note">${escapeHtmlSafe(item.rationale)}</div>` : ""}
              </div>
            `
            )
            .join("");
        }
      }
    }

    update(treePayload, sessionsList, filterOpts) {
      this._lastInputs = { treePayload, sessionsList, filterOpts: filterOpts || {} };
      this.recomputeFromLastInputs();
    }

    recomputeFromLastInputs() {
      if (!this._lastInputs) return;
      const { treePayload, sessionsList, filterOpts } = this._lastInputs;
      this.rawData = buildMemoryGraphData(treePayload, sessionsList, {
        searchQuery: filterOpts.searchQuery || "",
        statusFilter: filterOpts.statusFilter || "ALL",
        scopeFilter: filterOpts.scopeFilter || "ALL",
        showHubs: this.prefs.showHubs,
      });
      this.recomputeLayout();
      this.syncControlsUi();

      const nextSig = this.layout.nodes.map((n) => n.id).sort().join("|");
      const nodesChanged = nextSig !== this.lastNodeSig;
      this.lastNodeSig = nextSig;

      const rect = this.stage.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        this.size = { w: Math.round(rect.width), h: Math.round(rect.height) };
      }

      if ((!this.didFit || nodesChanged) && this.layout.nodes.length && this.size.w > 0 && this.size.h > 0) {
        const shouldAnimate = this.didFit && nodesChanged;
        this.didFit = true;
        this.fitView(shouldAnimate);
      } else {
        this.draw();
      }
    }

    recomputeLayout() {
      const { nodes, edges, rootId } = this.rawData;
      this.layout =
        this.prefs.visual === "cards"
          ? computeCardsLayout(nodes, edges)
          : computeAtomLayout(nodes, edges, rootId);

      // Precompute 1-hop adjacency map
      this.neighbors = new Map();
      this.layout.edges.forEach((e) => {
        if (!this.neighbors.has(e.source)) this.neighbors.set(e.source, new Set());
        if (!this.neighbors.has(e.target)) this.neighbors.set(e.target, new Set());
        this.neighbors.get(e.source).add(e.target);
        this.neighbors.get(e.target).add(e.source);
      });

      if (this.emptyEl) {
        this.emptyEl.classList.toggle("hidden", this.layout.nodes.length > 0);
      }
      if (this.hintEl) {
        const modeHint = this.prefs.visual === "atom" ? "Press V for Cards" : "Press V for Atom";
        const shellPart =
          this.prefs.visual === "atom" && this.layout.shells.length
            ? ` · ${this.layout.shells.length} shell${this.layout.shells.length === 1 ? "" : "s"}`
            : "";
        this.hintEl.textContent = `${this.layout.nodes.length} nodes · ${this.layout.edges.length} links${shellPart} · Scroll to zoom · Drag to pan · ${modeHint}`;
      }
    }

    getRenderNodes() {
      if (!this.layout) return [];
      if (!this.dragOverrides.size) return this.layout.nodes;
      return this.layout.nodes.map((n) => {
        const ov = this.dragOverrides.get(n.id);
        return ov ? { ...n, x: ov.x, y: ov.y } : n;
      });
    }

    screenToWorld(sx, sy) {
      return {
        x: (sx - this.transform.x) / this.transform.k,
        y: (sy - this.transform.y) / this.transform.k,
      };
    }

    hitTestNode(renderNodes, wx, wy) {
      if (this.prefs.visual === "cards") {
        for (let i = renderNodes.length - 1; i >= 0; i--) {
          const n = renderNodes[i];
          const hw = (n.w || CARD_W) / 2;
          const hh = (n.h || CARD_H) / 2;
          if (wx >= n.x - hw && wx <= n.x + hw && wy >= n.y - hh && wy <= n.y + hh) {
            return n;
          }
        }
        return null;
      }

      const pad = Math.max(5, 7 / this.transform.k);
      let best = null;
      let bestDist = Infinity;
      for (let i = renderNodes.length - 1; i >= 0; i--) {
        const n = renderNodes[i];
        const r = n.radius + pad;
        const dx = wx - n.x;
        const dy = wy - n.y;
        const d2 = dx * dx + dy * dy;
        if (d2 <= r * r && d2 < bestDist) {
          best = n;
          bestDist = d2;
        }
      }
      return best;
    }

    cancelAnim() {
      if (this.animFrame !== null) {
        cancelAnimationFrame(this.animFrame);
        this.animFrame = null;
      }
    }

    animateTo(target, duration = 260) {
      this.cancelAnim();
      const start = { ...this.transform };
      const t0 = performance.now();
      const tick = (now) => {
        const p = Math.min(1, (now - t0) / duration);
        const ease = 1 - Math.pow(1 - p, 3);
        this.transform = {
          x: start.x + (target.x - start.x) * ease,
          y: start.y + (target.y - start.y) * ease,
          k: start.k + (target.k - start.k) * ease,
        };
        this.draw();
        if (p < 1) this.animFrame = requestAnimationFrame(tick);
        else this.animFrame = null;
      };
      this.animFrame = requestAnimationFrame(tick);
    }

    fitView(animate = true) {
      if (!this.layout || !this.layout.nodes.length || this.size.w <= 0 || this.size.h <= 0) return;
      const target = computeFitTransform(this.layout.bounds, this.size.w, this.size.h, 64);
      if (animate) this.animateTo(target, 280);
      else {
        this.transform = target;
        this.draw();
      }
    }

    centerOn(nodeId, minZoom = 1.15) {
      const renderNodes = this.getRenderNodes();
      const target = renderNodes.find((n) => n.id === nodeId);
      if (!target) return;
      const k = clampZoom(Math.max(this.transform.k, minZoom));
      this.animateTo({ x: this.size.w / 2 - target.x * k, y: this.size.h / 2 - target.y * k, k }, 280);
    }

    zoomBy(factor, cx = this.size.w / 2, cy = this.size.h / 2) {
      const t = this.transform;
      const nextK = clampZoom(t.k * factor);
      const ratio = nextK / t.k;
      this.transform = {
        x: cx - (cx - t.x) * ratio,
        y: cy - (cy - t.y) * ratio,
        k: nextK,
      };
      this.draw();
    }

    showTip(item, sx, sy) {
      if (!this.tipEl) return;
      const n = item.node;
      const summaryParts = [
        n.badge || n.kind.toUpperCase(),
        `${item.degree} link${item.degree === 1 ? "" : "s"}`,
      ];
      if (n.subtitle) summaryParts.push(truncateLabel(n.subtitle, 36));
      this.tipEl.innerHTML = `
        <strong>${escapeHtmlSafe(n.title)}</strong>
        <span>${escapeHtmlSafe(summaryParts.join(" · "))}</span>
      `;
      const left = Math.min(Math.max(12, this.size.w - 248), Math.max(12, sx + 14));
      const top = Math.max(52, Math.min(this.size.h - 64, sy - 14));
      this.tipEl.style.left = `${left}px`;
      this.tipEl.style.top = `${top}px`;
      this.tipEl.classList.remove("hidden");
    }

    hideTip() {
      if (this.tipEl) this.tipEl.classList.add("hidden");
    }

    onPointerDown(e) {
      if (e.button !== 0) return;
      this.cancelAnim();
      const r = this.canvas.getBoundingClientRect();
      const sx = e.clientX - r.left;
      const sy = e.clientY - r.top;
      const w = this.screenToWorld(sx, sy);
      const renderNodes = this.getRenderNodes();
      const hit = this.hitTestNode(renderNodes, w.x, w.y);

      this.canvas.setPointerCapture(e.pointerId);
      const canDrag = hit && !(this.prefs.visual === "atom" && hit.isRoot);
      this.pointer = {
        mode: canDrag ? "drag" : "pan",
        pointerId: e.pointerId,
        startX: sx,
        startY: sy,
        lastX: sx,
        lastY: sy,
        moved: false,
        dragNodeId: canDrag ? hit.id : null,
      };
    }

    onPointerMove(e) {
      const r = this.canvas.getBoundingClientRect();
      const sx = e.clientX - r.left;
      const sy = e.clientY - r.top;
      const p = this.pointer;

      if (p.mode === "pan") {
        const dx = sx - p.lastX;
        const dy = sy - p.lastY;
        if (Math.hypot(sx - p.startX, sy - p.startY) > 3) p.moved = true;
        p.lastX = sx;
        p.lastY = sy;
        this.transform.x += dx;
        this.transform.y += dy;
        this.hideTip();
        this.draw();
        return;
      }

      if (p.mode === "drag" && p.dragNodeId) {
        if (Math.hypot(sx - p.startX, sy - p.startY) > 3) p.moved = true;
        p.lastX = sx;
        p.lastY = sy;
        if (p.moved) {
          const w = this.screenToWorld(sx, sy);
          this.dragOverrides.set(p.dragNodeId, {
            x: Math.round(w.x * 10) / 10,
            y: Math.round(w.y * 10) / 10,
          });
          this.syncControlsUi();
          this.hideTip();
          this.draw();
        }
        return;
      }

      const w = this.screenToWorld(sx, sy);
      const renderNodes = this.getRenderNodes();
      const hit = this.hitTestNode(renderNodes, w.x, w.y);
      if (hit) {
        if (this.hoveredId !== hit.id) {
          this.hoveredId = hit.id;
          this.canvas.classList.add("pointer");
          this.draw();
        }
        this.showTip(hit, sx, sy);
      } else {
        if (this.hoveredId !== null) {
          this.hoveredId = null;
          this.canvas.classList.remove("pointer");
          this.draw();
        }
        this.hideTip();
      }
    }

    onPointerUp(e) {
      const p = this.pointer;
      if (p.mode === "idle" || p.pointerId !== e.pointerId) return;
      if (this.canvas.hasPointerCapture(e.pointerId)) {
        this.canvas.releasePointerCapture(e.pointerId);
      }
      const wasClick = !p.moved;
      this.pointer = {
        mode: "idle",
        pointerId: -1,
        startX: 0,
        startY: 0,
        lastX: 0,
        lastY: 0,
        moved: false,
        dragNodeId: null,
      };
      if (!wasClick) return;

      const r = this.canvas.getBoundingClientRect();
      const w = this.screenToWorld(e.clientX - r.left, e.clientY - r.top);
      const hit = this.hitTestNode(this.getRenderNodes(), w.x, w.y);
      if (hit) {
        const now = performance.now();
        const isRapidRepeat = this.lastClickId === hit.id && now - this.lastClickTs < 350;
        this.lastClickId = hit.id;
        this.lastClickTs = now;
        this.selectedId = hit.id;
        this.draw();
        if (!isRapidRepeat && typeof this.callbacks.onSelectNode === "function") {
          this.callbacks.onSelectNode(hit.node);
        }
      } else if (this.selectedId !== null) {
        this.selectedId = null;
        this.draw();
      }
    }

    onDoubleClick(e) {
      const r = this.canvas.getBoundingClientRect();
      const w = this.screenToWorld(e.clientX - r.left, e.clientY - r.top);
      const hit = this.hitTestNode(this.getRenderNodes(), w.x, w.y);
      if (hit) this.centerOn(hit.id, 1.3);
      else this.fitView(true);
    }

    onKeyDown(e) {
      const panStep = 60;
      if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        this.zoomBy(1.22);
      } else if (e.key === "-") {
        e.preventDefault();
        this.zoomBy(1 / 1.22);
      } else if (e.key === "0") {
        e.preventDefault();
        this.fitView(true);
      } else if (e.key === "v" || e.key === "V") {
        e.preventDefault();
        this.setVisual(this.prefs.visual === "atom" ? "cards" : "atom");
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        this.transform.x += panStep;
        this.draw();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        this.transform.x -= panStep;
        this.draw();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        this.transform.y += panStep;
        this.draw();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        this.transform.y -= panStep;
        this.draw();
      }
    }

    getThemePalette() {
      const isDark = document.documentElement.getAttribute("data-theme") === "dark";
      const sans = '"Plus Jakarta Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
      const mono = '"JetBrains Mono", ui-monospace, SFMono-Regular, monospace';
      if (isDark) {
        return {
          isDark: true,
          sans,
          mono,
          bg: "#111827",
          dotGrid: "#1e2b42",
          ring: "#1e2b42",
          focus: "#22d3ee",
          rootGlow: "rgba(34, 211, 238, 0.16)",
          nodeOutline: "#111827",
          hoverOutline: "#94a3b8",
          labelBg: "rgba(17, 24, 39, 0.92)",
          labelText: "#f1f5f9",
          cardBg: "#172033",
          cardBorder: "#314464",
          cardHoverBorder: "#64748b",
          cardPort: "#475569",
          cardShadow: "rgba(0, 0, 0, 0.35)",
          cardFocusShadow: "rgba(34, 211, 238, 0.22)",
          titleText: "#f1f5f9",
          subtitleText: "#94a3b8",
          degreeText: "#64748b",
        };
      }
      return {
        isDark: false,
        sans,
        mono,
        bg: "#ffffff",
        dotGrid: "#d8e1ee",
        ring: "#d8e1ee",
        focus: "#0e7490",
        rootGlow: "rgba(14, 116, 144, 0.14)",
        nodeOutline: "#ffffff",
        hoverOutline: "#475569",
        labelBg: "rgba(255, 255, 255, 0.92)",
        labelText: "#0f172a",
        cardBg: "#ffffff",
        cardBorder: "#dbe1ea",
        cardHoverBorder: "#64748b",
        cardPort: "#cbd5e1",
        cardShadow: "rgba(15, 23, 42, 0.06)",
        cardFocusShadow: "rgba(14, 116, 144, 0.16)",
        titleText: "#0f172a",
        subtitleText: "#475569",
        degreeText: "#64748b",
      };
    }

    draw() {
      if (!this.canvas || !this.layout || this.size.w <= 0 || this.size.h <= 0) return;
      const ctx = this.canvas.getContext("2d");
      if (!ctx) return;

      const theme = this.getThemePalette();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const cw = Math.max(1, this.size.w);
      const ch = Math.max(1, this.size.h);
      if (this.canvas.width !== Math.round(cw * dpr) || this.canvas.height !== Math.round(ch * dpr)) {
        this.canvas.width = Math.round(cw * dpr);
        this.canvas.height = Math.round(ch * dpr);
      }

      const t = this.transform;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cw, ch);

      // Theme-aware Observatory surface
      ctx.fillStyle = theme.bg;
      ctx.fillRect(0, 0, cw, ch);

      // Subtle dot grid in Cards view
      if (this.prefs.visual === "cards") {
        this.drawDotGrid(ctx, cw, ch, t, theme);
      }

      const renderNodes = this.getRenderNodes();
      const nodeById = new Map(renderNodes.map((n) => [n.id, n]));
      const focusNodeId = this.hoveredId ?? this.selectedId;
      const focusNeighbors = focusNodeId ? this.neighbors.get(focusNodeId) : null;

      ctx.save();
      ctx.translate(t.x, t.y);
      ctx.scale(t.k, t.k);

      if (this.prefs.visual === "atom") {
        this.drawAtomScene(ctx, renderNodes, nodeById, focusNodeId, focusNeighbors, t, theme);
      } else {
        this.drawCardsScene(ctx, renderNodes, nodeById, focusNodeId, focusNeighbors, t, theme);
      }

      ctx.restore();

      // Screen-space collision-culled crisp labels for Atom view
      if (this.prefs.visual === "atom") {
        this.drawAtomLabels(ctx, renderNodes, focusNodeId, focusNeighbors, t, cw, ch, theme);
      }
    }

    drawDotGrid(ctx, cw, ch, t, theme) {
      const step = 24 * t.k;
      if (step < 10) return;
      const ox = ((t.x % step) + step) % step;
      const oy = ((t.y % step) + step) % step;
      ctx.save();
      ctx.fillStyle = theme.dotGrid;
      for (let x = ox; x < cw; x += step) {
        for (let y = oy; y < ch; y += step) {
          ctx.fillRect(x, y, 1.2, 1.2);
        }
      }
      ctx.restore();
    }

    drawAtomScene(ctx, renderNodes, nodeById, focusNodeId, focusNeighbors, t, theme) {
      // 1. Subtle concentric orbital shell rings
      if (this.prefs.showRings && this.layout.shells.length) {
        ctx.save();
        ctx.strokeStyle = theme.ring;
        ctx.lineWidth = Math.max(0.75, 1 / t.k);
        ctx.setLineDash([4 / t.k, 6 / t.k]);
        this.layout.shells.forEach((sh) => {
          ctx.beginPath();
          ctx.arc(0, 0, sh.radius, 0, Math.PI * 2);
          ctx.stroke();
        });
        ctx.restore();
      }

      // 2. Edges (dimmed first, focused on top)
      const drawEdge = (e) => {
        const a = nodeById.get(e.source);
        const b = nodeById.get(e.target);
        if (!a || !b) return;
        const isFocused = focusNodeId !== null && (e.source === focusNodeId || e.target === focusNodeId);
        let alpha = e.type === "part_of" ? 0.55 : 0.65;
        if (focusNodeId) alpha = isFocused ? 0.95 : 0.1;

        const stroke = isFocused ? theme.focus : edgeBaseColor(e.type, theme.isDark);
        const width = isFocused ? 2.0 : e.type === "part_of" ? 1.4 : 1.3;

        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.strokeStyle = stroke;
        ctx.lineWidth = width / Math.max(0.55, Math.min(1.4, t.k));
        const dash = edgeDash(e.type);
        if (dash.length) ctx.setLineDash(dash);

        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();

        // Directional arrowhead for provenance edges
        if (e.type !== "part_of") {
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const len = Math.hypot(dx, dy);
          if (len > a.radius + b.radius + 10) {
            const ux = dx / len;
            const uy = dy / len;
            const tipX = b.x - ux * (b.radius + 3);
            const tipY = b.y - uy * (b.radius + 3);
            const arr = 6.5;
            ctx.setLineDash([]);
            ctx.fillStyle = stroke;
            ctx.beginPath();
            ctx.moveTo(tipX, tipY);
            ctx.lineTo(tipX - ux * arr - uy * (arr * 0.55), tipY - uy * arr + ux * (arr * 0.55));
            ctx.lineTo(tipX - ux * arr + uy * (arr * 0.55), tipY - uy * arr - ux * (arr * 0.55));
            ctx.closePath();
            ctx.fill();
          }
        }
        ctx.restore();
      };

      this.layout.edges.forEach((e) => {
        const priority = focusNodeId !== null && (e.source === focusNodeId || e.target === focusNodeId);
        if (!priority) drawEdge(e);
      });
      this.layout.edges.forEach((e) => {
        const priority = focusNodeId !== null && (e.source === focusNodeId || e.target === focusNodeId);
        if (priority) drawEdge(e);
      });

      // 3. Atom nodes
      const evoCfg =
        this.evolveState && this.evolveState.status !== "idle" ? this.evolveState.activeConfig : null;
      const evoMinConf = evoCfg ? Number(evoCfg.certification_min_confidence ?? 0) : 0;
      const evoRingColor =
        this.evolveState?.autoApplied ? "#0f9d58" : "#6366f1";

      renderNodes.forEach((a) => {
        const isSel = this.selectedId === a.id;
        const isHov = this.hoveredId === a.id;
        const inFocus = !focusNodeId || a.id === focusNodeId || focusNeighbors?.has(a.id);
        const fill = nodeColor(a.node, theme.isDark);

        let evoDimmed = false;
        let evoHighlighted = false;
        if (evoCfg && a.node.kind === "memory") {
          const conf = Number(a.node.payload?.confidence_score ?? a.node.confidence ?? 1.0);
          if (conf < evoMinConf) {
            evoDimmed = true;
          } else {
            evoHighlighted = true;
          }
        }

        ctx.save();
        ctx.globalAlpha = !inFocus ? 0.22 : evoDimmed ? 0.34 : 1;

        if (a.isRoot) {
          ctx.beginPath();
          ctx.arc(a.x, a.y, a.radius + 6, 0, Math.PI * 2);
          ctx.fillStyle = theme.rootGlow;
          ctx.fill();
        } else if (a.node.kind === "proposal") {
          ctx.beginPath();
          ctx.arc(a.x, a.y, a.radius + 4, 0, Math.PI * 2);
          ctx.strokeStyle = fill;
          ctx.lineWidth = 1.4;
          ctx.setLineDash([3, 2]);
          ctx.stroke();
          ctx.setLineDash([]);
        } else if (evoHighlighted) {
          ctx.beginPath();
          ctx.arc(a.x, a.y, a.radius + 4, 0, Math.PI * 2);
          ctx.strokeStyle = evoRingColor;
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }

        ctx.beginPath();
        ctx.arc(a.x, a.y, a.radius, 0, Math.PI * 2);
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.strokeStyle = theme.nodeOutline;
        ctx.lineWidth = 1.8;
        ctx.stroke();

        if (isSel || isHov) {
          ctx.beginPath();
          ctx.arc(a.x, a.y, a.radius + 3.5, 0, Math.PI * 2);
          ctx.strokeStyle = isSel ? theme.focus : theme.hoverOutline;
          ctx.lineWidth = isSel ? 2.4 : 1.6;
          ctx.stroke();
        }
        ctx.restore();
      });
    }

    drawAtomLabels(ctx, renderNodes, focusNodeId, focusNeighbors, t, cw, ch, theme) {
      const placedBoxes = [];
      const overlaps = (x1, y1, x2, y2) =>
        placedBoxes.some((b) => !(x2 < b.x1 || x1 > b.x2 || y2 < b.y1 || y1 > b.y2));

      const score = (x) => {
        let s = 0;
        if (x.id === this.hoveredId) s += 1000;
        if (x.id === this.selectedId) s += 900;
        if (x.isRoot) s += 800;
        if (focusNodeId && focusNeighbors?.has(x.id)) s += 400;
        if (x.node.kind === "hub") s += 150;
        if (x.node.kind === "proposal") s += 110;
        s += x.degree * 10 - x.depth * 8;
        return s;
      };

      const candidates = [...renderNodes].sort((a, b) => score(b) - score(a));

      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "top";

      candidates.forEach((a) => {
        const isForced =
          a.isRoot ||
          a.id === this.hoveredId ||
          a.id === this.selectedId ||
          (focusNodeId !== null && !!focusNeighbors?.has(a.id));

        if (!isForced) {
          if (this.prefs.labelDensity === "minimal") return;
          if (this.prefs.labelDensity === "auto") {
            if (t.k < 0.48 && a.depth > 1 && a.node.kind !== "hub") return;
            if (t.k < 0.72 && a.depth > 2 && a.degree < 2) return;
          }
        }

        const sx = a.x * t.k + t.x;
        const sy = a.y * t.k + t.y + a.radius * t.k + 5;
        if (sx < -80 || sx > cw + 80 || sy < -30 || sy > ch + 30) return;

        const fontSize = a.isRoot ? 13 : isForced ? 12 : 11;
        ctx.font = `${a.isRoot || isForced ? 600 : 500} ${fontSize}px ${theme.sans}`;
        const label = truncateLabel(a.node.title, isForced ? 34 : 24);
        const tw = ctx.measureText(label).width;
        const padX = 5;
        const boxH = fontSize + 5;
        const x1 = sx - tw / 2 - padX;
        const y1 = sy - 1;
        const x2 = sx + tw / 2 + padX;
        const y2 = sy + boxH;

        if (!isForced && this.prefs.labelDensity !== "all" && overlaps(x1, y1, x2, y2)) return;
        placedBoxes.push({ x1, y1, x2, y2 });

        const inFocus = !focusNodeId || a.id === focusNodeId || focusNeighbors?.has(a.id);
        ctx.globalAlpha = inFocus ? 1 : 0.28;

        ctx.fillStyle = theme.labelBg;
        ctx.beginPath();
        ctx.roundRect(x1, y1, x2 - x1, y2 - y1, 4);
        ctx.fill();

        ctx.fillStyle = a.isRoot || a.id === this.selectedId ? theme.focus : theme.labelText;
        ctx.fillText(label, sx, sy + 1);
      });

      ctx.restore();
    }

    drawCardsScene(ctx, renderNodes, nodeById, focusNodeId, focusNeighbors, t, theme) {
      // 1. Curved cubic bezier edges between cards
      const drawCardEdge = (e) => {
        const a = nodeById.get(e.source);
        const b = nodeById.get(e.target);
        if (!a || !b) return;
        const isFocused = focusNodeId !== null && (e.source === focusNodeId || e.target === focusNodeId);
        let alpha = e.type === "part_of" ? 0.55 : 0.72;
        if (focusNodeId) alpha = isFocused ? 0.96 : 0.12;

        const stroke = isFocused ? theme.focus : edgeBaseColor(e.type, theme.isDark);
        const sameColumn = Math.abs(a.x - b.x) < (a.w || CARD_W) * 0.4;
        let x1, y1, x2, y2, cp1x, cp1y, cp2x, cp2y, arrowAngle;

        if (sameColumn) {
          const topToBottom = a.y <= b.y;
          x1 = a.x;
          y1 = a.y + (topToBottom ? (a.h || CARD_H) / 2 : -(a.h || CARD_H) / 2);
          x2 = b.x;
          y2 = b.y + (topToBottom ? -(b.h || CARD_H) / 2 : (b.h || CARD_H) / 2);
          const dy = Math.max(24, Math.abs(y2 - y1) * 0.45);
          cp1x = x1;
          cp1y = y1 + (topToBottom ? dy : -dy);
          cp2x = x2;
          cp2y = y2 + (topToBottom ? -dy : dy);
          arrowAngle = topToBottom ? Math.PI / 2 : -Math.PI / 2;
        } else {
          const leftToRight = a.x <= b.x;
          x1 = a.x + (leftToRight ? (a.w || CARD_W) / 2 : -(a.w || CARD_W) / 2);
          y1 = a.y;
          x2 = b.x + (leftToRight ? -(b.w || CARD_W) / 2 : (b.w || CARD_W) / 2);
          y2 = b.y;
          const dx = Math.max(48, Math.abs(x2 - x1) * 0.45);
          cp1x = x1 + (leftToRight ? dx : -dx);
          cp1y = y1;
          cp2x = x2 + (leftToRight ? -dx : dx);
          cp2y = y2;
          arrowAngle = leftToRight ? 0 : Math.PI;
        }

        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.strokeStyle = stroke;
        ctx.lineWidth = (isFocused ? 2.2 : 1.5) / Math.max(0.6, Math.min(1.2, t.k));
        const dash = edgeDash(e.type);
        if (dash.length) ctx.setLineDash(dash);

        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, x2, y2);
        ctx.stroke();

        // Arrowhead at target port
        ctx.setLineDash([]);
        ctx.fillStyle = stroke;
        const ux = Math.cos(arrowAngle);
        const uy = Math.sin(arrowAngle);
        ctx.beginPath();
        ctx.moveTo(x2, y2);
        ctx.lineTo(x2 - ux * 7 - uy * 4, y2 - uy * 7 + ux * 4);
        ctx.lineTo(x2 - ux * 7 + uy * 4, y2 - uy * 7 - ux * 4);
        ctx.closePath();
        ctx.fill();

        // Edge label when focused
        if (isFocused) {
          const mx = (x1 + x2) / 2;
          const my = (y1 + y2) / 2 - 8;
          ctx.font = `500 10px ${theme.sans}`;
          const lbl = edgeLabel(e.type);
          const tw = ctx.measureText(lbl).width;
          ctx.fillStyle = theme.labelBg;
          ctx.beginPath();
          ctx.roundRect(mx - tw / 2 - 4, my - 7, tw + 8, 14, 4);
          ctx.fill();
          ctx.fillStyle = theme.focus;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(lbl, mx, my);
        }
        ctx.restore();
      };

      this.layout.edges.forEach((e) => {
        const priority = focusNodeId !== null && (e.source === focusNodeId || e.target === focusNodeId);
        if (!priority) drawCardEdge(e);
      });
      this.layout.edges.forEach((e) => {
        const priority = focusNodeId !== null && (e.source === focusNodeId || e.target === focusNodeId);
        if (priority) drawCardEdge(e);
      });

      // 2. Node cards
      renderNodes.forEach((item) => {
        const n = item.node;
        const w = item.w || CARD_W;
        const h = item.h || CARD_H;
        const x = item.x - w / 2;
        const y = item.y - h / 2;
        const accent = nodeColor(n, theme.isDark);
        const isSel = this.selectedId === item.id;
        const isHov = this.hoveredId === item.id;
        const inFocus = !focusNodeId || item.id === focusNodeId || focusNeighbors?.has(item.id);

        ctx.save();
        ctx.globalAlpha = inFocus ? 1 : 0.25;

        // Card shadow & body
        ctx.shadowColor = isSel || isHov ? theme.cardFocusShadow : theme.cardShadow;
        ctx.shadowBlur = isSel || isHov ? 10 : 4;
        ctx.shadowOffsetY = 1;
        ctx.fillStyle = theme.cardBg;
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, 8);
        ctx.fill();
        ctx.shadowColor = "transparent";

        // Card border
        ctx.strokeStyle = isSel ? theme.focus : isHov ? theme.cardHoverBorder : theme.cardBorder;
        ctx.lineWidth = isSel ? 2 : 1;
        ctx.stroke();

        // Left colored accent bar
        ctx.fillStyle = accent;
        ctx.beginPath();
        ctx.roundRect(x, y, 4, h, [8, 0, 0, 8]);
        ctx.fill();

        // Connection ports
        ctx.fillStyle = theme.cardPort;
        ctx.beginPath();
        ctx.arc(x, item.y, 3, 0, Math.PI * 2);
        ctx.arc(x + w, item.y, 3, 0, Math.PI * 2);
        ctx.fill();

        // Top badge
        ctx.font = `600 9.5px ${theme.sans}`;
        const badgeText = String(n.badge || n.kind).toUpperCase();
        const badgeW = ctx.measureText(badgeText).width + 10;
        ctx.fillStyle = `${accent}18`;
        ctx.beginPath();
        ctx.roundRect(x + 12, y + 9, badgeW, 16, 4);
        ctx.fill();
        ctx.fillStyle = accent;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(badgeText, x + 17, y + 17);

        // Degree count on top-right
        ctx.font = `500 10px ${theme.mono}`;
        ctx.fillStyle = theme.degreeText;
        ctx.textAlign = "right";
        ctx.fillText(`${item.degree}`, x + w - 10, y + 17);

        // Main title
        ctx.font = `600 11.5px ${theme.mono}`;
        ctx.fillStyle = theme.titleText;
        ctx.textAlign = "left";
        ctx.fillText(truncateLabel(n.title, 25), x + 12, y + 39);

        // Subtitle
        ctx.font = `400 10.5px ${theme.sans}`;
        ctx.fillStyle = theme.subtitleText;
        ctx.fillText(truncateLabel(n.subtitle || "", 30), x + 12, y + 55);

        ctx.restore();
      });
    }
  }

  window.MemoryGraph = {
    MemoryGraphController,
    buildMemoryGraphData,
    computeAtomLayout,
    computeCardsLayout,
    computeFitTransform,
  };
})();
