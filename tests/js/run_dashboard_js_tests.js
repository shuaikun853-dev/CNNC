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
const { createPollController, createSaasFlowPoller, safeProgressText, sfPartialNotice,
        sfTreeHtml, sfGrowthText, sfChipsText, sfGrowthHistoryText,
        formatShanghaiDateTime } = require(
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
    closed_standard: 0, closed_no_same: 0,
    closed_total: 0, pool_state_other: 0,
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
  // R3:路由证据不足移入"未最终收口"子树,不再挤在头部单行
  check("P1 routing_evidence_incomplete=3 时阶段树必须可见该分类与数量",
        html.includes("路由证据不足") && html.includes("3"), html);
  check("P1 文案必须体现证据不足语义(同商品存在多条采集记录/无法可靠确定当前记录)",
        html.includes("同商品存在多条采集记录") || html.includes("无法可靠确定"), html);
  check("P1 不得把 3 加进正常分支计数",
        html.includes("爬虫链路未完成:1") && html.includes("爬虫已回调:1") &&
        !html.includes("爬虫链路未完成:4") && !html.includes("爬虫已回调:4"), html);
  check("P1 routing 文案不得误标为系统错误/数据丢失/爬虫失败/未生成任务",
        !html.includes("系统错误") && !html.includes("数据丢失") &&
        !html.includes("爬虫失败") && !html.includes("未生成任务"), html);
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

// ---- R3 BUG A:顶部实时 chips / 历史观察分离 / 上海时区显示 ----

function test_r3_chips_use_live_counts_not_growth_snapshot() {
  const live = { available: true, customer_count: 66, saas_count: 0,
                 observed_at: "2026-08-28T06:30:00+00:00" };
  const growth = { available: true, evaluated: true, evaluation_reason: "EVALUATED",
                   state: "LAG_WATCH", customer_count: 46, saas_count: 0,
                   observation_count: 6, last_observed_at: "2026-08-28T06:00:05+00:00",
                   customer_delta: 3, saas_delta: 0 };
  const chips = sfChipsText(live, growth, null);
  check("R3 A8 chips 显示客户实时 66", chips.includes("客户实时 66"), chips);
  check("R3 A8 chips 显示 SaaS实时 0", chips.includes("SaaS实时 0"), chips);
  check("R3 A8 chips 不得把历史 46 冒充实时",
        !chips.includes("客户实时 46") && !chips.includes("客户 46"), chips);
  const hist = sfGrowthHistoryText(growth);
  check("R3 A8 历史说明仍显示最近权威观察 46", hist.includes("客户 46"), hist);
  check("R3 A10 历史观察时间按上海显示 14:00:05,不再显示 06:00:05",
        hist.includes("14:00:05") && !hist.includes("06:00:05"), hist);
  const unavailable = sfChipsText(
    { available: false, customer_count: null, saas_count: null, observed_at: null },
    growth, null);
  check("R3 A9 live 不可用显示暂不可用,绝不 fallback 到历史 46",
        unavailable.includes("客户实时 暂不可用") &&
        unavailable.includes("SaaS实时 暂不可用") &&
        !unavailable.includes("客户实时 46"), unavailable);
}

function test_r3_format_shanghai_date_time() {
  check("R3 A10 UTC ISO → 上海 2026-08-28 14:00:05",
        formatShanghaiDateTime("2026-08-28T06:00:05+00:00") === "2026-08-28 14:00:05",
        formatShanghaiDateTime("2026-08-28T06:00:05+00:00"));
  check("R3 已带 +08 偏移的时间不得二次偏移",
        formatShanghaiDateTime("2026-08-28T00:00:05+08:00") === "2026-08-28 00:00:05",
        formatShanghaiDateTime("2026-08-28T00:00:05+08:00"));
  check("R3 空/非法输入返回占位",
        formatShanghaiDateTime("") === "—" &&
        formatShanghaiDateTime("not-a-date") === "—" &&
        formatShanghaiDateTime(null) === "—");
}

// ---- R3 BUG B:商品池数量视觉闭合 ----

function r3PoolStagesFixture(overrides) {
  return Object.assign({
    crawl_status_available: true, pool_counts_available: true,
    snapshot_consistent: true, snapshot_drift: [],
    pool_scope_total: 71, not_closed: 50, closed_standard: 15, closed_no_same: 6,
    closed_total: 21, pool_state_other: 0,
    routing_open_total: 50, no_commodity_list: 1,
    crawl_status_1: 0, crawl_status_2: 49, crawl_status_other: 0,
    routing_evidence_incomplete: 0, grace_seconds: 1800,
    crawler_branch: {}, downstream_branch: {},
  }, overrides || {});
}

function test_r3_pool_closure_tree_normal() {
  const html = sfTreeHtml(r3PoolStagesFixture());
  check("R3 B1 同时显示 71 / 21 / 50 三个数量",
        html.includes("当日商品池记录:71") && html.includes("已最终收口:21") &&
        html.includes("未最终收口:50"), html);
  check("R3 B2 未收口子树显示 1 / 0 / 49",
        html.includes("未进入采集列表</span><b>1</b>") &&
        html.includes("爬虫链路未完成</span><b>0</b>") &&
        html.includes("爬虫已回调</span><b>49</b>"), html);
  check("R3 正常闭合时不出现其他池状态行", !html.includes("其他池状态"), html);
}

function test_r3_pool_closure_other_and_unknown_visible() {
  const html = sfTreeHtml(r3PoolStagesFixture({
    closed_total: 20, pool_state_other: 1,
    crawl_status_2: 46, crawl_status_other: 2, routing_evidence_incomplete: 1,
  }));
  check("R3 B5 未知池状态单独可见,不并入已最终收口",
        html.includes("其他池状态:1") && html.includes("已最终收口:20") &&
        !html.includes("已最终收口:21"), html);
  check("R3 B3 crawl_status_other 必须可见",
        html.includes("其他采集状态</span><b>2</b>"), html);
  check("R3 B4 routing_evidence_incomplete 必须可见",
        html.includes("路由证据不足") && html.includes("同商品存在多条采集记录"), html);
}

function test_r3_pool_routing_drift_no_fake_closure() {
  const html = sfTreeHtml(r3PoolStagesFixture({
    snapshot_drift: ["pool_vs_routing"], routing_open_total: 49,
  }));
  check("R3 B6 drift 时显示阶段分布基数与数据变化提示",
        html.includes("阶段分布基数:49") && html.includes("暂不强制对账"), html);
  check("R3 B6 drift 时仍显示商品池侧 50,不硬凑闭合",
        html.includes("未最终收口:50"), html);
}

function test_r3_round2_contract_preserved_in_new_tree() {
  // B7:新布局不得破坏 Round2 契约(detail 树父标签 + 闭合语义不变)
  const stages = r3PoolStagesFixture({
    crawl_status_1: 2, crawl_status_2: 0, routing_open_total: 3, not_closed: 3,
    pool_scope_total: 3,
    crawler_branch: { CALLBACK_DONE_SAAS_PENDING: 2 }, downstream_branch: {},
  });
  const html = sfTreeHtml(stages);
  check("R3 B7 Round2 detail 树父标签仍在",
        html.includes("爬虫链路未完成:2") && html.includes("回调已完成，SaaS尚未推进") &&
        !html.includes("爬虫未回调"), html);
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
  test_r3_chips_use_live_counts_not_growth_snapshot();
  test_r3_format_shanghai_date_time();
  test_r3_pool_closure_tree_normal();
  test_r3_pool_closure_other_and_unknown_visible();
  test_r3_pool_routing_drift_no_fake_closure();
  test_r3_round2_contract_preserved_in_new_tree();
  if (failed === 0) { console.log("ALL PASS"); process.exit(0); }
  console.error(failed + " failing checks");
  process.exit(1);
})();
