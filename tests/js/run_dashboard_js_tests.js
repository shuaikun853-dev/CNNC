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
// 覆盖 Dashboard Auth/Timezone/Closeout 修复(2026-09-02):
//   TZ    fmtShanghaiDateTime 按 Asia/Shanghai 展示 UTC ISO(不做 +8 算术);
//   CNNC  fetch 8s 超时(AbortController)→ 失败文案 → inFlight 释放 →
//         poller 继续排程 → 下次恢复;在途去重无双链;poller 兜底 catch 防断链;
//   CACHE index.html 磁盘契约(不手写 ?v=,版本由服务端 mtime 注入);
//   CLOSE closeoutRunning 统一权威:applyCloseoutRunningOverlay /
//         computeCloseoutBackendRunning / 收口按钮两态幂等 / summary 重刷不覆盖。
// 覆盖 Review Fix Round 2 Round 2(2026-09-02):P1-1/P1-2/P2-2 前端同源谓词
//   isCloseoutWriter(与后端 _closeout_writer_active 同语义:tick/harvest-only/
//   drain-only 持锁或 command 缺失都不算收口)、closeoutWriterRunning 并入
//   closeoutUIRunning、hydration 未见 settle 前按钮不得可点(summarySeen/
//   actionsSeen)、/api/actions 顶层 business_date 服务端权威兜底
//   (resolveCloseoutActionsDate)、index.html 静态初始态契约。
// 覆盖 Review Fix Round 3(2026-09-03):
//   P2-1  updateCloseoutUI 最高优先级 opsUnavailable 四态:operator-context
//         引导失败 → 按钮 disabled"操作台不可用",不再出现可点"立即收口";
//   P2-2  pollDelay 上海时区 closeout 监视窗口 [17:55, 20:35](含两端):窗口内
//         未 finalized 即使 idle 也 5s 轮询(显式 Asia/Shanghai 换算,不依赖本地
//         时区);writer active / computed_pending 判定优先级不变,finalized 恢复 idle。
// 被测对象:app/static/dashboard/app.js 暴露的 createPollController /
// createSaasFlowPoller / safeProgressText / fmtShanghaiDateTime /
// createCnncAuthFetcher / computeCloseoutBackendRunning / applyCloseoutRunningOverlay /
// isCloseoutWriter / resolveCloseoutActionsDate / pollDelay / inCloseoutWatchWindow。
const path = require("path");

// R2 Case 22:renderCnncAuth 前端测试需要的最小 document stub,必须在 require
// app.js 之前设置。按选择器缓存同一假元素(测试要断言 renderCnncAuth 写入的
// display),未知选择器返回 null;其他测试不用 document,不受影响。
// 2026-09-02:#closeout-btn 返回可变按钮对象(updateCloseoutUI 两态断言)。
const cnncFakeElements = new Map();
const cnncKnownSelectors = new Set(["#cnnc-auth-chips", "#cnnc-auth-meta", "#cnnc-auth-login", "#closeout-btn"]);
global.document = {
  addEventListener() {},
  querySelector(sel) {
    if (!cnncKnownSelectors.has(sel)) return null;
    if (!cnncFakeElements.has(sel)) {
      cnncFakeElements.set(sel, sel === "#cnnc-auth-login"
        ? { style: { display: "none" } }
        : sel === "#closeout-btn"
          ? { textContent: "立即收口", disabled: false, dataset: {} }
          : { textContent: "" });
    }
    return cnncFakeElements.get(sel);
  },
};

