(() => {
  "use strict";
  const root = document.documentElement;
  const views = {ontology: document.querySelector("#ontology-view"), settings: document.querySelector("#settings-view")};
  const title = document.querySelector("#page-title");
  const themeButtons = [document.querySelector("#theme-toggle"), document.querySelector("#settings-theme")];
  let treeItems = [];
  let annotations = null;

  function applyTheme(theme) {
    root.dataset.theme = theme;
    localStorage.setItem("demo-theme", theme);
  }
  function toggleTheme() { applyTheme(root.dataset.theme === "dark" ? "light" : "dark"); }
  applyTheme(localStorage.getItem("demo-theme") === "light" ? "light" : "dark");
  themeButtons.forEach(button => button.addEventListener("click", toggleTheme));

  function routeFromPath(path) { return path === "/settings" ? "settings" : "ontology"; }
  function showRoute(route, push = false) {
    Object.entries(views).forEach(([name, view]) => { view.hidden = name !== route; });
    document.querySelectorAll("[data-route]").forEach(link => {
      const active = link.dataset.route === route;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    title.textContent = route === "settings" ? "Settings" : "Requirement ontology";
    if (push) history.pushState({route}, "", route === "settings" ? "/settings" : "/");
  }
  document.querySelectorAll("a[data-route]").forEach(link => link.addEventListener("click", event => {
    event.preventDefault(); showRoute(link.dataset.route, true);
  }));
  addEventListener("popstate", () => showRoute(routeFromPath(location.pathname)));
  showRoute(routeFromPath(location.pathname));

  function focusItem(index) {
    const visible = treeItems.filter(item => item.offsetParent !== null);
    if (!visible.length) return;
    visible[Math.max(0, Math.min(index, visible.length - 1))].focus();
  }
  function keyboard(event) {
    const visible = treeItems.filter(item => item.offsetParent !== null);
    const index = visible.indexOf(event.currentTarget);
    if (event.key === "ArrowDown") { event.preventDefault(); focusItem(index + 1); }
    if (event.key === "ArrowUp") { event.preventDefault(); focusItem(index - 1); }
    if ((event.key === "Enter" || event.key === " ") && event.currentTarget.dataset.key) {
      event.preventDefault(); selectLeaf(event.currentTarget.dataset.key, event.currentTarget);
    }
    if ((event.key === "Enter" || event.key === " ") && event.currentTarget.hasAttribute("aria-expanded")) {
      event.preventDefault(); toggleGroup(event.currentTarget);
    }
    if (event.key === "ArrowRight" && event.currentTarget.getAttribute("aria-expanded") === "false") toggleGroup(event.currentTarget);
    if (event.key === "ArrowLeft" && event.currentTarget.getAttribute("aria-expanded") === "true") toggleGroup(event.currentTarget);
  }
  function toggleGroup(item) {
    const expanded = item.getAttribute("aria-expanded") === "true";
    item.setAttribute("aria-expanded", String(!expanded));
    item.nextElementSibling.hidden = expanded;
  }
  function selectLeaf(key, item) {
    document.querySelectorAll(".tree-leaf").forEach(node => {
      node.classList.toggle("selected", node === item);
      node.setAttribute("aria-selected", String(node === item));
    });
    const value = annotations.annotations[key];
    document.querySelector("#detail-panel").innerHTML = `<p class="eyebrow">Field details</p><h2>${escapeText(value.label)}</h2><p>${escapeText(value.description)}</p><span class="detail-key">${escapeText(value.key)}</span><p class="disclaimer">${escapeText(annotations.disclaimer)}</p>`;
  }
  function escapeText(value) {
    const node = document.createElement("span"); node.textContent = value; return node.innerHTML;
  }
  function render(data) {
    const target = document.querySelector("#ontology-tree");
    data.tree.forEach(group => {
      const section = document.createElement("div"); section.className = "tree-group";
      const heading = document.createElement("div"); heading.setAttribute("role", "treeitem"); heading.tabIndex = 0;
      heading.setAttribute("aria-expanded", "true"); heading.innerHTML = `<span class="chevron"></span><span>${escapeText(group.label)}</span>`;
      heading.addEventListener("click", () => toggleGroup(heading)); heading.addEventListener("keydown", keyboard);
      const children = document.createElement("div"); children.setAttribute("role", "group");
      group.children.forEach(child => {
        const leaf = document.createElement("div"); leaf.className = "tree-leaf"; leaf.setAttribute("role", "treeitem");
        leaf.setAttribute("aria-selected", "false"); leaf.tabIndex = 0; leaf.dataset.key = child.key;
        leaf.textContent = annotations.annotations[child.key].label; leaf.addEventListener("click", () => selectLeaf(child.key, leaf));
        leaf.addEventListener("keydown", keyboard); children.appendChild(leaf);
      });
      section.append(heading, children); target.appendChild(section);
    });
    treeItems = [...target.querySelectorAll("[role=treeitem]")];
  }
  document.querySelector("#tree-search").addEventListener("input", event => {
    const query = event.target.value.trim().toLowerCase(); let matches = 0;
    document.querySelectorAll(".tree-group").forEach(group => {
      const leaves = [...group.querySelectorAll(".tree-leaf")];
      leaves.forEach(leaf => { const show = !query || leaf.textContent.toLowerCase().includes(query) || leaf.dataset.key.includes(query); leaf.hidden = !show; if (show) matches += 1; });
      group.hidden = query && !leaves.some(leaf => !leaf.hidden);
      if (query && !group.hidden) { group.firstElementChild.setAttribute("aria-expanded", "true"); group.lastElementChild.hidden = false; }
    });
    document.querySelector("#tree-empty").hidden = matches !== 0;
    document.querySelector("#tree-count").textContent = query ? `${matches} matching fields` : "4 groups · 11 fields";
  });

  Promise.all([fetch("/api/ontology").then(response => response.ok ? response.json() : Promise.reject()), fetch("/api/annotations").then(response => response.ok ? response.json() : Promise.reject())])
    .then(([ontology, notes]) => { annotations = notes; render(ontology); })
    .catch(() => { document.querySelector("#ontology-tree").textContent = "Verified data is unavailable."; });
})();
