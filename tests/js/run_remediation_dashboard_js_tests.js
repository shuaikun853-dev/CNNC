"use strict";
const path = require("path");
const {
  matchesFilter,
  matchesSearch,
  productionText,
  prText,
} = require(path.join(
  __dirname, "..", "..", "app", "static", "dashboard", "remediation.js"
));

let failed = 0;
function check(name, condition, detail) {
  if (condition) {
    console.log("ok - " + name);
  } else {
    failed += 1;
    console.error("FAIL - " + name + (detail ? " :: " + detail : ""));
  }
}

const base = {
  id: "F01",
  severity: "P0",
  title: "强制收口数据不一致",
  development: {state: "BRANCH_ONLY"},
  review: {status: "PENDING"},
  production: {state: "NOT_MERGED"},
  production_verification: {status: "NOT_VERIFIED"},
  codex: {title: "closeout evidence"},
  branch: "fix/F01-closeout",
};

check("P0 筛选命中 P0", matchesFilter(base, "p0") === true);
check("开发中筛选命中 BRANCH_ONLY", matchesFilter(base, "dev") === true);
check("待审计筛选命中 PENDING", matchesFilter(base, "review") === true);
check("已合并未部署不误命中未合并", matchesFilter(base, "merge") === false);

const merged = {...base, production: {state: "MERGED_NOT_DEPLOYED"}};
check("已合并未部署筛选命中", matchesFilter(merged, "merge") === true);

const deployed = {
  ...base,
  production: {state: "DEPLOYED"},
  production_verification: {status: "VERIFIED"},
};
check("生产验证筛选命中", matchesFilter(deployed, "prod") === true);
check("生产状态显示验证通过", productionText(deployed) === "生产验证通过");
check("搜索可按 Bug ID", matchesSearch(base, "f01") === true);
check("搜索可按 Codex 标题", matchesSearch(base, "evidence") === true);
check("无关搜索不命中", matchesSearch(base, "F22") === false);

if (failed) process.exit(1);
console.log("ALL PASS");

const prUnknown = {...base, development: {state: "PR_UNKNOWN"}};
check("开发中筛选包含 PR 状态未知", matchesFilter(prUnknown, "dev") === true);

check("PR 查询失败显示状态未知", prText({state: "UNKNOWN"}) === "状态未知");
check("明确无 PR 显示未创建", prText(null) === "未创建");