const { createPollController, createSaasFlowPoller, safeProgressText, sfPartialNotice,
        sfTreeHtml, sfGrowthText, jobProgressCell, progressDetail, outputDisplayName,
        createActionClient, actionOutcomeText, renderCnncAuth,
        fmtShanghaiDateTime, createCnncAuthFetcher, computeCloseoutBackendRunning,
        applyCloseoutRunningOverlay, isCloseoutWriter, resolveCloseoutActionsDate,
        updateCloseoutUI, pollDelay, inCloseoutWatchWindow, _setCloseoutStateForTest,
        cpStatusCn, cpStatusPill, cpRowMatchesFilter, cpSortRows } = require(
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

// ---- Dashboard Auth/Timezone/Closeout 修复(2026-09-02):F1-F9 ----

// F1:UTC/任意 offset ISO → 上海时区 "YYYY-MM-DD HH:mm:ss"(纯函数,无 +8 算术)
function test_fmt_shanghai_datetime() {
  check("TZ +00:00 → 上海 17:20:22",
        fmtShanghaiDateTime("2026-09-02T09:20:22+00:00") === "2026-09-02 17:20:22",
        fmtShanghaiDateTime("2026-09-02T09:20:22+00:00"));
  check("TZ Z 后缀 → 上海 17:20:22",
        fmtShanghaiDateTime("2026-09-02T09:20:22Z") === "2026-09-02 17:20:22",
        fmtShanghaiDateTime("2026-09-02T09:20:22Z"));
  check("TZ +08:00 原值即上海 → 17:20:22",
        fmtShanghaiDateTime("2026-09-02T17:20:22+08:00") === "2026-09-02 17:20:22",
        fmtShanghaiDateTime("2026-09-02T17:20:22+08:00"));
  check("TZ +05:30 非±8 offset 也按真实换算(非本地时区)",
        fmtShanghaiDateTime("2026-09-02T14:50:22+05:30") === "2026-09-02 17:20:22",
        fmtShanghaiDateTime("2026-09-02T14:50:22+05:30"));
  check("TZ null/undefined/空串/坏值 → —",
        fmtShanghaiDateTime(null) === "—" && fmtShanghaiDateTime(undefined) === "—" &&
        fmtShanghaiDateTime("") === "—" && fmtShanghaiDateTime("not-a-date") === "—");
}

function cnncOkPayload() {
  return { status: "CNNC_AUTH_OK", last_refresh_at: "", last_error: "", login_required: false };
}

// F2:CNNC auth 正常返回 → chips 恢复"状态 正常"
async function test_cnnc_fetcher_ok() {
  const chips = global.document.querySelector("#cnnc-auth-chips");
  chips.textContent = "状态 — · 检测中…";
  const fetcher = createCnncAuthFetcher({
    fetchImpl: async () => ({ ok: true, json: async () => cnncOkPayload() }),
  });
  await fetcher.run();
  check("CNNC 正常返回后 chips 显示 状态 正常", chips.textContent.includes("状态 正常"),
        chips.textContent);
  check("CNNC 正常返回后 inFlight 释放", fetcher.state.inFlight === false);
}

// F3:HTTP 500 / fetch reject → 失败文案;poller 链继续排程(不死亡)
async function test_cnnc_fetcher_failure_keeps_chain() {
  const chips = global.document.querySelector("#cnnc-auth-chips");
  const clock = makeClock();

  chips.textContent = "";
  const f500 = createCnncAuthFetcher({ fetchImpl: async () => ({ ok: false, status: 500 }) });
  await f500.run();
  check("CNNC HTTP 500 → 失败文案", chips.textContent.includes("CNNC 认证状态读取失败"),
        chips.textContent);

  chips.textContent = "";
  const fReject = createCnncAuthFetcher({
    fetchImpl: async () => { throw new Error("network down"); },
  });
  await fReject.run();
  check("CNNC fetch reject → 失败文案", chips.textContent.includes("CNNC 认证状态读取失败"),
        chips.textContent);

  // 失败被 fetcher 自吞 → poll reject 不外泄 → poller 必须继续排程(唯一一条链)
  const poller = createSaasFlowPoller({
    poll: fReject.run,
    visible: () => true,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    intervalMs: 30000,
  });
  poller.run();
  await flush();
  check("CNNC 失败 settle 后 poller 继续排程(clock.pending===1)", clock.pending() === 1,
        String(clock.pending()));
}

// F4(关键):永久 pending fetch → 8s 超时 abort → 失败文案 + inFlight 释放 +
// poller 继续排程;第二次 fetch 正常返回 → chips 恢复"状态 正常"(恢复闭环)
async function test_cnnc_fetcher_timeout_recovers() {
  const clock = makeClock();
  const chips = global.document.querySelector("#cnnc-auth-chips");
  let savedSignal = null;
  let calls = 0;
  const fetchImpl = (url, opts) => {
    calls += 1;
    if (calls === 1) {
      savedSignal = opts.signal;
      return new Promise((_, reject) => {
        savedSignal.addEventListener("abort", () => reject(new Error("AbortError")));
      });
    }
    return Promise.resolve({ ok: true, json: async () => cnncOkPayload() });
  };
  const fetcher = createCnncAuthFetcher({
    fetchImpl,
    timers: { setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout },
    timeoutMs: 8000,
  });
  chips.textContent = "";
  fetcher.run();
  await flush();
  check("CNNC 永久 pending:超时前 fetch 只发一次且 8s timer 在挂",
        calls === 1 && clock.pending() === 1 && fetcher.state.inFlight === true &&
        chips.textContent === "",
        JSON.stringify({ calls, pending: clock.pending(), chips: chips.textContent }));
  clock.advance(7999);
  await flush();
  check("CNNC 7999ms:尚未 abort、仍在途、无失败文案",
        savedSignal.aborted === false && fetcher.state.inFlight === true &&
        !chips.textContent.includes("读取失败"), savedSignal.aborted + "/" + chips.textContent);
  clock.advance(1);
  await flush();
  check("CNNC 8000ms:abort 触发 → 失败文案 + inFlight 释放",
        savedSignal.aborted === true && chips.textContent.includes("读取失败") &&
        fetcher.state.inFlight === false,
        savedSignal.aborted + "/" + chips.textContent);
  check("CNNC 超时后独立 fetcher 不自挂 timer", clock.pending() === 0, String(clock.pending()));

  const poller = createSaasFlowPoller({
    poll: fetcher.run,
    visible: () => true,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    intervalMs: 30000,
  });
  poller.run();
  await flush();
  check("CNNC 失败后 poller 立即发起第二次 fetch 且只排一条链",
        calls === 2 && clock.pending() === 1,
        JSON.stringify({ calls, pending: clock.pending() }));
  clock.advance(30000);
  await flush();
  check("CNNC 第二次正常返回 → chips 恢复 状态 正常(闭环)",
        chips.textContent.includes("状态 正常"), chips.textContent);
  check("CNNC 恢复后仍只有一条轮询链", clock.pending() === 1, String(clock.pending()));
}

// F5:在途去重无双链(poller 连续触发 + fetcher 直调 + onVisible 都不算新链)
async function test_cnnc_fetcher_inflight_dedup() {
  const clock = makeClock();
  let fetchCalls = 0;
  const gates = [];
  const fetchImpl = () => { fetchCalls += 1; const d = deferred(); gates.push(d); return d.promise; };
  const fetcher = createCnncAuthFetcher({
    fetchImpl,
    timers: { setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout },
    timeoutMs: 600000,
  });
  const poller = createSaasFlowPoller({
    poll: fetcher.run,
    visible: () => true,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    intervalMs: 30000,
  });
  poller.run();
  poller.run();          // 在途时重复触发
  fetcher.run();         // fetcher 直调(在途直接忽略)
  poller.onVisible();    // 回前台补刷(在途忽略)
  await flush();
  check("CNNC 在途期间各种触发都去重:fetch 恰好 1 次", fetchCalls === 1, String(fetchCalls));
  await gates[0].resolve({ ok: true, json: async () => cnncOkPayload() });
  await flush();
  check("CNNC 完成后仅排一条链", clock.pending() === 1, String(clock.pending()));
}

// F6:poller 兜底 catch——poll 直接 reject 也不断链
async function test_sf_poller_broken_poll_still_schedules() {
  const clock = makeClock();
  const poller = createSaasFlowPoller({
    poll: async () => { throw new Error("poll exploded"); },
    visible: () => true,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    intervalMs: 30000,
  });
  poller.run();
  await flush();
  check("POLLER poll reject 后仍继续排程(clock.pending===1)", clock.pending() === 1,
        String(clock.pending()));
}

// F7:index.html 磁盘契约——不手写 ?v=(版本由服务端 mtime 注入)
function test_index_html_disk_contract() {
  const fs = require("fs");
  const html = fs.readFileSync(
    path.join(__dirname, "..", "..", "app", "static", "dashboard", "index.html"), "utf-8");
  check("CACHE 磁盘 index.html 引用 /dashboard/static/app.js",
        html.includes('src="/dashboard/static/app.js'), "missing src hook");
  check("CACHE 磁盘 index.html 引用 /dashboard/static/app.css",
        html.includes('href="/dashboard/static/app.css'), "missing href hook");
  check("CACHE 磁盘 index.html 不得手写 ?v=", !html.includes("?v="), "found ?v= on disk");
}

// F8:Bug3/4 closeoutRunning——纯函数覆盖 + 当天 RUNNING 判定 + 按钮两态
function closeoutStageFixture(stateWord, freezeText) {
  return [{ key: "closeout", name: "最终收口", state: stateWord === "✓ 完成" ? "done"
            : (stateWord === "● 进行中" ? "cur" : "wait"),
            state_word: stateWord, num: "18:00 口径",
            sub: [["数据冻结", freezeText]] }];
}

function test_closeout_running_seam() {
  const wait = closeoutStageFixture("○ 等待", "未开始(等待 18:00)");
  const frozen = closeoutStageFixture("● 进行中", "已冻结");
  const done = closeoutStageFixture("✓ 完成", "已完成");

  const overlaid = applyCloseoutRunningOverlay(wait, { closeoutRunning: true });
  check("CLOSE wait+running → cur/● 正在收口",
        overlaid[0].state === "cur" && overlaid[0].state_word === "● 正在收口",
        JSON.stringify(overlaid));
  check("CLOSE wait+running → 数据冻结=收口任务已启动，正在准备",
        overlaid[0].sub[0][1] === "收口任务已启动，正在准备", JSON.stringify(overlaid[0].sub));
  const json = JSON.stringify(overlaid);
  check("CLOSE wait+running 输出绝无 ○ 等待/未开始 残留",
        !json.includes("○ 等待") && !json.includes("未开始"), json);

  const overlaidFrozen = applyCloseoutRunningOverlay(frozen, { closeoutRunning: true });
  check("CLOSE frozen+running → 数据冻结=已冻结，正在收口",
        overlaidFrozen[0].sub[0][1] === "已冻结，正在收口" &&
        overlaidFrozen[0].state === "cur", JSON.stringify(overlaidFrozen));

  const doneUntouched = applyCloseoutRunningOverlay(done, { closeoutRunning: true });
  check("CLOSE finalized(done)+running 不改写",
        doneUntouched[0].state === "done" && doneUntouched[0].state_word === "✓ 完成" &&
        doneUntouched[0].sub[0][1] === "已完成", JSON.stringify(doneUntouched));

  const notRunning = applyCloseoutRunningOverlay(wait, { closeoutRunning: false });
  check("CLOSE running=false 原样保留(○ 等待/未开始)",
        notRunning[0].state === "wait" && notRunning[0].sub[0][1] === "未开始(等待 18:00)",
        JSON.stringify(notRunning));

  const original = closeoutStageFixture("○ 等待", "未开始(等待 18:00)");
  applyCloseoutRunningOverlay(original, { closeoutRunning: true });
  check("CLOSE overlay 不 mutate 入参",
        original[0].state === "wait" && original[0].sub[0][1] === "未开始(等待 18:00)",
        JSON.stringify(original));

  const running = [{ action_type: "closeout", status: "RUNNING",
                     business_date: "2026-09-02", target: "2026-09-02:-" }];
  check("CLOSE 当天 closeout RUNNING → true",
        computeCloseoutBackendRunning(running, "2026-09-02") === true);
  check("CLOSE 空 running 列表 → false",
        computeCloseoutBackendRunning([], "2026-09-02") === false);
  check("CLOSE business_date 缺失但 target 前缀匹配 → true",
        computeCloseoutBackendRunning(
          [{ action_type: "closeout", status: "RUNNING", target: "2026-09-02:-" }],
          "2026-09-02") === true);
  check("CLOSE 别的日期 → false",
        computeCloseoutBackendRunning(running, "2026-09-01") === false);
  check("CLOSE 非 closeout 类型 → false",
        computeCloseoutBackendRunning(
          [{ action_type: "re-export", status: "RUNNING", business_date: "2026-09-02",
             target: "2026-09-02:7" }], "2026-09-02") === false);
  check("CLOSE status 非 RUNNING → false",
        computeCloseoutBackendRunning(
          [{ action_type: "closeout", status: "SUCCEEDED", business_date: "2026-09-02",
             target: "2026-09-02:-" }], "2026-09-02") === false);

  // 按钮两态(updateCloseoutUI 幂等;权威状态经测试 seam 驱动,与生产同源;
  // seen 缺省 true——旧式只关心 running 的断言不必每次都传)
  const btn = global.document.querySelector("#closeout-btn");
  _setCloseoutStateForTest({ actionInFlight: false, backendRunning: false });
  btn.disabled = false; btn.textContent = "立即收口";
  updateCloseoutUI();
  check("CLOSE 空闲态:按钮 enabled+立即收口",
        btn.disabled === false && btn.textContent === "立即收口",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ actionInFlight: true, backendRunning: false });
  updateCloseoutUI();
  check("CLOSE 本页在途:按钮 disabled+收口中…",
        btn.disabled === true && btn.textContent === "收口中…",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ actionInFlight: true, backendRunning: true });
  updateCloseoutUI();
  check("CLOSE 本页在途+后端 RUNNING:仍 disabled+收口中…",
        btn.disabled === true && btn.textContent === "收口中…",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ actionInFlight: false, backendRunning: true });
  updateCloseoutUI();
  check("CLOSE 仅后端 RUNNING(F5 恢复场景):disabled+收口中…",
        btn.disabled === true && btn.textContent === "收口中…",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ actionInFlight: false, backendRunning: false });
  updateCloseoutUI();
  check("CLOSE 终态后恢复:enabled+立即收口",
        btn.disabled === false && btn.textContent === "立即收口",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ actionInFlight: false, backendRunning: false });
}

