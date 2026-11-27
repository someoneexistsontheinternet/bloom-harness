const PF_PALETTE = {
  Astra: { ink: "#77549b", fill: "#a184c2" },
  Sol: { ink: "#376294", fill: "#658fc4" },
  Fork: { ink: "#267466", fill: "#64a79a" },
  Terra: { ink: "#95602a", fill: "#d3a063" },
  Luna: { ink: "#28767c", fill: "#6caeb1" },
  Web: { ink: "#60758a", fill: "#96a8bc" },
  Runtime: { ink: "#677369", fill: "#a9b4a7" },
  Inbox: { ink: "#88483b", fill: "#c58170" },
};
function pfElapsed(us, digits = 0) {
  const absolute = Math.abs(us),
    unit = 10 ** (6 - digits),
    count = Math.floor(absolute / unit + 0.00000001),
    parts = 10 ** digits;
  const seconds = Math.floor(count / parts);
  return (
    (us < 0 ? "−" : "") +
    Math.floor(seconds / 60) +
    ":" +
    String(seconds % 60).padStart(2, "0") +
    (digits ? "." + String(count % parts).padStart(digits, "0") : "")
  );
}
function pfEpoch(iso) {
  const fraction = (iso.match(/\.(\d+)/)?.[1] || "").padEnd(6, "0");
  return Date.parse(iso) * 1000 + Number(fraction.slice(3, 6));
}
function pfUTC(epoch, offset, fractional = true) {
  const n = Math.round(epoch + offset),
    seconds = Math.floor(n / 1000000);
  return (
    new Date(seconds * 1000).toISOString().slice(11, 19) +
    (fractional ? "." + String(n - seconds * 1000000).padStart(6, "0") : "")
  );
}
function pfDuration(us) {
  if (us < 1000) return Math.round(us) + " μs";
  if (us < 1000000)
    return (
      (us / 1000).toLocaleString("en-US", { maximumFractionDigits: 2 }) + " ms"
    );
  if (us < 60000000) return (us / 1000000).toFixed(2) + " s";
  return pfElapsed(us, 1);
}
function pfLower(arr, value, kind) {
  let a = 0,
    b = arr.length;
  while (a < b) {
    const m = (a + b) >>> 1;
    if ((kind ? arr[m][kind] : arr[m]) < value) a = m + 1;
    else b = m;
  }
  return a;
}
function pfCanvasSize(canvas, height) {
  const width = Math.max(1, Math.floor(canvas.getBoundingClientRect().width)),
    dpr = Math.min(3, window.devicePixelRatio || 1);
  if (canvas.style.height !== height + "px")
    canvas.style.height = height + "px";
  if (
    canvas.width !== Math.round(width * dpr) ||
    canvas.height !== Math.round(height * dpr)
  ) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const x = canvas.getContext("2d");
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  x.clearRect(0, 0, width, height);
  return { x, width };
}
function pfClipText(x, s, width) {
  if (width < 8) return "";
  if (x.measureText(s).width <= width) return s;
  let a = 0,
    b = s.length;
  while (a < b) {
    const m = Math.ceil((a + b) / 2);
    if (x.measureText(s.slice(0, m) + "…").width <= width) a = m;
    else b = m - 1;
  }
  return s.slice(0, a) + "…";
}
class TraceTimeline {
  constructor(api) {
    this.api = api;
    this.canvas = document.querySelector("#pf-timeline");
    this.over = document.querySelector("#pf-overview");
    this.tip = document.querySelector("#pf-tooltip");
    this.absolute = false;
    this.lifetimes = false;
    this.showEdges = true;
    this.frame = 0;
    this.resize = new ResizeObserver(() => this.queue());
    this.resize.observe(this.canvas.parentElement);
    this.bind(this.canvas, false);
    this.bind(this.over, true);
  }
  use(run) {
    this.run = run;
    this.list = run.events;
    this.by = new Map(run.references.map((e) => [e.n, e]));
    this.lanes = new Map(run.lanes.map((l) => [l.id, l]));
    this.epoch = pfEpoch(run.start);
    this.floor = Math.min(0, this.list[0].u);
    this.ceil = Math.max(
      this.floor + 50,
      this.list.at(-1).u,
      pfEpoch(run.end) - this.epoch,
    );
    this.packed = run.spans
      .map((s) => ({
        ...s,
        start: s.u ?? this.by.get(s.a)?.u,
        end: s.v ?? this.by.get(s.b)?.u,
      }))
      .filter((s) => Number.isFinite(s.start) && Number.isFinite(s.end))
      .sort((a, b) => a.start - b.start || a.end - b.end);
    const stacks = new Map();
    for (const s of this.packed) {
      if (s.k !== "model") continue;
      let ends = stacks.get(s.l) || [];
      let slot = ends.findIndex((n) => n <= s.start);
      if (slot < 0) slot = ends.length;
      ends[slot] = s.end;
      stacks.set(s.l, ends);
      s.slot = slot;
    }
    this.depth = new Map(
      [...stacks].map(([k, v]) => [k, Math.max(1, v.length)]),
    );
    this.domain([this.floor, this.ceil], false);
  }
  domain(range, notify = true) {
    const full = this.ceil - this.floor;
    let width = Math.min(
      full,
      Math.max(Math.min(100, full), range[1] - range[0]),
    );
    let left = Math.min(this.ceil - width, Math.max(this.floor, range[0]));
    this.lo = left;
    this.hi = left + width;
    this.queue();
    if (notify) this.api.range();
  }
  zoom(factor, anchor = (this.lo + this.hi) / 2) {
    this.domain([
      anchor + (this.lo - anchor) * factor,
      anchor + (this.hi - anchor) * factor,
    ]);
  }
  center(time) {
    const half = (this.hi - this.lo) / 2;
    this.domain([time - half, time + half]);
  }
  fit() {
    this.domain([this.floor, this.ceil]);
  }
  focus(e, span) {
    const a = span ? (span.u ?? this.by.get(span.a).u) : e.u,
      b = span ? (span.v ?? this.by.get(span.b).u) : e.u,
      neighbor = 3000000;
    const pad = Math.max((b - a) * 0.16, span ? 10000 : neighbor);
    this.domain([a - pad, b + pad]);
    this.ensureLane(e.l);
  }
  ensureLane(id) {
    const lane = this.geom?.rows.get(id),
      box = this.canvas.parentElement;
    if (!lane) return;
    if (lane.y < box.scrollTop) box.scrollTop = lane.y;
    else if (lane.y + lane.h > box.scrollTop + box.clientHeight)
      box.scrollTop = lane.y + lane.h - box.clientHeight;
  }
  queue() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (this.run && this.canvas.parentElement.offsetParent) this.paint();
    });
  }
  coords(canvas, e) {
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }
  at(x, mini = false) {
    if (mini)
      return (
        this.floor +
        (this.ceil - this.floor) *
          Math.max(0, Math.min(1, x / Math.max(1, this.over.clientWidth)))
      );
    const g = this.geom;
    return (
      this.lo +
      (this.hi - this.lo) *
        Math.max(0, Math.min(1, (x - g.left) / (g.width - g.left - g.right)))
    );
  }
  hit(x, y) {
    const near = this.hits.filter(
      (h) => x >= h.x1 - 1 && x <= h.x2 + 1 && y >= h.y1 && y <= h.y2,
    );
    if (!near.length) return null;
    near.sort(
      (a, b) =>
        b.priority - a.priority || Math.abs(a.x1 - x) - Math.abs(b.x1 - x),
    );
    return near[0];
  }
  bind(canvas, mini) {
    canvas.addEventListener("pointerdown", (e) => {
      if (!this.run || e.button !== 0 || (!mini && !this.geom)) return;
      const p = this.coords(canvas, e);
      if (!mini && p.x < this.geom.left) return;
      this.drag = {
        mini,
        start: p,
        now: p,
        lo: this.lo,
        hi: this.hi,
        range: mini || e.shiftKey,
        id: e.pointerId,
      };
      canvas.setPointerCapture(e.pointerId);
      this.tip.hidden = true;
    });
    canvas.addEventListener("pointermove", (e) => {
      const p = this.coords(canvas, e),
        d = this.drag;
      if (d && d.id === e.pointerId && d.mini === mini) {
        d.now = p;
        const dx = p.x - d.start.x;
        if (!d.range && Math.abs(dx) > 3) {
          const width = this.geom.width - this.geom.left - this.geom.right;
          const delta = (dx * (d.hi - d.lo)) / width;
          this.domain([d.lo - delta, d.hi - delta]);
        } else this.queue();
        return;
      }
      if (mini || !this.geom) return;
      const h = this.hit(p.x, p.y);
      if (!h) {
        this.tip.hidden = true;
        return;
      }
      const op = this.by.get(h.n);
      this.tip.textContent =
        (h.label || op.label) + " · " + pfElapsed(op.u, 3) + " · #" + op.n;
      const frame = document.querySelector(".pf-chart").getBoundingClientRect();
      this.tip.hidden = false;
      this.tip.style.left =
        Math.max(
          3,
          Math.min(
            frame.width - this.tip.offsetWidth - 4,
            e.clientX - frame.left + 9,
          ),
        ) + "px";
      this.tip.style.top =
        Math.max(
          2,
          Math.min(
            frame.height - this.tip.offsetHeight - 3,
            e.clientY - frame.top + 12,
          ),
        ) + "px";
    });
    canvas.addEventListener("pointerleave", () => {
      this.tip.hidden = true;
    });
    canvas.addEventListener("pointercancel", () => {
      this.drag = null;
      this.queue();
    });
    canvas.addEventListener("pointerup", (e) => {
      const d = this.drag;
      if (!d || d.id !== e.pointerId || d.mini !== mini) return;
      const p = this.coords(canvas, e);
      this.drag = null;
      if (Math.abs(p.x - d.start.x) > 5) {
        if (d.range) {
          const a = this.at(Math.min(p.x, d.start.x), mini),
            b = this.at(Math.max(p.x, d.start.x), mini);
          this.domain([a, b]);
        }
        this.queue();
        return;
      }
      if (mini) {
        this.center(this.at(p.x, true));
        return;
      }
      const h = this.hit(p.x, p.y);
      if (h) this.api.select(h.n, h.span);
      else {
        const layer = [...this.geom.rows].find(
          ([, a]) => p.y >= a.y && p.y < a.y + a.h,
        );
        const values = layer
          ? this.list.filter((v) => v.l === layer[0])
          : this.list;
        if (values.length) {
          const us = this.at(p.x),
            j = pfLower(values, us, "u"),
            best = [values[j - 1], values[j]]
              .filter(Boolean)
              .sort((a, b) => Math.abs(a.u - us) - Math.abs(b.u - us))[0];
          this.api.select(best.n);
        }
      }
    });
    canvas.addEventListener("dblclick", (e) => {
      if (mini) {
        this.fit();
        return;
      }
      if (!this.geom) return;
      const p = this.coords(canvas, e);
      if (p.x < this.geom.left) return;
      const h = this.hit(p.x, p.y);
      if (h) this.focus(this.by.get(h.n), h.span);
      else this.zoom(0.25, this.at(p.x));
    });
    canvas.addEventListener(
      "wheel",
      (e) => {
        if (!this.run || !this.geom) return;
        e.preventDefault();
        const p = this.coords(canvas, e);
        if (!mini && (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY))) {
          const delta =
            ((e.deltaX || e.deltaY) * (this.hi - this.lo)) / this.geom.width;
          this.domain([this.lo + delta, this.hi + delta]);
        } else
          this.zoom(
            Math.exp(Math.max(-1.3, Math.min(1.3, e.deltaY * 0.003))),
            this.at(p.x, mini),
          );
      },
      { passive: false },
    );
  }
  overview() {
    const { x, width } = pfCanvasSize(this.over, 31);
    const n = Math.max(25, Math.round(width / 3)),
      bins = new Uint32Array(n),
      full = this.ceil - this.floor;
    for (const e of this.list)
      bins[Math.min(n - 1, Math.floor(((e.u - this.floor) / full) * n))]++;
    const max = Math.max(1, ...bins);
    x.fillStyle = "#eaf0e6";
    x.fillRect(0, 0, width, 31);
    x.fillStyle = "#79a18c";
    for (let i = 0; i < n; i++) {
      const h = Math.sqrt(bins[i] / max) * 23;
      x.fillRect((i * width) / n, 26 - h, Math.max(0.8, width / n - 1), h);
    }
    const a = ((this.lo - this.floor) / full) * width,
      b = ((this.hi - this.floor) / full) * width;
    x.fillStyle = "#fafcfabf";
    x.fillRect(0, 0, a, 31);
    x.fillRect(b, 0, width - b, 31);
    x.strokeStyle = "#508262";
    x.lineWidth = 1.25;
    x.strokeRect(a + 0.6, 1, Math.max(1, b - a - 1.2), 28.5);
    x.fillStyle = "#508262";
    x.fillRect(a, 10, 2, 11);
    x.fillRect(Math.max(a, b - 2), 10, 2, 11);
    if (this.drag?.mini) {
      let p = this.drag.start.x,
        q = this.drag.now.x;
      x.fillStyle = "#426e594c";
      x.fillRect(Math.min(p, q), 0, Math.abs(p - q), 31);
    }
  }
  paint() {
    this.overview();
    const visible = this.api.shownLanes();
    const rows = new Map();
    let top = 29;
    for (const lane of visible) {
      const h = lane.role === "Luna" ? 40 : lane.role === "Terra" ? 32 : 28;
      rows.set(lane.id, { y: top, h });
      top += h;
    }
    const { x, width } = pfCanvasSize(this.canvas, top + 2),
      left = width < 480 ? 84 : width < 820 ? 115 : 143,
      right = 9,
      plot = Math.max(20, width - left - right),
      span = this.hi - this.lo;
    this.geom = { rows, width, left, right };
    this.hits = [];
    const xpos = (u) => left + ((u - this.lo) / span) * plot;
    x.fillStyle = "white";
    x.fillRect(0, 0, width, top + 2);
    x.font = "11px ui-monospace, Menlo, monospace";
    x.textBaseline = "middle";
    for (const [i, lane] of visible.entries()) {
      const { y, h } = rows.get(lane.id);
      x.fillStyle = i % 2 ? "#f7f9f5" : "#fcfdfb";
      x.fillRect(0, y, width, h);
      x.strokeStyle = "#e6ebe3";
      x.beginPath();
      x.moveTo(0, y + h - 0.5);
      x.lineTo(width, y + h - 0.5);
      x.stroke();
    }
    const steps = [
      100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000,
      500000, 1000000, 2000000, 5000000, 10000000, 15000000, 30000000, 60000000,
      120000000, 300000000, 600000000, 900000000, 1800000000, 3600000000,
    ];
    const step =
      steps.find(
        (s) =>
          s >=
          span / Math.max(2, Math.floor(plot / (this.absolute ? 103 : 78))),
      ) || steps.at(-1);
    x.lineWidth = 1;
    for (let v = Math.ceil(this.lo / step) * step; v <= this.hi; v += step) {
      const at = Math.round(xpos(v)) + 0.5;
      x.strokeStyle = "#dbe4db";
      x.beginPath();
      x.moveTo(at, 23);
      x.lineTo(at, top);
      x.stroke();
      x.fillStyle = "#55665b";
      const lab = this.absolute
        ? pfUTC(this.epoch, v, step < 1000000)
        : pfElapsed(v, step < 1000 ? 6 : step < 1000000 ? 3 : 0);
      if (at + x.measureText(lab).width < width - 2)
        x.fillText(lab, at + 3, 13);
    }
    x.save();
    x.beginPath();
    x.rect(left, 24, plot, top);
    x.clip();
    const order = { task: 0, wait: 1, fork: 2, model: 3, tool: 4 };
    const selected = this.api.selected();
    const draw = this.packed
      .filter(
        (s) =>
          s.end >= this.lo &&
          s.start <= this.hi &&
          rows.has(s.l) &&
          (s.k !== "task" ||
            this.lifetimes ||
            s.a === selected ||
            s.b === selected),
      )
      .sort((a, b) => order[a.k] - order[b.k]);
    for (const s of draw) {
      const row = rows.get(s.l),
        tone = PF_PALETTE[this.lanes.get(s.l)?.role] || PF_PALETTE.Runtime;
      let a = Math.max(left, xpos(s.start)),
        b = Math.min(width - right, xpos(s.end)),
        y = row.y + 3,
        h = row.h - 9;
      const barW = Math.max(1, b - a);
      if (s.k === "task") {
        y = row.y + 1;
        h = 3;
        x.fillStyle = "#aaba9e";
      }
      if (s.k === "fork") {
        y = row.y + 2;
        h = 3;
        x.fillStyle = tone.ink;
      }
      if (s.k === "wait") {
        y = row.y + row.h * 0.39;
        h = Math.max(5, row.h * 0.37);
        x.fillStyle = "#dce7d4";
      }
      if (s.k === "model") {
        const slots = this.depth.get(s.l) || 1;
        h = Math.max(3, Math.floor((row.h - 9) / slots) - 1);
        y = row.y + 4 + (s.slot || 0) * (h + 1);
        x.fillStyle = tone.fill;
      }
      if (s.k === "tool") {
        y = row.y + row.h - 7;
        h = 4;
        x.fillStyle = tone.ink;
      }
      x.fillRect(a, y, barW, h);
      if (s.k === "wait" && barW > 3) {
        x.save();
        x.beginPath();
        x.rect(a, y, barW, h);
        x.clip();
        x.strokeStyle = "#9cad92";
        x.beginPath();
        for (let i = a - h; i < b; i += 6) {
          x.moveTo(i, y + h);
          x.lineTo(i + h, y);
        }
        x.stroke();
        x.restore();
      }
      if (s.a === selected || s.b === selected) {
        x.strokeStyle = "#183c29";
        x.lineWidth = 1.7;
        x.strokeRect(a + 0.5, y - 0.35, Math.max(1, barW - 1), h + 0.7);
        x.lineWidth = 1;
      }
      if (
        barW > 80 &&
        h >= 8 &&
        s.k !== "task" &&
        !(
          s.k === "wait" &&
          ["luna", "terra", "runtime", "inbox", "memory", "web"].includes(s.l)
        )
      ) {
        x.fillStyle = "#183328";
        x.font = "10px Inter, Arial, sans-serif";
        x.fillText(pfClipText(x, s.label, barW - 7), a + 4, y + h / 2 + 0.5);
      }
      this.hits.push({
        x1: a,
        x2: a + barW,
        y1: y - 1,
        y2: y + h + 1,
        n: s.a,
        span: s,
        label: s.label,
        priority: 3 + order[s.k],
      });
    }
    const first = pfLower(this.list, this.lo, "u");
    for (let i = first; i < this.list.length; i++) {
      const e = this.list[i];
      if (e.u > this.hi) break;
      const row = rows.get(e.l);
      if (!row) continue;
      const at = xpos(e.u),
        isError = e.error;
      x.fillStyle = isError
        ? "#bb453b"
        : (PF_PALETTE[this.lanes.get(e.l)?.role] || PF_PALETTE.Runtime).ink;
      x.globalAlpha = isError ? 1 : 0.65;
      x.fillRect(Math.round(at), row.y + row.h - 4, isError ? 3 : 1.25, 3);
      x.globalAlpha = 1;
      this.hits.push({
        x1: at - 1.2,
        x2: at + 1.2,
        y1: row.y + row.h - 5,
        y2: row.y + row.h,
        n: e.n,
        priority: 10,
      });
    }
    const current = this.by.get(selected);
    if (current && current.u >= this.lo && current.u <= this.hi) {
      const at = xpos(current.u);
      const row = rows.get(current.l);
      if (row && this.showEdges) {
        const related = this.api.edges(selected);
        x.globalAlpha = 0.57;
        x.lineWidth = 1.3;
        for (const link of related) {
          const other = this.by.get(link.n),
            target = other && rows.get(other.l);
          if (!target || other.n === current.n) continue;
          const xx = xpos(other.u),
            one = row.y + row.h * 0.55,
            two = target.y + target.h * 0.55;
          if (xx < left || xx > width - right) continue;
          x.strokeStyle = link.dir === "input" ? "#7562a0" : "#317e68";
          x.beginPath();
          x.moveTo(at, one);
          x.bezierCurveTo((at + xx) / 2, one, (at + xx) / 2, two, xx, two);
          x.stroke();
          x.fillStyle = x.strokeStyle;
          x.fillRect(xx - 2, two - 2, 4, 4);
        }
        x.globalAlpha = 1;
      }
      x.strokeStyle = "#294f38";
      x.setLineDash([3, 3]);
      x.beginPath();
      x.moveTo(at, 25);
      x.lineTo(at, top);
      x.stroke();
      x.setLineDash([]);
      if (row) {
        x.fillStyle = "#244b35";
        x.beginPath();
        x.arc(at, row.y + row.h - 3, 3, 0, 2 * Math.PI);
        x.fill();
      }
    }
    if (this.drag && !this.drag.mini && this.drag.range) {
      const a = this.drag.start.x,
        b = this.drag.now.x;
      x.fillStyle = "#65875b35";
      x.fillRect(Math.min(a, b), 24, Math.abs(a - b), top - 24);
    }
    x.restore();
    x.fillStyle = "#f2f5ee";
    x.fillRect(0, 0, left, 28);
    x.font = "11px Inter, Arial, sans-serif";
    x.fillStyle = "#5a6d5e";
    x.fillText("Execution lane", 8, 14);
    for (const lane of visible) {
      const { y, h } = rows.get(lane.id),
        tone = PF_PALETTE[lane.role] || PF_PALETTE.Runtime;
      x.fillStyle = tone.ink;
      x.fillRect(8, y + h / 2 - 3, 5, 6);
      x.fillStyle = "#30463b";
      x.font =
        (lane.role === "Fork" ? "11px" : "600 12px") +
        " Inter, Arial, sans-serif";
      const name = lane.label;
      x.fillText(pfClipText(x, name, left - 23), 18, y + h / 2);
    }
    x.strokeStyle = "#cfdacd";
    x.beginPath();
    x.moveTo(left - 0.5, 0);
    x.lineTo(left - 0.5, top);
    x.stroke();
  }
}
