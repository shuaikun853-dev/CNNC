/* 客户链接预爬 — 页面脚本(vanilla IIFE,无模块无外链;风格对齐 app.js)。
   数据全部来自预爬投影 API 字段;客户端仅持有:16 项 KPI 中文标签映射 + badge 配色启发,
   不做任何业务解释。路由接口由后续切片接线,未接线时所有读取失败显示"暂无数据/接口未就绪"。 */
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const FETCH_TIMEOUT_MS = 8000;
  const POLL_INTERVAL_MS = 2000, MAX_POLLS = 150;

  // ---- 客户端仅有的两处映射:KPI 中文标签(展示顺序即渲染顺序)+ badge 配色启发 ----
  const KPI_LABELS = [
    ["reference_link_count", "客户参考链接总数"],
    ["unique_url_count", "唯一URL数"],
    ["candidate_count", "候选数"],
    ["cache_hit_candidate_count", "缓存命中"],
    ["historical_cache_hit_count", "历史缓存命中"],
    ["daily_internet_hit_count", "每日互联网命中"],
    ["prewarm_new_success_count", "预爬新增成功"],
    ["manual_recrawl_hit_count", "手动重爬命中"],
    ["zhaoshang_running_candidate_count", "招商局运行中(候选)"],
    ["zhaoshang_running_job_count", "招商局运行中(任务)"],
    ["staples_running_count", "史泰博运行中"],
    ["failure_count", "失败"],
    ["url_parsed_unverified_count", "URL解析未验证"],
    ["unresolved_sku_count", "未解析SKU"],
    ["remote_job_count", "远端任务数"],
    ["remote_url_count", "远端在途URL数"],
  ];
  // 仅配色分类(文案一律来自 API 字段):CACHE_HIT 绿;REMOTE_*/STAPLES_PENDING 蓝;
  // ROUTING_BLOCKED 红;MANUAL_SKIP 与其余(含 PENDING_PREWARM/未知)灰/黄。
  function cacheBadgeClass(status) {
    const s = String(status || "").toUpperCase();
    if (s === "CACHE_HIT") return "badge badge-ok";
    if (s === "ROUTING_BLOCKED") return "badge badge-err";
    if (s === "MANUAL_SKIP") return "badge badge-wait";
    if (s.indexOf("REMOTE_") === 0 || s === "STAPLES_PENDING") return "badge badge-run";
    return "badge badge-wait";
  }

  const state = {
    date: "",            // 业务日期(input;默认 operator-context.business_date,否则本机今天)
    page: 1,
    pageSize: 50,
    mall: "",
    status: "",
    source: "",
    flags: {},           // missing_title/unverified_sku/unresolved_sku/failure/cache_miss → true
  };
  let detail = null;     // 当前抽屉详情(含 candidate/current/history/remote_job/staples_job)
  let actionToken = null;
  let tokenPromise = null;
  let recrawlBusy = false;

  // ---- 基础工具 ----
  function todayLocal() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  }
  // 后端存 UTC ISO,展示固定上海时区(与 app.js 同规则,不做 +8 算术)。
  function fmtShanghaiDateTime(iso) {
    if (iso === null || iso === undefined || iso === "") return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai", hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(d);
    const get = (type) => {
      const p = parts.find((x) => x.type === type);
      return p ? p.value : "";
    };
    const hour = get("hour") === "24" ? "00" : get("hour");
    return get("year") + "-" + get("month") + "-" + get("day") + " " +
      hour + ":" + get("minute") + ":" + get("second");
  }
  async function fetchJson(url, opts) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), FETCH_TIMEOUT_MS);
    try {
      const res = await fetch(url, Object.assign({}, opts || {}, { signal: abort.signal }));
      let json = null;
      try { json = await res.json(); } catch (e) { json = null; }
      return { status: res.status, json };
    } finally {
      clearTimeout(timer);
    }
  }
  function kvRow(label, valueHtml) {
    return "<b>" + esc(label) + "</b><span>" + valueHtml + "</span>";
  }
  function kvText(v) { return esc(v === null || v === undefined || v === "" ? "—" : v); }

  // ---- operator-context 引导(仅 POST 需要;GET 流程不依赖) ----
  function ensureToken() {
    if (actionToken !== null) return Promise.resolve(actionToken);
    if (!tokenPromise) {
      tokenPromise = (async () => {
        try {
          const r = await fetchJson("/dashboard/api/operator-context");
          if (!r.status || r.status < 200 || r.status >= 300) throw new Error("HTTP " + r.status);
          actionToken = (r.json && r.json.action_token) || "";
          if (!state.date && r.json && r.json.business_date) {
            state.date = r.json.business_date;
            syncDateInput();
          }
        } catch (e) {
          actionToken = "";   // 令牌不可用:POST 时再行提示
        }
        return actionToken;
      })();
    }
    return tokenPromise;
  }

  // ---- 降级横幅 ----
  function showDegraded(text) {
    const el = $("#pw-degraded");
    if (!el) return;
    el.textContent = text;
    el.classList.add("show");
    el.hidden = false;
  }
  function hideDegraded() {
    const el = $("#pw-degraded");
    if (!el) return;
    el.classList.remove("show");
    el.hidden = true;
  }

  // ---- 汇总(KPI + 状态分布) ----
  async function loadSummary() {
    const kpiWrap = $("#pw-kpis");
    try {
      const r = await fetchJson("/dashboard/api/prewarm/summary?date=" + encodeURIComponent(state.date));
      if (!r.status || r.status < 200 || r.status >= 300) throw new Error("HTTP " + r.status);
      const s = r.json || {};
      $("#pw-bizdate-chip").textContent = "业务日期:" + (s.business_date || state.date);
      const kpi = s.kpi || {};
      kpiWrap.innerHTML = KPI_LABELS.map(([key, label]) =>
        '<div class="kpi-card"><div class="kpi-num">' + esc(kpi[key] ?? "—") +
        '</div><div class="kpi-label">' + esc(label) + "</div></div>").join("");
      // 状态分布:后端列表形状 [{status,label,count}](label 由后端单源提供)
      const dist = Array.isArray(kpi.status_distribution) ? kpi.status_distribution : [];
      const distEl = $("#pw-status-dist");
      distEl.innerHTML = (dist.length
        ? '<span class="pw-dist-label">状态分布:</span>' + dist.map((entry) =>
            '<span class="' + cacheBadgeClass(entry.status) + '">' +
            esc(entry.label || entry.status) + " × " + esc(entry.count) + "</span>").join("")
        : "");
      if (s.degraded) {
        const reasons = Array.isArray(s.degraded_reasons) ? s.degraded_reasons.join(";") : "";
        showDegraded("⚠ 预爬数据降级" + (reasons ? ":" + reasons : ""));
      } else {
        hideDegraded();
      }
    } catch (e) {
      // 接口未就绪(后续切片接线):展示空态而非报错
      if (kpiWrap) {
        kpiWrap.innerHTML = '<div class="kpi-card"><div class="kpi-num">—</div>' +
          '<div class="kpi-label">暂无数据</div></div>';
      }
      const distEl = $("#pw-status-dist");
      if (distEl) distEl.innerHTML = "";
      showDegraded("⚠ 预爬汇总暂不可用(接口未就绪),以下显示为暂无数据");
    }
  }

  // ---- 链接列表 ----
  function buildQuery() {
    const q = new URLSearchParams();
    q.set("date", state.date);
    q.set("page", String(state.page));
    q.set("page_size", String(state.pageSize));
    if (state.mall) q.set("mall", state.mall);
    if (state.status) q.set("status", state.status);
    if (state.source) q.set("source", state.source);
    Object.keys(state.flags).forEach((k) => { if (state.flags[k]) q.set(k, "1"); });
    return q.toString();
  }
  function docsCell(link) {
    const ids = Array.isArray(link.document_ids) ? link.document_ids.map(String) : [];
    const full = ids.join(", ");
    let short;
    if (ids.length === 0) short = "—";
    else if (ids.length <= 2) short = ids.join(" ");
    else short = ids.slice(0, 2).join(" ") + " +" + (ids.length - 2) + " more";
    const title = ids.length ? "文档×" + ids.length + " · " + full : "";
    return '<span class="pw-doc-label">' + (ids.length ? "文档×" + ids.length : "") + "</span>" +
      '<span class="mono" title="' + esc(title) + '">' + esc(short) + "</span>";
  }
  function skuCell(link) {
    const label = link.sku_status_label || link.sku_status || "";
    const variant = link.sku_requires_review ? "badge-warn" : "badge-wait";
    const prefix = link.sku_requires_review ? "⚠ " : "";
    const skuId = link.real_sku_id === null || link.real_sku_id === undefined || link.real_sku_id === ""
      ? "" : esc(link.real_sku_id);
    return skuId + (label ? '<span class="badge pw-sku-badge ' + variant + '">' +
      esc(prefix + label) + "</span>" : "");
  }
  function renderLinks(payload) {
    const tbody = $("#pw-links-body");
    const empty = $("#pw-links-empty");
    const links = (payload && Array.isArray(payload.links)) ? payload.links : [];
    tbody.innerHTML = links.map((link) => {
      const statusLabel = link.cache_status_label || link.cache_status || "—";
      return '<tr class="row" data-candidate-id="' + esc(link.candidate_id) + '">' +
        "<td>" + docsCell(link) + "</td>" +
        "<td>" + kvText(link.mall) + "</td>" +
        '<td class="pw-url mono" title="' + esc(link.original_url || "") + '">' + esc(link.original_url || "—") + "</td>" +
        '<td><span class="' + cacheBadgeClass(link.cache_status) + '">' + esc(statusLabel) + "</span></td>" +
        "<td>" + kvText(link.match_level) + "</td>" +
        "<td>" + kvText(link.source_class) + "</td>" +
        '<td class="pw-title">' + kvText(link.title) + "</td>" +
        '<td class="num">' + (link.price === null || link.price === undefined || link.price === "" ? "—" : esc(link.price)) + "</td>" +
        '<td class="mono">' + kvText(link.crawl_id) + "</td>" +
        '<td class="pw-sku">' + skuCell(link) + "</td>" +
        "<td>" + esc(fmtShanghaiDateTime(link.last_success_at)) + "</td>" +
        "</tr>";
    }).join("");
    empty.hidden = links.length > 0;
    empty.textContent = "暂无数据";
    renderPagination(payload && payload.pagination);
  }
  function renderPagination(p) {
    const el = $("#pw-pagination");
    const pg = p || { page: state.page, page_size: state.pageSize, total: 0, total_pages: 1 };
    const totalPages = Math.max(1, Number(pg.total_pages) || 1);
    const page = Math.min(Math.max(1, Number(pg.page) || 1), totalPages);
    el.innerHTML =
      '<button class="btn" data-page="prev"' + (page <= 1 ? " disabled" : "") + ">上一页</button>" +
      "<span>第 " + page + " / " + totalPages + " 页 · 共 " + esc(pg.total ?? 0) + " 条</span>" +
      '<button class="btn" data-page="next"' + (page >= totalPages ? " disabled" : "") + ">下一页</button>";
  }
  async function loadLinks() {
    const empty = $("#pw-links-empty");
    try {
      const r = await fetchJson("/dashboard/api/prewarm/links?" + buildQuery());
      if (!r.status || r.status < 200 || r.status >= 300) throw new Error("HTTP " + r.status);
      renderLinks(r.json || {});
    } catch (e) {
      // 接口未就绪:空表 + 空态文案,不抛控制台错误
      if (empty) { empty.hidden = false; empty.textContent = "暂无数据 · 接口未就绪"; }
      $("#pw-links-body").innerHTML = "";
      renderPagination(null);
    }
  }

  // ---- 详情抽屉 ----
  function detailCandidate(d) { return (d && d.candidate) || d || {}; }
  function detailDocuments(d) {
    if (Array.isArray(d && d.document_ids) && d.document_ids.length) return d.document_ids.map(String);
    const links = d && d.links;
    if (Array.isArray(links)) {
      return links.map((x) => (typeof x === "string" ? x : x && x.document_id)).filter(Boolean).map(String);
    }
    return [];
  }
  function fillDrawer(d) {
    const cand = detailCandidate(d);
    const cur = (d && d.current) || {};
    const docs = detailDocuments(d);
    const statusBadge = '<span class="' + cacheBadgeClass(cand.cache_status) + '">' +
      esc(cand.cache_status_label || cand.cache_status || "—") + "</span>";
    $("#pw-d-basic").innerHTML =
      kvRow("documentIds", docs.length ? '<span class="mono pw-url-evidence">' + esc(docs.join(", ")) + "</span>" : "—") +
      kvRow("商城", kvText(cand.mall)) +
      kvRow("状态", statusBadge + (cand.cache_status ? ' <span class="mono">' + esc(cand.cache_status) + "</span>" : "")) +
      kvRow("匹配级别", kvText(cand.match_level)) +
      kvRow("identity_id", kvText(cur.identity_id));
    const routing = cand.routing_url || d.routing_url;
    $("#pw-d-urls").innerHTML =
      kvRow("原始URL", '<span class="mono pw-url-evidence">' + esc(cand.original_url || "—") + "</span>") +
      kvRow("routing", '<span class="mono pw-url-evidence">' + esc(routing || "—") + "</span>") +
      kvRow("submitted", '<span class="mono pw-url-evidence">' + esc(cur.submitted_url || "—") + "</span>") +
      kvRow("returned", '<span class="mono pw-url-evidence">' + esc(cur.returned_url || "—") + "</span>") +
      kvRow("canonical", '<span class="mono pw-url-evidence">' + esc(cur.canonical_url || "—") + "</span>");
    const skuLabel = cur.sku_status_label || cur.sku_status || "";
    const skuRequiresReview = cur.sku_requires_review != null ? cur.sku_requires_review : cand.sku_requires_review;
    const skuCls = skuRequiresReview ? "badge badge-warn" : "badge badge-wait";
    const skuPrefix = skuRequiresReview ? "⚠ " : "";
    $("#pw-d-current").innerHTML =
      kvRow("标题", kvText(cur.title)) +
      kvRow("价格", kvText(cur.price)) +
      kvRow("crawl_id", kvText(cur.crawl_id)) +
      kvRow("real_sku_id", kvText(cur.real_sku_id)) +
      kvRow("SKU状态", skuLabel ? '<span class="' + skuCls + '">' + esc(skuPrefix + skuLabel) + "</span>" : "—") +
      kvRow("评论数", kvText(cur.comment_count)) +
      kvRow("来源", kvText(cur.source_class || cur.source)) +
      kvRow("成功时间", esc(fmtShanghaiDateTime(cur.succeeded_at)));
    const rj = (d && d.remote_job) || null;
    $("#pw-d-remote").innerHTML = rj
      ? kvRow("状态", kvText(rj.state)) + kvRow("任务名", kvText(rj.task_name)) +
        kvRow("远端标识", kvText(rj.remote_identity)) + kvRow("更新时间", esc(fmtShanghaiDateTime(rj.updated_at)))
      : kvRow("—", "—");
    const sj = (d && d.staples_job) || null;
    $("#pw-d-staples").innerHTML = sj
      ? kvRow("状态", kvText(sj.state)) + kvRow("尝试次数", kvText(sj.attempt_count)) +
        kvRow("更新时间", esc(fmtShanghaiDateTime(sj.updated_at)))
      : kvRow("—", "—");
    const hist = Array.isArray(d && d.history) ? d.history : [];
    $("#pw-history").innerHTML = hist.map((h) => {
      const bits = [h.source_class, h.title, h.price, h.real_sku_id, h.sku_status_label || h.sku_status]
        .filter((x) => x !== null && x !== undefined && x !== "");
      return '<li' + (h.is_current ? ' class="current"' : "") + ">" +
        "<time>" + esc(fmtShanghaiDateTime(h.succeeded_at)) + "</time>" +
        (h.is_current ? '<span class="badge badge-ok">当前</span> ' : "") +
        esc(bits.join(" · ")) + "</li>";
    }).join("") || '<li class="current">暂无历史</li>';
    // 人工操作:当前结果时间 + 确认面板 + 路由提示(优先服务端 detail.route,
    // 仅在路由缺失/为 null 时回退 stbchina.cn 主机启发;服务端始终为权威路由)
    $("#pw-recrawl-current").textContent =
      "当前缓存结果成功时间:" + fmtShanghaiDateTime(cur.succeeded_at);
    $("#pw-recrawl-facts").innerHTML =
      "商城:" + esc(cand.mall || "—") + " · URL:" + esc(cand.original_url || "—") +
      " · 当前结果时间:" + esc(fmtShanghaiDateTime(cur.succeeded_at));
    const routeHint = (d && d.route) === "staples-local" ? "本地重新爬取(史泰博)"
      : (d && d.route) === "zhaoshang" ? "招商局重新爬取 · priority -1"
      : (staplesRouteHint(cand.original_url) ? "本地重新爬取(史泰博)" : "招商局重新爬取 · priority -1");
    $("#recrawl-route-hint").textContent = routeHint;
    $("#pw-recrawl-confirm").hidden = false;
    setRecrawlStatus("", "");
  }
  function staplesRouteHint(url) {
    try { return String(url || "").indexOf("stbchina.cn") !== -1; } catch (e) { return false; }
  }
  async function openDrawer(candidateId) {
    const drawer = $("#pw-drawer");
    const backdrop = $("#pw-backdrop");
    try {
      const r = await fetchJson("/dashboard/api/prewarm/links/" + encodeURIComponent(candidateId) +
        "?date=" + encodeURIComponent(state.date));
      if (!r.status || r.status < 200 || r.status >= 300) {
        detail = null;
        setRecrawlStatus("详情读取失败(HTTP " + (r.status || 0) + ")", "err");
        return;
      }
      detail = r.json;
      fillDrawer(detail);
    } catch (e) {
      detail = null;
      setRecrawlStatus("详情读取失败(接口未就绪)", "err");
      return;
    }
    drawer.classList.add("show");
    drawer.setAttribute("aria-hidden", "false");
    backdrop.hidden = false;
  }
  function closeDrawer() {
    const drawer = $("#pw-drawer");
    drawer.classList.remove("show");
    drawer.setAttribute("aria-hidden", "true");
    $("#pw-backdrop").hidden = true;
    document.querySelectorAll("#pw-links-body tr.sel").forEach((tr) => tr.classList.remove("sel"));
    detail = null;
  }

  // ---- 人工重爬 ----
  function setRecrawlStatus(text, tone) {
    const el = $("#pw-recrawl-status");
    el.textContent = text || "";
    el.className = "action-status" + (tone ? " " + tone : "");
  }
  function setRecrawlBusy(busy) {
    recrawlBusy = busy;
    $("#pw-recrawl-btn").disabled = busy;
  }
  function outcomeSummary(outcome) {
    if (outcome === null || outcome === undefined) return "";
    try { return JSON.stringify(outcome); } catch (e) { return String(outcome); }
  }
  // 手动重爬动作状态 → 中文(诚实生命周期:只有 SUCCEEDED 携带缓存证明,
  // WAITING_REMOTE 仅招商局路径;史泰博同步爬取直接 RUNNING→SUCCEEDED/FAILED)。
  function recrawlStatusText(status) {
    switch (String(status || "")) {
      case "RUNNING": return "进行中…";
      case "WAITING_REMOTE": return "已派发，等待招商局完成";
      case "PRIORITY_PENDING": return "优先级待确认";
      case "SUCCEEDED": return "重爬完成 ✓";
      case "FAILED": return "重爬失败 ✗";
      default: return "重爬结束(" + status + ")";
    }
  }
  async function startRecrawl() {
    if (recrawlBusy || !detail) return;
    const cand = detailCandidate(detail);
    setRecrawlBusy(true);
    try {
      const token = await ensureToken();
      const r = await fetchJson("/dashboard/api/prewarm/links/" + encodeURIComponent(cand.candidate_id) + "/recrawl", {
        method: "POST",
        headers: { "X-Dashboard-Action-Token": token, "Content-Type": "application/json" },
        body: JSON.stringify({
          business_date: state.date,
          expected_original_url: cand.original_url || null,
          expected_status: cand.cache_status || null,
        }),
      });
      if (r.status === 403) { setRecrawlStatus("操作令牌失效,请刷新页面", "err"); return; }
      if (r.status === 409) {
        const code = r.json && r.json.detail && r.json.detail.error_code;
        setRecrawlStatus("无法发起重爬" + (code ? "(" + code + ")" : ""), "err");
        return;
      }
      if (r.status === 404) { setRecrawlStatus("候选不存在或接口未就绪", "err"); return; }
      if (!r.status || r.status < 200 || r.status >= 300) {
        setRecrawlStatus("发起重爬失败(HTTP " + (r.status || 0) + ")", "err");
        return;
      }
      const started = r.json || {};
      const pollUrl = started.poll || ("/dashboard/api/prewarm/recrawl-actions/" + encodeURIComponent(started.action_id));
      setRecrawlStatus("已受理(" + recrawlStatusText(started.status) + "),正在轮询…", "");
      for (let i = 0; i < MAX_POLLS; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        let pr;
        try { pr = await fetchJson(pollUrl, { headers: { "X-Dashboard-Action-Token": await ensureToken() } }); }
        catch (e) { pr = { status: 0, json: null }; }
        if (!pr.status || pr.status < 200 || pr.status >= 300) continue;   // 单次轮询失败不终止
        const data = pr.json || {};
        const st = String(data.status || "");
        // 每次轮询驱动一次懒完成评估(服务端 get 只读重估 WAITING_REMOTE)
        if (st === "SUCCEEDED") { setRecrawlStatus(recrawlStatusText(st) + " " + outcomeSummary(data.outcome), "ok"); return; }
        if (st === "FAILED") { setRecrawlStatus(recrawlStatusText(st) + " " + outcomeSummary(data.outcome), "err"); return; }
        if (st === "PRIORITY_PENDING") { setRecrawlStatus(recrawlStatusText(st) + " " + outcomeSummary(data.outcome), ""); return; }
        if (st === "WAITING_REMOTE") {
          setRecrawlStatus(recrawlStatusText(st) + "(已等待 " + ((i + 1) * POLL_INTERVAL_MS / 1000) + " 秒)", "");
          continue;
        }
        setRecrawlStatus(recrawlStatusText(st || "RUNNING") + "(已等待 " + ((i + 1) * POLL_INTERVAL_MS / 1000) + " 秒)", "");
      }
      setRecrawlStatus("轮询超时,请稍后刷新查看结果", "err");
    } finally {
      setRecrawlBusy(false);
    }
  }

  // ---- 事件接线 ----
  function syncDateInput() {
    const input = $("#pw-date");
    if (input && state.date) input.value = state.date;
    const chip = $("#pw-bizdate-chip");
    if (chip && state.date && chip.textContent.indexOf("—") !== -1) {
      chip.textContent = "业务日期:" + state.date;
    }
  }
  function applyFilters() {
    state.mall = $("#pw-f-mall").value.trim();
    state.status = $("#pw-f-status").value;
    state.source = $("#pw-f-source").value;
    state.page = 1;
    loadLinks();
  }
  function wire() {
    $("#pw-refresh").addEventListener("click", () => { loadSummary(); loadLinks(); });
    $("#pw-date").addEventListener("change", (ev) => {
      state.date = ev.target.value || todayLocal();
      $("#pw-bizdate-chip").textContent = "业务日期:" + state.date;
      state.page = 1;
      loadSummary();
      loadLinks();
    });
    $("#pw-apply").addEventListener("click", applyFilters);
    $("#pw-f-mall").addEventListener("keydown", (ev) => { if (ev.key === "Enter") applyFilters(); });
    document.querySelectorAll(".chip-toggle[data-flag]").forEach((chip) => {
      chip.addEventListener("click", () => {
        const flag = chip.getAttribute("data-flag");
        state.flags[flag] = !state.flags[flag];
        chip.classList.toggle("active", !!state.flags[flag]);
      });
    });
    $("#pw-pagination").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-page]");
      if (!btn || btn.disabled) return;
      if (btn.getAttribute("data-page") === "prev") state.page = Math.max(1, state.page - 1);
      else state.page += 1;
      loadLinks();
    });
    $("#pw-links-body").addEventListener("click", (ev) => {
      const tr = ev.target.closest("tr.row");
      if (!tr) return;
      document.querySelectorAll("#pw-links-body tr.sel").forEach((x) => x.classList.remove("sel"));
      tr.classList.add("sel");
      openDrawer(tr.getAttribute("data-candidate-id"));
    });
    $("#pw-drawer-close").addEventListener("click", closeDrawer);
    $("#pw-backdrop").addEventListener("click", closeDrawer);
    $("#pw-recrawl-btn").addEventListener("click", startRecrawl);
    $("#pw-recrawl-cancel").addEventListener("click", () => { $("#pw-recrawl-confirm").hidden = true; });
  }

  // ---- 启动 ----
  async function boot() {
    state.date = todayLocal();
    syncDateInput();
    wire();
    await ensureToken();          // 拿业务日期与令牌(失败不影响 GET 流程)
    syncDateInput();
    loadSummary();
    loadLinks();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
