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
    renderFinal(p.overall, p.status); renderMeta(p);
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
        const time = isNaN(d) ? "" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
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
    updateDrawerButtons();
  }

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
        if (res && !res.error && (oc.new_task_name || oc.outcome === "REBUILD_ALREADY_REPLACED")) {
          // 本地即时降级为 superseded 视图,等下一次 summary 校正
          j.superseded_by = j.superseded_by || {
            new_task_name: oc.new_task_name || "", new_batch_number: oc.new_batch_number || "" };
          j.state = "SUPERSEDED";
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
      if (closeoutBtn.disabled) return;   // 双击兜底(服务端 409 亦有 ACTION_IN_FLIGHT)
      setBtnLoading(closeoutBtn, true, "收口中…");
      if (st) st.textContent = "";
      try {
        const client = getActionClient();
        if (!client) { if (st) st.textContent = "操作台不可用"; return; }
        const res = await client.post("/dashboard/api/actions/closeout", { business_date: businessDate });
        if (st) st.textContent = actionOutcomeText("closeout", res);
      } catch (e) {
        if (st) st.textContent = "收口请求失败,请稍后重试";
      } finally {
        setBtnLoading(closeoutBtn, false, null, "立即收口");
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
    wireOperatorButtons();          // 受控操作按钮(唯一允许 POST 的入口)
    bootstrapOperatorContext();     // 拿 per-process 操作令牌;失败 → 按钮禁用
    setInterval(updateLive, 1000);
    ctl.run();
    sfPoller.run();  // SaaS Flow 独立启动,不等 summary
  }
  const api = { start, stop: () => { ctl.stop(); sfPoller.stop(); },
    createPollController, createSaasFlowPoller, safeProgressText, sfPartialNotice,
    sfTreeHtml, sfGrowthText, jobProgressCell, progressDetail, outputDisplayName,
    createActionClient, actionOutcomeText };
  if (typeof window !== "undefined") { window.Dashboards = api; }
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  if (typeof document !== "undefined") { document.addEventListener("DOMContentLoaded", start); }
})();
