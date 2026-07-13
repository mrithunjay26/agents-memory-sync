(function () {
  const app = document.querySelector(".app");
  if (!app) return;


  const tabs = Array.from(document.querySelectorAll(".stage-tab"));
  function setView(view) {
    app.dataset.view = view;
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === view));
  }
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => setView(tab.dataset.tab));
  });


  function syncProjectState() {
    const allActive = document
      .getElementById("all-projects-item")
      ?.classList.contains("active");
    app.classList.toggle("no-project", !!allActive);
  }


  function syncCounts() {
    const running = document.querySelectorAll(
      "#dispatch-history-list .dispatch-status.running"
    ).length;
    const depBadge = document.getElementById("tab-count-deploy");
    if (depBadge) depBadge.textContent = running ? String(running) : "";

    const conflicts = document.querySelectorAll(
      "#conflicts-list .conflict-row"
    ).length;
    const conBadge = document.getElementById("tab-count-conflicts");
    if (conBadge) conBadge.textContent = conflicts ? String(conflicts) : "";
  }

  const observer = new MutationObserver(() => {
    syncProjectState();
    syncCounts();
  });
  [
    document.querySelector(".rail-scroll"),
    document.getElementById("dispatch-history-list"),
    document.getElementById("conflicts-list"),
  ].forEach((node) => {
    if (node)
      observer.observe(node, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["class"],
      });
  });

  syncProjectState();
  syncCounts();
  setInterval(() => {
    syncProjectState();
    syncCounts();
  }, 1500);

  document.getElementById("rail-add")?.addEventListener("click", () => {
    const inp = document.getElementById("add-project-path");
    if (inp) {
      inp.focus();
    }
  });

  (function () {
    const search = document.getElementById("project-search");
    const list = document.getElementById("project-list");
    if (!search || !list) return;
    const apply = () => {
      const q = search.value.trim().toLowerCase();
      list.querySelectorAll(".sidebar-item").forEach((it) => {
        it.style.display =
          !q || it.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    };
    search.addEventListener("input", apply);
    new MutationObserver(apply).observe(list, { childList: true });
  })();
})();
