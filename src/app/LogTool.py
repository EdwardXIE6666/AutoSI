import sys
import os
import re
from datetime import datetime
from pathlib import Path
from colorama import init
init(strip=False, convert=False)

FUNCTION_LOG_NAMES = {
    "1": "功能1_网络频段信息",
    "2": "功能2_SIwave初始化",
    "3": "功能3_SIwave抽取",
    "4": "功能4_ADS仿真",
    "5": "功能5_refnum生成",
    "6": "功能6_器件Pin路径",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text):
    if not text or "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


class Logger:
    """
    对齐时间戳日志工具
    ◦ 终端：无时间戳，有emoji和颜色
    ◦ 文件：对齐的时间戳（无前置空格），带「」包裹
    ◦ 支持消息层级缩进
    """
    
    def __init__(self, log_dir="logs", log_file=None, console_only=False):
        self.console_only = console_only  # 新增控制变量
        
        if not self.console_only:
            os.makedirs(log_dir, exist_ok=True)
            
            if log_file is None:
                timestamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
                log_file = f"{timestamp}.log"
            
            self.log_path = os.path.join(log_dir, log_file)
            # 文件头写入对齐的时间戳
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(f"=== Log started at「{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}」===\n\n")
        else:
            self.log_path = None
        
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.style = LogStyle()

    def _append_file(self, message):
        if self.console_only or not self.log_path or not message:
            return
        try:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(strip_ansi(message))
        except Exception:
            pass

    def write(self, message):
        try:
            self.original_stdout.write(message)
        except Exception:
            pass
        self._append_file(message)

    def flush(self):
        try:
            self.original_stdout.flush()
        except Exception:
            pass

    def __enter__(self):
        sys.stdout = self
        sys.stderr = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
        if not self.console_only and self.log_path:
            ended = "aborted" if exc_type is not None else "ended"
            extra = f" ({exc_type.__name__})" if exc_type is not None else ""
            self._append_file(
                f"\n=== Log {ended}{extra} at「{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}」===\n"
            )
        return False
    
    def _log(self, message, level=0, log_type='info'):
        color = getattr(self.style, log_type.upper(), self.style.DEFAULT)
        emoji = self.style.EMOJI.get(log_type, '')
        indent = '  ' * level

        # 终端输出（无时间戳）
        console_msg = f"{indent}{emoji} {color}{message}{self.style.ENDC}\n"
        try:
            self.original_stdout.write(console_msg)
        except Exception:
            pass
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._append_file(f"「{timestamp}」{indent} {emoji} {message}\n")
    
    # 快捷方法保持不变
    def error(self, message, level=0):
        self._log(message, level, 'error')
    
    def success(self, message, level=0):
        self._log(message, level, 'success')
    
    def info(self, message, level=0):
        self._log(message, level, 'info')
    
    def warning(self, message, level=0):
        self._log(message, level, 'warning')
    
    def highlight(self, message, level=0):
        self._log(message, level, 'highlight')
    
    def notice(self, message, level=0):
        self._log(message, level, 'notice')

# LogStyle类保持不变
class LogStyle:
    """日志样式配置"""
    ERROR = '\033[1;38;2;255;85;85m'       # ❌ 红色 #FF5555
    SUCCESS = '\033[1;38;2;80;250;123m'    # ✅ 绿色 #50FA7B
    INFO = '\033[1;38;2;97;175;239m'       # ℹ️ 蓝色 #61AFEF
    WARNING = '\033[1;38;2;255;184;108m'   # ⚠️ 橙黄 #FFB86C
    HIGHLIGHT = '\033[1;38;2;189;147;249m' # 🌟 紫色 #BD93F9
    NOTICE = '\033[1;38;2;139;233;253m'    # 📝 青色 #8BE9FD
    DEFAULT = '\033[1;38;2;152;163;180m'   # 🔹 灰色 #98A3B4
    ENDC = '\033[0m'
    END = '\033[0m'
    EMOJI = {
        'error': '❌',
        'success': '✅',
        'info': 'ℹ️',
        'warning': '⚠️',
        'highlight': '🌟',
        'notice': '📝'
    }

def save_run_log(
    workspace,
    function,
    text,
    *,
    project="",
    status="",
    when=None,
    extra_root=None,
):
    """把一次功能运行的完整文字落到 OUTPUT/LOG。成功、失败、终止都写。"""
    from app.workspace import OUTPUT_LOG, output_dir

    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H.%M.%S")
    tag = {"fail": "_失败", "cancel": "_终止"}.get(status or "", "")
    folder = output_dir(Path(workspace), OUTPUT_LOG)
    if project:
        folder = folder / str(project)
    folder = folder / FUNCTION_LOG_NAMES.get(str(function), f"功能{function}")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}{tag}.log"
    body = strip_ansi(text or "")
    if body and not body.endswith("\n"):
        body += "\n"
    path.write_text(body, encoding="utf-8")
    if extra_root:
        extra = Path(extra_root) / "8_log"
        if project:
            extra = extra / str(project)
        extra = extra / FUNCTION_LOG_NAMES.get(str(function), f"功能{function}")
        extra.mkdir(parents=True, exist_ok=True)
        dest = extra / path.name
        if dest.resolve() != path.resolve():
            dest.write_text(body, encoding="utf-8")
    return path


log = Logger(console_only=True)  # 默认开启仅终端模式

