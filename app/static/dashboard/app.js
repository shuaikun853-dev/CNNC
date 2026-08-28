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

  function fmtAgoMs(ms) {
    const s = Math.max(0, (Date.now() - ms) / 1000);
    return s < 60 ? `${Math.round(s)} 秒前` : `${Math.round(s / 60)} 分钟前`;
  }
  function fmtAgo(iso) { return fmtAgoMs(new Date(iso).getTime()); }
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
  function pollDelay(payload) {
    const st = (payload && payload.status) || {};
    const finalized = st.final && st.final.finalized;
    const active = (st.production_writer && st.production_writer.active) ||
      (st.final && st.final.computed_pending > 0 && !finalized);
    return active ? ACTIVE_MS : IDLE_MS;
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
    renderHero(p.overall); renderAttention(p.overall.attention);
    renderStages(p.overall.stages); renderZs(p.overall.zs_summary);
    renderFinal(p.overall); renderMeta(p);
    renderJobs(p.jobs);
    window._timelines = p.timelines;
    renderTech(p.status);
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
          `<div><span>${k}</span><span>${esc(v)}</span></div>`).join("")}</div>
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
  function renderFinal(o) {
    const f = $("#final");
    f.className = "final" + (o.final_state === "ok" ? " ok" : "");
    f.querySelector(".why").textContent = o.final_text;
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
    $("#jobs-table tbody").innerHTML = (jobs || []).map((j) => `
      <tr class="row" data-id="${j.id}">
        <td><span class="sw pill-${j.pill}"></span>${esc(j.mall_key)}</td>
        <td class="mono">#${j.id} · ${esc(String(j.task_name).slice(0, 18))}…</td>
        <td><span class="pill ${j.pill}">${j.pill === "ok" ? "✓" : j.pill === "err" ? "✕" :
          j.pill === "warn" ? "?" : "○"} ${esc(j.state_cn)}</span></td>
        <td class="num">${j.subtasks ? j.subtasks.finished + " / " + j.subtasks.total : "—"}</td>
        <td class="num">${j.harvest_attempts} 次</td>
        <td class="num">${j.updated_at ? fmtAgo(j.updated_at) : "—"}</td>
      </tr>`).join("");
    document.querySelectorAll("#jobs-table tr.row").forEach((tr) =>
      tr.addEventListener("click", () => openDrawer(tr.dataset.id, jobs)));
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

  function openDrawer(id, jobs) {
    const j = jobs.find((x) => String(x.id) === String(id));
    const tl = (window._timelines || {})[String(id)] || {};
    $("#drawer").classList.add("show");
    $("#drawer .kv").innerHTML = `
      <b>任务编号</b><span class="mono">#${j.id} · ${esc(j.task_name)}</span>
      <b>远端标识</b><span class="mono">${esc(j.remote_identity || "—")}</span>
      <b>当前状态</b><span>${esc(j.state_cn)}</span>
      <b>采集轮次</b><span>harvest ${j.harvest_attempts} 次</span>
      <b>恢复阶段</b><span>${esc(tl.phase || "—")}${tl.recovery_attempts > 0 ? " · 已执行 " + tl.recovery_attempts + " 次自动恢复" : ""}</span>
      <b>子任务进度</b><span>${j.subtasks ? j.subtasks.finished + " / " + j.subtasks.total : "—"}</span>`;
    $("#drawer .tl").innerHTML = (tl.observations || []).slice(-8).map((o) => `
      <li><time>${(() => { const d = new Date(o.observed_at); return isNaN(d) ? "—" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" }); })()}</time>进度 ${safeProgressText(o.signature)} · 导出 ${o.export_count === 0 ? "无结果" : "有结果"}</li>`).join("");
    $("#drawer .note").textContent = "观察与探测不代表异常;“需要人工检查”需要人工介入;“自动恢复已用尽”已按无采集结果安全收口,无需人工处理。";
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

  const SF_STAGE_LABELS = {
    TASK_NOT_CREATED: "未生成task", TASK_CREATED_NO_CENTRAL_RECORD: "task未进分控",
    CENTRAL_NOT_ENQUEUED: "中控未入队", CENTRAL_ENQUEUED_WAITING: "已入队等待",
    CRAWLER_EXECUTING: "正在执行", CRAWLER_FAILED: "爬虫失败", CALLBACK_LAG: "callback lag",
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
        (g.last_observed_at ? String(g.last_observed_at).replace("T", " ").slice(0, 19) : "—") +
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
    const crawlerKeys = ["TASK_NOT_CREATED", "TASK_CREATED_NO_CENTRAL_RECORD", "CENTRAL_NOT_ENQUEUED",
      "CENTRAL_ENQUEUED_WAITING", "CRAWLER_EXECUTING", "CRAWLER_FAILED", "CALLBACK_LAG"];
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
    const crawlerHtml = stages.crawler_branch === null
      ? '<li class="bf">├─ 爬虫未回调:' + (drift.includes("crawler_branch") ? esc(driftNote) : "本轮不可用") + "</li>"
      : '<li class="bf">├─ 爬虫未回调:' + Number(stages.crawl_status_1 || 0) + "<ul>" +
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
    const detail = stages.central_status_detail || [];
    if (detail.length) {
      parts.push("分控子任务状态(前 " + detail.length + " 组,batch/任务/队列态×数量):" +
        detail.map((d) => esc(String(d.task_status) + "/" + String(d.queue_status) + " ×" + d.count))
          .join("、"));
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

  function start() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") { ctl.onVisible(); sfPoller.onVisible(); }
      else { ctl.onHidden(); sfPoller.onHidden(); }
    });
    $("#refresh").addEventListener("click", () => ctl.run());
    $("#jobs-toggle").addEventListener("click", () => {
      const m = $("#more"); m.classList.toggle("show");
      $("#jobs-toggle").textContent = m.classList.contains("show") ? "收起任务列表 ↑" : "查看全部招商局任务 ↓";
    });
    const sfRefresh = $("#sf-refresh");
    if (sfRefresh) sfRefresh.addEventListener("click", () => fetchSaasFlow(true));
    setInterval(updateLive, 1000);
    ctl.run();
    sfPoller.run();  // SaaS Flow 独立启动,不等 summary
  }
  const api = { start, stop: () => { ctl.stop(); sfPoller.stop(); },
    createPollController, createSaasFlowPoller, safeProgressText, sfPartialNotice,
    sfTreeHtml, sfGrowthText };
  if (typeof window !== "undefined") { window.Dashboards = api; }
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  if (typeof document !== "undefined") { document.addEventListener("DOMContentLoaded", start); }
})();
