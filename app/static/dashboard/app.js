(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const ACTIVE_MS = 5000, IDLE_MS = 30000, RETRY_MS = 10000;

  // ---- 轮询控制器(纯逻辑 seam,无 DOM;tests/js/run_dashboard_js_tests.js 直接驱动) ----
  // 语义(Review Fix Round 1 P2-1/P2-2):
  //   200 → 记一次成功同步 + 渲染内容 + 按状态排程;
  //   304 → 记一次成功同步,不重渲染,继续排程(数据没变不等于连接断了);
  //   失败 → 不记成功同步,进入 offline,仅在页面可见时排重试;
  //   页面隐藏 → 清除待触发 timer 且不再注册下一次;回到前台 → 立即补刷;
  //   在途 poll 期间重复触发(人工刷新连点)直接忽略,保证只有一条轮询链。
  function createPollController(deps) {
    const st = { timer: null, inFlight: false, lastPayload: null };
    function clearTimer() {
      if (st.timer !== null) { deps.clearTimeout(st.timer); st.timer = null; }
    }
    function schedule(payload) {
      clearTimer();
      if (!deps.visible()) return false;   // 不可见:不注册下一次 timer
      st.timer = deps.setTimeout(() => { st.timer = null; run(); }, deps.delay(payload));
      return true;
    }
    async function run() {
      if (st.inFlight) return;
      st.inFlight = true;
      try {
        const res = await deps.poll();
        if (res.status === 304) { deps.onSync(); schedule(st.lastPayload); return; }
        if (!res.status || res.status < 200 || res.status >= 300) throw new Error("HTTP " + res.status);
        st.lastPayload = res.payload;
        deps.onSync();
        deps.onContent(res.payload);
        schedule(res.payload);
      } catch (e) {
        deps.onOffline();
        clearTimer();
        if (deps.visible()) {
          st.timer = deps.setTimeout(() => { st.timer = null; run(); }, deps.retryDelay);
        }
      } finally {
        st.inFlight = false;
      }
    }
    return {
      run, schedule,
      onHidden: clearTimer,
      onVisible: () => { if (!st.inFlight) run(); },
      stop: clearTimer,
    };
  }

  let etag = null, lastSyncAt = null;

  function fmtAgoSeconds(s) {
    return s < 60 ? `${Math.round(s)} 秒前` : `${Math.round(s / 60)} 分钟前`;
  }
  function fmtAgoMs(ms) { return fmtAgoSeconds(Math.max(0, (Date.now() - ms) / 1000)); }
  function fmtAgo(iso) { return fmtAgoMs(new Date(iso).getTime()); }
  function fmtAgoAt(iso, nowMs) { return fmtAgoSeconds(Math.max(0, (nowMs - new Date(iso).getTime()) / 1000)); }
  // Bug1:后端全链路存 UTC ISO,展示必须固定为上海时区;不依赖系统/浏览器时区,
  // 也绝不做 +8 算术(时间戳可带任意 offset)。输出 "YYYY-MM-DD HH:mm:ss"。
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
    const hour = get("hour") === "24" ? "00" : get("hour");   // 个别 ICU 小时档输出 24
    return get("year") + "-" + get("month") + "-" + get("day") + " " +
      hour + ":" + get("minute") + ":" + get("second");
  }
  function setLive(ok, text) {
    $("#live").textContent = text;
    $("#live-dot").className = "dot" + (ok ? "" : " err");
  }
  // P2-1:首页"实时/最后同步"基于最后一次成功 HTTP 200 或 304,而不是 payload
  // generated_at——连续 304 的稳态必须显示刚刚同步,否则用户误以为页面卡死。
  function updateLive() {
    const offline = document.body.classList.contains("offline");
    if (offline) { setLive(false, "⚠ 连接中断,正在重试"); return; }
    if (lastSyncAt) setLive(true, "实时 · 最后同步 " + fmtAgoMs(lastSyncAt));
    else setLive(true, "实时 · 连接中…");
  }

  async function fetchSummary() {
    const headers = {};
    if (etag) headers["If-None-Match"] = etag;
    const res = await fetch("/dashboard/api/summary", { headers });
    if (res.status === 304) return { status: 304 };
    if (!res.ok) return { status: res.status };
    etag = res.headers.get("ETag");
    return { status: res.status, payload: await res.json() };
  }
  function pollDelay(payload, nowMs) {
    const st = (payload && payload.status) || {};
    const finalized = st.final && st.final.finalized;
    const active = (st.production_writer && st.production_writer.active) ||
      (st.final && st.final.computed_pending > 0 && !finalized);
    if (active) return ACTIVE_MS;
    if (!finalized && inCloseoutWatchWindow(nowMs)) return ACTIVE_MS;   // 自动收口窗口监视
    return IDLE_MS;
  }
  // R3 P2-2:上海时区 closeout 监视窗口 [17:55, 20:35](分钟闭区间,含两端):
  // Scheduler 18:00 自动 closeout 会让 writer 持锁,idle 30s 轮询最坏 30s 后才反映;
  // 窗口内即使当前 idle 也用 5s,让按钮/阶段卡在自动收口启动后 ≤5s 内跟上。显式
  // Asia/Shanghai 换算,不依赖浏览器/系统本地时区。finalized 后收口已出版,无需
  // 监视,恢复 idle。
  const CLOSEOUT_WATCH_START_MIN = 17 * 60 + 55;
  const CLOSEOUT_WATCH_END_MIN = 20 * 60 + 35;
  function shanghaiMinutesOfDay(nowMs) {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai", hour12: false,
      hour: "2-digit", minute: "2-digit",
    }).formatToParts(new Date(nowMs));
    const get = (type) => Number((parts.find((p) => p.type === type) || {}).value || 0);
    const hour = get("hour") === 24 ? 0 : get("hour");   // 个别 ICU 小时档输出 24
    return hour * 60 + get("minute");
  }
  function inCloseoutWatchWindow(nowMs) {
    const m = shanghaiMinutesOfDay(nowMs === undefined ? Date.now() : nowMs);
    return m >= CLOSEOUT_WATCH_START_MIN && m <= CLOSEOUT_WATCH_END_MIN;
  }
  const ctl = createPollController({
    poll: fetchSummary,
    delay: pollDelay,
    retryDelay: RETRY_MS,
    visible: () => document.visibilityState === "visible",
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    onSync: () => { lastSyncAt = Date.now(); document.body.classList.remove("offline"); updateLive(); },
    onContent: (p) => render(p),
    onOffline: () => { document.body.classList.add("offline"); updateLive(); },
  });

  function render(p) {
    // 首个成功 summary 即完成 summary 侧 hydration;writer 权威先于 overlay 计算,
    // closeoutRunning 覆盖必须施加在每次 summary 重渲上(5s 重刷不打回"○ 等待")。
    closeoutSummarySeen = true;
    closeoutWriterRunning = isCloseoutWriter(p.status && p.status.production_writer);
    renderHero(p.overall); renderAttention(p.overall.attention);
    renderStages(applyCloseoutRunningOverlay(p.overall.stages, { closeoutRunning: closeoutUIRunning() }));
    renderZs(p.overall.zs_summary);
    renderCustomerPrice(p.customer_price);
    renderFinal(p.overall, p.status); renderMeta(p);
    renderJobs(p.jobs);
    window._timelines = p.timelines;
    renderTech(p.status);
    updateCloseoutUI();
    // Review P1-4:SaaS Flow 有独立轮询生命周期,不再依赖 summary 的 200 render;
    // summary 连续 304 时 SaaS Flow 仍按自身 ~60s 周期刷新。
  }
  function renderHero(o) {
    const h = $("#hero");
    h.className = "hero " + o.color;
    $("#hero .word").textContent = (o.color === "red" ? "⚠ " : o.color === "blue" ? "● " :
      o.color === "green" ? "✓ " : "") + o.word;
    $("#hero .explain").textContent = o.explanation;
  }
  function renderAttention(items) {
    const box = $("#attn");
    const empty = box.querySelector(".empty"), ol = box.querySelector("ol");
    if (!items || !items.length) {
      box.classList.remove("has"); ol.innerHTML = ""; empty.style.display = ""; return;
    }
    box.classList.add("has"); empty.style.display = "none";
    ol.innerHTML = items.slice(0, 3).map((a) => `
      <li class="${a.level}">
        <div class="t"><span class="tag ${a.level}">${esc(a.tag)}</span>${esc(a.title)}</div>
        <div class="row"><b>问题</b> ${esc(a.problem)}</div>
        <div class="row"><b>影响</b> ${esc(a.impact)}</div>
        <div class="row"><b>系统动作</b> ${esc(a.system_action)}</div>
        <div class="row"><b>建议</b> ${esc(a.suggestion)}</div>
      </li>`).join("") +
      (items.length > 3 ? `<li class="t" style="color:var(--muted)">其余 ${items.length - 3} 项,详见技术详情</li>` : "");
  }
  function renderStages(stages) {
    $("#stages").innerHTML = (stages || []).map((s) => `
      <div class="stage ${s.state}" data-key="${s.key}">
        <div class="k">${s.name}</div>
        <div class="st">${s.state_word}</div><div class="num">${s.num}</div>
        <div class="sub">${(s.sub || []).map(([k, v]) =>
          `<div><span>${k}</span><span>${esc(outputDisplayName(v))}</span></div>`).join("")}</div>
      </div>`).join("");
    document.querySelectorAll("#stages .stage").forEach((el) =>
      el.addEventListener("click", () => el.classList.toggle("open")));
  }
  function renderZs(z) {
    const counts = $("#zs .counts");
    if (!z || !Object.keys(z).length) {
      counts.innerHTML = "<span class='lab'>暂无招商局任务</span>";
      return;
    }
    const map = [["executing", "执行中", "run"], ["awaiting_harvest", "等待回收", ""],
      ["awaiting_priority", "等待提优", ""], ["recovering", "自动恢复中", "run"],
      ["recovery_exhausted", "自动恢复已用尽", "warn"],
      ["manual", "需要人工", "err"], ["completed", "已完成", "ok"]];
    counts.innerHTML = map.map(([k, lab, cls]) =>
      `<span><span class="lab">${lab}</span><b class="${cls}">${z[k] ?? 0}</b></span>`).join("");
  }

  // ---- 客户价可观测性(2026-09-03,只读):汇总随 summary 刷新(200/304 机制
  // 驱动,无独立轮询链);明细仅在用户点击"查看明细"/过滤按钮时拉取。 ----
  // 纯逻辑 seam(tests/js 直测,无 DOM):状态中文映射与行过滤/排序。
  const CP_STATUS_CN = {
    READY: "✓ 已获取", PENDING: "● 获取中", RETRYING: "↻ 待重试",
    MISSING: "⚠ 客户价缺失", MISSING_PROVEN: "⚠ 客户价缺失",
    FAILED: "✕ 获取失败", INVALID: "✕ 获取失败",
  };
  function cpStatusCn(key) {
    return CP_STATUS_CN[key] || (key ? String(key) : "—");
  }
  function cpStatusPill(key) {
    if (key === "READY") return "ok";
    if (key === "RETRYING" || key === "PENDING") return "run";
    // R2-B:确定性缺席(MISSING_PROVEN)与缺失/失败/无效同为阻塞 → err。
    if (key === "MISSING_PROVEN" || key === "MISSING" || key === "FAILED" || key === "INVALID") return "err";
    return "wait";
  }
  // 行是否命中过滤(与后端 project_customer_price 同语义;仅用于前端已拉取数据的本地再过滤兜底)。
  function cpRowMatchesFilter(row, filter) {
    const st = row && row.state;
    if (filter === "retrying") return st === "RETRYING";
    if (filter === "terminal") return st === "MISSING" || st === "FAILED" || st === "INVALID";
    // R2-B:确定性缺席(MISSING_PROVEN)同属未获取——unacquired 只排除 READY。
    if (filter === "unacquired") return st !== "READY";
    return true;   // all(含未知 filter,防御为 all)
  }
  // 未完成优先排序:获取中→待重试→缺失→失败→无效,证据缺席次之,已获取最后。
  const CP_ORDER = { PENDING: 0, RETRYING: 1, MISSING: 2, FAILED: 3, INVALID: 4, MISSING_PROVEN: 5, READY: 6 };
  function cpSortRows(rows) {
    return (Array.isArray(rows) ? rows : []).slice()
      .sort((a, b) => (CP_ORDER[a && a.state] ?? CP_ORDER.PENDING) - (CP_ORDER[b && b.state] ?? CP_ORDER.PENDING));
  }
  function renderCustomerPrice(cp) {
    const box = $("#cp-counts");
    if (!box) return;
    if (!cp) { box.textContent = "客户价汇总暂不可用"; return; }
    const pct = cp.required > 0
      ? Math.round((Number(cp.ready || 0) / Number(cp.required)) * 100) + "%"
      : "—";
    const chips = [["已获取", cp.ready, "ok"], ["获取中", cp.pending, "run"],
      ["待重试", cp.retrying, "warn"], ["缺失", cp.missing, "err"],
      ["失败", cp.failed, "err"], ["无效", cp.invalid, "err"]];
    let html = chips.map(([lab, v, cls]) =>
      `<span><span class="lab">${lab}</span><b class="${cls}">${Number(v || 0)}</b></span>`).join("");
    if (Number(cp.absent_proven || 0) > 0) {
      // R2-B:确定性缺失同为阻塞 → err 配色,与“缺失/失败/无效”一致。
      html += `<span><span class="lab">确定性缺失</span><b class="err">${Number(cp.absent_proven)}</b></span>`;
    }
    html += `<span><span class="lab">获取率</span><b>${esc(pct)}</b></span>`;
    box.innerHTML = html;
  }

  let cpFilter = "unacquired";   // 明细默认"未获取"(先看拿不到的)
  function renderCpRows(data) {
    const tbody = $("#cp-table tbody");
    const meta = $("#cp-meta");
    if (!tbody) return;
    const rows = cpSortRows((data && data.rows) || []);
    tbody.innerHTML = rows.map((r) => `
      <tr>
        <td class="mono">${esc(r.document_id || "—")}</td>
        <td class="cp-title" title="${esc(r.title || "")}">${esc(r.title || "—")}</td>
        <td class="mono">${esc(r.sku_id || "—")}</td>
        <td class="num">${r.saas_price == null ? "—" : esc(r.saas_price)}</td>
        <td class="num">${r.customer_price == null ? "—" : esc(r.customer_price)}</td>
        <td><span class="pill ${cpStatusPill(r.state)}">${esc(cpStatusCn(r.state))}</span></td>
        <td class="num">${Number(r.attempts || 0)}/${Number(r.max_attempts || 3)}</td>
        <td class="num">${r.updated_at ? esc(fmtShanghaiDateTime(r.updated_at)) : "—"}</td>
        <td class="cp-err" title="${esc(r.error_text || "")}">${r.error_text ? esc(String(r.error_text).slice(0, 40)) : "—"}</td>
      </tr>`).join("") ||
      `<tr><td colspan="9" style="color:var(--muted)">当前过滤条件下没有明细行</td></tr>`;
    if (meta) {
      const truncated = Number((data && data.truncated) || 0);
      meta.textContent = `共 ${Number((data && data.total_required) || 0)} 条需求价行,本次显示 ${rows.length}` +
        (truncated > 0 ? `(已截断 ${truncated} 条,收窄过滤条件可见更多)` : "");
    }
  }
  function markCpFilterButtons() {
    document.querySelectorAll("#cp-filters .cp-filter").forEach((b) =>
      b.classList.toggle("active", b.dataset.filter === cpFilter));
  }
  async function fetchCpRows() {
    const tbody = $("#cp-table tbody");
    if (tbody) tbody.innerHTML = `<tr><td colspan="9" style="color:var(--muted)">加载中…</td></tr>`;
    try {
      const res = await fetch(`/dashboard/api/customer-price?filter=${encodeURIComponent(cpFilter)}`);
      if (!res.ok) throw new Error("HTTP " + res.status);
      renderCpRows(await res.json());
    } catch (e) {
      if (tbody) tbody.innerHTML = `<tr><td colspan="9" style="color:var(--warn)">客户价明细读取失败,请稍后重试</td></tr>`;
    }
  }
  function openCpDrawer() {
    $("#cp-drawer").classList.add("show");
    markCpFilterButtons();
    // 明细只在用户打开/切换过滤时拉取(简单、可预期);汇总行仍随 summary 刷新。
    fetchCpRows();
  }
  function wireCustomerPrice() {
    const open = $("#cp-open");
    if (open) open.addEventListener("click", openCpDrawer);
    document.querySelectorAll("#cp-filters .cp-filter").forEach((b) =>
      b.addEventListener("click", () => {
        cpFilter = b.dataset.filter || "all";
        markCpFilterButtons();
        fetchCpRows();
      }));
  }

  function renderFinal(o, status) {
    const f = $("#final");
    f.className = "final" + (o.final_state === "ok" ? " ok" : "");
    f.querySelector(".why").textContent = o.final_text;
    // 已 finalized(final_state != "pending",含 forced)不显示收口按钮
    const finalized = o.final_state !== "pending";
    const actionsBox = $("#final-actions");
    if (actionsBox) actionsBox.style.display = finalized ? "none" : "";
    // 输出位置:完整路径仅在已出表且 payload 提供时显示(可选中复制)
    const outBox = $("#final-output");
    const outputPath = status && status.final && status.final.output_path;
    if (outBox) {
      if (finalized && outputPath) {
        outBox.style.display = "";
        $("#final-output-path").textContent = outputPath;
      } else {
        outBox.style.display = "none";
      }
    }
  }
  function renderMeta(p) {
    const st = p.status || {};
    $("#bizdate").textContent = "业务日期:" + (esc(st.business_date) || "—");
    const s = p.scheduler || {};
    $("#sched").textContent = !s.available ? "计划任务:不可用" :
      (s.running ? "计划任务:正在运行" :
       (s.last_result_cn ? "计划任务:" + s.last_result_cn :
        (s.next_run ? "下次运行:" + String(s.next_run) : "计划任务:正常")));
  }
  function renderJobs(jobs) {
    lastSummaryJobs = jobs || [];
    $("#jobs-table tbody").innerHTML = (jobs || []).map((j) => {
      const cell = jobProgressCell(j);
      return `
      <tr class="row" data-id="${j.id}">
        <td><span class="sw pill-${j.pill}"></span>${esc(j.mall_key)}</td>
        <td class="mono">#${j.id} · ${esc(String(j.task_name).slice(0, 18))}…</td>
        <td><span class="pill ${j.pill}">${j.pill === "ok" ? "✓" : j.pill === "err" ? "✕" :
          j.pill === "warn" ? "?" : "○"} ${esc(j.state_cn)}</span></td>
        <td class="num${cell.cls ? " " + cell.cls : ""}">${esc(cell.text)}</td>
        <td class="num">${j.harvest_attempts} 次</td>
        <td class="num">${j.updated_at ? fmtAgo(j.updated_at) : "—"}</td>
      </tr>`; }).join("");
    document.querySelectorAll("#jobs-table tr.row").forEach((tr) =>
      tr.addEventListener("click", () => openDrawer(tr.dataset.id, jobs)));
    settleRebuildDrawerSwitch();   // 重建后等替代任务进入投影即自动切换
  }
  // P3(Review Fix Round 2):恢复时间线 signature 来自持久化 JSON,页面层不无条件
  // 信任——显式 Number 转换 + Number.isFinite 校验,恶意/损坏内容只能得到"—",
  // 永不作为 HTML 片段进入 innerHTML。
  function safeProgressText(signature) {
    if (!Array.isArray(signature) || signature.length < 6) return "—";
    const finished = Number(signature[5]);
    const total = Number(signature[0]);
    if (!Number.isFinite(finished) || !Number.isFinite(total)) return "—";
    return finished + "/" + total;
  }

  // ---- 本机受控操作台(2026-08-28)纯逻辑 seam(tests/js 直测,无 DOM) ----
  // jobs[i].live_progress 形状(projection + progress probe):
  //   {available, classification:"REMOTE_ACTIVE"|"REMOTE_TERMINAL"|"REMOTE_UNKNOWN"|null,
  //    progress:{total,pending,running,failed,waiting_db,finished}|null,
  //    observed_at, stale, conflict, error_code}
  // 铁律:子任务进度绝不输出裸 "—",所有分支都必须可解释。
  function jobProgressCell(job) {
    const lp = (job && job.live_progress) || null;
    const crawlTotal = Number((job && job.crawl_total) || 0);
    if (lp && lp.conflict) return { text: "名称重复,无法安全确定", cls: "err-warn" };
    if (lp && lp.available && lp.progress && Number(lp.progress.total) > 0) {
      const total = Number(lp.progress.total);
      const finished = Number(lp.progress.finished);
      const failed = Number(lp.progress.failed || 0);
      if (lp.classification === "REMOTE_TERMINAL") {
        if (failed > 0) {
          return { text: "⚠ " + (finished + failed) + "/" + total + "(失败 " + failed + ")", cls: "err-warn" };
        }
        return { text: "✓ " + finished + "/" + total, cls: "ok" };
      }
      return { text: finished + "/" + total, cls: "" };
    }
    if (lp && lp.available && !lp.progress) return { text: "远端进度暂不可用", cls: "err-warn" };
    if (lp && !lp.available && lp.error_code && lp.error_code !== "PROBE_DISABLED") {
      return { text: "远端进度暂不可用", cls: "err-warn" };
    }
    if (crawlTotal > 0) return { text: "含 " + crawlTotal + " 条采集", cls: "" };
    return { text: "未开始", cls: "" };
  }

  const LP_CLASS_CN = { REMOTE_ACTIVE: "执行中", REMOTE_TERMINAL: "已结束", REMOTE_UNKNOWN: "未知" };
  // Drawer 实时进度块的数据源(纯函数):conflict → 重复说明;不可用 → 占位 + 本地绑定数;
  // 可用 → classification 中文 + 六行 breakdown + observed/stale + 汇总句。
  function progressDetail(liveProgress, crawlTotal, nowMs) {
    if (nowMs === undefined) nowMs = Date.now();
    const lp = liveProgress || {};
    const localTotal = Number(crawlTotal || 0);
    if (lp.conflict) {
      return { conflict: true, available: false, crawlTotal: localTotal,
        text: "任务名称/批次号存在重复,无法安全确定远端任务" };
    }
    if (!lp.available || !lp.progress) {
      return { conflict: false, available: false, crawlTotal: localTotal,
        text: "远端进度暂不可用" };
    }
    const p = lp.progress;
    const failed = Number(p.failed || 0);
    const rows = [["总数", Number(p.total) || 0], ["已完成", Number(p.finished) || 0],
      ["执行中", Number(p.running) || 0], ["待执行", Number(p.pending) || 0],
      ["待入库", Number(p.waiting_db) || 0], ["失败", failed]];
    let summary = "远端执行状态未知";
    if (lp.classification === "REMOTE_TERMINAL") {
      summary = failed > 0 ? "远端任务已结束,其中失败 " + failed + " 个子任务" : "远端任务已全部完成";
    } else if (lp.classification === "REMOTE_ACTIVE") {
      summary = "远端任务执行中";
    }
    const observedText = lp.observed_at
      ? "最后远端进度 " + fmtAgoAt(lp.observed_at, nowMs) + (lp.stale ? "(缓存)" : "")
      : "";
    return {
      conflict: false, available: true, crawlTotal: localTotal,
      classCn: LP_CLASS_CN[lp.classification] || String(lp.classification || "未知"),
      cls: lp.classification === "REMOTE_TERMINAL" ? "ok"
        : (lp.classification === "REMOTE_ACTIVE" ? "run" : "wait"),
      rows, observedText, summary,
    };
  }

  // 阶段卡/输出位置的文件名防御:服务端已只发布文件名,若历史缓存仍带
  // 完整路径(含 / 或 \),只显示最后一段,绝不撑坏布局。
  function outputDisplayName(v) {
    const s = String(v ?? "");
    const cut = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
    return cut === -1 ? s : s.slice(cut + 1);
  }

  // ---- 操作结果文案映射(白名单;未知码原样透传由调用方 esc) ----
  const ACTION_ERROR_TEXT = {
    INVALID_ACTION_TOKEN: "操作令牌失效,请刷新页面",
    INVALID_REMOTE_STATE: "该任务缺少可用的精确远端身份",
    ACTION_IN_FLIGHT: "同类型操作正在执行,请等待其完成",
  };
  function _blockersSummary(outcome) {
    const b = outcome && outcome.publication_blocked;
    if (!b) return "";
    if (typeof b === "string") return b;
    if (Array.isArray(b)) return b.join("、");
    if (typeof b === "object") {
      if (Array.isArray(b.blockers)) return b.blockers.join("、");
      return Object.keys(b).join("、");
    }
    return String(b);
  }
  function closeoutOutcomeText(oc) {
    // 运行句柄带 result_code;durable 记录只带 {outcome, publication, publication_blocked},
    // 与服务端 _closeout_result_code 同规则推导兜底。
    let code = oc.result_code || "";
    if (!code) {
      const raw = String(oc.outcome || "");
      if (raw === "ALREADY_FINALIZED" || raw === "ALREADY_RUNNING") code = raw;
      else if (raw === "CLOSEOUT_DEADLINE") code = "DEADLINE";
      else if (oc.publication) code = "FINALIZED";
      else if (oc.publication_blocked) code = "BLOCKED";
    }
    if (code === "FINALIZED") return "✓ 最终 Excel 已生成";
    if (code === "ALREADY_FINALIZED") return "今日已完成最终出表";
    if (code === "ALREADY_RUNNING") return "已有生产 writer 正在运行,未启动第二个";
    if (code === "DEADLINE") return "本轮收口到达时限,进度已保存,下一次调度继续";
    if (code === "BLOCKED") return "暂时不能收口:" + (_blockersSummary(oc) || oc.outcome || "存在未清零阻塞");
    return "收口失败:" + (code || oc.error || oc.outcome || "未知错误");
  }
  function reexportOutcomeText(oc) {
    const code = oc.outcome || "";
    if (code === "REEXPORT_COMPLETED") {
      return "重新导出成功:导入 " + Number(oc.validated || oc.result_count || 0) + " 条结果";
    }
    if (code === "REEXPORT_COMPLETED_NO_RESULT") return "任务已完成,本次导出 0 条,可稍后重试";
    if (code === "REEXPORT_STILL_RUNNING") return "招商局任务仍在执行,本次未取得最终导出";
    if (code === "REEXPORT_MANUAL") return "导出进入人工诊断,请按人工恢复手册处理";
    if (code === "REEXPORT_REJECTED") {
      if (!oc.error_code || oc.error_code === "REMOTE_PROGRESS_UNKNOWN") return "无法确认远端进度,未执行导出";
      return "重新导出被拒绝:" + oc.error_code;
    }
    return "重新导出未完成:" + (code || oc.error_code || "未知结果");
  }
  function rebuildOutcomeText(oc) {
    const code = oc.outcome || "";
    if (code === "REBUILD_DISPATCHED") return "重建完成:新任务 " + (oc.new_task_name || "") + " 已提交";
    if (code === "REBUILD_QUEUED") return "重建完成:新任务已创建,等待调度提交";
    if (code === "REBUILD_SUBMIT_UNKNOWN") return "重建已创建新任务,提交结果待系统自动对账";
    if (code === "REBUILD_ALREADY_REPLACED") return "该任务已有替代任务,未重复重建";
    if (code === "REBUILD_NOT_ALLOWED") return "当前状态不允许重建:" + (oc.reason || oc.error_code || "");
    if (code === "REBUILD_REJECTED") return "重建被拒绝:" + (oc.error_code || oc.reason || "未知原因");
    return "重建结果:" + (code || "未知结果");
  }
  function actionOutcomeText(actionType, result) {
    if (!result) return "操作结果未知";
    if (result.duplicate) return ACTION_ERROR_TEXT.ACTION_IN_FLIGHT;
    if (result.error) {
      const code = result.error.code || "";
      if (code === "ACTION_IN_FLIGHT" && actionType === "closeout") return "收口已在进行中";
      if (ACTION_ERROR_TEXT[code]) return ACTION_ERROR_TEXT[code];
      return "操作失败:" + (code || ("HTTP " + result.error.status));
    }
    if (result.status === "TIMEOUT") return "操作仍在执行,结果请稍后在操作记录中查看";
    const oc = result.outcome || {};
    if (actionType === "closeout") return closeoutOutcomeText(oc);
    if (actionType === "re-export") return reexportOutcomeText(oc);
    if (actionType === "rebuild") return rebuildOutcomeText(oc);
    return "操作完成";
  }

  // ---- action 客户端(纯逻辑 seam):受控写只能由用户点击触发,轮询仅存在于
  // post() 内部;summary 轮询控制器(createPollController)绝不 POST。 ----
  // deps: {token, fetchJson(url, opts)→{status, json}, pollIntervalMs=2000,
  //        maxPolls=150, setTimeout, clearTimeout}
  // post: 同 path 在途去重({duplicate:true});非 2xx → {error:{code,status}};
  //       2xx(202)→自动轮询 poll 路径至非 RUNNING,超 maxPolls → {status:"TIMEOUT"}。
  // refresh: 单次 POST,不轮询,2xx 直接返回响应 json。
  function createActionClient(deps) {
    const fetchJson = deps.fetchJson;
    const pollIntervalMs = deps.pollIntervalMs == null ? 2000 : Number(deps.pollIntervalMs);
    const maxPolls = deps.maxPolls == null ? 150 : Number(deps.maxPolls);
    const token = String(deps.token || "");
    const timers = {
      setTimeout: deps.setTimeout || ((fn, ms) => setTimeout(fn, ms)),
      clearTimeout: deps.clearTimeout || ((t) => clearTimeout(t)),
    };
    const inFlight = {};   // path → 进行中的 promise(同 path 重复点击直接 duplicate)
    function actionHeaders() {
      return { "X-Dashboard-Action-Token": token, "Content-Type": "application/json" };
    }
    function errorCode(json) {
      return (json && json.detail && json.detail.error_code) || null;
    }
    function httpError(res) {
      const status = res ? Number(res.status) : 0;
      return { error: { code: errorCode(res && res.json) || ("HTTP_" + status), status } };
    }
    function sleep(ms) {
      return new Promise((resolve) => { timers.setTimeout(resolve, ms); });
    }
    async function pollUntilDone(pollPath) {
      for (let i = 0; i < maxPolls; i += 1) {
        await sleep(pollIntervalMs);
        const res = await fetchJson(pollPath, { method: "GET", headers: actionHeaders() });
        if (!res || typeof res.status !== "number" || res.status < 200 || res.status >= 300) {
          return httpError(res);
        }
        const data = res.json || {};
        if (data.status && data.status !== "RUNNING") {
          return { action_id: data.action_id, status: data.status, outcome: data.outcome };
        }
      }
      return { status: "TIMEOUT" };
    }
    function post(path, body) {
      if (inFlight[path]) return Promise.resolve({ duplicate: true });
      const run = (async () => {
        try {
          const res = await fetchJson(path, { method: "POST", headers: actionHeaders(), body: JSON.stringify(body || {}) });
          if (!res || typeof res.status !== "number" || res.status < 200 || res.status >= 300) {
            return httpError(res);
          }
          const json = res.json || {};
          if (!json.poll) return json;   // 同步结果(无轮询路径)
          return await pollUntilDone(json.poll);
        } finally {
          delete inFlight[path];
        }
      })();
      inFlight[path] = run;
      return run;
    }
    async function refresh(path, body) {
      const res = await fetchJson(path, { method: "POST", headers: actionHeaders(), body: JSON.stringify(body || {}) });
      if (!res || typeof res.status !== "number" || res.status < 200 || res.status >= 300) {
        return httpError(res);
      }
      return res.json || {};
    }
    return { post, refresh };
  }

  let drawerJob = null;

  // ---- 重建成功后的 drawer 自动切换(2026-09-04 hotfix) ----
  // 重建 POST 成功后触发一次 summary 刷新;刷新完成(以及此后每次 summary
  // 渲染)在投影里找替代任务:找到 → drawer 切到新任务;还没出现 → 明确
  // "正在同步状态"文案,绝不静默回退成旧任务视图。纯判定逻辑抽成
  // resolveRebuildDrawerSwitch(tests/js 直测,无 DOM)。
  let pendingRebuildSwitch = null;
  let lastSummaryJobs = [];

  function armRebuildDrawerSwitch(oldJobId, oc) {
    const newId = Number(oc && oc.new_remote_job_id);
    if (!Number.isFinite(newId) || newId <= 0) return null;
    if (String(newId) === String(oldJobId)) return null;
    return {
      oldJobId: String(oldJobId),
      newJobId: String(newId),
      newTaskName: String((oc && (oc.new_task_name || oc.new_batch_number)) || ""),
    };
  }

  // ---- 重建成功分支收紧(2026-09-04 Review Round 2) ----
  // 本地立即降级为 SUPERSEDED 视图只允许出现在服务端确认的重建成功 outcome
  // 上,且必须带有有效 new_remote_job_id。REBUILD_REJECTED / REBUILD_NOT_ALLOWED
  // 即使错误负载带了候选 new_task_name(例如 TASK_NAME_CONFLICT 的候选名)也
  // 绝不能把旧任务改写成 SUPERSEDED、绝不武装 drawer 自动切换。纯判定 seam
  // (tests/js 直测,无 DOM)。
  const REBUILD_LOCAL_SUPERSEDE_OUTCOMES = new Set([
    "REBUILD_DISPATCHED", "REBUILD_QUEUED", "REBUILD_SUBMIT_UNKNOWN", "REBUILD_ALREADY_REPLACED"]);

  function rebuildOutcomeAdmitsLocalSupersede(oc) {
    if (!oc || typeof oc !== "object") return false;
    if (!REBUILD_LOCAL_SUPERSEDE_OUTCOMES.has(oc.outcome)) return false;
    const newId = Number(oc.new_remote_job_id);
    return Number.isFinite(newId) && newId > 0;
  }

  function resolveRebuildDrawerSwitch(jobs, pending) {
    if (!pending) return { action: "none" };
    const list = Array.isArray(jobs) ? jobs : [];
    const target = list.find((x) => x && String(x.id) === pending.newJobId);
    if (target) return { action: "switch", job: target };
    const label = pending.newTaskName || ("#" + pending.newJobId);
    return { action: "wait", message: "旧任务已被替代,新任务:" + label + ",正在同步状态" };
  }

  function settleRebuildDrawerSwitch() {
    const pending = pendingRebuildSwitch;
    if (!pending) return;
    const drawer = $("#drawer");
    // 用户已收起 drawer 或手动打开了别的任务 → 放弃自动切换,不再打扰。
    if (!drawer || !drawer.classList.contains("show") ||
        !drawerJob || String(drawerJob.id) !== pending.oldJobId) {
      pendingRebuildSwitch = null;
      return;
    }
    const r = resolveRebuildDrawerSwitch(lastSummaryJobs, pending);
    if (r.action === "switch") {
      pendingRebuildSwitch = null;
      openDrawer(r.job.id, lastSummaryJobs);
      const st = drawerActionStatus();
      if (st) st.textContent = "已切换到替代任务";
      return;
    }
    if (r.action === "wait") {
      const st = drawerActionStatus();
      if (st) st.textContent = r.message;
    }
  }

  function renderLiveProgressBlock(statusDict, crawlTotal) {
    const box = $("#drawer-live .lp-body");
    if (!box) return;
    const d = progressDetail(statusDict, crawlTotal);
    if (d.conflict) {
      box.innerHTML = `<p class="lp-warn">${esc(d.text)}</p>`;
      return;
    }
    if (!d.available) {
      box.innerHTML = `<p>${esc(d.text)}</p>` +
        `<p class="lp-note">本地采集绑定数 ${Number(d.crawlTotal)}(非远端完成数)</p>`;
      return;
    }
    box.innerHTML = `
      <div class="lp-head"><span class="pill ${d.cls}">${esc(d.classCn)}</span>` +
      (d.observedText ? `<span class="lp-ago">${esc(d.observedText)}</span>` : "") + `</div>
      <div class="lp-rows">${d.rows.map(([k, v]) =>
        `<div><span>${esc(k)}</span><span>${Number(v)}</span></div>`).join("")}</div>
      <p class="lp-note">${esc(d.summary)}</p>`;
  }

  // ---- 操作按钮可用性(与后端门完全一致;不可用时禁用而非隐藏) ----
  const REEXPORT_ALLOWED_STATES = ["SUBMITTED", "RUNNING", "EXPORTED", "MANUAL_REQUIRED", "RECOVERY_EXHAUSTED"];
  const REBUILD_ALLOWED_STATES = ["SUBMITTED", "RUNNING", "EXPORTED", "RECOVERY_EXHAUSTED", "MANUAL_REQUIRED"];
  function updateDrawerButtons() {
    const j = drawerJob;
    const hasIdentity = !!(j && j.remote_identity);
    const state = j ? String(j.state || "") : "";
    const superseded = !!(j && j.superseded_by);
    const usable = !opsUnavailable;
    const refreshBtn = $("#btn-refresh-progress");
    const reexportBtn = $("#btn-reexport");
    const rebuildBtn = $("#btn-rebuild");
    if (refreshBtn) refreshBtn.disabled = !(usable && hasIdentity);
    if (reexportBtn) reexportBtn.disabled = !(usable && hasIdentity && REEXPORT_ALLOWED_STATES.indexOf(state) !== -1);
    if (rebuildBtn) rebuildBtn.disabled = !(usable && hasIdentity && REBUILD_ALLOWED_STATES.indexOf(state) !== -1 && !superseded);
  }
  function setBtnLoading(btn, loading, loadingText, normalText) {
    if (!btn) return;
    if (loading) { btn.disabled = true; btn.textContent = loadingText; }
    else { btn.disabled = false; btn.textContent = normalText; }
  }
  function drawerActionStatus() { return $("#action-status"); }

  function openDrawer(id, jobs) {
    const j = jobs.find((x) => String(x.id) === String(id));
    if (!j) return;
    drawerJob = j;
    const tl = (window._timelines || {})[String(id)] || {};
    $("#drawer").classList.add("show");
    $("#drawer .kv").innerHTML = `
      <b>任务编号</b><span class="mono">#${j.id}</span>
      <b>任务名称</b><span class="mono">${esc(j.task_name)}</span>
      <b>批次号</b><span class="mono">${esc(j.task_name)}</span>
      <b>远端标识</b><span class="mono">${esc(j.remote_identity || "—")}</span>
      <b>当前状态</b><span>${esc(j.state_cn)}</span>
      <b>采集轮次</b><span>harvest ${Number(j.harvest_attempts)} 次</span>
      <b>恢复阶段</b><span>${esc(tl.phase || "—")}${tl.recovery_attempts > 0 ? " · 已执行 " + tl.recovery_attempts + " 次自动恢复" : ""}</span>` +
      (j.superseded_by
        ? `<b>替代任务</b><span class="mono">已被人工重建替代 → 新任务 ${esc(j.superseded_by.new_task_name)}(新批次 ${esc(j.superseded_by.new_batch_number)})</span>`
        : "");
    renderLiveProgressBlock(j.live_progress, j.crawl_total);
    const status = drawerActionStatus();
    if (status) status.textContent = "";
    hideRebuildConfirm();
    updateDrawerButtons();
    $("#drawer .tl").innerHTML = (tl.observations || []).slice(-8).map((o) => `
      <li><time>${(() => { const d = new Date(o.observed_at); return isNaN(d) ? "—" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" }); })()}</time>进度 ${safeProgressText(o.signature)} · 导出 ${o.export_count === 0 ? "无结果" : "有结果"}</li>`).join("");
    $("#drawer .note").textContent = "观察与探测不代表异常;“需要人工检查”需要人工介入;“自动恢复已用尽”已按无采集结果安全收口,无需人工处理。";
    loadOpLog(j.id);
  }

  // ---- 操作记录(Drawer 底部;GET /dashboard/api/actions 的 recent 过滤本 job) ----
  const OPLOG_TYPE_CN = { "re-export": "重新导出", reexport: "重新导出", manual_reexport: "重新导出", rebuild: "重建", closeout: "收口" };
  function oplogOutcomeText(record) {
    if (typeof record.outcome === "string" && record.outcome) return record.outcome;
    if (record.outcome && typeof record.outcome === "object") {
      return String(record.outcome.outcome || record.outcome.result_code || "");
    }
    return String(record.result_code || "");
  }
  async function loadOpLog(jobId) {
    const ul = $("#drawer-oplog");
    if (!ul) return;
    const sec = ul.closest(".sec");
    try {
      const res = await fetch("/dashboard/api/actions");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const idStr = String(jobId);
      const rows = ((data && data.recent) || []).filter((r) =>
        String(r.remote_job_id || "") === idStr ||
        String(r.old_remote_job_id || "") === idStr ||
        String(r.target || "").endsWith(":" + idStr)).slice(0, 8);
      ul.innerHTML = rows.map((r) => {
        const d = new Date(r.recorded_at);
        const time = isNaN(d) ? "" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" });
        const typeCn = OPLOG_TYPE_CN[r.action_type] || esc(r.action_type);
        return `<li><time>${esc(time)}</time>${typeCn} · ${esc(oplogOutcomeText(r))}</li>`;
      }).join("");
      if (sec) sec.style.display = rows.length ? "" : "none";
    } catch (e) {
      if (sec) sec.style.display = "none";   // 请求失败静默隐藏
    }
  }

  // ---- operator-context 引导:拿 per-process 令牌;失败 → 操作按钮全部禁用 ----
  let actionToken = null, businessDate = null, actionClient = null, opsUnavailable = false;
  async function realFetchJson(url, opts) {
    const res = await fetch(url, opts);
    let json = null;
    try { json = await res.json(); } catch (e) { json = null; }
    return { status: res.status, json };
  }
  function getActionClient() { return opsUnavailable ? null : actionClient; }
  async function bootstrapOperatorContext() {
    try {
      const res = await fetch("/dashboard/api/operator-context");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      actionToken = data.action_token || "";
      businessDate = data.business_date || "";
      actionClient = createActionClient({
        token: actionToken, fetchJson: realFetchJson,
        setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (t) => clearTimeout(t),
      });
    } catch (e) {
      opsUnavailable = true;
      const status = drawerActionStatus();
      if (status) status.textContent = "操作台不可用";
      const finalStatus = $("#closeout-status");
      if (finalStatus) finalStatus.textContent = "操作台不可用";
    }
    // 成功(F5 恢复:拿到当天 businessDate 立即查 RUNNING)与失败(actions GET
    // 无需 token;操作台不可用也应显示正在收口)两条路径都要查一次。
    refreshCloseoutActions();
    updateDrawerButtons();
  }

  // ---- 收口进行中(closeoutRunning)统一权威:拒绝猜测 ----
  // 权威 = 本页 action 在途 OR /api/actions 报告当天 closeout RUNNING OR summary
  // 报告 production writer 正被 closeout/finalize 持有。后端 writer_active 经
  // summary stages 参与 fingerprint,自动出新 ETag,无 304 陈旧;F5/页面重启后靠
  // /api/actions 轮询恢复同状态,绝不用 localStorage 充当权威。
  let closeoutActionInFlight = false;   // 本页 POST 202 已接受,poll 未到终态
  let closeoutBackendRunning = false;   // /api/actions 报告当天 closeout RUNNING
  let closeoutWriterRunning = false;    // summary 报告 production writer 被收口链持有
  // hydration:settle(成功或失败)前按钮不得出现可点击"立即收口"。
  let closeoutSummarySeen = false, closeoutActionsSeen = false;
  function closeoutUIRunning() {
    return closeoutActionInFlight || closeoutBackendRunning || closeoutWriterRunning;
  }

  // 与后端 _closeout_writer_active 同语义:active 且 command ∈ {closeout,finalize};
  // tick/harvest-only/drain-only 持锁或 command 缺失都不算。
  const CLOSEOUT_WRITER_COMMANDS = ["closeout", "finalize"];
  function isCloseoutWriter(writer) {
    if (!writer || !writer.active) return false;
    return CLOSEOUT_WRITER_COMMANDS.indexOf(String(writer.command || "")) !== -1;
  }

  // 纯函数:当天 running 列表是否存在 closeout RUNNING(business_date 优先,target 前缀兜底)
  function computeCloseoutBackendRunning(runningList, businessDate) {
    const date = String(businessDate || "");
    return (Array.isArray(runningList) ? runningList : []).some((r) =>
      r && r.action_type === "closeout" && r.status === "RUNNING" &&
      (date && (String(r.business_date || "") === date ||
                String(r.target || "").startsWith(date + ":"))));
  }

  // 纯函数:/api/actions 顶层 business_date(服务端权威)优先;operator-context 的
  // businessDate 兜底;都无则空串(computeCloseoutBackendRunning 拒绝匹配)。
  // bootstrap 失败时 businessDate 为空,此前导致该路径永远匹配不到 RUNNING。
  function resolveCloseoutActionsDate(actionsData, bootstrapBusinessDate) {
    return String((actionsData && actionsData.business_date) || bootstrapBusinessDate || "");
  }

  // 纯函数:summary stages 的 closeout 阶段叠加正在收口覆盖(不 mutate 入参)。
  // 规则:finalized(done)不动;closeoutRunning → cur/"● 正在收口",数据冻结行:
  //   原"已冻结"→"已冻结，正在收口";否则 →"收口任务已启动，正在准备"。
  function applyCloseoutRunningOverlay(stages, opts) {
    if (!opts || !opts.closeoutRunning || !Array.isArray(stages)) return stages;
    return stages.map((stg) => {
      if (!stg || stg.key !== "closeout" || stg.state === "done") return stg;
      const sub = (stg.sub || []).map(([k, v]) => {
        if (k !== "数据冻结") return [k, v];
        return [k, String(v || "").indexOf("已冻结") === 0 ? "已冻结，正在收口" : "收口任务已启动，正在准备"];
      });
      return Object.assign({}, stg, { state: "cur", state_word: "● 正在收口", sub });
    });
  }

  // 纯 DOM,幂等:summary 重刷(render)反复调用也不闪文案;finalized 后区域隐藏
  // 仍由 renderFinal 管,这里只负责"操作台不可用/进行中/hydration 未完成/可点"四态。
  function updateCloseoutUI() {
    const btn = $("#closeout-btn");
    if (!btn) return;
    if (opsUnavailable) {
      // R3 P2-1:操作台不可用是最高优先级——按钮语义是"人工可发起收口",此时必不
      // 可能发起;收口运行态由阶段卡 overlay 传达,按钮只需不可点+说明原因。
      btn.disabled = true;
      if (btn.textContent !== "操作台不可用") btn.textContent = "操作台不可用";
    } else if (closeoutUIRunning()) {
      btn.disabled = true;
      if (btn.textContent !== "收口中…") btn.textContent = "收口中…";
    } else if (!(closeoutSummarySeen && closeoutActionsSeen)) {
      btn.disabled = true;   // hydration 未完成:不得出现可点击"立即收口"
      if (btn.textContent !== "检查收口状态…") btn.textContent = "检查收口状态…";
    } else {
      btn.disabled = false;
      if (btn.textContent !== "立即收口") btn.textContent = "立即收口";
    }
  }
  // 测试 seam:行为级验证需要驱动模块内权威状态(与生产同源,生产路径绝不调用)。
  // seen 缺省 true,旧式只关心 running 的断言不必每次都传;hydration 专项测试显式传 false;
  // opsUnavailable 缺省 false。
  function _setCloseoutStateForTest(state) {
    const s = state || {};
    closeoutActionInFlight = !!s.actionInFlight;
    closeoutBackendRunning = !!s.backendRunning;
    closeoutWriterRunning = !!s.writerRunning;
    opsUnavailable = !!s.opsUnavailable;
    closeoutSummarySeen = s.summarySeen === undefined ? true : !!s.summarySeen;
    closeoutActionsSeen = s.actionsSeen === undefined ? true : !!s.actionsSeen;
  }

  // actions 轻量轮询:复用 createSaasFlowPoller,零新基建;有 RUNNING 收口时提速到 5s。
  const CLOSEOUT_ACTIONS_IDLE_MS = 30000, CLOSEOUT_ACTIONS_ACTIVE_MS = 5000;
  const closeoutActionsDeps = {
    visible: () => document.visibilityState === "visible",
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    intervalMs: CLOSEOUT_ACTIONS_IDLE_MS,
    poll: async () => {
      // try/finally 保证首轮读取 settle(成功或失败)都完成 hydration。
      try {
        const res = await fetch("/dashboard/api/actions");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        // 权威日期优先取服务端顶层 business_date(bootstrap 失败时 businessDate
        // 为空,此前导致该路径永远匹配不到);operator-context 兜底。
        const matchDate = resolveCloseoutActionsDate(data, businessDate);
        closeoutBackendRunning = computeCloseoutBackendRunning(data && data.running, matchDate);
        closeoutActionsDeps.intervalMs = closeoutBackendRunning ? CLOSEOUT_ACTIONS_ACTIVE_MS : CLOSEOUT_ACTIONS_IDLE_MS;
        if (closeoutBackendRunning) ctl.run();   // 立即拉一次 summary,阶段卡马上反映
      } finally {
        closeoutActionsSeen = true;
        updateCloseoutUI();
      }
    },
  };
  const closeoutActionsPoller = createSaasFlowPoller(closeoutActionsDeps);
  async function refreshCloseoutActions() { try { await closeoutActionsPoller.run(); } catch (e) { /* 下轮再试 */ } }

  function hideRebuildConfirm() {
    const panel = $("#rebuild-confirm");
    if (panel) panel.style.display = "none";
  }
  function showRebuildConfirm() {
    const j = drawerJob;
    const panel = $("#rebuild-confirm");
    if (!panel || !j) return;
    panel.querySelector(".rc-text").textContent =
      "即将停用当前招商局任务并创建新的替代任务。旧任务:" + j.task_name +
      "。新任务名称和批次号将由系统自动生成(不会复用旧值),未完成的采集行将迁移到新任务。";
    panel.style.display = "";
  }

  function wireOperatorButtons() {
    const refreshBtn = $("#btn-refresh-progress");
    if (refreshBtn) refreshBtn.addEventListener("click", async () => {
      const j = drawerJob, st = drawerActionStatus();
      if (!j || refreshBtn.disabled) return;
      setBtnLoading(refreshBtn, true, "刷新任务进度…");
      if (st) st.textContent = "";
      try {
        const client = getActionClient();
        if (!client) { if (st) st.textContent = "操作台不可用"; return; }
        const res = await client.refresh(`/dashboard/api/jobs/${j.id}/refresh-progress`, { business_date: businessDate });
        if (res.error) {
          if (st) st.textContent = ACTION_ERROR_TEXT[res.error.code] ||
            ("刷新失败:" + (res.error.code || ("HTTP " + res.error.status)));
          return;
        }
        if (res.rate_limited) {
          if (st) st.textContent = "刷新过于频繁,请稍候";
          return;
        }
        renderLiveProgressBlock(res, j.crawl_total);
        if (st) st.textContent = "已刷新";
      } catch (e) {
        if (st) st.textContent = "刷新请求失败,请稍后重试";
      } finally {
        setBtnLoading(refreshBtn, false, null, "刷新任务进度");
        updateDrawerButtons();
      }
    });

    const reexportBtn = $("#btn-reexport");
    if (reexportBtn) reexportBtn.addEventListener("click", async () => {
      const j = drawerJob, st = drawerActionStatus();
      if (!j || reexportBtn.disabled) return;
      setBtnLoading(reexportBtn, true, "重新导出…");
      if (st) st.textContent = "";
      try {
        const client = getActionClient();
        if (!client) { if (st) st.textContent = "操作台不可用"; return; }
        const res = await client.post(`/dashboard/api/jobs/${j.id}/re-export`, { business_date: businessDate });
        if (st) st.textContent = actionOutcomeText("re-export", res);
      } catch (e) {
        if (st) st.textContent = "重新导出请求失败,请稍后重试";
      } finally {
        setBtnLoading(reexportBtn, false, null, "重新导出");
        updateDrawerButtons();
        ctl.run();
        if (j) loadOpLog(j.id);
      }
    });

    const rebuildBtn = $("#btn-rebuild");
    if (rebuildBtn) rebuildBtn.addEventListener("click", () => {
      if (!drawerJob || rebuildBtn.disabled) return;
      const st = drawerActionStatus();
      if (st) st.textContent = "";
      showRebuildConfirm();   // 二次确认(内嵌面板,不用浏览器原生 confirm)
    });
    const rebuildOk = $("#btn-rebuild-ok");
    if (rebuildOk) rebuildOk.addEventListener("click", async () => {
      const j = drawerJob, st = drawerActionStatus();
      if (!j) return;
      hideRebuildConfirm();
      setBtnLoading(rebuildBtn, true, "重建中…");
      if (st) st.textContent = "";
      try {
        const client = getActionClient();
        if (!client) { if (st) st.textContent = "操作台不可用"; return; }
        const res = await client.post(`/dashboard/api/jobs/${j.id}/rebuild`, { business_date: businessDate, confirm: true });
        if (st) st.textContent = actionOutcomeText("rebuild", res);
        const oc = (res && res.outcome) || {};
        // Review Round 2:只有确认的重建成功 outcome + 有效 new_remote_job_id
        // 才本地降级为 superseded 视图;REBUILD_REJECTED(含 TASK_NAME_CONFLICT
        // 带候选名)不改旧任务、不武装自动切换。
        if (res && !res.error && rebuildOutcomeAdmitsLocalSupersede(oc)) {
          // 本地即时降级为 superseded 视图,等下一次 summary 校正
          j.superseded_by = j.superseded_by || {
            new_task_name: oc.new_task_name || "", new_batch_number: oc.new_batch_number || "" };
          j.state = "SUPERSEDED";
          // 2026-09-04 hotfix:重建成功后等 summary 刷新完成,自动把 drawer
          // 切到替代任务;新任务尚未进投影时给出明确同步文案,不回退旧视图。
          pendingRebuildSwitch = armRebuildDrawerSwitch(j.id, oc);
          if (pendingRebuildSwitch) {
            await ctl.run();
            settleRebuildDrawerSwitch();
          }
        }
      } catch (e) {
        if (st) st.textContent = "重建请求失败,请稍后重试";
      } finally {
        setBtnLoading(rebuildBtn, false, null, "重建任务");
        updateDrawerButtons();
        ctl.run();
        if (j) loadOpLog(j.id);
      }
    });
    const rebuildCancel = $("#btn-rebuild-cancel");
    if (rebuildCancel) rebuildCancel.addEventListener("click", hideRebuildConfirm);

    const closeoutBtn = $("#closeout-btn");
    if (closeoutBtn) closeoutBtn.addEventListener("click", async () => {
      const st = $("#closeout-status");
      // 双击/在途兜底:按钮态由 closeoutRunning 权威驱动(服务端 409 亦有 ACTION_IN_FLIGHT)
      if (closeoutUIRunning() || closeoutBtn.disabled) return;
      closeoutActionInFlight = true;   // POST 202 已接受→poll 到终态前保持"收口中…"
      updateCloseoutUI();
      if (st) st.textContent = "";
      try {
        const client = getActionClient();
        if (!client) { if (st) st.textContent = "操作台不可用"; return; }
        const res = await client.post("/dashboard/api/actions/closeout", { business_date: businessDate });
        if (res.error && res.error.code === "ACTION_IN_FLIGHT") {
          // 409:已有在途收口,同步真实状态为"正在收口",不当失败处理
          refreshCloseoutActions();
        }
        if (st) st.textContent = actionOutcomeText("closeout", res);
      } catch (e) {
        if (st) st.textContent = "收口请求失败,请稍后重试";
      } finally {
        closeoutActionInFlight = false;   // action client post 已轮询到终态才 resolve
        refreshCloseoutActions();   // 终态后立即刷新权威列表,马上清掉 backendRunning(FAILED/BLOCKED 不多等一轮轮询)
        updateCloseoutUI();
        ctl.run();   // 完成后刷新一次 summary
      }
    });

    const copyBtn = $("#copy-path");
    if (copyBtn) copyBtn.addEventListener("click", () => {
      const text = $("#final-output-path") ? $("#final-output-path").textContent : "";
      if (text && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => {});   // 复制失败静默
      }
    });

    const cnncLoginBtn = $("#cnnc-auth-login");
    if (cnncLoginBtn) cnncLoginBtn.addEventListener("click", cnncAuthLogin);
  }
  function renderTech(status) {
    $("#tech pre").textContent = JSON.stringify(status, null, 2);
  }

  // ---- SaaS 首次采集链路(独立只读诊断;失败只降级本面板,不影响 summary 轮询) ----
  // Review P1-4:独立 polling 生命周期——页面 visible 时约每 60 秒自查询一次,
  // hidden 停止,回前台立即补刷;与 summary 的 ETag/304 生命周期完全解耦。
  // 服务端仍有 ≥60s cache 与 ≥30s 强刷限流,绝不形成 5 秒级生产 DB 轮询。
  const SF_POLL_INTERVAL_MS = 60000;
  const SF_FETCH_MIN_INTERVAL_MS = 55000;   // 双保险:即便轮询器被密集触发也不打接口
  let sfLastFetchAt = 0, sfInFlight = false;

  function createSaasFlowPoller(deps) {
    const st = { timer: null, inFlight: false };
    function clearTimer() {
      if (st.timer !== null) { deps.clearTimeout(st.timer); st.timer = null; }
    }
    function schedule() {
      clearTimer();
      if (!deps.visible()) return false;
      st.timer = deps.setTimeout(() => { st.timer = null; run(); }, deps.intervalMs);
      return true;
    }
    async function run() {
      if (st.inFlight) return;
      st.inFlight = true;
      try {
        await deps.poll();
      } catch (e) {
        // poll 契约是自吞错误;此处兜底防断链(poll reject 时仍排程)
      } finally {
        st.inFlight = false;
      }
      schedule();
    }
    return {
      run, schedule,
      onHidden: clearTimer,
      onVisible: () => { if (!st.inFlight) run(); },
      stop: clearTimer,
    };
  }

  async function fetchSaasFlow(force) {
    if (sfInFlight) return;
    const now = Date.now();
    if (!force && now - sfLastFetchAt < SF_FETCH_MIN_INTERVAL_MS) return;
    sfInFlight = true;
    sfLastFetchAt = now;
    try {
      const url = force ? "/dashboard/api/saas-flow?refresh=1" : "/dashboard/api/saas-flow";
      const res = await fetch(url);
      if (!res.ok) throw new Error("HTTP " + res.status);
      renderSaasFlow(await res.json());
    } catch (e) {
      const notice = $("#sf-notice");
      if (notice) notice.textContent = "SaaS 链路诊断请求失败,稍后自动重试;主看板不受影响。";
    } finally {
      sfInFlight = false;
    }
  }

  const sfPoller = createSaasFlowPoller({
    poll: () => fetchSaasFlow(false),
    visible: () => document.visibilityState === "visible",
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    intervalMs: SF_POLL_INTERVAL_MS,
  });

  // R2 Central Bypass:crawl1 链改为 task→推送→回调证据;central 状态已退役。
  // "未发现记录"≠"未推送":缺记录只是当前证据下未见,文案不得写成确定未发生。
  const SF_STAGE_LABELS = {
    TASK_NOT_CREATED: "未生成task", PUSH_RECORD_NOT_FOUND: "未发现爬虫推送记录",
    PUSH_CONFIRMED_NO_CALLBACK_RECORD: "已推送，暂无回调记录",
    CALLBACK_IN_FLIGHT: "回调进行中", CALLBACK_PARTIAL: "部分回调完成",
    CALLBACK_DONE_SAAS_PENDING: "回调已完成，SaaS尚未推进",
    CALLBACK_DONE_NO_SEARCH: "callback后暂未search", MODEL_PENDING: "模型处理中",
    INTERNET_SAME_PENDING_APPROVAL: "互联网同款待审批",
    APPROVAL_OR_FINAL_CLOSURE_PENDING: "最终收口中",
    INTERNET_CANDIDATES_ALL_NON_SAME: "互联网候选全部判非同款",
    REFERENCE_SAME_ONLY: "只有参考链接同款", MODEL_DONE_NO_SAME: "模型完成无同款",
    NO_INTERNET_CANDIDATE: "无互联网候选", NO_COMMODITY_LIST: "未进入采集列表",
    EVIDENCE_INCOMPLETE: "证据不足",
  };
  // Review P2-2:区分"评估过=正常"与"证据不足/非评估时段",不把后者显示成"正常"
  // Review R2 P2-1:本地观察读取失败另有专用文案,不冒充"暂无观察"
  const SF_GROWTH_LABELS = { NORMAL: "正常", LAG_WATCH: "增长落后观察", LAG_PERSISTING: "持续增长落后" };
  const SF_REASON_LABELS = {
    NO_OBSERVATIONS: "暂无权威观察",
    INSUFFICIENT_OBSERVATIONS: "观察次数不足(需至少两次)",
    // UI Hotfix P2-1:evaluated=false,不能显示成"正常";只表达不评估
    AFTER_CUTOFF: "18:00 后不再评估",
    OBSERVATION_READ_FAILED: "权威观察读取失败",
  };

  function sfGrowthText(g) {
    if (!g) return "—";
    const reason = g.evaluation_reason || (g.evaluated ? "EVALUATED" : "");
    if (reason && SF_REASON_LABELS[reason]) return SF_REASON_LABELS[reason];
    return SF_GROWTH_LABELS[g.state] || g.state || "—";
  }

  // R3 P2:partial 顶部 notice 三态——查询失败 / 取证漂移 / 两者都有;
  // 纯函数 seam(tests/js 直接驱动),绝不出现"部分查询失败:;"空列表文案。
  function sfPartialNotice(degradedQueries, snapshotDrift) {
    const degraded = (degradedQueries || []).length > 0;
    const drift = (snapshotDrift || []).length > 0;
    if (degraded && drift) {
      return "部分查询失败,同时检测到查询期间数据变化;相关结果已降级,约 60 秒后重新取证。";
    }
    if (degraded) {
      return "部分诊断查询失败,相关证据已降级,不冒充业务 0:" + (degradedQueries || []).join(", ") + "。";
    }
    return "查询期间生产数据发生变化,本轮部分诊断按取证时点漂移处理;约 60 秒后自动重新取证。这不是业务异常。";
  }

  function renderSaasFlow(p) {
    const g = p.growth || {};
    const chips = [
      "客户 " + (g.customer_count ?? "—"),
      "SaaS " + (g.saas_count ?? "—"),
      "增长状态 " + sfGrowthText(g),
      "主等待阶段 " + (p.stages && p.stages.dominant_stage ? p.stages.dominant_stage.label_zh : "—"),
    ];
    $("#sf-chips").textContent = chips.join(" · ");
    const notice = $("#sf-notice");
    const driftList = (p.stages && p.stages.snapshot_drift) || [];
    if (p.status === "unconfigured") {
      notice.textContent = "数据库诊断未配置(需本机 CNNC_DIAG_DB_* 只读账号);以下仅显示本地权威观察的增长判断。";
    } else if (p.status === "unavailable") {
      notice.textContent = "数据库诊断不可用(连接失败或超时);已降级,主看板不受影响。";
    } else if (p.status === "rate_limited" || p.rate_limited) {
      notice.textContent = "刷新过于频繁,已按限流返回上次缓存结果(强制刷新间隔 ≥30 秒)。";
    } else if (p.status === "partial") {
      notice.textContent = sfPartialNotice(p.degraded_queries, driftList);
    } else {
      notice.textContent = "";
    }
    const growthBox = $("#sf-growth");
    const reason = g.evaluation_reason || "";
    if (reason === "OBSERVATION_READ_FAILED") {
      growthBox.textContent = "权威观察读取失败,增长判断暂不可用(本地状态文件异常;约 60 秒后自动重试)。";
    } else if (reason === "NO_OBSERVATIONS") {
      growthBox.textContent = "今日暂无权威观察(增量审计发现尚未运行),增长判断待首次快照后可用。";
    } else if (reason === "INSUFFICIENT_OBSERVATIONS") {
      growthBox.textContent = "今日权威观察仅 " + (g.observation_count ?? 0) +
        " 次,需至少两次才能判断增长分叉;最近一次:客户 " + (g.customer_count ?? "—") +
        ",SaaS " + (g.saas_count ?? "—") + "。";
    } else if (g.available) {
      let text = "今日权威观察 " + (g.observation_count ?? 0) + " 次;最近一次 " +
        fmtShanghaiDateTime(g.last_observed_at) +
        ":客户 " + (g.customer_count ?? "—") + ",SaaS " + (g.saas_count ?? "—");
      if (g.evaluated) text += ";本轮 客户 " + (g.customer_delta ?? 0) + " / SaaS " + (g.saas_delta ?? 0);
      growthBox.textContent = text;
      if (g.state === "LAG_WATCH" || g.state === "LAG_PERSISTING") {
        const dist = sfTopStages(p.stages, 4);
        growthBox.textContent += "。客户侧仍在新增,但 SaaS 数量本轮未增长;当前未收口链路主要分布于:" +
          (dist.length ? dist.join("、") : "证据不足") + "。";
      }
    } else {
      growthBox.textContent = "本地暂无今日权威观察序列,增长分叉判断不可用。";
    }
    $("#sf-tree").innerHTML = sfTreeHtml(p.stages);
    $("#sf-evidence").innerHTML = sfEvidenceHtml(p);
    $("#sf-meta").textContent = p.cached ? "缓存结果(≤60 秒)" : "实时查询";
  }

  function sfStageLine(label, count, extra) {
    return '<li><span>' + esc(label) + '</span><b>' + Number(count || 0) + '</b>' +
      (extra ? '<i>' + esc(extra) + '</i>' : "") + "</li>";
  }

  function sfTreeHtml(stages) {
    if (!stages) return "";
    if (stages.crawl_status_available === false) {
      return '<div class="sf-head">分层数据不可用(crawl_status 核心查询失败,不冒充业务 0)。</div>';
    }
    const drift = stages.snapshot_drift || [];
    const driftNote = "查询期间数据变化,本轮该分支分类暂不发布(约 60 秒后自动重新取证;这是取证时点问题,不是业务异常)";
    const crawler = stages.crawler_branch || {}, down = stages.downstream_branch || {};
    const crawlerKeys = ["TASK_NOT_CREATED", "PUSH_RECORD_NOT_FOUND",
      "PUSH_CONFIRMED_NO_CALLBACK_RECORD", "CALLBACK_IN_FLIGHT", "CALLBACK_PARTIAL",
      "CALLBACK_DONE_SAAS_PENDING"];
    const downKeys = ["CALLBACK_DONE_NO_SEARCH", "MODEL_PENDING", "INTERNET_SAME_PENDING_APPROVAL",
      "APPROVAL_OR_FINAL_CLOSURE_PENDING", "INTERNET_CANDIDATES_ALL_NON_SAME", "REFERENCE_SAME_ONLY",
      "MODEL_DONE_NO_SAME", "NO_INTERNET_CANDIDATE"];
    const graceNote = stages.callback_done_no_search_within_grace
      ? "其中 " + stages.callback_done_no_search_within_grace + " 个在回调宽限期(" +
        Math.round((stages.grace_seconds || 0) / 60) + " 分钟)内" : "";
    // Review P1-6:SQL 池计数不是权威客户计数,只称"当日商品池记录";
    // 权威客户/SaaS 数量只来自 growth.customer_count / growth.saas_count。
    // UI Hotfix P1:routing_evidence_incomplete 是 fail-safe 证据分类(同一商品
    // 多条 commodity_list、无法可靠选择 current row),不是业务故障;数量 > 0
    // 必须可见,且不得计入 crawl_status_1/2 或正常分支。
    const head = "当日商品池记录:" +
      (stages.pool_counts_available === false ? "不可用" : Number(stages.pool_scope_total || 0)) +
      " · 未最终收口:" +
      (stages.not_closed == null ? "不可用" : Number(stages.not_closed)) +
      (stages.no_commodity_list ? " · 未进入采集列表:" + Number(stages.no_commodity_list) : "") +
      (stages.routing_evidence_incomplete
        ? " · 路由证据不足(同商品存在多条采集记录,无法可靠确定当前记录):" +
          Number(stages.routing_evidence_incomplete)
        : "");
    // R2 Review P1-3:父节点语义。crawl_status=1 的子状态已包含
    // CALLBACK_DONE_SAAS_PENDING(回调已完成,SaaS尚未推进),旧的"未回调"
    // 父标签与之自相矛盾;改为按链路进度命名的"爬虫链路未完成"。
    // crawl_status=2 的"爬虫已回调"不受影响。
    const crawlerHtml = stages.crawler_branch === null
      ? '<li class="bf">├─ 爬虫链路未完成:' + (drift.includes("crawler_branch") ? esc(driftNote) : "本轮不可用") + "</li>"
      : '<li class="bf">├─ 爬虫链路未完成:' + Number(stages.crawl_status_1 || 0) + "<ul>" +
        crawlerKeys.map((k) => sfStageLine(SF_STAGE_LABELS[k], crawler[k])).join("") +
        sfStageLine("证据不足", crawler.EVIDENCE_INCOMPLETE) + "</ul></li>";
    const downHtml = stages.downstream_branch === null
      ? '<li class="bf">└─ 爬虫已回调:' + (drift.includes("downstream_branch") ? esc(driftNote) : "本轮不可用") + "</li>"
      : '<li class="bf">└─ 爬虫已回调:' + Number(stages.crawl_status_2 || 0) + "<ul>" +
        downKeys.map((k) => sfStageLine(SF_STAGE_LABELS[k], down[k],
          k === "CALLBACK_DONE_NO_SEARCH" ? graceNote : "")).join("") +
        sfStageLine("证据不足", down.EVIDENCE_INCOMPLETE) + "</ul></li>";
    return '<div class="sf-head">' + esc(head) + "</div>" +
      '<ul class="sf-branch">' + crawlerHtml + downHtml + "</ul>";
  }

  function sfEvidenceHtml(p) {
    const stages = p.stages || {};
    const parts = [];
    if (stages.reference_pending_approval_total) {
      parts.push("其中参考链接同款待人工审批 " + Number(stages.reference_pending_approval_total) + " 个商品");
    }
    if (!parts.length) return "";
    return "<p>" + parts.join("。</p><p>") + "。</p>";
  }

  function sfTopStages(stages, n) {
    if (!stages) return [];
    const merged = Object.assign({}, stages.crawler_branch || {}, stages.downstream_branch || {});
    if (stages.no_commodity_list) merged.NO_COMMODITY_LIST = stages.no_commodity_list;
    return Object.keys(merged)
      .filter((k) => k !== "EVIDENCE_INCOMPLETE" && merged[k] > 0)
      .sort((a, b) => merged[b] - merged[a])
      .slice(0, n)
      .map((k) => (SF_STAGE_LABELS[k] || k) + " " + merged[k]);
  }

  // ---- CNNC 认证状态(只读 30s 轮询;login_required 时显示受控重新登录按钮) ----
  const CNNC_POLL_INTERVAL_MS = 30000;
  const CNNC_STATUS_LABELS = {
    CNNC_AUTH_OK: "正常",
    CNNC_AUTH_REFRESHED: "正常（已自动续签）",
    SSO_LOGIN_REQUIRED: "⚠ 中核 SSO 已失效，需要重新登录",
    CNNC_AUTH_REFRESH_IN_PROGRESS: "正在续签",
    AUTH_REFRESH_DEADLINE_EXCEEDED: "续签超时（业务预算内未完成）",
    CNNC_WAF_CONTEXT_STALE: "WAF 安全层拦截（非登录失效）",
    LOGIN_RECOVERY_FAILED: "恢复失败（查看错误信息）",
  };
  function cnncStatusText(p) {
    return CNNC_STATUS_LABELS[p.status] || ("异常：" + p.status);
  }
  // 纯函数 seam(离线单测,不触碰 DOM):登录区视图模型。
  // manualRecoveryText 由 last_manual_recovery 工件(后端 /api/cnnc-auth/status
  // 原样透传)生成非敏感文案:只含时间(HH:mm 上海)、outcome、cURL 计数与
  // 失败文件名——绝不含 at_redacted/路径/任何凭证形态;无该字段返回 ""。
  function cnncAuthView(p) {
    const r = p && p.last_manual_recovery;
    let manualRecoveryText = "";
    if (r && typeof r === "object") {
      const bits = ["上次手动刷新"];
      const t = fmtShanghaiDateTime(r.completed_at);
      if (t !== "—") bits.push(t.slice(11, 16));
      bits.push(String(r.outcome || "UNKNOWN"));
      const expected = Number(r.curl_expected);
      const synced = Number(r.curl_synced);
      if (Number.isFinite(expected) && Number.isFinite(synced)) {
        bits.push("cURL " + synced + "/" + expected);
      }
      const failed = Array.isArray(r.curl_failed_files)
        ? r.curl_failed_files.filter((f) => f) : [];
      if (failed.length) bits.push("失败: " + failed.join(","));
      manualRecoveryText = bits.join(" · ");
    }
    return {
      statusText: cnncStatusText(p || {}),
      autoExpand: Boolean(p && p.login_required),
      manualRecoveryText,
    };
  }
  function renderCnncAuth(p) {
    const chips = $("#cnnc-auth-chips");
    if (!chips) return;
    const view = cnncAuthView(p);
    const parts = ["状态 " + view.statusText];
    const refreshedAt = new Date(p.last_refresh_at || "").getTime();
    if (!Number.isNaN(refreshedAt) && p.last_refresh_at) {
      parts.push("最后刷新 " + fmtAgoAt(p.last_refresh_at, Date.now()));
    }
    chips.textContent = parts.join(" · ");
    const meta = $("#cnnc-auth-meta");
    if (meta) {
      const metaBits = [];
      if (view.manualRecoveryText) metaBits.push(view.manualRecoveryText);
      if (p.last_error) metaBits.push("最近错误：" + p.last_error);
      meta.textContent = metaBits.join(" ｜ ");
    }
    // 重新登录入口常驻(2026-09-10):login_required 只决定警告与自动展开,
    // 不决定按钮存在性——认证正常也可主动点。details 只开不关:正常状态
    // 绝不强制展开/收起,不重置用户操作。
    const d = $("#cnnc-auth");
    if (d && view.autoExpand && !d.open) d.open = true;
  }
  // Bug2B:fetch 必须有超时——永久 pending 的响应曾让 inFlight 永真、轮询链死亡。
  // fetcher 做成可注入 factory(tests/js 用 fake clock 驱动超时行为),失败文案逐字保留。
  const CNNC_FETCH_TIMEOUT_MS = 8000;
  // deps: {fetchImpl(url,opts), timers:{setTimeout,clearTimeout}, timeoutMs}
  function createCnncAuthFetcher(deps) {
    const fetchImpl = deps.fetchImpl || ((url, opts) => fetch(url, opts));
    const timers = deps.timers || {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (t) => clearTimeout(t),
    };
    const timeoutMs = deps.timeoutMs == null ? CNNC_FETCH_TIMEOUT_MS : Number(deps.timeoutMs);
    const st = { inFlight: false };
    async function run() {
      if (st.inFlight) return;
      st.inFlight = true;
      const abort = new AbortController();
      const timer = timers.setTimeout(() => abort.abort(), timeoutMs);
      try {
        const res = await fetchImpl("/dashboard/api/cnnc-auth/status", { signal: abort.signal });
        if (!res.ok) throw new Error("HTTP " + res.status);
        renderCnncAuth(await res.json());
      } catch (e) {
        const chips = $("#cnnc-auth-chips");
        if (chips) chips.textContent = "状态 — · CNNC 认证状态读取失败，稍后自动重试";
      } finally {
        timers.clearTimeout(timer);
        st.inFlight = false;
      }
    }
    return { run, state: st };
  }
  const cnncAuthFetcher = createCnncAuthFetcher({});
  const cnncPoller = createSaasFlowPoller({
    poll: () => cnncAuthFetcher.run(),
    visible: () => document.visibilityState === "visible",
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    intervalMs: CNNC_POLL_INTERVAL_MS,
  });

  async function cnncAuthLogin() {
    const btn = $("#cnnc-auth-login");
    const notice = $("#cnnc-auth-notice");
    if (!btn || btn.disabled) return;
    const client = getActionClient();
    if (!client) { if (notice) notice.textContent = "操作台不可用，无法发起重新登录。"; return; }
    setBtnLoading(btn, true, "正在启动登录浏览器…");
    if (notice) notice.textContent = "";
    try {
      const res = await client.refresh("/dashboard/api/cnnc-auth/login", {});
      if (res.error) {
        if (notice) notice.textContent = res.error.status === 429
          ? "登录浏览器已在启动中（60 秒内），请稍候。"
          : "登录请求失败：" + (res.error.code || "HTTP " + res.error.status);
        return;
      }
      if (notice) notice.textContent = (res && res.note) || "登录浏览器已启动，完成滑块/短信后系统自动恢复。";
    } catch (e) {
      if (notice) notice.textContent = "登录请求失败，请稍后重试。";
    } finally {
      setBtnLoading(btn, false, null, "重新登录");
    }
  }

  // ---- 生产运行版本(Production Deployment Identity;只读 60s 轮询,与主看板解耦) ----
  const PI_POLL_INTERVAL_MS = 60000;
  const PI_FETCH_TIMEOUT_MS = 8000;
  const PI_STATUS_TEXTS = {
    MATCH: "✅ 当前生产代码与 GitHub 生产分支一致",
    CODE_CHANGED_AFTER_PROCESS_START: "⚠ 代码已更新但进程未重启(进程仍在运行旧版本)",
    BEHIND: "⚠ 当前生产机运行版本落后于 GitHub Production HEAD",
    AHEAD: "⚠ 本地存在未推送的生产提交",
    REMOTE_DIFFERS: "⚠ 本机代码与 GitHub 生产分支不一致",
    REMOTE_UNKNOWN: "GitHub 生产分支 HEAD 暂不可达(本地版本状态未知)",
  };
  function piStatusText(versionStatus) {
    return PI_STATUS_TEXTS[versionStatus] || "运行版本状态未知";
  }
  const PI_STATUS_WORDS = {
    MATCH: "版本一致",
    CODE_CHANGED_AFTER_PROCESS_START: "代码已更新未重启",
    BEHIND: "落后远端",
    AHEAD: "领先远端",
    REMOTE_DIFFERS: "与远端不一致",
    REMOTE_UNKNOWN: "远端不可达",
  };
  function piStatusWord(versionStatus) {
    return PI_STATUS_WORDS[versionStatus] || "未知";
  }
  function piWorkingTreeText(local) {
    const wt = local && local.working_tree;
    if (wt !== "CLEAN" && wt !== "DIRTY") return "未知";
    if (wt === "CLEAN") return "CLEAN";
    const cls = local.dirty && local.dirty.classification;
    if (cls === "ALL_ARTIFACTS") return "DIRTY(仅运行产物)";
    if (cls === "HAS_CODE_CHANGES") return "DIRTY(含代码改动)";
    return "DIRTY";
  }
  function piShaText(sha) {
    if (!sha) return "未知";
    return sha.length > 16 ? sha.slice(0, 8) + "…" + sha.slice(-6) : sha;
  }
  function renderProductionIdentity(p) {
    const chips = $("#pi-chips");
    if (!chips) return;
    const local = p.local || {}, startup = p.startup || {};
    const remote = p.remote || {}, cmp = p.comparison || {};
    const schedOk = p.scheduler && p.scheduler.status === "OK";
    chips.textContent = "版本 " + piStatusWord(cmp.version_status) +
      " · " + (local.git_branch || remote.branch || "—") +
      " · " + (local.git_sha || startup.git_sha || "").slice(0, 8) +
      " · 调度器 " + (schedOk ? "已注册" : "未知");
    const statusEl = $("#pi-status");
    if (statusEl) {
      const color = cmp.version_status === "MATCH" ? "var(--ok)"
        : (cmp.version_status === "REMOTE_UNKNOWN" || !cmp.version_status ||
           cmp.version_status === "UNKNOWN") ? "var(--muted)"
        : "var(--warn)";
      let html = '<div style="color:' + color + ';font-weight:600">' +
        esc(piStatusText(cmp.version_status)) + "</div>";
      (cmp.alerts || []).forEach((a) => {
        html += '<div style="color:var(--warn)">⚠ ' + esc(a) + "</div>";
      });
      statusEl.innerHTML = html;
    }
    const kv = $("#pi-kv");
    if (kv) {
      const proc = startup.process || {}, py = startup.python || {};
      const rows = [
        ["运行进程版本 Startup SHA", piShaText(startup.git_sha)],
        ["本地仓库 HEAD", piShaText(local.git_sha)],
        ["GitHub 生产分支 HEAD", piShaText(remote.git_sha)],
        ["生产分支", remote.branch || "—"],
        ["工作区", piWorkingTreeText(local)],
        ["进程启动时间", (proc.started_at || "—") +
          (proc.started_at_source ? "(" + proc.started_at_source + ")" : "")],
        ["Python", (py.version || "—") + (py.virtualenv ? " · venv" : " · 系统")],
      ];
      ((p.scheduler && p.scheduler.tasks) || []).forEach((t) => {
        rows.push(["计划任务 " + (t.name || "—"),
          (t.registered ? "已注册" : "未注册") +
          " · " + (t.state || "—") +
          " · LastRun " + (t.last_run_time || "—") +
          " · Result " + (t.last_task_result == null ? "—" : t.last_task_result)]);
      });
      kv.innerHTML = rows.map((r) => "<b>" + esc(r[0]) + "</b><span>" + esc(r[1]) + "</span>").join("");
    }
    const meta = $("#pi-meta");
    if (meta) meta.textContent = "生成于 " + (p.generated_at || "—");
  }
  // fetch 必须有超时(与 cnnc-auth 同型 factory;失败只降级本卡 chips 文案)
  function createProductionIdentityFetcher(deps) {
    const fetchImpl = deps.fetchImpl || ((url, opts) => fetch(url, opts));
    const timers = deps.timers || {
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (t) => clearTimeout(t),
    };
    const timeoutMs = deps.timeoutMs == null ? PI_FETCH_TIMEOUT_MS : Number(deps.timeoutMs);
    const st = { inFlight: false };
    async function run() {
      if (st.inFlight) return;
      st.inFlight = true;
      const abort = new AbortController();
      const timer = timers.setTimeout(() => abort.abort(), timeoutMs);
      try {
        const res = await fetchImpl("/dashboard/api/production-identity", { signal: abort.signal });
        if (!res.ok) throw new Error("HTTP " + res.status);
        renderProductionIdentity(await res.json());
      } catch (e) {
        const chips = $("#pi-chips");
        if (chips) chips.textContent = "状态 — · 生产运行版本读取失败，稍后自动重试";
      } finally {
        timers.clearTimeout(timer);
        st.inFlight = false;
      }
    }
    return { run, state: st };
  }
  const prodIdentityFetcher = createProductionIdentityFetcher({});
  const prodIdentityPoller = createSaasFlowPoller({
    poll: () => prodIdentityFetcher.run(),
    visible: () => document.visibilityState === "visible",
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (t) => clearTimeout(t),
    intervalMs: PI_POLL_INTERVAL_MS,
  });

  function start() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        ctl.onVisible(); sfPoller.onVisible(); cnncPoller.onVisible(); prodIdentityPoller.onVisible(); closeoutActionsPoller.onVisible();
      } else {
        ctl.onHidden(); sfPoller.onHidden(); cnncPoller.onHidden(); prodIdentityPoller.onHidden(); closeoutActionsPoller.onHidden();
      }
    });
    $("#refresh").addEventListener("click", () => ctl.run());
    $("#jobs-toggle").addEventListener("click", () => {
      const m = $("#more"); m.classList.toggle("show");
      $("#jobs-toggle").textContent = m.classList.contains("show") ? "收起任务列表 ↑" : "查看全部招商局任务 ↓";
    });
    const sfRefresh = $("#sf-refresh");
    if (sfRefresh) sfRefresh.addEventListener("click", () => fetchSaasFlow(true));
    wireCustomerPrice();            // 客户价汇总/明细(只读 GET;无独立轮询链)
    wireOperatorButtons();          // 受控操作按钮(唯一允许 POST 的入口)
    bootstrapOperatorContext();     // 拿 per-process 操作令牌;失败 → 按钮禁用
    setInterval(updateLive, 1000);
    ctl.run();
    sfPoller.run();  // SaaS Flow 独立启动,不等 summary
    cnncPoller.run();  // CNNC 认证状态独立启动(30s 只读轮询)
    prodIdentityPoller.run();  // 生产运行版本独立启动(60s 只读轮询)
  }
  const api = { start, stop: () => { ctl.stop(); sfPoller.stop(); cnncPoller.stop(); prodIdentityPoller.stop(); closeoutActionsPoller.stop(); },
    createPollController, createSaasFlowPoller, safeProgressText, sfPartialNotice,
    sfTreeHtml, sfGrowthText, jobProgressCell, progressDetail, outputDisplayName,
    createActionClient, actionOutcomeText, cnncStatusText, renderCnncAuth, cnncAuthView,
    fmtShanghaiDateTime, createCnncAuthFetcher,
    piStatusText, piStatusWord, piWorkingTreeText, renderProductionIdentity,
    createProductionIdentityFetcher,
    computeCloseoutBackendRunning, applyCloseoutRunningOverlay, isCloseoutWriter,
    resolveCloseoutActionsDate, closeoutUIRunning, updateCloseoutUI,
    pollDelay, inCloseoutWatchWindow, _setCloseoutStateForTest,
    armRebuildDrawerSwitch, resolveRebuildDrawerSwitch,
    rebuildOutcomeAdmitsLocalSupersede,
    cpStatusCn, cpStatusPill, cpRowMatchesFilter, cpSortRows };
  if (typeof window !== "undefined") { window.Dashboards = api; }
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  if (typeof document !== "undefined") { document.addEventListener("DOMContentLoaded", start); }
})();