// F9(Test C 语义):5s summary 重刷反复重渲,overlay 必须每轮都生效,不打回"○ 等待"
function test_closeout_overlay_survives_summary_rerenders() {
  let allCur = true;
  for (let round = 0; round < 3; round += 1) {
    const fresh = closeoutStageFixture("○ 等待", "未开始(等待 18:00)");
    const rendered = applyCloseoutRunningOverlay(fresh, { closeoutRunning: true });
    if (rendered[0].state !== "cur" || rendered[0].state_word !== "● 正在收口" ||
        rendered[0].sub[0][1] !== "收口任务已启动，正在准备") {
      allCur = false;
    }
  }
  check("CLOSE 3 轮 summary 重刷每轮都是 ● 正在收口(不被打回等待)", allCur);
}

// R2 Round 2(2026-09-02):P1-1 前端同源谓词 / P1-2 writerRunning 并入权威 /
// P2-2 hydration 防护 / actions 顶层 business_date 兜底 / index.html 初始契约。
function test_closeout_r2_round2() {
  // ---- isCloseoutWriter 矩阵(与后端 _closeout_writer_active 完全同语义) ----
  check("R2R2 tick 持锁 → false", isCloseoutWriter({ active: true, command: "tick" }) === false);
  check("R2R2 harvest-only 持锁 → false",
        isCloseoutWriter({ active: true, command: "harvest-only" }) === false);
  check("R2R2 drain-only 持锁 → false",
        isCloseoutWriter({ active: true, command: "drain-only" }) === false);
  check("R2R2 closeout 持锁 → true", isCloseoutWriter({ active: true, command: "closeout" }) === true);
  check("R2R2 finalize 持锁 → true", isCloseoutWriter({ active: true, command: "finalize" }) === true);
  check("R2R2 active=false + closeout → false",
        isCloseoutWriter({ active: false, command: "closeout" }) === false);
  check("R2R2 active 但缺 command → false", isCloseoutWriter({ active: true }) === false);
  check("R2R2 null/undefined → false",
        isCloseoutWriter(null) === false && isCloseoutWriter(undefined) === false);

  // ---- P1-2:closeoutWriterRunning 并入 closeoutUIRunning(经 seam + updateCloseoutUI) ----
  const btn = global.document.querySelector("#closeout-btn");
  // a. scheduler closeout(writer 权威)持锁:即使 action 权威为空也必须"收口中…"
  _setCloseoutStateForTest({ writerRunning: true, actionInFlight: false, backendRunning: false });
  btn.disabled = false; btn.textContent = "立即收口";
  updateCloseoutUI();
  check("R2R2 scheduler closeout writer 持锁 → disabled+收口中…(actions 为空也禁用)",
        btn.disabled === true && btn.textContent === "收口中…",
        btn.disabled + "/" + btn.textContent);
  // b. 普通 tick 持锁:不得误判收口中
  _setCloseoutStateForTest({ writerRunning: false });
  updateCloseoutUI();
  check("R2R2 普通 tick(writerRunning=false)→ enabled+立即收口(不误判收口中)",
        btn.disabled === false && btn.textContent === "立即收口",
        btn.disabled + "/" + btn.textContent);
  // c. Dashboard action 内存丢失(进程重启)但 writer closeout 持锁:仍禁用
  _setCloseoutStateForTest({ writerRunning: true, backendRunning: false, actionInFlight: false });
  updateCloseoutUI();
  check("R2R2 action 内存丢失但 writer closeout → disabled+收口中…",
        btn.disabled === true && btn.textContent === "收口中…",
        btn.disabled + "/" + btn.textContent);
  // d. Dashboard action RUNNING(经典后端权威路径)→ disabled
  _setCloseoutStateForTest({ backendRunning: true });
  updateCloseoutUI();
  check("R2R2 Dashboard action RUNNING → disabled+收口中…",
        btn.disabled === true && btn.textContent === "收口中…",
        btn.disabled + "/" + btn.textContent);

  // ---- P2-2 hydration:settle 前按钮不得出现可点击"立即收口" ----
  _setCloseoutStateForTest({ summarySeen: false, actionsSeen: true });
  btn.disabled = false; btn.textContent = "立即收口";
  updateCloseoutUI();
  check("R2R2 summary 未见 settle → disabled+检查收口状态…",
        btn.disabled === true && btn.textContent === "检查收口状态…",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ summarySeen: true, actionsSeen: false });
  updateCloseoutUI();
  check("R2R2 actions 未见 settle → disabled+检查收口状态…",
        btn.disabled === true && btn.textContent === "检查收口状态…",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ summarySeen: true, actionsSeen: true });
  updateCloseoutUI();
  check("R2R2 双侧 settle 且无 running → enabled+立即收口",
        btn.disabled === false && btn.textContent === "立即收口",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({ summarySeen: false, actionsSeen: false });
  updateCloseoutUI();
  check("R2R2 双侧都未 settle → disabled+检查收口状态…",
        btn.disabled === true && btn.textContent === "检查收口状态…",
        btn.disabled + "/" + btn.textContent);

  // ---- resolveCloseoutActionsDate:/api/actions 顶层 business_date 服务端权威 ----
  check("R2R2 actions 顶层日期恢复 bootstrap 失败场景",
        resolveCloseoutActionsDate({ business_date: "2026-09-02" }, "") === "2026-09-02");
  check("R2R2 服务端权威优先于 operator-context 兜底",
        resolveCloseoutActionsDate({ business_date: "2026-09-02" }, "2026-09-01") === "2026-09-02");
  check("R2R2 actions 无日期时回退 bootstrap",
        resolveCloseoutActionsDate(null, "2026-09-02") === "2026-09-02");
  check("R2R2 都无日期 → 空串(拒绝匹配)",
        resolveCloseoutActionsDate(null, "") === "");

  // ---- index.html 静态初始契约(JS 加载前不得出现可点击"立即收口") ----
  const fs = require("fs");
  const html = fs.readFileSync(
    path.join(__dirname, "..", "..", "app", "static", "dashboard", "index.html"), "utf-8");
  check("R2R2 index.html closeout-btn 初始 disabled",
        html.includes('id="closeout-btn" disabled'), "missing disabled attr");
  check("R2R2 index.html closeout-btn 初始文案 检查收口状态…",
        html.includes("检查收口状态…"), "missing initial text");
  // 结束前复位 seam(不影响后续/既有断言)
  _setCloseoutStateForTest({ actionInFlight: false, backendRunning: false,
                             writerRunning: false, summarySeen: false, actionsSeen: false });
}

