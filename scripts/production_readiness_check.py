"""CNNC 生产就绪只读检查 CLI（thin wrapper，逻辑全在服务层）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.production_readiness import (  # noqa: E402
    EXPECTED_BRANCH_DEFAULT,
    build_readiness_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CNNC 生产就绪只读检查（不注册任务、不连库、不刷新认证、不发网络请求）"
    )
    parser.add_argument("--repo-root", type=Path, default=ROOT, help="仓库根目录")
    parser.add_argument("--expected-branch", default=EXPECTED_BRANCH_DEFAULT)
    parser.add_argument(
        "--json", action="store_true", help="输出机器可读 JSON（UTF-8，ensure_ascii=False）"
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="单条外部命令超时秒数")
    args = parser.parse_args(argv)

    # Windows 控制台编码安全：不用 emoji，遇不可编码字符降级而不是崩溃。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - 非常规流
            pass

    try:
        report = build_readiness_report(
            args.repo_root,
            expected_branch=args.expected_branch,
            timeout_seconds=args.timeout,
        )
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print("CNNC Production Readiness")
            print("=" * 40)
            for check in report.checks:
                print(f"{check.name:<20}[{check.status}] {check.summary}")
                for line in check.details:
                    print(f"    {line}")
            print()
            print(f"Overall: {report.overall}")
            print("No changes were made.（只读检查）")
    except Exception as exc:  # 顶层意外错误：退出码 2。
        print(f"意外错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
