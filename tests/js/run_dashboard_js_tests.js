"use strict";
// Dashboard 前端轮询控制器行为测试(node 直跑,零依赖、无 DOM)。
// 覆盖 Independent Review Fix Round 1:
//   P2-1  304 也是成功同步(不重渲染、不误显"最后同步 X 分钟前");
//   P2-2  页面 hidden 真暂停(clearTimeout + 不再注册),visible 立即补刷;
//         在途 poll 去重,人工刷新不产生重复轮询链。
// 覆盖 Independent Review Fix Round 2:
//   P3    恢复时间线 signature 的安全渲染(safeProgressText)。
// 覆盖 SaaS Flow Diagnostic Review Fix Round 1:
//   P1-4  SaaS Flow 独立轮询生命周期:summary 连续 304 仍按 ~60s 周期刷新;
//         hidden 停止、visible 立即补刷;与 summary ETag/304 生命周期解耦。
// 被测对象:app/static/dashboard/app.js 暴露的 createPollController /
// createSaasFlowPoller / safeProgressText。
const path = require("path");

// R2 Case 22:renderCnncAuth 前端测试需要的最小 document stub,必须在 require
// app.js 之前设置。按选择器缓存同一假元素(测试要断言 renderCnncAuth 写入的
// display),未知选择器返回 null;其他测试不用 document,不受影响。
const cnncFakeElements = new Map();
const cnncKnownSelectors = new Set(["#cnnc-auth-chips", "#cnnc-auth-meta", "#cnnc-auth-login"]);
global.document = {
  addEventListener() {},
  querySelector(sel) {
    if (!cnncKnownSelectors.has(sel)) return null;
    if (!cnncFakeElements.has(sel)) {
      cnncFakeElements.set(sel, sel === "#cnnc-auth-login"
        ? { style: { display: "none" } }
        : { textContent: "" });
    }
    return cnncFakeElements.get(sel);
  },
};

const { createPollController, createSaasFlowPoller, safeProgressText, sfPartialNotice,
        sfTreeHtml, sfGrowthText, jobProgressCell, progressDetail, outputDisplayName,
        createActionClient, actionOutcomeText, renderCnncAuth } = require(
  path.join(__dirname, "..", "..", "app", "static", "dashboard", "app.js"));

let failed = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log("ok - " + name);
  } else {
    failed += 1;
    console.error("FAIL - " + name + (detail === undefined ? "" : " :: " + String(detail)));
  }
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

