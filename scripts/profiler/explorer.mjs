function startProfiler(graph, loadDetails) {
  const $ = (id) => document.getElementById("pf-" + id);
  const make = (tag, cls, txt) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt !== undefined) n.textContent = txt;
    return n;
  };
  const btn = (text, fn, cls) => {
    const n = make("button", cls, text);
    n.type = "button";
    n.addEventListener("click", fn);
    return n;
  };
  const fmt = (n) => n.toLocaleString("en-US");
  const byID = new Map(),
    incoming = new Map(),
    outgoing = new Map(),
    paired = new Map(),
    turns = new Map();
  const aliases = new Map(Object.values(graph).flatMap(run => run.aliases || []));
  const eventMaps = Object.fromEntries(Object.entries(graph).map(([key, run]) => [key, new Map(run.events.map(e => [e.n, e]))]));
  const referenceMaps = Object.fromEntries(Object.entries(graph).map(([key, run]) => [key, new Map(run.references.map(e => [e.n, e]))]));
  const edgeKeys = new Set();
  const add = (map, key, item) => {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(item);
  };
  for (const [key, run] of Object.entries(graph)) {
    for (const e of run.events) {
      if (!byID.has(e.n)) byID.set(e.n, { e, key });
    }
    for (const s of run.spans) {
      add(paired, key + ":" + s.a, s);
      if (s.b !== s.a) add(paired, key + ":" + s.b, s);
      if (s.k === "model") {
        turns.set(s.a, s.a);
        turns.set(s.b, s.a);
      }
    }
    for (const [from, to, why] of run.edges) {
      const id = JSON.stringify([from, to, why]);
      if (edgeKeys.has(id)) continue;
      edgeKeys.add(id);
      add(incoming, to, { n: from, why, dir: "input" });
      add(outgoing, from, { n: to, why, dir: "output" });
    }
  }
  const landmarks = Object.fromEntries(Object.entries(graph).map(([key, run]) => [key, run.landmarks || []]));
  const notes = new Map(Object.values(graph).flatMap(run => run.notes || []));
  const order = ["model", "tool", "wait", "fork", "task"];
  let key = Object.keys(graph)[0],
    run = graph[key],
    selected = run.initial,
    view = "timeline",
    section = "overview",
    page = 0,
    q = "",
    lane = "",
    family = "",
    scope = "",
    onlyWindow = false,
    selectionSpan = null;
  let graphPages = { input: 0, output: 0 },
    tickets = 0,
    graphFrame = 0;
  const pageSize = 16;
  const lanesFor = (k) => new Map(graph[k].lanes.map((l) => [l.id, l]));
  const laneMaps = Object.fromEntries(Object.keys(graph).map(key => [key, lanesFor(key)]));
  const laneOf = (n) => {
    const v = byID.get(n);
    return v ? laneMaps[key].get(v.e.l) || laneMaps[v.key].get(v.e.l) : null;
  };
  const tone = (n) => PF_PALETTE[laneOf(n)?.role] || PF_PALETTE.Runtime;
  const getEvent = (n) => referenceMaps[key].get(n) || byID.get(n)?.e;
  const allLinks = (n) => [
    ...(incoming.get(n) || []),
    ...(outgoing.get(n) || []),
  ];
  const shownLanes = () => run.lanes.filter((l) => !lane || l.id === lane);
  const chart = new TraceTimeline({
    selected: () => selected,
    edges: allLinks,
    shownLanes,
    select: (n, s) => choose(n, { span: s }),
    range: () => {
      rangeLabel();
      if (onlyWindow) ledger();
    },
  });
  const active = (sel, value, attr) => {
    for (const n of document.querySelectorAll(sel))
      n.setAttribute("aria-pressed", String(n.dataset[attr] === value));
  };
  const optionalNote = (text) => {
    const n = $("notice");
    n.textContent = text || "";
    n.hidden = !text;
  };
  const spanFor = (n) =>
    selectionSpan && [selectionSpan.a, selectionSpan.b].includes(n)
      ? selectionSpan
      : (paired.get(key + ":" + n) || [])
          .slice()
          .sort((a, b) => order.indexOf(a.k) - order.indexOf(b.k))[0];
  function rangeLabel() {
    if (!chart.run) return;
    $("range").textContent =
      pfElapsed(Math.max(0, chart.lo), chart.hi - chart.lo < 3000000 ? 3 : 0) +
      " – " +
      pfElapsed(chart.hi, chart.hi - chart.lo < 3000000 ? 3 : 0);
  }
  function routeURL(withWindow = false) {
    const url = new URL(location.href);
    url.search = "";
    url.searchParams.set("run", key);
    url.searchParams.set("thread", "profiler-v41");
    url.searchParams.set("turn", String(selected));
    const h = new URLSearchParams();
    if (view === "graph") h.set("view", "dependencies");
    if (section !== "overview") h.set("panel", section);
    if (withWindow && (chart.lo > chart.floor + 1 || chart.hi < chart.ceil - 1))
      h.set("window", Math.floor(chart.lo) + "," + Math.ceil(chart.hi));
    url.hash = h.toString();
    return url;
  }
  function sync(historyMode = "push") {
    const url = routeURL();
    if (url.href !== location.href)
      history[historyMode === "replace" ? "replaceState" : "pushState"](
        null,
        "",
        url,
      );
  }
  function relatedTurn(id) {
    if (getEvent(id)?.model) return getEvent(id).model;
    if (turns.has(id)) return turns.get(id);
    if (getEvent(id)?.k === "result.seen") {
      const used = (outgoing.get(id) || []).find(
        (edge) =>
          getEvent(edge.n)?.k === "model.started" &&
          /next parent input/.test(edge.why),
      );
      if (used) return used.n;
    }
    const checked = new Set([id]);
    let frame = [id];
    for (let i = 0; i < 3; i++) {
      const next = [];
      for (const n of frame) {
        for (const edge of incoming.get(n) || []) {
          if (
            checked.has(edge.n) ||
            !/(authoriz|produc|model response|tool result|fork action|decision|tool failure|step result)/i.test(
              edge.why,
            )
          )
            continue;
          if (turns.has(edge.n)) return turns.get(edge.n);
          checked.add(edge.n);
          next.push(edge.n);
        }
      }
      frame = next;
    }
    return null;
  }
  function setRun(k) {
    key = k;
    run = graph[k];
    q = "";
    lane = "";
    family = "";
    scope = "";
    onlyWindow = false;
    page = 0;
    selectionSpan = null;
    $("search").value = "";
    $("window").checked = false;
    optionalNote("");
    $("lane").replaceChildren(
      new Option("All lanes", ""),
      ...run.lanes.map(
        (l) => new Option((l.role === "Fork" ? "Fork · " : "") + l.label, l.id),
      ),
    );
    const families = [
      ...new Set(run.events.map((e) => e.k.split(".")[0])),
    ].sort();
    $("kind").replaceChildren(
      new Option("All event types", ""),
      new Option("Failures / blocked work", "!failure"),
      ...families.map((f) => new Option(f, f)),
    );
    $("milestone").replaceChildren(
      new Option("Go to a recorded moment", ""),
      ...landmarks[k]
        .filter(([n]) => byID.has(n))
        .map(([n, l]) => new Option(pfElapsed(getEvent(n).u) + " · " + l, n)),
    );
    $("brief").textContent = run.brief;
    $("date").textContent = "Recorded " + new Date(run.start).toLocaleDateString("en-GB", {day:"numeric", month:"long", year:"numeric", timeZone:"UTC"});
    $("stats").replaceChildren();
    for (const [value, name] of [
      [pfElapsed(Math.round((pfEpoch(run.end) - pfEpoch(run.start)) / 1e6) * 1e6), "elapsed"],
      [
        fmt(
          run.spans.filter(
            (s) =>
              s.k === "model" &&
              ["Astra", "Sol", "Fork"].includes(laneMaps[key].get(s.l)?.role),
          ).length,
        ),
        "owner calls",
      ],
      [fmt(run.spans.filter((s) => s.k === "model").length), "model calls"],
      [fmt(run.receiptCount), "receipts"],
    ]) {
      const el = make("span");
      el.append(make("b", "", value), document.createTextNode(" " + name));
      $("stats").append(el);
    }
    active("[data-run]", k, "run");
    chart.use(run);
    rangeLabel();
  }
  function navigateFromURL() {
    const params = new URLSearchParams(location.search),
      hash = new URLSearchParams(location.hash.slice(1));
    const k = Object.hasOwn(graph, params.get("run")) ? params.get("run") : Object.keys(graph)[0];
    const n = Number(params.get("turn")),
      valid = eventMaps[k].has(n);
    setRun(k);
    view = hash.get("view") === "dependencies" ? "graph" : "timeline";
    section = ["data", "turn"].includes(hash.get("panel"))
      ? hash.get("panel")
      : "overview";
    const windowText = hash.get("window");
    if (windowText && /^-?\d+,-?\d+$/.test(windowText)) {
      const [start, end] = windowText.split(",").map(Number);
      if (end > start) chart.domain([start, end], false);
    }
    choose(valid ? n : run.initial, { history: false });
    applyView();
    rangeLabel();
  }
  function choose(n, opts = {}) {
    const v = byID.get(Number(n));
    if (!v) return;
    if (!eventMaps[key].has(Number(n))) setRun(v.key);
    const event = getEvent(Number(n));
    const old = selected;
    selected = v.e.n;
    selectionSpan = opts.span ? run.spans.find(span => span.id === opts.span.id) || null : null;
    if (old !== selected) graphPages = { input: 0, output: 0 };
    if (lane && lane !== event.l) {
      lane = "";
      $("lane").value = "";
    }
    if (scope && scope !== event.t) {
      scope = "";
    }
    if (opts.focus !== false && (event.u < chart.lo || event.u > chart.hi))
      chart.center(event.u);
    if (section === "turn" && !relatedTurn(selected)) section = "overview";
    if (opts.history !== false) sync(opts.history || "push");
    if (opts.fromTable !== true) {
      const arr = filtered();
      const idx = arr.findIndex((e) => e.n === selected);
      if (idx >= 0) page = Math.floor(idx / pageSize);
    }
    $("milestone").value = landmarks[key].some(([i]) => i === selected)
      ? String(selected)
      : "";
    renderInspector();
    ledger();
    if (view === "graph") dependencyGraph();
    chart.queue();
    chart.ensureLane(event.l);
  }
  function applyView() {
    active("[data-view]", view, "view");
    $("time-view").hidden = view !== "timeline";
    $("graph-view").hidden = view !== "graph";
    $("zoom").hidden = view !== "timeline";
    if (view === "graph") dependencyGraph();
    else {
      chart.queue();
      requestAnimationFrame(() => chart.ensureLane(getEvent(selected).l));
    }
  }
  function setView(v) {
    view = v;
    applyView();
    sync();
  }
  function filtered() {
    const needle = q.trim().toLowerCase(),
      exact = /^[#e]\d+$/.test(needle) ? Number(needle.slice(1)) : null;
    return run.events.filter(
      (e) =>
        (!lane || e.l === lane) &&
        (!scope || e.t === scope) &&
        (!family ||
          (family === "!failure"
            ? e.error
            : e.k.split(".")[0] === family)) &&
        (!onlyWindow || (e.u >= chart.lo && e.u <= chart.hi)) &&
        (!needle ||
          (exact !== null
            ? e.n === exact
            : (
                e.k +
                " " +
                e.label +
                " " +
                e.t +
                " " +
                e.who +
                " " +
                laneOf(e.n)?.label +
                " #" +
                e.n
              )
                .toLowerCase()
                .includes(needle))),
    );
  }
  function ledger() {
    const list = filtered();
    page = Math.min(
      Math.max(0, page),
      Math.max(0, Math.ceil(list.length / pageSize) - 1),
    );
    const start = page * pageSize;
    const items = list.slice(start, start + pageSize),
      frag = document.createDocumentFragment();
    for (const e of items) {
      const row = make("tr", e.n === selected ? "selected" : "");
      row.dataset.n = e.n;
      if (e.n === selected) row.setAttribute("aria-current", "true");
      const time = make(
        "td",
        "pf-time",
        chart.absolute ? pfUTC(chart.epoch, e.u, false) : pfElapsed(e.u, 3),
      );
      const op = make("td", "pf-op"),
        pick = btn(e.label, () => choose(e.n, { fromTable: true }));
      pick.title = e.k + " · " + e.label;
      op.append(pick);
      const l = make("td", "pf-lane-cell");
      l.append(dot(e.n), document.createTextNode(laneOf(e.n)?.label || e.l));
      const id = make("td", "pf-seq", "#" + e.n);
      row.append(time, op, l, id);
      row.addEventListener("click", (ev) => {
        if (!ev.target.closest("button")) choose(e.n, { fromTable: true });
      });
      frag.append(row);
    }
    if (!items.length) {
      const tr = make("tr"),
        td = make(
          "td",
          "pf-muted",
          "No matching operations. Adjust the filters or visible range.",
        );
      td.colSpan = 4;
      tr.append(td);
      frag.append(tr);
    }
    const exactPosition = list.findIndex(e => e.n === selected),
      isHere = exactPosition >= 0,
      pos = isHere ? exactPosition : pfLower(list, getEvent(selected).u, "u");
    $("prev").disabled = pos === 0;
    $("next").disabled = (isHere ? pos + 1 : pos) >= list.length;
    $("rows").replaceChildren(frag);
    $("count").textContent =
      fmt(list.length) +
      " / " +
      fmt(run.events.length) +
      (scope ? " · " + scope : "");
    $("paging").textContent = list.length
      ? fmt(start + 1) +
        "–" +
        fmt(start + items.length) +
        " of " +
        fmt(list.length)
      : "0 events";
    $("prevpage").disabled = page === 0;
    $("nextpage").disabled = start + pageSize >= list.length;
    $("clear").hidden = !(q || lane || family || scope || onlyWindow);
    $("isolate").textContent = scope ? "Show all work" : "Filter related work";
  }
  function dot(n) {
    const el = make("i", "pf-role-dot");
    el.style.setProperty("--role", tone(n).ink);
    return el;
  }
  function link(n, why) {
    const r = byID.get(n);
    if (!r) return make("span", "pf-muted", "Unavailable event");
    const b = btn("", () => choose(n), "pf-link");
    b.dataset.target = n;
    b.append(
      make("span", "", r.e.label),
      make("code", "", "#" + n),
      make(
        "small",
        "",
        (r.key === key ? "" : r.key.toUpperCase() + " · ") + why,
      ),
    );
    return b;
  }
  function grouped(n, dir) {
    const m = new Map();
    for (const item of (dir === "input" ? incoming : outgoing).get(n) || []) {
      if (!m.has(item.n)) m.set(item.n, []);
      if (!m.get(item.n).includes(item.why)) m.get(item.n).push(item.why);
    }
    return [...m].map(([k, v]) => ({ n: k, why: v.join(" · ") }));
  }
  function addLinks(host, which, title) {
    const list = grouped(selected, which);
    host.append(make("h4", "", title + " (" + list.length + ")"));
    if (!list.length) {
      host.append(
        make(
          "p",
          "pf-muted",
          "No recorded " +
            (which === "input" ? "incoming" : "outgoing") +
            " reference.",
        ),
      );
      return;
    }
    const links = make("div", "pf-links");
    for (const x of list.slice(0, 4)) links.append(link(x.n, x.why));
    host.append(links);
    if (list.length > 4) {
      const more = make("details");
      more.append(make("summary", "", "Show " + (list.length - 4) + " more"));
      more.addEventListener("toggle", () => {
        if (more.open && more.children.length === 1) {
          const z = make("div", "pf-links");
          for (const x of list.slice(4)) z.append(link(x.n, x.why));
          more.append(z);
        }
      });
      host.append(more);
    }
  }
  function dl(host, pairs) {
    const box = make("dl", "pf-dl");
    for (const [k, v] of pairs) {
      if (v == null || v === "") continue;
      box.append(make("dt", "", k), make("dd", "", v));
    }
    host.append(box);
  }
  function fullText(label, data, opened = false) {
    const d = make("details"),
      s = make("summary", "", label);
    d.append(s);
    const fill = () => {
      if (d.open && d.children.length === 1) {
        const pre = make("pre");
        pre.tabIndex = 0;
        let offset = 0;
        for (const found of data.matchAll(/\b(?:reader|compact|turn|job|send|curate|message|result|web|context|branch|source)_[A-Za-z0-9_]+\b/g)) {
          const n = aliases.get(found[0]);
          if (!byID.has(n)) continue;
          pre.append(document.createTextNode(data.slice(offset, found.index)));
          const a = make("a", "pf-ref", found[0]);
          a.href = "?run=" + byID.get(n).key + "&thread=profiler-v41&turn=" + n;
          a.addEventListener("click", (ev) => {
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
            ev.preventDefault();
            clearFilters();
            section = "overview";
            choose(n);
          });
          pre.append(a);
          offset = found.index + found[0].length;
        }
        pre.append(document.createTextNode(data.slice(offset)));
        d.append(pre);
      }
    };
    d.addEventListener("toggle", fill);
    if (opened) {
      d.open = true;
      fill();
    }
    return d;
  }
  function detailMod() {
    return loadDetails(key, selected, relatedTurn(selected));
  }
  function renderInspector() {
    const e = getEvent(selected),
      l = laneOf(selected),
      alias = e.t || "",
      span = spanFor(selected),
      turn = relatedTurn(selected);
    $("identity").replaceChildren(
      dot(selected),
      make("span", "", l?.label || e.l),
      make("code", "pf-tag", "#" + e.n),
    );
    $("selected").textContent = e.label;
    $("event-kind").textContent = e.k;
    $("prev").disabled = e.n === run.events[0].n;
    $("next").disabled = e.n === run.events.at(-1).n;
    $("isolate").disabled = !e.t;
    $("copy").textContent = "Copy event link";
    document.querySelector('[data-ins="turn"]').disabled = !turn;
    active("[data-ins]", section, "ins");
    const host = $("ins-content");
    host.replaceChildren();
    host.scrollTop = 0;
    const ticket = ++tickets;
    if (section === "overview") {
      if (notes.has(e.n)) host.append(make("p", "pf-note", notes.get(e.n)));
      const pairs = [
        ["Elapsed", pfElapsed(e.u, 6)],
        ["UTC", pfUTC(chart.epoch, e.u)],
        ["Actor", e.who],
        ["Turn / context", e.model ? "Model request #" + e.model : alias],
        ["Record", e.derived ? "Derived timer boundary · see its recorded source below" : "Original journal receipt #" + e.n],
      ];
      if (span) {
        pairs.push([
          "Span",
          span.label + " · " + pfDuration((span.v ?? getEvent(span.b).u) - (span.u ?? getEvent(span.a).u)),
        ]);
        if (span.measurement) pairs.push(["Timing", span.measurement]);
      }
      dl(host, pairs);
      if (span && span.a !== span.b) {
        const other = span.a === selected ? span.b : span.a;
        host.append(
          make("h4", "", "Paired boundary"),
          link(
            other,
            other === span.b ? "End of " + span.k : "Start of " + span.k,
          ),
        );
      }
      addLinks(host, "input", "Inputs and causes");
      addLinks(host, "output", "Effects and consumers");
      return;
    }
    host.append(make("p", "pf-muted", "Loading the recorded payload…"));
    detailMod()
      .then((data) => {
        if (ticket !== tickets) return;
        host.replaceChildren();
        if (section === "data") {
          const fields = data.events[String(e.n)] || [];
          if (!fields.length)
            host.append(
              make(
                "p",
                "pf-muted",
                "This receipt has no additional shareable fields.",
              ),
            );
          for (const [i, [name, id]] of fields.entries())
            host.append(fullText(name, data.texts[id] ?? "", i === 0));
          if (span && ["tool", "model"].includes(span.k)) {
            const other = span.a === e.n ? span.b : span.a;
            host.append(
              make(
                "h4",
                "",
                span.k === "tool"
                  ? "Related tool boundary"
                  : "Related model boundary",
              ),
              link(
                other,
                other === span.b ? "Recorded result" : "Recorded start",
              ),
            );
          }
          return;
        }
        const t = data.turns[String(turn)];
        if (!t) {
          host.append(
            make(
              "p",
              "pf-muted",
              "This event has no corresponding model turn.",
            ),
          );
          return;
        }
        dl(host, [
          ["Model", t.model],
          [
            "Relation",
            e.k === "result.seen"
              ? "Next consuming turn"
              : turns.has(e.n)
                ? "Selected turn"
                : "Related decision",
          ],
          ["Effort", t.effort],
          ["Request", "#" + turn],
          ["Response", "#" + t.end],
          ["Duration", pfDuration(getEvent(t.end).u - getEvent(turn).u)],
        ]);
        for (const [field, title] of [
          ["input", "Input"],
          ["output", "Output"],
        ]) {
          const vals = t[field] || [];
          const group = make("details", "pf-message-group");
          group.append(
            make(
              "summary",
              "",
              title +
                " · " +
                vals.length +
                " visible item" +
                (vals.length === 1 ? "" : "s"),
            ),
          );
          const fill = () => {
            if (!group.open || group.children.length > 1) return;
            const stack = make("div");
            for (const [i, [role, id, label]] of vals.entries())
              stack.append(
                fullText(
                  i + 1 + ". " + role + (label ? " · " + label : ""),
                  data.texts[id] ?? "",
                  false,
                ),
              );
            group.append(stack);
          };
          group.addEventListener("toggle", fill);
          group.open = true;
          fill();
          host.append(group);
        }
        for (const [k, label] of [
          ["instructions", "Instructions"],
          ["tools", "Tool definitions"],
        ])
          if (Number.isInteger(t[k]))
            host.append(fullText(label, data.texts[t[k]] || ""));
      })
      .catch(() => {
        if (ticket !== tickets) return;
        host.replaceChildren(
          make("p", "pf-error", "The recorded payload could not be loaded."),
        );
        const a = make("a", "", "Reload and unlock");
        a.href = routeURL(true).href;
        host.append(a);
      });
  }
  function dependencyGraph() {
    const host = $("depmap");
    host.replaceChildren();
    host.style.position = "relative";
    const nodeFor = (n, why, center = false) => {
      const e = getEvent(n),
        item = center
          ? make("div", "pf-graph-node")
          : btn("", () => choose(n), "pf-graph-node");
      if (!center) item.dataset.target = n;
      item.style.setProperty("--role", tone(n).ink);
      item.append(
        make("code", "", "#" + n + " · " + pfElapsed(e.u, 3)),
        make("span", "", e.label),
      );
      if (why) item.append(make("small", "", why));
      return item;
    };
    for (const dir of ["input", "center", "output"]) {
      const col = make(
        "div",
        "pf-graph-col " + (dir === "center" ? "pf-graph-center" : ""),
      );
      col.dataset.dir = dir;
      host.append(col);
      if (dir === "center") {
        col.append(
          make("h4", "", "Selected operation"),
          nodeFor(selected, getEvent(selected).k, true),
        );
        continue;
      }
      const list = grouped(selected, dir),
        size = 4,
        pages = Math.max(1, Math.ceil(list.length / size));
      graphPages[dir] = Math.min(graphPages[dir], pages - 1);
      const idx = graphPages[dir];
      col.append(
        make(
          "h4",
          "",
          (dir === "input" ? "Upstream" : "Downstream") + " · " + list.length,
        ),
      );
      for (const x of list.slice(idx * size, (idx + 1) * size))
        col.append(nodeFor(x.n, x.why));
      if (!list.length)
        col.append(
          make(
            "p",
            "pf-muted",
            dir === "input" ? "No prior reference" : "No later reference",
          ),
        );
      if (pages > 1) {
        const pager = make("div", "pf-graph-pager"),
          prev = btn("‹", () => {
            graphPages[dir]--;
            dependencyGraph();
          }),
          next = btn("›", () => {
            graphPages[dir]++;
            dependencyGraph();
          });
        prev.disabled = !idx;
        next.disabled = idx + 1 >= pages;
        prev.setAttribute("aria-label", "Previous " + dir + " nodes");
        next.setAttribute("aria-label", "More " + dir + " nodes");
        pager.append(prev, make("span", "", idx + 1 + " / " + pages), next);
        col.append(pager);
      }
    }
    cancelAnimationFrame(graphFrame);
    graphFrame = requestAnimationFrame(drawWires);
  }
  function drawWires() {
    const host = $("depmap");
    host.querySelector(".pf-graph-wire")?.remove();
    if (view !== "graph" || innerWidth <= 540) return;
    const rect = host.getBoundingClientRect(),
      center = host
        .querySelector(".pf-graph-center .pf-graph-node")
        ?.getBoundingClientRect();
    if (!center || !rect.width) return;
    const wire = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    wire.setAttribute("aria-hidden", "true");
    wire.setAttribute("class", "pf-graph-wire");
    wire.setAttribute("viewBox", "0 0 " + rect.width + " " + rect.height);
    for (const dir of ["input", "output"]) {
      for (const node of host.querySelectorAll(
        '[data-dir="' + dir + '"] .pf-graph-node',
      )) {
        const r = node.getBoundingClientRect(),
          start = dir === "input" ? r : center,
          end = dir === "input" ? center : r;
        const x = start.right - rect.left,
          y = (start.top + start.bottom) / 2 - rect.top,
          xx = end.left - rect.left,
          yy = (end.top + end.bottom) / 2 - rect.top,
          m = (x + xx) / 2;
        const path = document.createElementNS(wire.namespaceURI, "path");
        path.setAttribute(
          "d",
          "M" +
            x +
            " " +
            y +
            " C" +
            m +
            " " +
            y +
            " " +
            m +
            " " +
            yy +
            " " +
            xx +
            " " +
            yy,
        );
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", dir === "input" ? "#9d8db2" : "#73a68e");
        path.setAttribute("stroke-width", "1.6");
        wire.append(path);
        const arr = document.createElementNS(wire.namespaceURI, "path");
        arr.setAttribute(
          "d",
          "M" +
            (xx - 4) +
            " " +
            (yy - 2.5) +
            " L" +
            xx +
            " " +
            yy +
            " L" +
            (xx - 4) +
            " " +
            (yy + 2.5),
        );
        arr.setAttribute("fill", "none");
        arr.setAttribute("stroke", "#5b806c");
        wire.append(arr);
      }
    }
    host.prepend(wire);
  }
  function move(dir) {
    const list = filtered(),
      exact = list.findIndex(e => e.n === selected),
      i = exact >= 0 ? exact : pfLower(list, getEvent(selected).u, "u");
    const val = list[dir < 0 ? i - 1 : i + (list[i]?.n === selected ? 1 : 0)];
    if (val) choose(val.n);
  }
  function jump(value) {
    const s = value.trim();
    if (!s) return;
    const id = s.match(/^[#e](\d+)$/i);
    if (id) {
      const n = Number(id[1]);
      if (!byID.has(n)) {
        optionalNote("That event number does not exist in this recording.");
        return;
      }
      clearFilters();
      choose(n);
      optionalNote("");
      return;
    }
    let us = null;
    const iso = s.match(
        /^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/i,
      ),
      clock = s.match(
        /^(\d{1,2}):(\d\d):(\d\d)(?:\.(\d{1,6}))?(?:\s*UTC|Z)?$/i,
      ),
      elapsed = s.match(/^(\d+):([0-5]\d)(?:\.(\d{1,6}))?$/);
    if (iso) us = pfEpoch(s.replace(" ", "T")) - chart.epoch;
    else if (
      clock &&
      Number(clock[1]) < 24 &&
      Number(clock[2]) < 60 &&
      Number(clock[3]) < 60
    ) {
      us =
        pfEpoch(
          run.start.slice(0, 10) +
            "T" +
            clock[1].padStart(2, "0") +
            ":" +
            clock[2] +
            ":" +
            clock[3] +
            "." +
            (clock[4] || "0") +
            "Z",
        ) - chart.epoch;
    } else if (elapsed)
      us =
        (Number(elapsed[1]) * 60 + Number(elapsed[2])) * 1e6 +
        Number((elapsed[3] || "").padEnd(6, "0"));
    else if (/^\d+(?:\.\d{1,6})?$/.test(s)) us = Math.round(Number(s) * 1e6);
    if (!Number.isFinite(us) || us < chart.floor || us > chart.ceil) {
      optionalNote(
        "Enter elapsed m:ss, a UTC time in this run, or a journal event number.",
      );
      return;
    }
    clearFilters();
    const i = pfLower(run.events, us, "u"),
      nearest = [run.events[i - 1], run.events[i]]
        .filter(Boolean)
        .sort((a, b) => Math.abs(a.u - us) - Math.abs(b.u - us))[0];
    choose(nearest.n);
    const radius = Math.min((chart.hi - chart.lo) / 2, 10000000);
    chart.domain([us - radius, us + radius]);
    optionalNote(
      "Nearest event to " + pfElapsed(us, 6) + " is #" + nearest.n + ".",
    );
  }
  function clearFilters() {
    q = "";
    lane = "";
    family = "";
    scope = "";
    onlyWindow = false;
    $("search").value = "";
    $("lane").value = "";
    $("kind").value = "";
    $("window").checked = false;
    page = 0;
    ledger();
    chart.queue();
  }
  for (const n of document.querySelectorAll("[data-run]"))
    n.addEventListener("click", () => {
      if (n.dataset.run === key) return;
      setRun(n.dataset.run);
      section = "overview";
      choose(run.initial);
      applyView();
    });
  for (const n of document.querySelectorAll("[data-view]"))
    n.addEventListener("click", () => setView(n.dataset.view));
  for (const n of document.querySelectorAll("[data-ins]"))
    n.addEventListener("click", () => {
      section = n.dataset.ins;
      renderInspector();
      sync();
    });
  $("search").addEventListener("input", () => {
    q = $("search").value;
    page = 0;
    ledger();
  });
  $("lane").addEventListener("change", () => {
    lane = $("lane").value;
    page = 0;
    ledger();
    chart.queue();
  });
  $("kind").addEventListener("change", () => {
    family = $("kind").value;
    page = 0;
    ledger();
  });
  $("window").addEventListener("change", () => {
    onlyWindow = $("window").checked;
    page = 0;
    ledger();
  });
  $("clear").addEventListener("click", clearFilters);
  $("isolate").addEventListener("click", () => {
    scope = scope ? "" : getEvent(selected).t;
    page = 0;
    ledger();
  });
  $("prevpage").addEventListener("click", () => {
    page--;
    ledger();
  });
  $("nextpage").addEventListener("click", () => {
    page++;
    ledger();
  });
  $("milestone").addEventListener("change", () => {
    if ($("milestone").value) {
      clearFilters();
      choose(Number($("milestone").value));
    }
  });
  $("jump-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    jump($("jump").value);
  });
  $("in").addEventListener("click", () => chart.zoom(0.5));
  $("out").addEventListener("click", () => chart.zoom(2));
  $("fit").addEventListener("click", () => chart.fit());
  $("focus").addEventListener("click", () =>
    chart.focus(getEvent(selected), spanFor(selected)),
  );
  $("clock").addEventListener("click", () => {
    chart.absolute = !chart.absolute;
    $("clock").textContent = chart.absolute ? "UTC time" : "Elapsed time";
    chart.queue();
    ledger();
  });
  $("lifetimes").addEventListener("change", () => {
    chart.lifetimes = $("lifetimes").checked;
    chart.queue();
  });
  $("arrows").addEventListener("change", () => {
    chart.showEdges = $("arrows").checked;
    chart.queue();
  });
  $("prev").addEventListener("click", () => move(-1));
  $("next").addEventListener("click", () => move(1));
  $("copy").addEventListener("click", async () => {
    const url = routeURL(true).href;
    try {
      await navigator.clipboard.writeText(url);
      $("copy").textContent = "Link copied";
    } catch {
      const field = make("input");
      field.value = url;
      field.readOnly = true;
      field.setAttribute("aria-label", "Event link");
      $("ins-content").prepend(field);
      field.focus();
      field.select();
    }
  });
  new ResizeObserver(() => {
    if (view === "graph") {
      cancelAnimationFrame(graphFrame);
      graphFrame = requestAnimationFrame(drawWires);
    }
  }).observe($("depmap"));
  window.addEventListener("keydown", (ev) => {
    if (
      ev.ctrlKey ||
      ev.metaKey ||
      ev.altKey ||
      ev.target.closest("input,select,textarea")
    )
      return;
    if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
      ev.preventDefault();
      move(ev.key === "ArrowLeft" ? -1 : 1);
    } else if (view === "timeline") {
      if (ev.key === "+" || ev.key === "=") chart.zoom(0.5);
      else if (ev.key === "-" || ev.key === "_") chart.zoom(2);
      else if (ev.key === "0") chart.fit();
      else if (ev.key.toLowerCase() === "f")
        chart.focus(getEvent(selected), spanFor(selected));
    }
  });
  window.addEventListener("popstate", () => {
    navigateFromURL();
  });
  $("workspace").parentElement.hidden = false;
  document.title = "Bloom v4.1 · Execution trace";
  navigateFromURL();
}
