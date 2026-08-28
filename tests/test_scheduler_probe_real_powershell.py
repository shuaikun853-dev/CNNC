"""L2 REAL COMPONENT TEST — 真实 powershell.exe 5.1 子进程,禁止 monkeypatch PowerShell subprocess;
唯一注入点是喂给生产 decode/projection 的真实跨边界字节。INCIDENT_20260827_UINT32_REPLAY 覆盖 2147946720/0x800710E0。

与 L0(test_dashboard_projection.py 全程 monkeypatch _run_scheduler_probe)的区别:本文件每一行
stdout 字节都真实跨过 powershell.exe 5.1 / .NET 类型系统 / ConvertTo-Json / stdout 编码边界取得,
再交给生产 _decode_scheduler_probe / project_scheduler_status 消费——monkeypatch 只等价于现有 L0
的 seam 注入点,喂入的字节永不伪造。真实探针只读(Get-ScheduledTask/Get-ScheduledTaskInfo),
不注册、不修改任何计划任务。
"""

import json
import re
import shutil
import subprocess

import pytest

import app.services.dashboard_projection as dp

pytestmark = pytest.mark.skipif(shutil.which("powershell") is None, reason="powershell.exe unavailable")


@pytest.fixture(autouse=True)
def _reset_scheduler_cache():
    """每个用例前后清 scheduler 缓存,避免真实探针结果跨用例泄漏(参照现有 dashboard 测试做法)。"""
    dp._scheduler_cache.update({"at": 0.0, "value": None})
    yield
    dp._scheduler_cache.update({"at": 0.0, "value": None})


def _run_real_powershell(script: str) -> subprocess.CompletedProcess:
    """真实 powershell.exe 5.1 子进程执行(仅只读命令,无任何 mock/patch)。"""
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, timeout=60, check=False)


def _incident_replay_script(last_result_expr: str) -> str:
    """用事故时间线的 UInt32 结果码构造真实回放管道(表达式来自生产命令或旧代码形状)。"""
    return (
        "$i = [pscustomobject]@{ LastTaskResult = [uint32]2147946720; "
        "LastRunTime = [datetime]'2026-08-27 18:25:00'; NextRunTime = [datetime]'2026-08-27 18:30:00' }; "
        "$t = [pscustomobject]@{ State = 'Ready' }; "
        "[pscustomobject]@{ state = [string]$t.State; last_run = [string]$i.LastRunTime; "
        f"last_result = {last_result_expr}; next_run = [string]$i.NextRunTime }} | ConvertTo-Json -Compress")


def _project_with(monkeypatch, decoded):
    """把真实解码结果经生产 seam 注入并跑生产 projection(等价 L0 注入点,字节为真实跨边界字节)。"""
    monkeypatch.setattr(dp, "_run_scheduler_probe", lambda: decoded)
    return dp.project_scheduler_status()


def test_production_probe_full_real_chain():
    """生产函数+生产命令+真实任务+真 Get-ScheduledTask 全链路(dashboard 每分钟都在做的只读操作)。"""
    data = dp._run_scheduler_probe()
    assert data is None or isinstance(data, dict)
    if isinstance(data, dict):
        assert {"state", "last_run", "last_result", "next_run"} <= set(data)
        # [int64] 经 ConvertTo-Json 必为 JSON 数字;再出现 {} / null 即事故形状回归
        assert isinstance(data["last_result"], int) and not isinstance(data["last_result"], bool)
    out = dp.project_scheduler_status()  # 不得抛异常(summary 不可 503)
    assert isinstance(out, dict)
    assert out["available"] is (data is not None)


def test_scheduler_probe_command_pinned_to_production_task():
    """命令必须钉死生产任务名与压缩 JSON 序列化,防漂移到别的任务/别的序列化器。"""
    cmd_text = " ".join(dp._scheduler_probe_command())
    assert "CNNC-PriceAudit-Incremental" in cmd_text
    assert "ConvertTo-Json -Compress" in cmd_text


def test_INCIDENT_20260827_UINT32_REPLAY():
    """INCIDENT_20260827_UINT32_REPLAY:UInt32 2147946720(0x800710E0)经生产 [int64] 表达式必须原样成为 JSON 数字。

    last_result 表达式用正则从生产命令文本提取(容许空白差异):任何人把 [int64] 改回 [int],
    提取到的表达式随之变化,PS 5.1 真实输出退化为 last_result={},下方断言红——行为化钉死修复。
    """
    script = dp._scheduler_probe_command()[-1]
    m = re.search(
        r"last_result\s*=\s*(if\s*\(\s*\$\w+\s*\)\s*\{[^{}]*\}\s*else\s*\{[^{}]*\})", script)
    assert m, "未能从生产命令中提取 last_result if 表达式"
    expr = m.group(1)

    proc = _run_real_powershell(_incident_replay_script(expr))
    assert proc.returncode == 0
    assert proc.stdout, "真实 PowerShell 链路必须有 stdout"
    data = json.loads(proc.stdout.decode("utf-8-sig", errors="replace"))
    assert data["last_result"] == 2147946720
    assert isinstance(data["last_result"], int) and not isinstance(data["last_result"], bool)