// R3 P2-1(2026-09-03):opsUnavailable 最高优先级——operator-context 引导失败时
// 按钮必须 disabled"操作台不可用",不得出现可点"立即收口"。
function test_closeout_r3_ops_unavailable() {
  const btn = global.document.querySelector("#closeout-btn");
  // bootstrapOperatorContext 失败(#closeout-status 已写"操作台不可用"):按钮同步。
  _setCloseoutStateForTest({ opsUnavailable: true, summarySeen: true, actionsSeen: true });
  btn.disabled = false; btn.textContent = "立即收口";
  updateCloseoutUI();
  check("R3 opsUnavailable → disabled+操作台不可用(不出现可点立即收口)",
        btn.disabled === true && btn.textContent === "操作台不可用",
        btn.disabled + "/" + btn.textContent);
  // 收口运行态并存时仍"操作台不可用":按钮语义=人工发起面,此时必不可能发起;
  // 运行态由阶段卡 overlay 传达,disabled 已保证不可点。
  _setCloseoutStateForTest({ opsUnavailable: true, writerRunning: true });
  updateCloseoutUI();
  check("R3 opsUnavailable+writerRunning → disabled+操作台不可用(运行态由阶段卡传达)",
        btn.disabled === true && btn.textContent === "操作台不可用",
        btn.disabled + "/" + btn.textContent);
  // 回归:opsUnavailable=false 时既有三态语义不变。
  _setCloseoutStateForTest({ opsUnavailable: false, writerRunning: true });
  updateCloseoutUI();
  check("R3 回归 opsUnavailable=false+writerRunning → disabled+收口中…",
        btn.disabled === true && btn.textContent === "收口中…",
        btn.disabled + "/" + btn.textContent);
  _setCloseoutStateForTest({});   // seen 缺省 true
  updateCloseoutUI();
  check("R3 回归 空状态(seen 缺省 true)→ enabled+立即收口",
        btn.disabled === false && btn.textContent === "立即收口",
        btn.disabled + "/" + btn.textContent);
  // 结束前复位 seam
  _setCloseoutStateForTest({});
}

