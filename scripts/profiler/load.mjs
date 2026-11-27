const loadCache = new Map();
async function recordedJSON(path) {
  if (!loadCache.has(path)) {
    if (loadCache.size >= 12) loadCache.delete(loadCache.keys().next().value);
    loadCache.set(path, fetch(path, {credentials:"same-origin", cache:"no-store"}).then(response => {
      if (!response.ok) throw new Error("Recorded payload unavailable; reload and unlock the viewer.");
      return response.json();
    }).catch(error => {loadCache.delete(path); throw error;}));
  }
  return loadCache.get(path);
}

function materialize(data) {
  const graph = {};
  for (const [key, view] of Object.entries(data.views)) {
    const references = data.events.map(event => ({...event, u:event.u-view.lo}));
    const events = references.filter(event => event.derived
      ? event.u >= 0 && event.u <= view.hi-view.lo
      : event.n >= view.firstReceipt && event.n <= view.lastReceipt);
    const spans = data.spans.filter(span => span.v >= view.lo && span.u <= view.hi).map(span => ({...span, u:Math.max(span.u,view.lo)-view.lo, v:Math.min(span.v,view.hi)-view.lo}));
    const lanes = new Set([...events.map(e=>e.l), ...spans.map(s=>s.l)]);
    graph[key] = {...view, references, events, spans, lanes:data.lanes.filter(l=>lanes.has(l.id)), edges:data.edges, aliases:data.aliases, receiptCount:events.filter(e=>!e.derived).length};
  }
  return graph;
}

function detailsLoader(data) {
  const events = new Map(data.events.map(e=>[e.n,e]));
  const visible = item => !["private_reasoning_omitted","nonpublic_message_omitted"].includes(item?.type);
  return async function loadDetails(key, selected, turn) {
    const event = events.get(selected);
    const result = {texts:[],events:{[selected]:[]},turns:{}};
    const text = value => {
      result.texts.push(typeof value === "string" ? value : JSON.stringify(value,null,2));
      return result.texts.length-1;
    };
    const chunk = await recordedJSON(event.record);
    result.events[selected].push([event.derived ? "Recorded timer source" : "Original receipt",text(chunk[selected])]);
    for (const [title,path] of event.details || []) {
      const payload = await recordedJSON(path);
      result.events[selected].push([title,text(payload)]);
    }
    const model = data.models[turn];
    if (model) {
      const payload = await recordedJSON(model.file);
      const request = payload.request || {}, response = payload.response || {};
      const items = values => (Array.isArray(values) ? values : [{role:"user",content:values}]).filter(visible).map(item => [item.role || item.type || "native item",text(item),item.phase || item.name || ""]);
      result.turns[turn] = {model:request.model,effort:request.reasoning?.effort,end:model.end,input:items(request.input || []),output:items(response.output || []),instructions:text(request.instructions || ""),tools:text(request.tools || [])};
      if (payload.error) result.turns[turn].output.push(["Recorded failure",text(payload.error),""]);
    }
    return result;
  };
}

try {
  const data = await recordedJSON("./v41-graph.json");
  if (data.schema !== 1) throw new Error("This viewer requires a matching version of its recorded data.");
  const graph = materialize(data);
  const chooser = document.querySelector(".pf-switch");
  chooser.replaceChildren(...Object.entries(graph).map(([key,run]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.run = key;
    button.textContent = run.label;
    return button;
  }));
  startProfiler(graph,detailsLoader(data));
  document.getElementById("pf-loading").hidden = true;
} catch (error) {
  document.getElementById("pf-loading").textContent = error.message;
}