def test_incident_shape_int_cast_still_produces_degraded_not_crash(monkeypatch):
    """事故形状回放:旧 [int] 强转失败时 PS 5.1 管道不死(rc=0),last_result 序列化为空对象 {}。

    该真实 stdout 字节经生产 _decode_scheduler_probe / project_scheduler_status 后必须降级为
    available=True + last_result="-1",而不是像 2026-08-27 18:2x 那样把 /api/summary 打成 503。
    """
    old_shape = "if ($i) { [int]$i.LastTaskResult } else { -1 }"
    proc = _run_real_powershell(_incident_replay_script(old_shape))
    assert proc.returncode == 0, "PS 5.1 强转失败不终止管道"
    assert proc.stdout
    data = json.loads(proc.stdout.decode("utf-8-sig", errors="replace"))
    assert isinstance(data["last_result"], dict), "旧形状:强转失败属性必须序列化为对象而非数字"
    assert data["last_result"] == {}

    decoded = dp._decode_scheduler_probe(
        subprocess.CompletedProcess(args=proc.args, returncode=0, stdout=proc.stdout))
    assert isinstance(decoded, dict)
    out = _project_with(monkeypatch, decoded)
    assert out["available"] is True
    assert out["last_result"] == "-1"


_TYPE_MATRIX = [
    # (PS last_result 表达式, 期望生产 project_scheduler_status 后的 last_result 字符串)
    ("0", "0"),
    ("267009", "267009"),
    ("2147946720", "2147946720"),
    ("-1", "-1"),
    ("[int64][uint32]2147946720", "2147946720"),
    ("[uint32]2147946720", "2147946720"),   # PS 自动 UInt32 → JSON 数字
    ("$null", "-1"),
    ("'2147946720'", "2147946720"),         # 数字字符串可 int()
    ("'abc'", "-1"),
    ("''", "-1"),
    ("[pscustomobject]@{a=1}", "-1"),       # 嵌套对象 → TypeError 降级
    ("@(1,2)", "-1"),                       # 数组 → TypeError 降级
    ("42.5", "42"),                         # 意外标量:int(42.5)==42 截断
]


@pytest.mark.parametrize("expr,expected", _TYPE_MATRIX, ids=[c[0] for c in _TYPE_MATRIX])
def test_scheduler_probe_type_matrix(expr, expected, monkeypatch):
    """每个 last_result 形状跑真实 PS 管道→真实字节→生产 decode→生产 projection,断言分类且不抛异常。"""
    ps = ("[pscustomobject]@{ state='Running'; last_run='x'; last_result = "
          + expr + "; next_run='y' } | ConvertTo-Json -Compress")
    proc = _run_real_powershell(ps)
    assert proc.returncode == 0, f"case {expr!r}: 真实管道 rc"
    assert proc.stdout, f"case {expr!r}: 真实 PS 输出不得为空"
    decoded = dp._decode_scheduler_probe(
        subprocess.CompletedProcess(args=proc.args, returncode=0, stdout=proc.stdout))
    assert isinstance(decoded, dict), f"case {expr!r}: 真实 JSON 必须解码为 dict"
    out = _project_with(monkeypatch, decoded)
    assert isinstance(out, dict), f"case {expr!r}: summary 不可因此 503"
    assert out["available"] is True
    assert out["last_result"] == expected, f"case {expr!r}: 分类结果"


def test_empty_output_degrades_unavailable(monkeypatch):
    """真实零输出命令 → 生产 decode 得 None → projection 降级 unavailable,不抛异常。"""
    proc = _run_real_powershell("exit 0")
    assert proc.returncode == 0 and proc.stdout == b""
    assert dp._decode_scheduler_probe(
        subprocess.CompletedProcess(args=proc.args, returncode=0, stdout=proc.stdout)) is None
    assert _project_with(monkeypatch, None) == {"available": False}


def test_invalid_json_degrades_unavailable(monkeypatch):
    """真实坏 JSON 输出 → 生产 decode 得 None → projection 降级 unavailable,不抛异常。"""
    proc = _run_real_powershell("Write-Output '{\"state\": \"Running\", oops'")
    assert proc.returncode == 0
    assert proc.stdout, "Write-Output 必须产生真实 stdout"
    assert dp._decode_scheduler_probe(
        subprocess.CompletedProcess(args=proc.args, returncode=0, stdout=proc.stdout)) is None
    assert _project_with(monkeypatch, None) == {"available": False}
