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
      <li><time>${(() => { const d = new Date(o.observed_at); return isNaN(d) ? "—" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" }); })()}</time>进度 ${o.signature ? o.signature[5] + "/" + o.signature[0] : "—"} · 导出 ${o.export_count === 0 ? "无结果" : "有结果"}</li>`).join("");
    $("#drawer .note").textContent = "观察与探测不代表异常;“需要人工检查”需要人工介入;“自动恢复已用尽”已按无采集结果安全收口,无需人工处理。";
  }
  function renderTech(status) {
    $("#tech pre").textContent = JSON.stringify(status, null, 2);
  }
  function start() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") ctl.onVisible();
      else ctl.onHidden();
    });
    $("#refresh").addEventListener("click", () => ctl.run());
    $("#jobs-toggle").addEventListener("click", () => {
      const m = $("#more"); m.classList.toggle("show");
      $("#jobs-toggle").textContent = m.classList.contains("show") ? "收起任务列表 ↑" : "查看全部招商局任务 ↓";
    });
    setInterval(updateLive, 1000);
    ctl.run();
  }
  const api = { start, stop: () => ctl.stop(), createPollController };
  if (typeof window !== "undefined") { window.Dashboards = api; }
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  if (typeof document !== "undefined") { document.addEventListener("DOMContentLoaded", start); }
})();