// R3 P2-2(2026-09-03):pollDelay 上海时区 closeout 监视窗口 [17:55, 20:35]。
// 时间输入一律用带 offset 的 ISO,证明走 offset 换算而非本地时区;ACTIVE_MS /
// IDLE_MS 未导出,以"断言返回值 === 5000/30000"同时锁定常量未变。
function test_closeout_r3_poll_watch_window() {
  const idle = { status: { production_writer: { active: false, command: "tick" },
                           final: { finalized: false, computed_pending: 0 } } };
  // 窗口边界(上海分钟闭区间,含两端):
  check("R3 窗口前 17:54:59 上海 → IDLE 30000",
        pollDelay(idle, Date.parse("2026-09-02T09:54:59Z")) === 30000);
  check("R3 窗口起点 17:55:00 上海 → ACTIVE 5000(起点含端)",
        pollDelay(idle, Date.parse("2026-09-02T09:55:00Z")) === 5000);
  check("R3 窗口内 18:00 上海 → ACTIVE 5000",
        pollDelay(idle, Date.parse("2026-09-02T10:00:00Z")) === 5000);
  check("R3 窗口终点 20:35 上海 → ACTIVE 5000(闭区间含端)",
        pollDelay(idle, Date.parse("2026-09-02T12:35:00Z")) === 5000);
  check("R3 窗口后 20:36 上海 → IDLE 30000",
        pollDelay(idle, Date.parse("2026-09-02T12:36:00Z")) === 30000);
  // 非上海 offset 表达同一时刻(=09:55Z=17:55 上海):证明按 Asia/Shanghai 换算,
  // 不依赖浏览器/系统本地时区。
  check("R3 -07:00 offset 同一时刻 17:55 上海 → ACTIVE 5000",
        pollDelay(idle, Date.parse("2026-09-02T02:55:00-07:00")) === 5000);
  // 跨天:16:00Z 是上海次日 00:00,不在窗口。
  check("R3 跨天窗口外 上海 00:00 → IDLE 30000",
        pollDelay(idle, Date.parse("2026-09-02T16:00:00Z")) === 30000);
  // finalized + 窗口内 → IDLE:收口已出版,无需监视,恢复 idle(监视只为尽早发现
  // 自动收口启动;已终态后 5s 轮询纯属浪费)。
  const fin = { status: { production_writer: { active: false },
                          final: { finalized: true, computed_pending: 0 } } };
  check("R3 finalized+窗口内 → IDLE 30000(收口已出版恢复 idle)",
        pollDelay(fin, Date.parse("2026-09-02T10:00:00Z")) === 30000);
  // 回归:writer active / computed_pending 原判定优先级不变(窗口外仍 ACTIVE)。
  const activeWriter = { status: { production_writer: { active: true, command: "tick" },
                                   final: { finalized: false, computed_pending: 0 } } };
  check("R3 回归 writer.active=true 窗口外(03:00 上海)→ ACTIVE 5000",
        pollDelay(activeWriter, Date.parse("2026-09-01T19:00:00Z")) === 5000);
  const pending = { status: { production_writer: { active: false, command: "tick" },
                              final: { finalized: false, computed_pending: 3 } } };
  check("R3 回归 computed_pending>0 未 finalized 窗口外 → ACTIVE 5000",
        pollDelay(pending, Date.parse("2026-09-01T19:00:00Z")) === 5000);
  // inCloseoutWatchWindow 直测(导出即测试面):边界含端 + 无参走 Date.now()。
  check("R3 inCloseoutWatchWindow 边界含端",
        inCloseoutWatchWindow(Date.parse("2026-09-02T09:55:00Z")) === true &&
        inCloseoutWatchWindow(Date.parse("2026-09-02T12:35:00Z")) === true &&
        inCloseoutWatchWindow(Date.parse("2026-09-02T09:54:59Z")) === false &&
        inCloseoutWatchWindow(Date.parse("2026-09-02T12:36:00Z")) === false);
  check("R3 inCloseoutWatchWindow 无参不抛(默认 Date.now())",
        typeof inCloseoutWatchWindow() === "boolean");
}

