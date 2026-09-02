(() => {
  "use strict";
  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];
  const root = document.documentElement;
  let ontology = null, selectedId = "coding-requirement-ontology";
  let activeTask = null, eventOffset = 0, pollTimer = null, seenPhases = new Set(), routeShown = false;
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
    activeTask = null; eventOffset = 0; seenPhases = new Set(); routeShown = false; clearTimeout(pollTimer);
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
    const badge = $("#task-status");
    const statusMap = {
      "queued": "排队中",
      "interviewing": "访谈中",
      "awaiting_user": "等待回答",
      "awaiting_confirmation": "待确认",
      "running": "执行中",
      "completed": "已完成",
      "failed": "失败"
      ,"stopped": "已停止"
    };
    badge.textContent = statusMap[status] || status[0].toUpperCase() + status.slice(1);
    badge.className = "task-status " + status;
  }
  const PHASES = [["需求识别", "intake"], ["主动澄清", "refinement"], ["需求基线", "baseline"], ["仓库调查", "investigation"], ["代码修改", "implementation"], ["验证", "verification"], ["完成", "complete"]];
  const PHASE_LABELS = Object.fromEntries(PHASES.map(([label, key]) => [key, label]));
  const REASON_LABELS = {
    weak_language: "表述较弱", abstract_behavior: "行为仍抽象", unresolved_reference: "存在未解析指代",
    goal: "缺少明确目标", target: "缺少代码范围", observable_behavior: "缺少可观察行为", validation: "缺少验证方式",
    detailed_behavior_contract: "任务已包含完整行为契约", task_is_actionable: "任务已可直接执行"
  };
  const SKILL_LABELS = {reference_resolution: "指代消解", specificity_expansion: "具体性扩展", omission_recovery: "缺失维度恢复"};
  const TOOL_LABELS = {
    list_files: "查看文件 · list_files", read_file: "读取文件 · read_file", search_text: "检索代码 · search_text",
    record_requirement_brief: "记录需求基线 · record_requirement_brief", apply_patch: "应用补丁 · apply_patch",
    run_command: "执行验证 · run_command", submit: "提交结果 · submit"
  };
  const SLOT_STATUS_LABELS = {
    confirmed: "已确认", rejected: "已排除", unresolved: "待解决", unexplored: "未探索",
    explicit: "用户明确", inferred: "仓库推断", defaulted: "安全默认"
  };

  function renderPhases(task, events) {
    seenPhases.add("intake");
    if (task.route_decision?.mode === "refine") seenPhases.add("refinement");
    if (["interviewing", "awaiting_user"].includes(task.status)) seenPhases.add("refinement");
    if (task.status === "awaiting_confirmation") seenPhases.add("baseline");
    events.forEach(event => { if (event.phase) seenPhases.add(event.phase); });
    const seen = seenPhases;
    const done = task.status === "completed" || task.stop_reason === "submitted";
    $("#phase-nav").innerHTML = PHASES.map(([label, key]) => {
      const active = seen.has(key) || (key === "refinement" && ["interviewing", "awaiting_user", "awaiting_confirmation"].includes(task.status));
      return `<li class="phase ${active ? "active" : ""} ${done && key === "complete" ? "active" : ""}"><span>${escapeText(label)}</span></li>`;
    }).join("");
  }
  function renderCoverage(target, coverage) {
    if (!coverage) { target.innerHTML = ""; return; }
    target.innerHTML = `<div class="coverage-heading"><strong>需求维度覆盖</strong><span>${coverage.covered} / ${coverage.total}</span></div><div class="coverage-grid">${coverage.categories.map(category => `<section><h4>${escapeText(category.name_zh)} <small>${category.slots.length}</small></h4>${category.slots.map(slot => `<div class="coverage-slot ${slot.status}"><span>${escapeText(slot.name_zh)}</span><code>${escapeText(slot.id)}</code><em>${escapeText(SLOT_STATUS_LABELS[slot.status] || slot.status)}</em>${slot.selection_reason ? `<p>${escapeText(slot.selection_reason)}</p>` : ""}</div>`).join("")}</section>`).join("")}</div><p class="coverage-note">${escapeText(coverage.note)}</p>`;
  }

  function renderInitialRoute(task) {
    if (routeShown || !task.route_decision?.mode) return;
    addEvent({...task.route_decision, kind: "route_decision", phase: "intake"});
    routeShown = true;
  }

  function addEvent(event) {
    const item = document.createElement("li");
    let label, detail;
    if (event.kind === "route_decision") {
      const afterBaseline = event.source !== "interactive_router" && seenPhases.has("baseline") && event.mode === "fast";
      label = afterBaseline ? "需求基线已确认，进入执行" : (event.mode === "refine" ? "判断需要需求细化" : "判断可直接执行");
      const reasons = event.reasons || [], skills = event.selected_skills || [];
      const reasonsText = reasons.length ? reasons.map(reason => REASON_LABELS[reason] || reason).join("、") : "无";
      const skillsText = skills.length ? skills.map(skill => SKILL_LABELS[skill] || skill).join("、") : "无";
      detail = `模式: ${event.mode}; 原因: ${reasonsText}; 选择技能: ${skillsText}`;
    } else if (event.kind === "requirement_brief_recorded") {
      label = "需求基线已形成";
      detail = "需求细化完成，进入代码实现阶段";
    } else if (event.kind === "tool_result") {
      label = TOOL_LABELS[event.tool] || event.tool;
      detail = event.summary;
    } else {
      label = "模型响应";
      detail = event.text || (event.tools.length
        ? "请求调用：" + event.tools.map(tool => TOOL_LABELS[tool] || tool).join("、")
        : "模型响应已完成。");
    }
    item.className = "timeline-item " + (event.ok === false ? "failed" : "");
    item.innerHTML = `<span class="timeline-dot"></span><div><div class="timeline-meta"><strong>${escapeText(label)}</strong><span>${escapeText(PHASE_LABELS[event.phase] || event.phase || "")}</span></div><p>${escapeText(detail)}</p></div>`;
    $("#timeline").append(item);
  }
  async function refreshTask() {
    if (!activeTask) return;
    try {
      const [task, feed] = await Promise.all([
        api(`/api/tasks/${activeTask}`),
        api(`/api/tasks/${activeTask}/events?after=${eventOffset}`)
      ]);
      setStatus(task.status); renderPhases(task, feed.events); renderInitialRoute(task);
      feed.events.forEach(event => {
        if (event.kind === "route_decision" && routeShown && !seenPhases.has("baseline")) return;
        addEvent(event);
      });
      eventOffset = feed.next_offset;
      if (task.requirement_coverage) { renderCoverage($("#coverage-panel"), task.requirement_coverage); renderCoverage($("#baseline-coverage"), task.requirement_coverage); }

      // Handle interview states
      if (task.status === "awaiting_user") {
        showInterviewQuestion(task);
        return;
      } else if (task.status === "awaiting_confirmation") {
        showBaselineConfirmation(task);
        return;
      } else if (["completed", "failed", "stopped"].includes(task.status)) {
        updateComposerState();
        await showResult(task); return;
      }

      pollTimer = setTimeout(refreshTask, 700);
    } catch (error) {
      toast(error.message); pollTimer = setTimeout(refreshTask, 1600);
    }
  }

  function showInterviewQuestion(task) {
    const card = $("#interview-card");
    if (!card) return; // Card will be created in HTML
    card.hidden = false;
    $("#result-card").hidden = true;

    const q = task.current_question;
    if (!q) return;

    $("#interview-turn").textContent = `问题 ${q.turn_number} / 最多 ${q.max_turns} 轮`;
    $("#interview-question").textContent = q.question;
    $("#interview-slots").innerHTML = q.slot_ids.map(id => `<span class="slot-chip">${escapeText(id)}</span>`).join("") + (q.selection_reason ? `<p class="selection-reason">${escapeText(q.selection_reason)}</p>` : "");

    // Show history
    const history = task.interview_history || [];
    const historyHTML = history.map((h, i) => `
      <div class="qa-pair">
        <div class="qa-question"><strong>问题 ${i + 1}:</strong> ${escapeText(h.question)}</div>
        <div class="qa-answer"><strong>回答:</strong> ${escapeText(h.answer)}</div>
      </div>
    `).join("");
    $("#interview-history").innerHTML = historyHTML;

    $("#interview-answer").value = "";
    $("#interview-answer").disabled = false;
    $("#interview-submit").disabled = false;
    $("#interview-submit").onclick = () => submitAnswer(task.id, q.turn_id);
  }

  async function submitAnswer(taskId, turnId) {
    const answer = $("#interview-answer").value.trim();
    if (!answer) {
      toast("请输入回答");
      return;
    }

    $("#interview-answer").disabled = true;
    $("#interview-submit").disabled = true;

    try {
      await api(`/api/tasks/${taskId}/answer`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({turn_id: turnId, answer})
      });
      toast("回答已提交");
      pollTimer = setTimeout(refreshTask, 500);
    } catch (error) {
      toast(error.message);
      $("#interview-answer").disabled = false;
      $("#interview-submit").disabled = false;
    }
  }

  function showBaselineConfirmation(task) {
    const card = $("#baseline-card");
    if (!card) return;
    card.hidden = false;
    $("#interview-card").hidden = true;
    $("#result-card").hidden = true;

    const b = task.baseline;
    if (!b) return;

    $("#baseline-summary").textContent = b.refined_summary;
    $("#baseline-requirements").innerHTML = b.requirements.map(r => `<li>${escapeText(r)}</li>`).join("");
    $("#baseline-acceptance").innerHTML = b.acceptance_criteria.map(c => `<li>${escapeText(c)}</li>`).join("");
    $("#baseline-constraints").innerHTML = b.constraints.map(c => `<li>${escapeText(c)}</li>`).join("") || "<li>无</li>";
    $("#baseline-excluded").innerHTML = b.excluded_scope.map(e => `<li>${escapeText(e)}</li>`).join("") || "<li>无</li>";
    $("#baseline-assumptions").innerHTML = b.assumptions.map(a => `<li>${escapeText(a)}</li>`).join("") || "<li>无</li>";
    $("#baseline-unresolved").innerHTML = b.unresolved_items.map(u => `<li>${escapeText(u)}</li>`).join("") || "<li>无</li>";

    $("#baseline-confirm-btn").disabled = false;
    $("#baseline-confirm-btn").onclick = () => confirmBaseline(task.id);
  }

  async function confirmBaseline(taskId) {
    $("#baseline-confirm-btn").disabled = true;

    try {
      await api(`/api/tasks/${taskId}/confirm`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({})
      });
      toast("需求已确认，开始编码");
      $("#baseline-card").hidden = true;
      pollTimer = setTimeout(refreshTask, 500);
    } catch (error) {
      toast(error.message);
      $("#baseline-confirm-btn").disabled = false;
    }
  }

  async function showResult(task) {
    const card = $("#result-card"); card.hidden = false;
    const isSubmitted = task.stop_reason === "submitted";
    $("#result-title").textContent = isSubmitted ? "Agent 已完成" : (task.status === "stopped" ? "Agent 已停止" : "Agent 执行失败");
    $("#stop-reason").textContent = task.stop_reason || task.status;
    $("#result-summary").textContent = task.summary || task.error || "未生成最终摘要。";
    $("#result-limitations").textContent = task.limitations || "";
    $("#result-evidence").innerHTML = `<div><dt>步骤 / 工具调用</dt><dd>${task.steps || 0} / ${task.tool_calls || 0}</dd></div><div><dt>已执行验证</dt><dd>${(task.submitted_tests || []).length ? task.submitted_tests.map(escapeText).join("; ") : "无"}</dd></div>${task.unverified_test_claims ? '<div><dt>提醒</dt><dd class="warning-text">存在未核验的测试声明</dd></div>' : ""}`;
    const stats = task.patch || {};
    $("#patch-meta").textContent = `${stats.files || 0} files · +${stats.additions || 0} · −${stats.deletions || 0}`;
    $("#download-patch").href = `/api/tasks/${task.id}/patch/download`;
    try {
      const payload = await api(`/api/tasks/${task.id}/patch`);
      $("#patch-preview").textContent = payload.patch || "未生成补丁。";
    } catch (error) {
      $("#patch-preview").textContent = error.message;
    }
    card.scrollIntoView({behavior: "smooth", block: "nearest"});
  }
  async function submitTask() {
    if (!workspaceReady) { openWorkspaceDialog("submit"); return; }
    const task = $("#task-input").value.trim();
    if (!task) { toast("请先描述任务。"); return; }
    $("#send-task").disabled = true; $("#task-input").disabled = true;
    try {
      const record = await api("/api/tasks", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({task})});
      activeTask = record.id; eventOffset = 0; seenPhases = new Set(); routeShown = false;
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
    if (node.type === "root") return "通用编码需求本体";
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
    if (node.type === "root") body = `<dl><div><dt>版本</dt><dd><code>${escapeText(ontology.version)}</code></dd></div><div><dt>冻结基线</dt><dd>${escapeText(ontology.baseline)}</dd></div><div><dt>完整性</dt><dd class="success-text">SHA-256 已验证</dd></div><div><dt>结构</dt><dd>${ontology.category_count} 类 · ${ontology.slot_count} 个槽位</dd></div></dl><h3>在需求细化中的作用</h3><p>${escapeText(note.purpose)}</p>`;
    else if (node.type === "category") body = `<p class="node-id">${escapeText(node.id)}</p><p>${escapeText(note.purpose)}</p><dl><div><dt>槽位数</dt><dd>${node.children.length}</dd></div></dl><h3>细化作用</h3><p>${escapeText(note.role)}</p>`;
    else body = `<p class="node-id">${escapeText(node.id)}</p><p>${escapeText(note.definition)}</p><h3>为什么重要</h3><p>${escapeText(note.importance)}</p><h3>推荐证据</h3><div class="chips">${note.evidence.map(item => `<span>${escapeText(item)}</span>`).join("")}</div><h3>跨场景示例</h3><p class="example">${escapeText(note.example)}</p><h3>支持的状态</h3><div class="chips mono">${ontology.annotations.statuses.map(item => `<span>${escapeText(SLOT_STATUS_LABELS[item] || item)}</span>`).join("")}</div>`;
    const typeLabel = node.type === "slot" ? "需求槽位" : (node.type === "category" ? "需求维度" : "通用本体");
    $("#detail-panel").innerHTML = `<p class="eyebrow">${typeLabel}</p><h2>${escapeText(nodeLabel(node))}</h2>${body}`;
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
    $("#slot-count").textContent = data.slot_count; $("#annotation-disclaimer").textContent = data.annotations.disclaimer;
    if (data.scenario) {
      const banner = $("#scenario-overlay-note");
      banner.hidden = false;
      banner.innerHTML = `<strong>当前案例映射（只读）</strong><span>${escapeText(data.scenario.title)}：仅把任务信息映射到通用槽位，不新增槽位，也不改变路由或决策逻辑。</span>`;
    }
    renderTree();
  }).catch(error => {
    $("#ontology-loading").hidden = true; const panel = $("#ontology-error"); panel.hidden = false; panel.textContent = error.message;
    const badge = $("#integrity-badge"); badge.className = "failed"; badge.textContent = "完整性校验失败";
  });
})();
