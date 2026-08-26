(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const ACTIVE_MS = 5000, IDLE_MS = 30000;
  let timer = null, etag = null, lastGenerated = null, lastPayload = null;

  function fmtAgo(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    return s < 60 ? `${Math.round(s)} 秒前` : `${Math.round(s / 60)} 分钟前`;
  }
  function setLive(ok, text) {
    $("#live").textContent = text;
    $("#live-dot").className = "dot" + (ok ? "" : " err");
  }
  async function poll() {
    try {
      const headers = {};
      if (etag) headers["If-None-Match"] = etag;
      const res = await fetch("/dashboard/api/summary", { headers });
      if (res.status === 304) { onOk(); schedule(lastPayload); return; }   // 304 也是稳态:续排
      if (!res.ok) throw new Error(String(res.status));
      etag = res.headers.get("ETag");
      const payload = await res.json();
      lastPayload = payload;
      render(payload); onOk();
    } catch (e) {
      document.body.classList.add("offline");   // 断线横幅 + 旧数据保留
      clearTimeout(timer);
      timer = setTimeout(poll, 10000);          // 自动重试(横幅承诺兑现)
    }
  }
  function onOk() {
    document.body.classList.remove("offline");
    if (lastGenerated) setLive(true, "实时 · 最后更新 " + fmtAgo(lastGenerated));
  }
  function schedule(payload) {
    clearTimeout(timer);
    const st = (payload && payload.status) || {};
    const finalized = st.final && st.final.finalized;
    const active = (st.production_writer && st.production_writer.active) ||
      (st.final && st.final.computed_pending > 0 && !finalized);
    const interval = active ? ACTIVE_MS : IDLE_MS;
    timer = setTimeout(poll, interval);
  }
  function render(p) {
    lastGenerated = p.generated_at;
    renderHero(p.overall); renderAttention(p.overall.attention);
    renderStages(p.overall.stages); renderZs(p.overall.zs_summary);
    renderFinal(p.overall); renderMeta(p);
    renderJobs(p.jobs);
    window._timelines = p.timelines;
    renderTech(p.status); schedule(p);
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
    $("#bizdate").textContent = "Business date:" + (esc(st.business_date) || "—");
    const s = p.scheduler || {};
    $("#sched").textContent = !s.available ? "Scheduled task: unavailable" :
      (s.running ? "Scheduled task: currently running" :
       (s.last_result_cn ? "Scheduled task:" + s.last_result_cn :
        (s.next_run ? "Scheduled task: next run " + String(s.next_run) : "Scheduled task: normal")));
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
      <b>恢复阶段</b><span>${esc(tl.phase || "—")}</span>
      <b>子任务进度</b><span>${j.subtasks ? j.subtasks.finished + " / " + j.subtasks.total : "—"}</span>`;
    $("#drawer .tl").innerHTML = (tl.observations || []).slice(-8).map((o) => `
      <li><time>${(() => { const d = new Date(o.observed_at); return isNaN(d) ? "—" : d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" }); })()}</time>进度 ${o.signature ? o.signature[5] + "/" + o.signature[0] : "—"} · 导出 ${o.export_count === 0 ? "无结果" : "有结果"}</li>`).join("");
    $("#drawer .note").textContent = "观察与探测不代表异常;仅“需要人工处理/恢复已用尽”需要人工介入。";
  }
  function renderTech(status) {
    $("#tech pre").textContent = JSON.stringify(status, null, 2);
  }
  function start() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") poll();
    });
    $("#refresh").addEventListener("click", () => poll());
    $("#jobs-toggle").addEventListener("click", () => {
      const m = $("#more"); m.classList.toggle("show");
      $("#jobs-toggle").textContent = m.classList.contains("show") ? "收起任务列表 ↑" : "查看全部招商局任务 ↓";
    });
    setInterval(() => { if (lastGenerated) setLive(!document.body.classList.contains("offline"),
      (document.body.classList.contains("offline") ? "⚠ 连接中断,正在重试" : "实时 · 最后更新 ") + (document.body.classList.contains("offline") ? "" : fmtAgo(lastGenerated))); }, 1000);
    poll();
  }
  window.Dashboards = { start, stop: () => clearTimeout(timer) };
  document.addEventListener("DOMContentLoaded", start);
})();