_issues = []           # 元素为 [level, message]（可变，便于后续按 key 回填详情）
_issue_keys = set()
_issue_map = {}        # key -> _issues 索引，供 enrich_issue 定位回填

def add_issue(level: str, message: str, key: str = None):
    """收集运行中的错误/警告，在最终汇总时打印。level='ERROR' 或 'WARNING'。"""
    if key and key in _issue_keys:
        return
    if key:
        _issue_keys.add(key)
        _issue_map[key] = len(_issues)
    _issues.append([level, message])

def enrich_issue(key: str, extra: str):
    """向已收集的 issue 追加详情行（按 add_issue 时的 key 定位）。

    用于“先报基础信息、后补上下文”：例如 S 参数缺失在解析期先报“型号+位号”，
    等射频图反向遍历算出“影响哪条天线口”后再用同一 key 回填。
    key 未命中（基础 issue 未产生）或 extra 为空时静默跳过。
    """
    idx = _issue_map.get(key)
    if idx is None or not extra:
        return
    _issues[idx][1] = _issues[idx][1] + '\n' + extra


def _uniq_keep(seq):
    out = []
    seen = set()
    for item in seq or ():
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _net_sort_key(name: str):
    text = str(name or "")
    return (text.startswith("$$$"), text)


def missing_snp_impact_text(nets) -> str:
    """缺库后果：受影响的网拿不到这颗器件的频率。"""
    nets = _uniq_keep(sorted(nets or (), key=_net_sort_key))
    if nets:
        return f"   受影响网络：{','.join(nets)}无法获得这颗器件的频率信息；"
    return "   受影响网络：当前路径没走到这些位号，网上的频段这次不受影响；"


def report_missing_snp_impacts(failed_components, refdes_nets):
    """缺库型号：回填汇总，并立刻打进 log。"""
    if not failed_components:
        return
    model_to_refdes = {}
    for fail in failed_components.values():
        model = fail.get("external_model") or str(
            fail.get("error", "")
        ).split("找不到SNP文件: ", 1)[-1]
        model = str(model or "").strip()
        if not model:
            continue
        model_to_refdes.setdefault(model, []).extend(fail.get("components") or [])
    if not model_to_refdes:
        return
    print(f"\n{LogStyle.ERROR}{'='*62}{LogStyle.ENDC}")
    print(f"{LogStyle.ERROR}❌ 缺 S 参数（影响如下）{LogStyle.ENDC}")
    print(f"{LogStyle.ERROR}{'='*62}{LogStyle.ENDC}")
    for model, refs in model_to_refdes.items():
        refs = _uniq_keep(refs)
        nets = set()
        for ref in refs:
            nets |= set(refdes_nets.get(ref) or ())
        extra = missing_snp_impact_text(nets)
        enrich_issue(f"snp_missing_{model}", extra)
        ref_s = ", ".join(refs) if refs else "（无位号）"
        print(f"{LogStyle.ERROR}❌ [S参数未找到] {model}  位号: {ref_s}{LogStyle.ENDC}")
        print("   S参数库中没有这个型号")
        print(extra)
    print(f"{LogStyle.ERROR}{'='*62}{LogStyle.ENDC}\n")

def print_issue_summary():
    """在运行结束时打印所有收集到的问题，按类别分组显示。"""
    errors = [(l, m) for l, m in _issues if l == 'ERROR']
    warnings = [(l, m) for l, m in _issues if l == 'WARNING']
    if not errors and not warnings:
        return
    RED = LogStyle.ERROR
    YEL = LogStyle.WARNING
    END = LogStyle.ENDC

    def _parse(msg):
        lines = msg.split('\n')
        first = lines[0]
        if first.startswith('[') and ']' in first:
            cat = first[1:first.index(']')]
            headline = first[first.index(']')+1:].strip()
        else:
            cat, headline = '其他', first
        details = [l.strip() for l in lines[1:] if l.strip()]
        return cat, headline, details

    def _group(items):
        g = {}
        order = []
        for _, msg in items:
            cat, headline, details = _parse(msg)
            if cat not in g:
                g[cat] = []
                order.append(cat)
            g[cat].append((headline, details))
        return [(cat, g[cat]) for cat in order]

    def _print_section(color, icon, label, items):
        groups = _group(items)
        print(f"\n{color}{icon}  {label}  （共 {len(items)} 项）{END}")
        print(f"{color}{'─'*62}{END}")
        for cat, entries in groups:
            print(f"\n{color}  ◆ [{cat}]  {len(entries)} 项{END}")
            for i, (headline, details) in enumerate(entries, 1):
                print(f"{color}    [{i}] {headline}{END}")
                for d in details:
                    print(f"          ▸ {d}")

    total = len(errors) + len(warnings)
    print(f"\n{RED}{'═'*62}{END}")
    print(f"{RED}   运行问题汇总  ——  共 {total} 项（{len(errors)} 错误 / {len(warnings)} 警告）{END}")
    print(f"{RED}{'═'*62}{END}")

    if warnings:
        _print_section(YEL, "⚠️", "警告", warnings)
    if errors:
        _print_section(RED, "❌", "错误", errors)

    print(f"\n{RED}{'═'*62}{END}\n")

if __name__ == "__main__":
    # 演示输出效果
    with Logger(console_only=False) as log:  # 开启仅终端模式
        log.notice("终端演示：无时间戳")
        log.info("第1级信息", level=1)
        log.warning("第2级警告", level=2)
        log.success("返回第0级", level=0)
        print("普通print消息会正常显示且不写入文件")
        input()