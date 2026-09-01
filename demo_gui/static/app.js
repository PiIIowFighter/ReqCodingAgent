(() => {
  "use strict";
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const root = document.documentElement;
  let ontology = null, selectedId = "coding-requirement-ontology";
  let activeTask = null, eventOffset = 0, pollTimer = null;
  let workspacePath = null, workspaceReady = false, workspaceDialogReason = "new-task";
  const escapeText = value => { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; };

  function toast(message) {
    const box = $("#runtime-toast"); box.textContent = message; box.classList.add("visible");
    setTimeout(() => box.classList.remove("visible"), 3000);
  }
  const applyTheme = theme => { root.dataset.theme = theme; localStorage.setItem("req-demo-theme", theme); };
  applyTheme(localStorage.getItem("req-demo-theme") === "light" ? "light" : "dark");
  $("#theme-toggle").addEventListener("click", () => applyTheme(root.dataset.theme === "dark" ? "light" : "dark"));
  $("#sidebar-toggle").addEventListener("click", event => {
    const collapsed = $("#sidebar").classList.toggle("collapsed");
    event.currentTarget.setAttribute("aria-expanded", String(!collapsed));
    event.currentTarget.textContent = collapsed ? "›" : "‹";
  });

  function settingFromPath(path) {
    return path.startsWith("/settings") ? "ontology" : null;
  }
  function showRoute(path, push = false) {
    const isSettings = path.startsWith("/settings");
    $("#home-view").hidden = isSettings; $("#settings-view").hidden = !isSettings;
    $$("[data-nav]").forEach(item => item.classList.toggle("active", item.dataset.nav === (isSettings ? "settings" : "home")));
    if (isSettings) {
      $$("[data-setting]").forEach(item => { item.classList.toggle("active", item.dataset.setting === "ontology"); item.toggleAttribute("aria-current", item.dataset.setting === "ontology"); });
      $$("[data-settings-page]").forEach(item => item.hidden = item.dataset.settingsPage !== "ontology");
    }
    if (push) history.pushState({}, "", isSettings ? "/settings/ontology" : path);
  }
  $$('a[href^="/"]').forEach(link => link.addEventListener("click", event => {
    if (link.id === "download-patch") return;
    event.preventDefault(); showRoute(link.getAttribute("href"), true);
  }));
  addEventListener("popstate", () => showRoute(location.pathname));
  showRoute(location.pathname);

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({error: "Unexpected server response."}));
    if (!response.ok) throw new Error(data.error || "Request failed.");
    return data;
  }

  function renderWorkspaceLabel() {
    const label = $("#workspace-label");
    label.textContent = workspacePath || "未选择";
    label.classList.toggle("unset", !workspacePath);
  }

  function updateComposerState() {
    const enabled = workspaceReady && !activeTask;
    $("#task-input").disabled = !enabled;
    $("#send-task").disabled = !enabled;
    $("#empty-state-hint").textContent = workspaceReady
      ? "Agent 会直接在工作目录中创建或修改文件。"
      : "请先选择本地工作目录，再描述任务。Agent 会直接在该目录中创建或修改文件。";
  }

  function applyRuntime(runtime) {
    workspacePath = runtime.workspace_path || null;
    workspaceReady = Boolean(runtime.ready);
    renderWorkspaceLabel();
    updateComposerState();
  }

  api("/api/runtime").then(runtime => {
    applyRuntime(runtime);
    if (!runtime.ready) openWorkspaceDialog("startup");
  }).catch(error => {
    workspacePath = null; workspaceReady = false;
    renderWorkspaceLabel(); updateComposerState();
    openWorkspaceDialog("startup");
    toast(error.message);
  });

  function openWorkspaceDialog(reason = "edit") {
    workspaceDialogReason = reason;
    $("#workspace-input").value = workspacePath || "";
    $("#workspace-dialog").hidden = false;
    $("#workspace-input").focus();
    $("#workspace-input").select();
  }

  function closeWorkspaceDialog() {
    $("#workspace-dialog").hidden = true;
  }

  async function confirmWorkspace() {
    const path = $("#workspace-input").value.trim();
    if (!path) { toast("请输入工作目录路径。"); return; }
    $("#workspace-confirm").disabled = true;
    try {
      const runtime = await api("/api/workspace", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path})
      });
      applyRuntime(runtime);
      closeWorkspaceDialog();
      toast("工作目录已设置。");
      if (workspaceDialogReason === "new-task") {
        $("#task-input").focus();
      }
    } catch (error) {
      toast(error.message);
    } finally {
      $("#workspace-confirm").disabled = false;
    }
  }

  $("#workspace-edit").addEventListener("click", () => openWorkspaceDialog("edit"));
  $("#workspace-cancel").addEventListener("click", closeWorkspaceDialog);
  $("#workspace-confirm").addEventListener("click", confirmWorkspace);
  $("#workspace-input").addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); confirmWorkspace(); }
    else if (event.key === "Escape") closeWorkspaceDialog();
  });
  $("#workspace-dialog").addEventListener("click", event => {
    if (event.target === $("#workspace-dialog")) closeWorkspaceDialog();
  });

  function resetTask() {
    activeTask = null; eventOffset = 0; clearTimeout(pollTimer);
    $("#empty-state").hidden = false; $("#task-view").hidden = true; $("#result-card").hidden = true;
    $("#timeline").replaceChildren(); $("#patch-preview").textContent = "";
    $("#task-input").value = "";
    workspaceReady = false;
    renderWorkspaceLabel();
    updateComposerState();
    openWorkspaceDialog("new-task");
  }
  $("#new-task").addEventListener("click", () => { showRoute("/", true); resetTask(); });

  function setStatus(status) {
    const badge = $("#task-status"); badge.textContent = status[0].toUpperCase() + status.slice(1);
    badge.className = "task-status " + status;
  }
  function addEvent(event) {
    const item = document.createElement("li");
    const label = event.kind === "tool_result" ? event.tool : "Model response";
    const detail = event.kind === "tool_result" ? event.summary : (event.text || (event.tools.length ? "Requested " + event.tools.join(", ") : "Response completed."));
    item.className = "timeline-item " + (event.ok === false ? "failed" : "");
    item.innerHTML = `<span class="timeline-dot"></span><div><div class="timeline-meta"><strong>${escapeText(label)}</strong><span>${escapeText(event.phase)}</span></div><p>${escapeText(detail)}</p></div>`;
    $("#timeline").append(item);
  }
  async function refreshTask() {
    if (!activeTask) return;
    try {
      const [task, feed] = await Promise.all([
        api(`/api/tasks/${activeTask}`),
        api(`/api/tasks/${activeTask}/events?after=${eventOffset}`)
      ]);
      setStatus(task.status); feed.events.forEach(addEvent); eventOffset = feed.next_offset;
      if (task.status === "completed" || task.status === "failed") {
        updateComposerState();
        await showResult(task); return;
      }
      pollTimer = setTimeout(refreshTask, 700);
    } catch (error) {
      toast(error.message); pollTimer = setTimeout(refreshTask, 1600);
    }
  }
  async function showResult(task) {
    const card = $("#result-card"); card.hidden = false;
    $("#result-title").textContent = task.status === "completed" ? "Agent finished" : "Agent failed";
    $("#stop-reason").textContent = task.stop_reason || task.status;
    $("#result-summary").textContent = task.summary || task.error || "No final summary was produced.";
    $("#result-limitations").textContent = task.limitations || "";
    const stats = task.patch || {};
    $("#patch-meta").textContent = `${stats.files || 0} files · +${stats.additions || 0} · −${stats.deletions || 0}`;
    $("#download-patch").href = `/api/tasks/${task.id}/patch/download`;
    try {
      const payload = await api(`/api/tasks/${task.id}/patch`);
      $("#patch-preview").textContent = payload.patch || "No patch was generated.";
    } catch (error) {
      $("#patch-preview").textContent = error.message;
    }
    card.scrollIntoView({behavior: "smooth", block: "nearest"});
  }
  async function submitTask() {
    if (!workspaceReady) { openWorkspaceDialog("submit"); return; }
    const task = $("#task-input").value.trim();
    if (!task) { toast("Describe a task first."); return; }
    $("#send-task").disabled = true; $("#task-input").disabled = true;
    try {
      const record = await api("/api/tasks", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({task})});
      activeTask = record.id; eventOffset = 0;
      $("#empty-state").hidden = true; $("#task-view").hidden = false; $("#result-card").hidden = true;
      $("#timeline").replaceChildren(); $("#task-title").textContent = task; setStatus(record.status);
      refreshTask();
    } catch (error) {
      updateComposerState(); toast(error.message);
    }
  }
  $("#send-task").addEventListener("click", submitTask);
  $("#task-input").addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submitTask(); }
  });

  function annotationFor(node) {
    if (node.type === "root") return ontology.annotations.root;
    if (node.type === "category") return ontology.annotations.categories[node.id];
    return ontology.annotations.slots[node.id];
  }
  function nodeLabel(node) {
    if (node.type === "root") return "Coding Requirement Ontology";
    return `${annotationFor(node).name_zh} · ${node.id}`;
  }
  function allNodes() { return [ontology.tree, ...ontology.tree.children, ...ontology.tree.children.flatMap(category => category.children)]; }
  function nodeById(id) { return allNodes().find(node => node.id === id); }
  function parentOf(id) { return ontology.tree.children.find(category => category.children.some(slot => slot.id === id)) || (id !== ontology.tree.id ? ontology.tree : null); }
  function selectNode(id, focus = false) {
    selectedId = id;
    $$("[role=treeitem]").forEach(item => {
      const selected = item.dataset.id === id; item.classList.toggle("selected", selected);
      item.setAttribute("aria-selected", String(selected)); if (selected && focus) item.focus();
    });
    renderDetail(nodeById(id));
  }
  function renderDetail(node) {
    const note = annotationFor(node); let body;
    if (node.type === "root") body = `<dl><div><dt>Version</dt><dd><code>${escapeText(ontology.version)}</code></dd></div><div><dt>Baseline</dt><dd>${escapeText(ontology.baseline)}</dd></div><div><dt>Integrity</dt><dd class="success-text">Verified</dd></div><div><dt>Structure</dt><dd>${ontology.category_count} categories · ${ontology.slot_count} slots</dd></div></dl><h3>Role in ReqRefine</h3><p>${escapeText(note.purpose)}</p>`;
    else if (node.type === "category") body = `<p class="node-id">${escapeText(node.id)}</p><p>${escapeText(note.purpose)}</p><dl><div><dt>Slots</dt><dd>${node.children.length}</dd></div></dl><h3>Role in refinement</h3><p>${escapeText(note.role)}</p>`;
    else body = `<p class="node-id">${escapeText(node.id)}</p><p>${escapeText(note.definition)}</p><h3>Why it matters</h3><p>${escapeText(note.importance)}</p><h3>Recommended evidence</h3><div class="chips">${note.evidence.map(item => `<span>${escapeText(item)}</span>`).join("")}</div><h3>Example</h3><p class="example">${escapeText(note.example)}</p><h3>Supported states</h3><div class="chips mono">${ontology.annotations.statuses.map(item => `<span>${escapeText(item)}</span>`).join("")}</div>`;
    $("#detail-panel").innerHTML = `<p class="eyebrow">${node.type === "slot" ? "Requirement slot" : node.type}</p><h2>${escapeText(nodeLabel(node))}</h2>${body}`;
  }
  function makeItem(node, level, expanded) {
    const item = document.createElement("div"); item.className = `tree-item ${node.type}`; item.role = "treeitem";
    item.tabIndex = 0; item.dataset.id = node.id; item.setAttribute("aria-level", String(level)); item.setAttribute("aria-selected", "false");
    if (node.children) item.setAttribute("aria-expanded", String(expanded));
    item.innerHTML = `${node.children ? '<span class="chevron"></span>' : '<span class="leaf-mark"></span>'}<span>${escapeText(nodeLabel(node))}</span>${node.children ? `<small>${node.children.length}</small>` : ""}`;
    item.addEventListener("click", () => { selectNode(node.id); if (node.children) toggleNode(item); });
    item.addEventListener("keydown", onTreeKey); return item;
  }
  function renderTree() {
    const target = $("#ontology-tree"); target.replaceChildren(); const rootGroup = document.createElement("div"); rootGroup.className = "tree-node";
    const rootItem = makeItem(ontology.tree, 1, true); rootGroup.append(rootItem); const categories = document.createElement("div"); categories.role = "group";
    ontology.tree.children.forEach((category, index) => {
      const categoryNode = document.createElement("div"); categoryNode.className = "tree-node"; const categoryItem = makeItem(category, 2, index === 0);
      const slots = document.createElement("div"); slots.role = "group"; slots.hidden = index !== 0;
      category.children.forEach(slot => slots.append(makeItem(slot, 3, false))); categoryNode.append(categoryItem, slots); categories.append(categoryNode);
    });
    rootGroup.append(categories); target.append(rootGroup); selectNode(selectedId);
  }
  function toggleNode(item, force) {
    const group = item.nextElementSibling; if (!group) return;
    const next = force === undefined ? item.getAttribute("aria-expanded") !== "true" : force;
    item.setAttribute("aria-expanded", String(next)); group.hidden = !next;
  }
  function visibleItems() { return $$("[role=treeitem]").filter(item => !item.closest("[role=group][hidden]") && !item.closest(".tree-node[hidden]")); }
  function onTreeKey(event) {
    const items = visibleItems(), index = items.indexOf(event.currentTarget), expanded = event.currentTarget.getAttribute("aria-expanded");
    if (["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Enter", " "].includes(event.key)) event.preventDefault();
    if (event.key === "ArrowDown") (items[index + 1] || items[0]).focus();
    else if (event.key === "ArrowUp") (items[index - 1] || items.at(-1)).focus();
    else if (event.key === "ArrowRight" && expanded === "false") toggleNode(event.currentTarget, true);
    else if (event.key === "ArrowLeft" && expanded === "true") toggleNode(event.currentTarget, false);
    else if (event.key === "ArrowLeft") { const parent = parentOf(event.currentTarget.dataset.id); if (parent) selectNode(parent.id, true); }
    else if (event.key === "Enter" || event.key === " ") { selectNode(event.currentTarget.dataset.id); if (expanded !== null) toggleNode(event.currentTarget); }
  }
  $("#expand-all").addEventListener("click", () => $$("[role=treeitem][aria-expanded]").forEach(item => toggleNode(item, true)));
  $("#collapse-all").addEventListener("click", () => $$("[role=treeitem][aria-expanded]").forEach(item => toggleNode(item, false)));
  $("#ontology-search").addEventListener("input", event => {
    const query = event.target.value.trim().toLowerCase(); let matches = 0;
    $$(".tree-node").forEach(node => node.hidden = false); $$("[role=treeitem]").forEach(item => item.classList.remove("match"));
    ontology.tree.children.forEach(category => {
      let categoryMatch = !query || nodeLabel(category).toLowerCase().includes(query) || annotationFor(category).purpose.toLowerCase().includes(query);
      category.children.forEach(slot => {
        const note = annotationFor(slot), match = !query || [slot.id, note.name_zh, note.definition, note.importance, note.example].join(" ").toLowerCase().includes(query);
        const item = $("[data-id=\"" + CSS.escape(slot.id) + "\"]"); item.parentElement.hidden = !match;
        item.classList.toggle("match", Boolean(query && match)); if (match) matches += 1; categoryMatch ||= match;
      });
      const item = $("[data-id=\"" + CSS.escape(category.id) + "\"]"); item.parentElement.hidden = !categoryMatch;
      if (query && categoryMatch) { toggleNode(item, true); const rootItem = $('[data-id="coding-requirement-ontology"]'); toggleNode(rootItem, true); }
    });
    $("#tree-empty").hidden = matches !== 0;
  });
  api("/api/ontology").then(data => {
    if (!data.verified) throw new Error(data.integrity_error);
    ontology = data; $("#ontology-loading").hidden = true; $("#ontology-workspace").hidden = false;
    $("#ontology-source").textContent = data.source; $("#category-count").textContent = data.category_count;
    $("#slot-count").textContent = data.slot_count; $("#annotation-disclaimer").textContent = data.annotations.disclaimer; renderTree();
  }).catch(error => {
    $("#ontology-loading").hidden = true; const panel = $("#ontology-error"); panel.hidden = false; panel.textContent = error.message;
    const badge = $("#integrity-badge"); badge.className = "failed"; badge.textContent = "Integrity failure";
  });
})();
