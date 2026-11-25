"""CLI entry: python -m siwave_extractor --project X.siw --nets nets.txt

Falls back to interactive prompts (matching the original UX) when --project or
--nets are omitted.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from .config import load_config
from .logging_setup import build_logger
from .netlist import NetlistError, load_skip_series_nc, read_netlist
from .workflow import run, format_summary


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="siwave_extractor",
                                     description="SIwave SYZ extraction automation")
    parser.add_argument("--project", help="path to the .siw project file")
    parser.add_argument("--nets", help="path to the net-info .txt file")
    parser.add_argument("--config", default="config.json",
                        help="path to config JSON (default: config.json)")
    parser.add_argument("--out", help="results output directory "
                        "(default: <project dir>/../results)")
    parser.add_argument("--stackup", help="叠层 XML 文件路径，仅用于 --stackup-only SIwave 初始化")
    parser.add_argument("--deactivate-rlc", action="store_true",
                        help="SIwave 初始化时将 Capacitors/Inductors/Resistors 设为 inactive")
    parser.add_argument("--stackup-only", action="store_true",
                        help="初始化 SIwave：可导入叠层、去激活 R/L/C 并保存 .siw 工程，不执行 S 参数抽取")
    parser.add_argument("--import-odb",
                        help="ODB++ 文件路径（.tgz）：导入 SIwave 生成新工程（初始化第一步）")
    parser.add_argument("--out-siw",
                        help="--import-odb 成功后另存的 .siw 工程路径")
    return parser.parse_args(argv)


def _prompt(label: str) -> str:
    return input(label).strip().replace('"', "")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    logger = build_logger(config.log_file)

    if args.import_odb:
        # 初始化第一步：把 ODB++(.tgz) 导入 SIwave，生成新工程并另存为 .siw。
        # 不需要 --project（导入本身会创建工程），也独立于 --stackup-only。
        if not args.out_siw:
            print("--import-odb 必须配合 --out-siw 指定输出 .siw 路径")
            return 1
        if not os.path.isfile(args.import_odb):
            print(f"ODB 文件不存在: {args.import_odb}")
            return 1
        from .siwave_session import ComSiwaveSession
        session = ComSiwaveSession(ref_net=config.ref_net, com_timeout=config.com_timeout)
        print(f"Importing ODB: {args.import_odb}", flush=True)
        print(f"Output .siw: {args.out_siw}", flush=True)
        try:
            session.import_odb(args.import_odb, "", args.out_siw)
        except Exception:  # noqa: BLE001
            import traceback
            print(traceback.format_exc(), flush=True)
            return 1
        print(f"ODB imported and saved: {args.out_siw}", flush=True)
        logger.info("ODB imported: %s -> %s", args.import_odb, args.out_siw)
        return 0

    project = args.project or _prompt("please input project .siw file path: ")

    if args.stackup_only:
        if not args.stackup and not args.deactivate_rlc:
            print("stackup-only 模式必须提供 --stackup 或 --deactivate-rlc 至少一项")
            return 1
        from .siwave_session import ComSiwaveSession
        session = ComSiwaveSession(ref_net=config.ref_net, com_timeout=config.com_timeout)
        print(f"Opening project: {project}", flush=True)
        session.open_project(project)
        ret = None
        count = 0
        if args.stackup:
            from .stackup_xml import load_stackup_xml, stackup_siwave_problems
            try:
                problems = stackup_siwave_problems(load_stackup_xml(args.stackup))
            except Exception as exc:
                print(f"叠层 XML 读失败: {exc}", flush=True)
                return 1
            if problems:
                print("叠层 XML 不能导入 SIwave（会崩溃）:", flush=True)
                for line in problems:
                    print(f"  - {line}", flush=True)
                return 1
            print(f"Importing stackup (ScrImportLayerStackupXML): {args.stackup}", flush=True)
            try:
                ret = session.import_stackup(args.stackup)
            except Exception:  # noqa: BLE001
                import traceback
                print(traceback.format_exc(), flush=True)
                return 1
            print(f"Stackup import returned: {ret} (1=success)", flush=True)
        if args.deactivate_rlc:
            print("Deactivating R/L/C circuit elements...", flush=True)
            count, _failed = session.deactivate_rlc_circuit_elements(project)
            print(f"Deactivated R/L/C circuit elements: {count}", flush=True)
        print("Saving project...", flush=True)
        session.save_as(project)
        print("SIwave initialization completed.", flush=True)
        logger.info(
            "SIwave initialized: stackup=%s imported into %s (ret=%s), "
            "R/L/C deactivated=%s",
            args.stackup, project, ret, count)
        return 0

    if args.stackup:
        print("SIwave 抽取阶段不再自动导入叠层或去激活 R/L/C；请先运行独立的 SIwave 初始化功能（--stackup-only）。")
        return 1

    nets_path = args.nets or _prompt("please input net information .txt file path: ")

    try:
        grouped = read_netlist(nets_path)
    except NetlistError as exc:
        print(str(exc), flush=True)
        return 1
    config.skip_series_nc = tuple(sorted(load_skip_series_nc(nets_path)))
    if config.skip_series_nc:
        print(f"串联不贴焊盘不建口: {len(config.skip_series_nc)} 个", flush=True)

    if config.parallel_workers >= 2:
        # 常驻若干 SIwave（-RunScript），不走 COM：COM 初始化会 taskkill 全部 SIwave。
        from .parallel import run_parallel
        results = run_parallel(config, project, grouped, out_dir=args.out)
    else:
        # 串行路径：单个 COM 会话顺序处理（lazy import 让 tests/CI 不依赖 pywin32）。
        from .siwave_session import ComSiwaveSession
        session = ComSiwaveSession(ref_net=config.ref_net, com_timeout=config.com_timeout)
        results = run(session, config, project, grouped, out_dir=args.out)
    summary = format_summary(results)
    logger.info("\n%s", summary)
    print(summary)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
