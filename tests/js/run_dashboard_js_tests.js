"use strict";
// Dashboard 前端轮询控制器行为测试(node 直跑,零依赖、无 DOM)。
// 覆盖 Independent Review Fix Round 1:
//   P2-1  304 也是成功同步(不重渲染、不误显"最后同步 X 分钟前");
//   P2-2  页面 hidden 真暂停(clearTimeout + 不再注册),visible 立即补刷;
//         在途 poll 去重,人工刷新不产生重复轮询链。
// 覆盖 Independent Review Fix Round 2:
//   P3    恢复时间线 signature 的安全渲染(safeProgressText)。
// 被测对象:app/static/dashboard/app.js 暴露的 createPollController / safeProgressText。
const path = require("path");
const { createPollController, safeProgressText } = require(
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

(async () => {
  await test_304_keeps_sync_fresh_without_rerender();
  await test_hidden_pauses_and_visible_resumes();
  await test_schedule_refuses_when_hidden();
  await test_no_duplicate_poll_chains();
  await test_offline_retry_then_recover();
  test_safe_progress_text();
  if (failed === 0) { console.log("ALL PASS"); process.exit(0); }
  console.error(failed + " failing checks");
  process.exit(1);
})();