// ---- 客户价可观测性(2026-09-03,plan §8/§9.E):状态映射 + 过滤/排序纯函数 ----
function test_customer_price_seams() {
  check("CP 状态中文映射:READY→✓ 已获取 / PENDING→● 获取中 / RETRYING→↻ 待重试",
        cpStatusCn("READY") === "✓ 已获取" && cpStatusCn("PENDING") === "● 获取中" &&
        cpStatusCn("RETRYING") === "↻ 待重试");
  check("CP 状态中文映射:MISSING 与 MISSING_PROVEN 同为 ⚠ 客户价缺失;FAILED/INVALID → ✕ 获取失败",
        cpStatusCn("MISSING") === "⚠ 客户价缺失" && cpStatusCn("MISSING_PROVEN") === "⚠ 客户价缺失" &&
        cpStatusCn("FAILED") === "✕ 获取失败" && cpStatusCn("INVALID") === "✕ 获取失败");
  check("CP 未知状态透传原文,空值显示 —(esc 由调用方负责)",
        cpStatusCn("SOMETHING_NEW") === "SOMETHING_NEW" && cpStatusCn("") === "—" &&
        cpStatusCn(null) === "—");
  check("CP 状态 pill 配色:ok/run/warn/err 分桶",
        cpStatusPill("READY") === "ok" && cpStatusPill("PENDING") === "run" &&
        cpStatusPill("RETRYING") === "run" && cpStatusPill("MISSING_PROVEN") === "warn" &&
        cpStatusPill("MISSING") === "err" && cpStatusPill("FAILED") === "err" &&
        cpStatusPill("INVALID") === "err" && cpStatusPill("OTHER") === "wait");
  const rows = [
    { id: "a", state: "READY" }, { id: "b", state: "MISSING" }, { id: "c", state: "PENDING" },
    { id: "d", state: "MISSING_PROVEN" }, { id: "e", state: "RETRYING" },
    { id: "f", state: "INVALID" }, { id: "g", state: "FAILED" }];
  const sorted = cpSortRows(rows);
  check("CP 排序:未完成(获取中→待重试→缺失→失败→无效)在前,证据缺席次之,已获取最后",
        JSON.stringify(sorted.map((r) => r.id)) ===
        JSON.stringify(["c", "e", "b", "g", "f", "d", "a"]));
  check("CP 排序不 mutate 入参,未知状态兜底排最前(先处理)",
        rows[0].id === "a" &&
        cpSortRows([{ id: "x", state: "MYSTERY" }, { id: "y", state: "READY" }])[0].id === "x" &&
        cpSortRows(null).length === 0);
  check("CP 过滤 unacquired:排除 READY 与 MISSING_PROVEN,其余全含",
        ["a", "b", "c", "d", "e", "f", "g"].filter((id) =>
          cpRowMatchesFilter(rows.find((r) => r.id === id), "unacquired")).join(",") ===
        "b,c,e,f,g");
  check("CP 过滤 retrying / terminal:语义与后端 project_customer_price 一致",
        rows.filter((r) => cpRowMatchesFilter(r, "retrying")).map((r) => r.id).join(",") === "e" &&
        rows.filter((r) => cpRowMatchesFilter(r, "terminal")).map((r) => r.id).join(",") === "b,f,g");
  check("CP 过滤 all 与未知值防御为全部(含异常入参不抛)",
        rows.every((r) => cpRowMatchesFilter(r, "all")) &&
        rows.every((r) => cpRowMatchesFilter(r, ";drop")) &&
        cpRowMatchesFilter(null, "all") === true);
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
  test_fmt_shanghai_datetime();
  await test_cnnc_fetcher_ok();
  await test_cnnc_fetcher_failure_keeps_chain();
  await test_cnnc_fetcher_timeout_recovers();
  await test_cnnc_fetcher_inflight_dedup();
  await test_sf_poller_broken_poll_still_schedules();
  test_index_html_disk_contract();
  test_closeout_running_seam();
  test_closeout_overlay_survives_summary_rerenders();
  test_closeout_r2_round2();
  test_closeout_r3_ops_unavailable();
  test_closeout_r3_poll_watch_window();
  test_customer_price_seams();
  if (failed === 0) { console.log("ALL PASS"); process.exit(0); }
  console.error(failed + " failing checks");
  process.exit(1);
})();