function makeClock() {
  const timers = new Map();
  let nextId = 1;
  let now = 0;
  return {
    setTimeout(fn, ms) {
      const id = nextId++;
      timers.set(id, { fn, at: now + ms });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    pending: () => timers.size,
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let bestId = null;
        let bestAt = Infinity;
        for (const [id, t] of timers) {
          if (t.at <= end && t.at < bestAt) { bestId = id; bestAt = t.at; }
        }
        if (bestId === null) break;
        const t = timers.get(bestId);
        timers.delete(bestId);
        now = Math.max(now, t.at);
        t.fn();
      }
      now = end;
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeHarness() {
  const clock = makeClock();
  const polls = [];
  const counters = { poll: 0, sync: 0, content: 0, offline: 0 };
  let visible = true;
  const deps = {
    visible: () => visible,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    delay: () => 5000,
    retryDelay: 10000,
    poll: () => { counters.poll += 1; const d = deferred(); polls.push(d); return d.promise; },
    onSync: () => { counters.sync += 1; },
    onContent: () => { counters.content += 1; },
    onOffline: () => { counters.offline += 1; },
  };
  return { clock, polls, counters, deps, setVisible: (v) => { visible = v; } };
}

async function test_304_keeps_sync_fresh_without_rerender() {
  const h = makeHarness();
  const ctl = createPollController(h.deps);
  ctl.run();
  await h.polls[0].resolve({ status: 200, payload: { generated_at: "t0" } });
  await flush();
  check("P2-1 首次 200 渲染一次内容并记一次成功同步",
        h.counters.content === 1 && h.counters.sync === 1, JSON.stringify(h.counters));
  for (let i = 0; i < 3; i += 1) {
    h.clock.advance(5000);  // 到点触发下一次轮询
    await h.polls[h.polls.length - 1].resolve({ status: 304 });
    await flush();
  }
  check("P2-1 连续 304 不重复渲染内容", h.counters.content === 1, JSON.stringify(h.counters));
  check("P2-1 连续 304 每次都记成功同步", h.counters.sync === 4, JSON.stringify(h.counters));
  check("P2-1 304 后继续排程且只有一条链", h.clock.pending() === 1 && h.counters.poll === 4);
}

async function test_hidden_pauses_and_visible_resumes() {
  const h = makeHarness();
  const ctl = createPollController(h.deps);
  ctl.run();
  await h.polls[0].resolve({ status: 200, payload: { generated_at: "t0" } });
  await flush();
  check("P2-2 可见时排程下一次轮询", h.clock.pending() === 1);
  h.setVisible(false);
  ctl.onHidden();  // 模拟 visibilitychange → hidden
  check("P2-2 hidden 清除待触发 timer", h.clock.pending() === 0);
  h.clock.advance(600000);
  check("P2-2 hidden 期间不再发起轮询", h.counters.poll === 1, JSON.stringify(h.counters));
  h.setVisible(true);
  ctl.onVisible();  // 模拟 visibilitychange → visible
  check("P2-2 回到前台立即补刷", h.counters.poll === 2);
  await h.polls[1].resolve({ status: 304 });
  await flush();
}

async function test_schedule_refuses_when_hidden() {
  const h = makeHarness();
  h.setVisible(false);
  const ctl = createPollController(h.deps);
  ctl.run();
  await h.polls[0].resolve({ status: 200, payload: { generated_at: "t0" } });
  await flush();
  check("P2-2 隐藏状态下收到 200 后不注册下一次 timer", h.clock.pending() === 0);
  h.clock.advance(600000);
  check("P2-2 隐藏状态下不继续轮询", h.counters.poll === 1);
}

async function test_no_duplicate_poll_chains() {
  const h = makeHarness();
  const ctl = createPollController(h.deps);
  ctl.run();
  ctl.run();  // 在途时重复触发(等价人工刷新连点)
  await flush();
  check("P2-2 在途 poll 去重", h.counters.poll === 1, JSON.stringify(h.counters));
  await h.polls[0].resolve({ status: 200, payload: { generated_at: "t0" } });
  await flush();
  check("P2-2 完成后仅排程一条链", h.clock.pending() === 1);
  ctl.run();  // 有 pending timer 时人工刷新
  await flush();
  check("P2-2 人工刷新立即发起一次轮询", h.counters.poll === 2);
  await h.polls[1].resolve({ status: 200, payload: { generated_at: "t1" } });
  await flush();
  check("P2-2 人工刷新后仍只有一条排程链", h.clock.pending() === 1);
  h.clock.advance(5000);
  check("P2-2 到点只发起一次轮询", h.counters.poll === 3);
}

async function test_offline_retry_then_recover() {
  const h = makeHarness();
  const ctl = createPollController(h.deps);
  ctl.run();
  h.polls[0].reject(new Error("network down"));
  await flush();
  check("P2-1 失败进入 offline 且不记成功同步",
        h.counters.offline === 1 && h.counters.sync === 0, JSON.stringify(h.counters));
  check("P2-1 失败后排重试 timer", h.clock.pending() === 1);
  h.clock.advance(10000);
  await h.polls[1].resolve({ status: 200, payload: { generated_at: "t1" } });
  await flush();
  check("P2-1 重试成功恢复内容与同步",
        h.counters.content === 1 && h.counters.sync === 1 && h.counters.offline === 1);
}

function test_safe_progress_text() {
  check("P3 正常 signature 渲染 45/120", safeProgressText([120, 75, 0, 0, 0, 45]) === "45/120",
        safeProgressText([120, 75, 0, 0, 0, 45]));
  const evil = safeProgressText(["<img src=x onerror=alert(1)>", "75", 0, 0, 0, "45"]);
  check("P3 恶意 signature 不作为 HTML 注入", evil === "—" && !String(evil).includes("<img"), evil);
  check("P3 非数组 signature 返回占位",
        safeProgressText(null) === "—" && safeProgressText("45/120") === "—");
  check("P3 长度不足 signature 返回占位", safeProgressText([120, 75]) === "—");
  check("P3 NaN/Infinity signature 返回占位",
        safeProgressText([120, 75, 0, 0, 0, NaN]) === "—" &&
        safeProgressText([Infinity, 1, 1, 1, 1, 1]) === "—");
}

// ---- SaaS Flow Diagnostic Review Fix P1-4:独立轮询生命周期 ----

function makeSfHarness(clock) {
  const polls = [];
  const counters = { poll: 0 };
  const deps = {
    visible: () => true,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    intervalMs: 60000,
    poll: () => { counters.poll += 1; const d = deferred(); polls.push(d); return d.promise; },
  };
  return { polls, counters, deps };
}

async function test_sf_polls_independently_of_summary_304() {
  const h = makeHarness();            // summary 控制器(5s 延迟)
  const clock = h.clock;
  const sf = makeSfHarness(clock);    // 共享同一 fake clock,60s 周期
  const sfPoller = createSaasFlowPoller(sf.deps);
  const ctl = createPollController(h.deps);

  sfPoller.run();
  ctl.run();
  await sf.polls[0].resolve({ ok: true });
  await h.polls[0].resolve({ status: 200, payload: { generated_at: "t0" } });
  await flush();

  let resolvedSummary = 1;
  let resolvedSf = 1;
  // 推进 125 秒:summary 全程 304(数据指纹不变),SaaS Flow 必须仍按自身周期刷新
  for (let i = 0; i < 25; i += 1) {
    clock.advance(5000);
    while (resolvedSummary < h.polls.length) { h.polls[resolvedSummary].resolve({ status: 304 }); resolvedSummary += 1; }
    while (resolvedSf < sf.polls.length) { sf.polls[resolvedSf].resolve({ ok: true }); resolvedSf += 1; }
    await flush();
  }
  check("P1-4 summary 连续 304 不再重渲染", h.counters.content === 1, JSON.stringify(h.counters));
  check("P1-4 SaaS Flow 120+ 秒内独立刷新 >=3 次", sf.counters.poll >= 3, JSON.stringify(sf.counters));
  check("P1-4 SaaS Flow 只有一条排程链", clock.pending() === 2);  // summary 5s + sf 60s 各一个 timer
}

async function test_sf_hidden_pauses_and_visible_resumes() {
  const clock = makeClock();
  const sf = makeSfHarness(clock);
  let visible = true;
  sf.deps.visible = () => visible;
  const sfPoller = createSaasFlowPoller(sf.deps);
  sfPoller.run();
  await sf.polls[0].resolve({ ok: true });
  await flush();
  check("P1-4 SaaS Flow 可见时排下一次 60s", clock.pending() === 1);
  visible = false;
  sfPoller.onHidden();
  check("P1-4 hidden 清除 SaaS Flow timer", clock.pending() === 0);
  clock.advance(600000);
  check("P1-4 hidden 期间 SaaS Flow 不轮询", sf.counters.poll === 1, JSON.stringify(sf.counters));
  visible = true;
  sfPoller.onVisible();
  check("P1-4 回前台立即补刷 SaaS Flow", sf.counters.poll === 2);
  await sf.polls[1].resolve({ ok: true });
  await flush();
  check("P1-4 补刷后恢复排程", clock.pending() === 1);
}

async function test_sf_inflight_dedup() {
  const clock = makeClock();
  const sf = makeSfHarness(clock);
  const sfPoller = createSaasFlowPoller(sf.deps);
  sfPoller.run();
  sfPoller.run();  // 在途时重复触发
  await flush();
  check("P1-4 SaaS Flow 在途 poll 去重", sf.counters.poll === 1, JSON.stringify(sf.counters));
  await sf.polls[0].resolve({ ok: true });
  await flush();
  check("P1-4 完成后仅一条排程链", clock.pending() === 1);
}

function test_sf_partial_notice_three_states() {
  // R3 P2:pure query failure / pure snapshot drift / both 三态文案分开
  const degradedOnly = sfPartialNotice(["crawl1_central_rows"], []);
  check("R3 纯查询失败文案含降级与查询名",
        degradedOnly.includes("部分诊断查询失败") && degradedOnly.includes("crawl1_central_rows"),
        degradedOnly);
  const driftOnly = sfPartialNotice([], ["pool_vs_routing"]);
  check("R3 纯漂移文案不提查询失败且说明非业务异常",
        driftOnly.includes("查询期间") && driftOnly.includes("不是业务异常") &&
        !driftOnly.includes("查询失败"), driftOnly);
  const both = sfPartialNotice(["crawl1_task_rows"], ["downstream_branch"]);
  check("R3 双态文案同时提及失败与数据变化",
        both.includes("部分查询失败") && both.includes("数据变化"), both);
  check("R3 不得出现空列表拼接文案",
        !sfPartialNotice([], []).includes("::") &&
        !sfPartialNotice(["x"], []).includes(":;"));
}

// ---- UI/Contract Hotfix 2026-08-28:P1 路由证据不足可见 / P2-1 AFTER_CUTOFF 文案 ----

function sfStagesFixture() {
  return {
    crawl_status_available: true,
    pool_counts_available: true,
    snapshot_consistent: true,
    snapshot_drift: [],
    pool_scope_total: 5, not_closed: 5,
    routing_open_total: 5,
    no_commodity_list: 0,
    crawl_status_1: 1, crawl_status_2: 1, crawl_status_other: 0,
    routing_evidence_incomplete: 3,
    grace_seconds: 1800,
    crawler_branch: { TASK_NOT_CREATED: 1 },
    downstream_branch: { CALLBACK_DONE_NO_SEARCH: 1 },
  };
}

function test_sf_tree_shows_routing_evidence_incomplete() {
  const html = sfTreeHtml(sfStagesFixture());
  const head = html.slice(0, html.indexOf("</div>"));  // routing 文案所在的头部行
  check("P1 routing_evidence_incomplete=3 时阶段树必须可见该分类与数量",
        html.includes("路由证据不足") && html.includes("3"), html);
  check("P1 文案必须体现证据不足语义(同商品存在多条采集记录/无法可靠确定当前记录)",
        head.includes("同商品存在多条采集记录") || head.includes("无法可靠确定"), head);
  check("P1 不得把 3 加进正常分支计数",
        html.includes("爬虫链路未完成:1") && html.includes("爬虫已回调:1") &&
        !html.includes("爬虫链路未完成:4") && !html.includes("爬虫已回调:4"), html);
  check("P1 routing 文案不得误标为系统错误/数据丢失/爬虫失败/未生成任务",
        !head.includes("系统错误") && !head.includes("数据丢失") &&
        !head.includes("爬虫失败") && !head.includes("未生成任务"), head);
  const zero = sfTreeHtml(Object.assign(sfStagesFixture(), { routing_evidence_incomplete: 0 }));
  check("P1 数量为 0 时不额外突出", !zero.includes("路由证据不足"), zero);
}

function test_sf_growth_after_cutoff_text_not_normal() {
  const text = sfGrowthText({ evaluation_reason: "AFTER_CUTOFF", evaluated: false, state: "NORMAL" });
  check("P2-1 AFTER_CUTOFF 文案表达不评估",
        text.includes("不评估") || text.includes("不再评估"), text);
  check("P2-1 AFTER_CUTOFF 文案不得包含「正常」", !text.includes("正常"), text);
}

// ---- R2 Central Bypass:crawl1 状态树文案契约 ----

function test_r2_crawler_tree_uses_callback_evidence_labels() {
  const fs = require("fs");
  const js = fs.readFileSync(
    path.join(__dirname, "..", "..", "app", "static", "dashboard", "app.js"), "utf-8");
  for (const oldLabel of ["task未进分控", "中控未入队", "已入队等待", "正在执行",
                          "爬虫失败", "callback lag"]) {
    check("R2 旧 central 文案必须从页面移除:" + oldLabel,
          !js.includes('"' + oldLabel + '"'), oldLabel);
  }
  for (const newLabel of ["未发现爬虫推送记录", "已推送，暂无回调记录", "回调进行中",
                          "部分回调完成", "回调已完成，SaaS尚未推进"]) {
    check("R2 新回调链文案必须存在:" + newLabel, js.includes('"' + newLabel + '"'), newLabel);
  }
  check("R2 禁止出现未经验证的 PENDING_URL_SYNC",
        !js.includes("PENDING_URL_SYNC") && !js.includes("s_task_info"));
  // 阶段树渲染:新状态键生成对应 <li>
  const stages = {
    crawl_status_available: true, pool_counts_available: true,
    snapshot_consistent: true, snapshot_drift: [],
    pool_scope_total: 2, not_closed: 2, crawl_status_1: 2, crawl_status_2: 0,
    grace_seconds: 1800,
    crawler_branch: { TASK_NOT_CREATED: 1, CALLBACK_IN_FLIGHT: 1 },
    downstream_branch: {},
  };
  const html = sfTreeHtml(stages);
  check("R2 阶段树渲染新状态(未生成task/回调进行中)",
        html.includes("未生成task") && html.includes("回调进行中") &&
        html.includes("爬虫链路未完成:2"), html);
}

// ---- R2 Review P1-3:父节点标签语义(爬虫未回调 → 爬虫链路未完成) ----

function test_r2_review_p1_3_parent_label_no_contradiction() {
  const fs = require("fs");
  const js = fs.readFileSync(
    path.join(__dirname, "..", "..", "app", "static", "dashboard", "app.js"), "utf-8");
  check("P1-3 新父标签「爬虫链路未完成」必须存在", js.includes("爬虫链路未完成"));
  check("P1-3 旧父标签「爬虫未回调」不得再出现在页面主逻辑",
        !js.includes("爬虫未回调"));
  // 子状态 CALLBACK_DONE_SAAS_PENDING 挂在新父节点下不再自相矛盾
  const stages = {
    crawl_status_available: true, pool_counts_available: true,
    snapshot_consistent: true, snapshot_drift: [],
    pool_scope_total: 2, not_closed: 2, crawl_status_1: 2, crawl_status_2: 0,
    grace_seconds: 1800,
    crawler_branch: { CALLBACK_DONE_SAAS_PENDING: 2 },
    downstream_branch: {},
  };
  const html = sfTreeHtml(stages);
  check("P1-3 「回调已完成，SaaS尚未推进」归类于「爬虫链路未完成」父节点",
        html.includes("爬虫链路未完成:2") &&
        html.includes("回调已完成，SaaS尚未推进") &&
        !html.includes("爬虫未回调"), html);
}

// ---- 本机受控操作台 UI(2026-08-28):进度单元格 / 实时进度 / 路径防御 / action 客户端 ----
// live_progress 形状对齐 app/services/dashboard_projection.py + zhaoshang_progress_probe.py。

function lpFixture(overrides) {
  return Object.assign({
    available: true, classification: "REMOTE_TERMINAL",
    progress: { total: 18, pending: 0, running: 0, failed: 0, waiting_db: 0, finished: 18 },
    observed_at: "2026-08-28T10:00:00Z", stale: false, conflict: false, error_code: null,
  }, overrides || {});
}

function test_job_progress_cell() {
  const cells = {
    termOk: jobProgressCell({ live_progress: lpFixture(), crawl_total: 18 }),
    termFail: jobProgressCell({ live_progress: lpFixture(
      { progress: { total: 18, pending: 0, running: 0, failed: 1, waiting_db: 0, finished: 17 } }), crawl_total: 18 }),
    active: jobProgressCell({ live_progress: lpFixture(
      { classification: "REMOTE_ACTIVE",
        progress: { total: 18, pending: 10, running: 3, failed: 0, waiting_db: 0, finished: 5 } }), crawl_total: 18 }),
    nullProgress: jobProgressCell({ live_progress: lpFixture({ progress: null }), crawl_total: 3 }),
    probeFailed: jobProgressCell({ live_progress: lpFixture(
      { available: false, classification: null, progress: null, observed_at: null, error_code: "PROBE_FAILED" }),
      crawl_total: 4 }),
    probeDisabled: jobProgressCell({ live_progress: lpFixture(
      { available: false, classification: null, progress: null, observed_at: null, error_code: "PROBE_DISABLED" }),
      crawl_total: 18 }),
    conflict: jobProgressCell({ live_progress: lpFixture(
      { available: false, classification: null, progress: null, observed_at: null,
        conflict: true, error_code: "TASK_BATCH_CONFLICT" }), crawl_total: 18 }),
    notStarted: jobProgressCell({ live_progress: lpFixture(
      { available: false, classification: null, progress: null, observed_at: null, error_code: "PROBE_DISABLED" }),
      crawl_total: 0 }),
  };
  check("OPS TERMINAL 18/18 全成功", cells.termOk.text === "✓ 18/18", cells.termOk.text);
  check("OPS TERMINAL 17+1 失败", cells.termFail.text === "⚠ 18/18(失败 1)", cells.termFail.text);
  check("OPS ACTIVE 5/18", cells.active.text === "5/18", cells.active.text);
  check("OPS available 但 progress null", cells.nullProgress.text === "远端进度暂不可用", cells.nullProgress.text);
  check("OPS 非 PROBE_DISABLED 失败同样不可用", cells.probeFailed.text === "远端进度暂不可用", cells.probeFailed.text);
  check("OPS PROBE_DISABLED+crawl_total 显示本地采集数", cells.probeDisabled.text === "含 18 条采集", cells.probeDisabled.text);
  check("OPS conflict 重复文案", cells.conflict.text === "名称重复,无法安全确定", cells.conflict.text);
  check("OPS 全都无 → 未开始", cells.notStarted.text === "未开始", cells.notStarted.text);
  for (const key of Object.keys(cells)) {
    check("OPS 进度格绝无裸 —:" + key, cells[key].text !== "—", cells[key].text);
  }
}

function test_progress_detail() {
  const lp = lpFixture({ progress: { total: 18, pending: 0, running: 0, failed: 1, waiting_db: 0, finished: 17 } });
  const d = progressDetail(lp, 18, Date.parse("2026-08-28T10:00:05Z"));
  check("OPS DETAIL 六行 breakdown 存在",
        ["总数", "已完成", "执行中", "待执行", "待入库", "失败"]
          .every((k) => d.rows.some(([rk]) => rk === k)), JSON.stringify(d.rows));
  check("OPS DETAIL TERMINAL failed>0 含 失败 1",
        d.rows.some(([k, v]) => k === "失败" && v === 1), JSON.stringify(d.rows));
  check("OPS DETAIL 失败态不显示全成功文案", !d.summary.includes("全部完成"), d.summary);
  check("OPS DETAIL classification 中文", d.classCn === "已结束", d.classCn);
  check("OPS DETAIL observed X 秒前",
        d.observedText.includes("最后远端进度") && d.observedText.includes("秒前"), d.observedText);
  const staleD = progressDetail(Object.assign({}, lp, { stale: true }), 18, Date.parse("2026-08-28T10:00:05Z"));
  check("OPS DETAIL stale 附缓存标记", staleD.observedText.includes("(缓存)"), staleD.observedText);
  const conflictD = progressDetail(lpFixture(
    { available: false, classification: null, progress: null, observed_at: null, conflict: true }), 5);
  check("OPS DETAIL conflict 文案", conflictD.conflict && conflictD.text.includes("无法安全确定"), conflictD.text);
  const unD = progressDetail(lpFixture(
    { available: false, classification: null, progress: null, observed_at: null, error_code: "PROBE_DISABLED" }), 7);
  check("OPS DETAIL 不可用文案", unD.text === "远端进度暂不可用" && !unD.available, unD.text);
  check("OPS DETAIL 不可用附本地绑定数", unD.crawlTotal === 7, String(unD.crawlTotal));
}

function test_output_display_name() {
  check("OPS PATH 反斜杠取最后一段",
        outputDisplayName("E:\\a\\b\\saas_audit_final_20260828.xlsx") === "saas_audit_final_20260828.xlsx",
        outputDisplayName("E:\\a\\b\\saas_audit_final_20260828.xlsx"));
  check("OPS PATH 正斜杠取最后一段", outputDisplayName("/data/out/final.xlsx") === "final.xlsx");
  check("OPS PATH 短名透传",
        outputDisplayName("saas_audit_final_20260828.xlsx") === "saas_audit_final_20260828.xlsx");
  check("OPS PATH 空值安全", outputDisplayName("") === "" && outputDisplayName(null) === "");
}

function immediateTimers() {
  return { setTimeout: (fn) => { fn(); return 0; }, clearTimeout: () => {} };
}

async function test_action_client_post_dedup_and_poll() {
  const calls = [];
  const gates = [];
  const fetchJson = (url, opts) => {
    calls.push({ url, opts });
    const d = deferred();
    gates.push(d);
    return d.promise;
  };
  const client = createActionClient(Object.assign({ token: "tok-1", fetchJson }, immediateTimers()));
  const p1 = client.post("/dashboard/api/actions/closeout", {});
  const p2 = client.post("/dashboard/api/actions/closeout", {});
  check("OPS CLIENT 在途重复 post → duplicate:true", (await p2).duplicate === true);
  check("OPS CLIENT 在途重复只发一次 fetch", calls.length === 1, String(calls.length));
  await gates[0].resolve({ status: 202, json: { action_id: "a1", status: "RUNNING", poll: "/dashboard/api/actions/a1" } });
  await flush();
  await gates[1].resolve({ status: 200, json: { action_id: "a1", status: "RUNNING", outcome: null } });
  await flush();
  await gates[2].resolve({ status: 200, json: { action_id: "a1", status: "SUCCEEDED", outcome: { result_code: "FINALIZED" } } });
  const res = await p1;
  check("OPS CLIENT 轮询到 SUCCEEDED",
        res.status === "SUCCEEDED" && res.outcome && res.outcome.result_code === "FINALIZED", JSON.stringify(res));
  const pollCalls = calls.filter((c) => c.opts.method === "GET");
  check("OPS CLIENT poll 恰好两次(RUNNING→SUCCEEDED)", pollCalls.length === 2, String(pollCalls.length));
  // 完成后同一 path 可再次发起(in-flight 已清除)
  const p3 = client.post("/dashboard/api/actions/closeout", {});
  await flush();
  await gates[3].resolve({ status: 409, json: { detail: { error_code: "ACTION_IN_FLIGHT" } } });
  const r3 = await p3;
  check("OPS CLIENT 完成后同 path 不再 duplicate",
        r3.duplicate !== true && r3.error && r3.error.code === "ACTION_IN_FLIGHT", JSON.stringify(r3));
}

async function test_action_client_error_and_timeout() {
  const fetch403 = async () => ({ status: 403, json: { detail: { error_code: "INVALID_ACTION_TOKEN" } } });
  const client403 = createActionClient(Object.assign({ token: "t", fetchJson: fetch403 }, immediateTimers()));
  const err = await client403.post("/dashboard/api/actions/closeout", {});
  check("OPS CLIENT 非 2xx → error.code/status",
        err.error && err.error.code === "INVALID_ACTION_TOKEN" && err.error.status === 403, JSON.stringify(err));
  let polls = 0;
  const fetchRunning = async (url, opts) => {
    if (opts.method === "GET") { polls += 1; return { status: 200, json: { status: "RUNNING" } }; }
    return { status: 202, json: { action_id: "a2", status: "RUNNING", poll: "/p" } };
  };
  const clientTimeout = createActionClient(
    Object.assign({ token: "t", fetchJson: fetchRunning, maxPolls: 2 }, immediateTimers()));
  const timed = await clientTimeout.post("/x", {});
  check("OPS CLIENT 超时 maxPolls → TIMEOUT", timed.status === "TIMEOUT" && polls === 2,
        JSON.stringify(timed) + " polls=" + polls);
  let count = 0;
  const fetchOnce = async () => { count += 1; return { status: 200, json: { available: true, job_id: 1 } }; };
  const clientRefresh = createActionClient(Object.assign({ token: "t", fetchJson: fetchOnce }, immediateTimers()));
  const r = await clientRefresh.refresh("/dashboard/api/jobs/1/refresh-progress", {});
  check("OPS CLIENT refresh 单次不轮询", r.available === true && count === 1,
        JSON.stringify(r) + " count=" + count);
}

async function test_action_client_headers() {
  let seen = null;
  const fetchJson = async (url, opts) => { seen = opts; return { status: 403, json: { detail: { error_code: "X" } } }; };
  const client = createActionClient(Object.assign({ token: "tok-abc", fetchJson }, immediateTimers()));
  await client.post("/dashboard/api/actions/closeout", {});
  check("OPS CLIENT POST 头含 X-Dashboard-Action-Token",
        seen && seen.headers["X-Dashboard-Action-Token"] === "tok-abc", JSON.stringify(seen && seen.headers));
  check("OPS CLIENT POST 头含 Content-Type",
        seen && seen.headers["Content-Type"] === "application/json", JSON.stringify(seen && seen.headers));
}

function test_action_outcome_text() {
  check("OPS TEXT REEXPORT_COMPLETED 含条数",
        actionOutcomeText("re-export", { status: "SUCCEEDED", outcome: { outcome: "REEXPORT_COMPLETED", validated: 18 } })
          .includes("18 条"));
  check("OPS TEXT REEXPORT_COMPLETED_NO_RESULT 文案",
        actionOutcomeText("re-export", { status: "SUCCEEDED", outcome: { outcome: "REEXPORT_COMPLETED_NO_RESULT", validated: 0 } })
          === "任务已完成,本次导出 0 条,可稍后重试");
  check("OPS TEXT REEXPORT_STILL_RUNNING 文案",
        actionOutcomeText("re-export", { status: "SUCCEEDED", outcome: { outcome: "REEXPORT_STILL_RUNNING" } })
          === "招商局任务仍在执行,本次未取得最终导出");
  check("OPS TEXT REJECTED+REMOTE_PROGRESS_UNKNOWN 文案",
        actionOutcomeText("re-export", { status: "SUCCEEDED", outcome: { outcome: "REEXPORT_REJECTED", error_code: "REMOTE_PROGRESS_UNKNOWN" } })
          === "无法确认远端进度,未执行导出");
  check("OPS TEXT closeout BLOCKED 文案",
        actionOutcomeText("closeout", { status: "SUCCEEDED", outcome: { result_code: "BLOCKED", publication_blocked: "remote_manual" } })
          .includes("暂时不能收口"));
  check("OPS TEXT closeout durable 记录(无 result_code)也推导 BLOCKED",
        actionOutcomeText("closeout", { status: "SUCCEEDED", outcome: { outcome: "INCOMPLETE", publication_blocked: true } })
          .includes("暂时不能收口"));
  check("OPS TEXT closeout ALREADY_RUNNING 文案",
        actionOutcomeText("closeout", { status: "SUCCEEDED", outcome: { result_code: "ALREADY_RUNNING" } })
          .includes("未启动第二个"));
  check("OPS TEXT closeout ALREADY_FINALIZED 文案",
        actionOutcomeText("closeout", { status: "SUCCEEDED", outcome: { result_code: "ALREADY_FINALIZED" } })
          .includes("今日已完成最终出表"));
  check("OPS TEXT closeout FINALIZED 文案",
        actionOutcomeText("closeout", { status: "SUCCEEDED", outcome: { result_code: "FINALIZED" } })
          .includes("最终 Excel 已生成"));
  check("OPS TEXT closeout DEADLINE 文案",
        actionOutcomeText("closeout", { status: "SUCCEEDED", outcome: { result_code: "DEADLINE" } })
          .includes("下一次调度继续"));
  check("OPS TEXT closeout 409 ACTION_IN_FLIGHT 文案",
        actionOutcomeText("closeout", { error: { code: "ACTION_IN_FLIGHT", status: 409 } }) === "收口已在进行中");
  check("OPS TEXT closeout FAILED 附错误码",
        actionOutcomeText("closeout", { status: "FAILED", outcome: { result_code: "FAILED" } }).includes("收口失败"));
}

// ---- R2 Case 22(F3):LOGIN_RECOVERY_FAILED 不得隐藏重新登录按钮 ----

function test_r2_case22_login_required_covers_recovery_failure() {
  const btn = global.document.querySelector("#cnnc-auth-login");
  // payload 形状对齐后端 get_status 契约(login_required 由后端计算);
  // last_refresh_at 留空,避免 renderCnncAuth 走 fmtAgoAt/Date 路径
  const payload = (status, loginRequired) => ({
    status, last_error: "", last_refresh_at: "", login_required: loginRequired,
  });
  for (const status of ["SSO_LOGIN_REQUIRED", "LOGIN_RECOVERY_FAILED"]) {
    btn.style.display = "none";  // 每个断言前重置,防上一轮结果泄漏
    renderCnncAuth(payload(status, true));
    check("R2 Case 22 " + status + " → 重新登录按钮必须显示(不消失)",
          btn.style.display === "", btn.style.display);
  }
  for (const status of ["CNNC_AUTH_OK", "CNNC_AUTH_REFRESHED",
                        "OC_EXCHANGE_FAILED", "CNNC_WAF_CONTEXT_STALE"]) {
    btn.style.display = "none";  // 每个断言前重置(WAF 拦截≠需要登录)
    renderCnncAuth(payload(status, false));
    check("R2 Case 22 " + status + " → 重新登录按钮必须隐藏",
          btn.style.display === "none", btn.style.display);
  }
}

(async () => {
  await test_304_keeps_sync_fresh_without_rerender();
  await test_hidden_pauses_and_visible_resumes();
  await test_schedule_refuses_when_hidden();
  await test_no_duplicate_poll_chains();
  await test_offline_retry_then_recover();
  test_safe_progress_text();
  await test_sf_polls_independently_of_summary_304();
  await test_sf_hidden_pauses_and_visible_resumes();
  await test_sf_inflight_dedup();
  test_sf_partial_notice_three_states();
  test_sf_tree_shows_routing_evidence_incomplete();
  test_sf_growth_after_cutoff_text_not_normal();
  test_r2_crawler_tree_uses_callback_evidence_labels();
  test_r2_review_p1_3_parent_label_no_contradiction();
  test_job_progress_cell();
  test_progress_detail();
  test_output_display_name();
  await test_action_client_post_dedup_and_poll();
  await test_action_client_error_and_timeout();
  await test_action_client_headers();
  test_action_outcome_text();
  test_r2_case22_login_required_covers_recovery_failure();
  if (failed === 0) { console.log("ALL PASS"); process.exit(0); }
  console.error(failed + " failing checks");
  process.exit(1);
})();
