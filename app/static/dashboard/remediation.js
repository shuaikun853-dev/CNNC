(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
  const DEV = {
    NOT_STARTED: "未开始", ASSIGNED: "已交给 Codex", BRANCH_ONLY: "开发中",
    PR_OPEN: "PR 中", PR_DRAFT: "PR 草稿", PR_UNKNOWN: "PR 状态未知",
    MERGED: "已合并"
  };
  const REVIEW = {
    NOT_REVIEWED: "未审", PENDING: "待审计", PARTIAL: "部分通过",
    PASSED: "审计通过", FAILED: "审计不通过"
  };
  const PROD = {
    NOT_STARTED: "未开始", NOT_MERGED: "未进生产",
    MERGED_NOT_DEPLOYED: "已合并未部署", DEPLOYED: "生产已有代码"
  };
  let payload = null;
  let activeFilter = "all";

  function badge(text, cls) {
    return '<span class="badge ' + cls + '">' + esc(text) + "</span>";
  }
  function reviewClass(state) {
    if (state === "PASSED") return "good";
    if (state === "FAILED") return "bad";
    if (state === "PENDING" || state === "PARTIAL") return "warn";
    return "neutral";
  }
  function productionText(item) {
    const state = item.production?.state || "NOT_STARTED";
    if (state === "DEPLOYED" && item.production_verification?.status === "VERIFIED") {
      return "生产验证通过";
    }
    if (state === "DEPLOYED") return "已部署 · 待验证";
    return PROD[state] || state;
  }
  function prText(pr) {
    if (!pr) return "未创建";
    if (pr.number) return "#" + pr.number + " · " + (pr.state || "UNKNOWN");
    if (pr.state === "UNKNOWN") return "状态未知";
    return "未创建";
  }

  function matchesFilter(item, filter) {
    if (filter === "p0") return item.severity === "P0";
    if (filter === "dev") return [
      "ASSIGNED", "BRANCH_ONLY", "PR_OPEN", "PR_DRAFT", "PR_UNKNOWN"
    ].includes(item.development?.state);
    if (filter === "review") return ["PENDING", "PARTIAL"].includes(item.review?.status);
    if (filter === "merge") return item.production?.state === "MERGED_NOT_DEPLOYED";
    if (filter === "prod") return item.production_verification?.status === "VERIFIED";
    return true;
  }
  function matchesSearch(item, q) {
    if (!q) return true;
    const hay = [
      item.id, item.title, item.branch, item.next_action,
      item.chatgpt?.title, item.codex?.title, item.pr?.number
    ].join(" ").toLowerCase();
    return hay.includes(q.toLowerCase());
  }
  function renderRows(items) {
    const q = ($("#search")?.value || "").trim();
    const rows = (items || []).filter((item) =>
      matchesFilter(item, activeFilter) && matchesSearch(item, q));
    if (!rows.length) {
      $("#rows").innerHTML = '<tr><td colspan="8" class="loading">没有符合条件的整改项</td></tr>';
      return;
    }
    $("#rows").innerHTML = rows.map((item) => {
      const dev = item.development?.state || "NOT_STARTED";
      const rv = item.review?.status || "NOT_REVIEWED";
      const pr = item.pr;
      const prLabel = prText(pr);
      const prod = productionText(item);
      const prodCls = item.production?.state === "DEPLOYED" ? "good" :
        item.production?.state === "MERGED_NOT_DEPLOYED" ? "warn" : "neutral";
      return '<tr data-id="' + esc(item.id) + '">' +
        '<td><span class="bug">' + esc(item.id) + "</span><br>" +
          badge(item.severity, item.severity.toLowerCase()) + "</td>" +
        '<td class="title">' + esc(item.title) + "</td>" +
        "<td>" + esc(item.chatgpt?.title || "未关联") + "</td>" +
        "<td>" + badge(DEV[dev] || dev, dev === "NOT_STARTED" ? "neutral" : "dev") +
          '<br><span class="muted">' + esc(item.codex?.title || item.branch || "—") + "</span></td>" +
        "<td>" + badge(REVIEW[rv] || rv, reviewClass(rv)) + "</td>" +
        "<td>" + esc(prLabel) + "</td>" +
        "<td>" + badge(prod, prodCls) + "</td>" +
        '<td class="next-cell">' + esc(item.next_action || "—") + "</td></tr>";
    }).join("");
    document.querySelectorAll("#rows tr[data-id]").forEach((tr) =>
      tr.addEventListener("click", () => openDetail(tr.dataset.id)));
  }

  function step(name, state, text) {
    return '<div class="step ' + state + '"><strong>' + esc(name) +
      "</strong><span>" + esc(text) + "</span></div>";
  }
  function line(label, value, url) {
    const val = value || "—";
    const shown = url
      ? '<a href="' + esc(url) + '" target="_blank" rel="noreferrer">' + esc(val) + "</a>"
      : esc(val);
    return "<p><strong>" + esc(label) + "：</strong>" + shown + "</p>";
  }
  function openDetail(id) {
    const item = payload?.items?.find((x) => x.id === id);
    if (!item) return;
    const tpl = $("#detail-template").content.cloneNode(true);
    tpl.querySelector(".severity").innerHTML = badge(item.severity, item.severity.toLowerCase());
    tpl.querySelector(".bug-id").textContent = item.id;
    tpl.querySelector("h2").textContent = item.title;
    const dev = item.development?.state || "NOT_STARTED";
    const rv = item.review?.status || "NOT_REVIEWED";
    const prod = item.production?.state || "NOT_STARTED";
    tpl.querySelector(".timeline").innerHTML =
      step("ChatGPT", "done", "已登记") +
      step("Codex", dev === "NOT_STARTED" ? "" : "current", DEV[dev] || dev) +
      step("审计", rv === "PASSED" ? "done" : rv === "FAILED" ? "blocked" : "current", REVIEW[rv] || rv) +
      step("Merge", ["MERGED_NOT_DEPLOYED", "DEPLOYED"].includes(prod) ? "done" : "", PROD[prod] || prod) +
      step("Production", prod === "DEPLOYED" ? "done" : "", productionText(item));
    tpl.querySelector(".chatgpt").innerHTML =
      line("对话", item.chatgpt?.title, item.chatgpt?.url) +
      line("关联状态", item.chatgpt?.url ? "可直接打开" : "已登记标题，历史链接待补");
    tpl.querySelector(".codex").innerHTML =
      line("Codex", item.codex?.title, item.codex?.url) +
      line("Session", item.codex?.session_id) + line("Branch", item.branch) +
      line("HEAD", item.branch_status?.head_sha) +
      line("PR", prText(item.pr), item.pr?.url);

    tpl.querySelector(".review").innerHTML =
      line("状态", REVIEW[rv] || rv) + line("备注", item.review?.note);
    tpl.querySelector(".production").innerHTML =
      line("状态", productionText(item)) +
      line("生产分支 SHA", item.production?.production_branch_sha) +
      line("运行进程 SHA", item.production?.runtime_sha) +
      line("生产验证", item.production_verification?.status === "VERIFIED" ? "通过" : "未验证");
    tpl.querySelector(".next strong").textContent = item.next_action || "—";
    $("#detail-body").replaceChildren(tpl);
    $("#detail").showModal();
  }
  function render(data) {
    payload = data;
    const s = data.summary || {};
    $("#kpi-total").textContent = s.total ?? 0;
    $("#kpi-p0").textContent = s.p0 ?? 0;
    $("#kpi-dev").textContent = s.in_development ?? 0;
    $("#kpi-review").textContent = s.pending_review ?? 0;
    $("#kpi-merge").textContent = s.merged_not_deployed ?? 0;
    $("#kpi-prod").textContent = s.production_verified ?? 0;
    $("#generated-at").textContent = data.generated_at ? "更新 " + data.generated_at : "更新时间未知";
    renderRows(data.items || []);
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {matchesFilter, matchesSearch, productionText, prText};
    return;
  }

  async function load() {
    $("#error").hidden = true;
    try {
      const res = await fetch("/dashboard/api/remediation", {cache: "no-store"});
      if (!res.ok) throw new Error("HTTP " + res.status);
      render(await res.json());
    } catch (err) {
      $("#error").hidden = false;
      $("#error").textContent = "整改追踪读取失败：" + err.message;
    }
  }
  $("#filters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    activeFilter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((x) =>
      x.classList.toggle("active", x === button));
    renderRows(payload?.items || []);
  });
  $("#search")?.addEventListener("input", () => renderRows(payload?.items || []));
  $("#refresh")?.addEventListener("click", load);
  $("#detail-close")?.addEventListener("click", () => $("#detail").close());
  $("#detail")?.addEventListener("click", (event) => {
    if (event.target === $("#detail")) $("#detail").close();
  });
  globalThis.__remediationTestHooks = {matchesFilter, matchesSearch, productionText};
  load();
})();
