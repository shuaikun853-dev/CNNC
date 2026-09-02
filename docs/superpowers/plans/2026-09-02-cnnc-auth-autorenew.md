# CNNC 生产认证自动续签落地计划（2026-09-02）

授权范围：仅 CNNC 认证自动续签/自动恢复及必要配套；不改无关业务；不做无关生产写；不降低安全性。
生产权威分支：`origin/prod/closeout-20260818`。
**PRE_CHANGE_PROD_SHA = 89aa9b080f0fb42d9fc3765cab80c115230e0104**（2026-09-02 fetch 实测）。
开发分支：`feat/cnnc-auth-autorenew-20260902`（worktree `E:\.A中核运维\cnnc-ops-agent-worktrees\cnnc-auth-autorenew-20260902`）。
生产运行目录：`E:\.A中核运维\cnnc-ops-agent`（计划任务 CNNC-Dashboard-ReadOnly，`E:\Python314\python.exe -m uvicorn app.main:app --port 8765`；系统 Python 已含 playwright 48.0.0 + 系统 Chrome）。

## 一、已验证机制（PoC 实测，不重研）

- 刷新链：SSO 会话（storage_state 持久化）→ 纯净 Chrome+CDP 访问 `https://sso.cnncecp.com/` → 302 链 → 监听 `/esc-sso/oauth2.0/authorize` 302 Location（须含 `one.cnncecp.com`，防 SSO 内部 OC 混入）→ OC → `POST /cnnc-gateway/pm/n/anon/account/login {code}`（无 WAF）→ 新 AT → 业务 API `rspCode=0000`。
- 约束：禁 `ctx.route()`（CDP 卡 302 链）；只注入 5 个会话 cookie（TGC/SESSION/GWSID1-sso.cnncecp.com/mfaQueryValue/CookieLocaleResolver.LOCALE）；OC 一次性（捕获即 `page.close()`）；直接 goto `/authorize` 会卡 WAF。
- SSO 失效特征：302 落 `/esc-sso/login` 或 `sso.cnncecp.com/login`。
- 业务认证失败特征：HTTP 401 或 body `rspCode=3333`（`auth_manager._payload_is_auth_failure` 已覆盖）。

## 二、架构

```
CnncAuthProvider (scripts/cnnc_auth_core.py)   ← 新增，唯一 CNNC 认证权威
│  get_access_token() / inject_auth() / request_json_cnnc()
│  refresh(stale_at) / ensure_valid() / get_status() / login_required()
│  状态: data/cnnc_auth_state.json（原子写）
│  锁:  data/.cnnc_auth_state.json.refresh.lock（线程RLock+msvcrt文件锁）
│       + 锁元数据(owner/pid/started_at) + >10min 过期抢占
│  台账: data/cnnc_auth_ledger.jsonl（脱敏 OC-137***/AT-128***）
│
├─ harvest: subprocess(sys.executable, scripts/cnnc_auth_browser.py harvest)
│           解析 stdout `OC=`；SSO_LOGIN_REQUIRED 透传
├─ exchange: urllib POST anon/account/login → AT
├─ verify: 用新 AT 调真实只读查询 API，要求 rspCode=0000+rows 结构
└─ publish: ①原子写 state ②三个 curl 同步（全部 tmp→校验→os.replace，
             失败回滚已替换文件并报 CURL_SYNC_FAILED，不留半更新）
             curl 仅作兼容层/诊断模板，不再是 Adapter 认证来源
```

生产接线（改动点）：
1. `cnnc_saas_snapshot_adapter.py`：customer 侧请求改走 `provider.request_json_cncc()`（模板仍提供 URL/body/K5 头；AT 每请求动态注入 access-token 头 + token cookie；401/3333→refresh→重试一次）。provider=None 时保持旧行为（测试兼容）。
2. `incremental_audit_runtime.py`：`build_default_dependencies` 构造共享 provider 单例传入 adapter；`_detail_worker` 每次调用动态注入当前 AT，payload 认证失败→refresh→重试一次；`snapshot_discoverer` 首次 discover 前 `ensure_valid()`（异常不阻塞，lazy 路径兜底）。
3. `dashboard_api.py`：`GET /api/cnnc-auth/status`（状态枚举+最后成功时间+错误）；`POST /api/cnnc-auth/login`（detached subprocess 启动 login 浏览器，不阻塞 HTTP 线程）；前端卡片显示五态。

## 三、状态机（§15 全集）

```
CNNC_AUTH_OK / CNNC_AUTH_REFRESHED / CNNC_AUTH_REFRESH_IN_PROGRESS
SSO_LOGIN_REQUIRED
WAF_CHALLENGE_FAILED / BROWSER_START_FAILED / BROWSER_CONNECT_FAILED
OC_NOT_CAPTURED / OC_EXCHANGE_FAILED / AT_VERIFY_FAILED
CREDENTIAL_PUBLISH_FAILED / CURL_SYNC_FAILED / AUTH_RETRY_FAILED
```
Dashboard 与日志同一套常量（模块级 `CnncAuthStatus`）。

## 四、安全

- 日志/台账只出现 `OC-137***`/`AT-128***` 或布尔；旧 `data/cnnc_auth_probe.log` 格式（12/14 字符前缀）废弃，由 ledger 取代。
- `.gitignore` 追加：`data/cnnc_auth_profile/`、`data/cnnc_auth_ledger.jsonl`、`data/cnnc_auth_state.json`（已含于 data/*.json）。
- 新文件命名避开全局忽略子串 `*token*/*cookie*/*credential*/*secret*`。
- 不逆向 K5/y1；不自动破解 MFA；SSO 失效不无限重试（refresh 单次失败即返回状态，无定时循环）。

## 五、测试（十 Case 映射）

| Case | 测试 |
|---|---|
| 1 AT 有效不刷新 | fake opener 200/0000，断言 refresh 不被调 |
| 2 AT 失效自动恢复 | 3333→harvest/exchange stub→verify 0000→retry 成功 |
| 3 并发只刷一次 | 双线程同时失效，锁+stale 双检，第二个复用 |
| 4 SSO 失效 | harvest 返回 SSO_LOGIN_REQUIRED→透传，无重试循环 |
| 5 exchange 失败 | 不覆盖旧 state |
| 6 verify 失败 | 不 publish |
| 7 curl 第2文件写失败 | 回滚第1文件，全旧一致 |
| 8 不重启进程生效（P0） | adapter 实例化后 provider 刷新→下一请求用新 AT |
| 9 Location 捕获 | harvest 内部 302 Location 单测（无 ctx.route） |
| 10 不注陈旧 K5 | storage_state 注入集 == 5 会话 cookie 白名单 |

全部用注入 seam（harvest_func/exchange_func/verify_func/opener/时间），hermetic。

## 六、门禁与部署

1. worktree 内 fresh full suite（对照基线已知红集）；
2. `git diff`/`status` 凭证扫描（无 TGC/SESSION/OC/AT/K5/profile/storageState 入库）；
3. commit → push → merge `origin/prod/closeout-20260818`（生产授权已给）；
4. 部署 = 生产运行目录 ff 到 merge SHA + Stop/Start CNNC-Dashboard-ReadOnly；
5. 部署后验证：Dashboard 200 + 认证状态 + 自然 tick + SaaS/招商局回归只读检查 + 真实 CNNC read-only fetch。
