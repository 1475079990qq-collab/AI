"""统一启动入口：读 config.yaml → 打印当前配置 → 启动大脑。

用法：
    python -m brain.launch brain      # 大脑（已含控制台与情绪判断）
    python -m brain.launch all        # 大脑 + 语音服务
    python -m brain.launch status     # 只看模型服务是否在跑

端口 / 地址全部来自 config.yaml，bat 不再写死。

哪些模型服务会被一键拉起：
    ASR（:8103）  TTS（:8102）
    —— 这两个都是本项目 .venv 里装得上的纯 Python/CPU 服务。
不需要独立进程了：
    控制台 → 已并入大脑，同一个 8000
    情绪判断 → 走进程内 onnx（provider judge mode=onnx）
不包括：
    对话大模型（F 盘，你自己启动）
    GPT-SoVITS（:9880）：独立整合包自带 torch
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import traceback
import webbrowser
from pathlib import Path

import uvicorn

from brain import config, config_store
from brain import log_setup
from brain.config import settings


def _fix_console() -> None:
    """Windows 控制台默认 GBK，输出中文可能花屏。

    bat 里已经用 PYTHONIOENCODING 指定了编码（GBK，跟 chcp 936 对齐），
    这里要再硬掰成 UTF-8，中文就会全变成乱码，所以外部指定了就听它的。
    """
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


def _port_busy(port: int) -> bool:
    s = socket.socket()
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def _probe_import(module: str) -> bool:
    """探测某个 Python 包是否已经装了（不抛异常，True=已装）。"""
    code = f"import {module}"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, timeout=10,
    )
    return result.returncode == 0


def _probe_model_file(path: str) -> bool:
    """模型文件/目录是否存在（相对路径按项目根解释）。"""
    p = Path(path)
    if not p.is_absolute():
        p = Path(settings.ROOT) / p
    # 可能是目录（ASR_MODEL_DIR）→ 目录里有 model.int8.onnx 或 model.onnx 就算 OK
    if p.is_dir():
        return (p / "model.int8.onnx").is_file() or (p / "model.onnx").is_file()
    return p.is_file()


# 一键启动涉及的独立模型服务。每个 entry 定义：
#   provider_code  → 对应 config_store 里的 provider code（决定是否需要独立进程）
#   module         → Python 模块（只有 provider mode=http 才会拉起）
#   mode_off_vals  → runtime config 里这些 mode 值表示"不需要独立进程"（跳过）
#
# 例如 judge 的 mode=onnx 是进程内加载，不需要启动独立的 judge_server.py，
# 所以 mode_off_vals=["onnx", "rule"]，runtime 查到这些值就跳过。
_MODEL_SERVICES = [
    {
        "name": "情感判断",
        "provider_code": "judge",
        "module": "brain.judge_server",
        "mode_off_vals": ["onnx", "rule"],   # 进程内 / 规则兜底都不需要独立服务
        "port_attr": "JUDGE_PORT",
        "import": "onnxruntime",
        "model_file_attr": "JUDGE_MODEL_PATH",
        "model_file_desc": "情感判断模型 (.onnx)",
        "hint": "判断模型走进程内 onnx 模式，不需要启动独立服务",
    },
    {
        "name": "语音识别",
        "provider_code": "asr",
        "module": "brain.asr_server",
        "mode_off_vals": ["off"],
        "port_attr": "ASR_PORT",
        "import": "sherpa_onnx",
        "model_file_attr": "ASR_MODEL_DIR",
        "model_file_desc": "SenseVoice 识别模型目录",
        "hint": "先跑  tools\\setup_asr_model.py --download  下载模型",
    },
    {
        "name": "语音合成",
        "provider_code": "tts",
        "module": "brain.tts_server",
        # 已并入大脑进程：正式链路直接调进程内引擎，不再另起 :8102。
        # 只有想单独调试语音时才手动跑 python -m brain.tts_server。
        "skip_always": True,
        "mode_off_vals": ["off", "inproc"],
        "port_attr": "TTS_PORT",
        "import": "httpx",
        "model_file_attr": None,
        "model_file_desc": "",
        "hint": "语音引擎已并入大脑（进程内），不再单独启动",
    },
]

def _get_provider_mode(code: str) -> str:
    """读 config_store 里 provider 的当前 mode（运行时配置，比 config.yaml 新）。"""
    try:
        p = config_store.get_provider(code) or {}
        return str(p.get("mode") or "").strip().lower()
    except Exception:
        return ""


def _check_model_services() -> tuple[list[dict], list[str]]:
    """启动前逐个检查：runtime mode 决定要不要独立进程、依赖装了？模型文件在？

    返回 (服务检查列表, 配置提醒列表)。
    """
    results = []
    hints = []

    # ---- 模型服务检查 ----
    for svc in _MODEL_SERVICES:
        if svc.get("skip_always"):
            results.append({
                "name": svc["name"], "port": getattr(settings, svc["port_attr"], 0),
                "ok": True, "reason": "已并入大脑进程（不需要独立服务）", "skip": True,
            })
            continue
        mode = _get_provider_mode(svc["provider_code"])
        port = getattr(settings, svc["port_attr"], 0)

        # runtime mode 表示"不需要独立进程" → 跳过，不检查、不拉
        if mode in svc.get("mode_off_vals", []):
            if mode == "onnx":
                results.append({"name": svc["name"], "port": port, "ok": True,
                                "reason": f"进程内 {mode} 模式（不需要独立服务）",
                                "skip": True})
            elif mode == "rule":
                results.append({"name": svc["name"], "port": port, "ok": True,
                                "reason": "关键词规则兜底（不需要独立服务）",
                                "skip": True})
            elif mode == "off":
                results.append({"name": svc["name"], "port": port, "ok": True,
                                "reason": "已关闭", "skip": True})
            continue

        # 需要独立进程 → 检查依赖和模型文件
        reasons = []
        ok = True
        if not _probe_import(svc["import"]):
            ok = False
            reasons.append(f"缺 Python 包：{svc['import']}")
        mf = svc["model_file_attr"]
        if mf:
            path = getattr(settings, mf, "")
            if path and not _probe_model_file(path):
                ok = False
                reasons.append(f"缺模型文件：{svc['model_file_desc']}")

        if _port_busy(port):
            results.append({"name": svc["name"], "port": port, "ok": True,
                            "reason": "已在运行", "skip": True})
            continue

        results.append({
            "name": svc["name"], "port": port, "ok": ok,
            "reason": "；".join(reasons) if reasons else "就绪",
            "hint": svc["hint"] if not ok else "",
            "module": svc["module"],
        })

    return results, hints


_LABEL = {
    "brain": "大脑 / 实时链路 + 控制台 + 情绪判断（同一个进程）",
    "console": "控制台（已并入大脑，这里只是兼容旧脚本）",
    "all": "一键全启（大脑 + 语音服务）",
    "status": "只看模型服务状态（不启动任何东西）",
}


def _print_models() -> None:
    """模型服务清单：只探活，不加载。"""
    try:
        from brain.services import model_status

        print("  模型服务（判断/合成在大脑进程内，识别可选，对话模型自己起）")
        for line in model_status.detail_lines():
            print(line)
    except Exception:
        pass


def _print_config(which: str) -> None:
    print("=" * 58)
    print("  companion 启动配置  (改 config.yaml 后重启生效)")
    print("=" * 58)
    print(f"  本次启动    : {_LABEL.get(which, which)}")
    print("  运行模式    : 真实模型（程序里没有模拟路径；模型服务没起会明确报错）")
    print(f"  大脑地址    : http://{settings.BRAIN_HOST}:{settings.BRAIN_PORT}")
    print(f"  控制台地址  : http://{settings.BRAIN_HOST}:{settings.BRAIN_PORT}/admin.html（同一进程）")
    print(f"  数据库      : {settings.DB_PATH}")
    print(f"  控制台口令  : {'已设置' if settings.ADMIN_TOKEN else '未设置（本地免密）'}")
    print("=" * 58)
    _print_models()
    print("=" * 58)
    print("  提示：控制台里改人格/音色/参数保存即生效；改本文件要重启两个窗口。")
    print()


def main() -> None:
    import os

    _fix_console()
    which = (sys.argv[1] if len(sys.argv) > 1 else "brain").strip().lower()
    if which not in {"brain", "console", "all", "status"}:
        which = "brain"
    is_child = os.environ.get("COMPANION_CHILD") == "1"

    if which == "status":
        print("=" * 58)
        print("  模型服务状态（只检查，不启动）")
        print("=" * 58)
        _print_models()
        print("=" * 58)
        print("  大脑起来了就能用；模型没起会自动降级，状态以上面为准。")
        print()
        return

    # 从这里开始所有输出（含三方库报错）都会同时写进 data/logs/
    log_setup.start(which)

    # 启动时把端口写给前端，浏览器不再写死 8000/8001
    config.write_web_config()
    config_store.init_store()
    if not is_child:
        _print_config(which)

    if which == "all":
        admin_url = f"http://127.0.0.1:{settings.BRAIN_PORT}/admin.html"

        # ----- Step 1: 按 runtime mode 检查需要独立启动的模型服务 -----
        model_checks, config_hints = _check_model_services()
        to_start: list[dict] = []
        skip_count = 0
        print("  模型服务检查（按当前配置） ...", flush=True)
        for mc in model_checks:
            if mc.get("skip"):
                print(f"    [·]  {mc['name']}  :{mc['port']}  {mc['reason']}", flush=True)
                skip_count += 1
                continue
            if mc["ok"]:
                print(f"    [√]  {mc['name']}  :{mc['port']}  就绪，会一起拉起", flush=True)
                to_start.append(mc)
            else:
                print(f"    [×]  {mc['name']}  :{mc['port']}  跳过 —— {mc['reason']}", flush=True)
                if mc.get("hint"):
                    print(f"         → {mc['hint']}", flush=True)
        if skip_count:
            print(f"    （{skip_count} 个服务走进程内/已在运行，不需要拉起）", flush=True)

        # 配置提醒
        for h in config_hints:
            print(f"    [!]  {h}", flush=True)
        print(flush=True)

        # ----- Step 2: 检查大脑 / 控制台端口 -----
        # 控制台与情绪判断都并进大脑了：应用侧只拉一个进程、只占一个端口。
        plan = [("brain", settings.BRAIN_PORT)]
        app_targets = []
        for name, port in plan:
            if _port_busy(port):
                print(f"  [提示] 端口 {port} 已有服务在跑，跳过 {name}。", flush=True)
            else:
                app_targets.append((name, port))
        all_targets = to_start + [{"name": n, "port": p, "module": f"brain.{n}" if n != "brain" else "brain.app_brain"}
                                  for n, p in app_targets]
        if not all_targets:
            print("  所有服务都已经在跑了，直接打开控制台即可。", flush=True)
            try:
                webbrowser.open(admin_url)
            except Exception:
                pass
            return

        names = "、".join(t["name"] for t in all_targets)
        print(f"  启动：{names}    关掉这个窗口就一起停。", flush=True)
        print("  正在拉起，首次启动约 10~20 秒，别关窗口 ...", flush=True)
        print(flush=True)

        # ----- Step 3: 拉起全部 -----
        env = dict(os.environ, COMPANION_CHILD="1")
        procs = []
        # 先拉模型服务（它们启动慢，大脑等端口起来就开浏览器）
        for t in to_start:
            procs.append(
                subprocess.Popen([sys.executable, "-u", "-m", t["module"]], env=env)
            )
        # 再拉大服务（brain / console）
        for name, _port in app_targets:
            procs.append(
                subprocess.Popen([sys.executable, "-u", "-m", "brain.launch", name], env=env)
            )

        # 等关键端口真的起来再开浏览器，避免打开一个空白页
        key_ports = [port for _, port in app_targets]
        # 也等模型服务（如果计划拉的话），好让控制台 /health 一看就是绿色
        key_ports += [t["port"] for t in to_start]
        deadline = time.time() + 40
        ready_count = 0
        while time.time() < deadline:
            if any(p.poll() is not None for p in procs):
                break
            alive = sum(1 for p in procs if p.poll() is None)
            busy = sum(1 for p in key_ports if _port_busy(p))
            if busy >= len(key_ports) * 0.8:   # 至少 80% 端口起来就算 ready
                ready_count += 1
                if ready_count >= 2:
                    break
            time.sleep(0.4)

        all_busy = all(_port_busy(p) for p in key_ports)
        if all_busy:
            print()
            print("  [OK] 全部就绪！", flush=True)
            print(f"       大脑      http://127.0.0.1:{settings.BRAIN_PORT}/", flush=True)
            print(f"       控制台    {admin_url}", flush=True)
            for t in to_start:
                print(f"       {t['name']}    http://127.0.0.1:{t['port']}", flush=True)
            print("  这个窗口保持打开就是服务在跑；关掉窗口即全部停止。", flush=True)
            print(flush=True)
            try:
                webbrowser.open(admin_url)
            except Exception:
                pass
        else:
            print()
            print("  [警告] 部分服务没能在 40 秒内就绪，请看下面的报错。", flush=True)

        # 任一子进程退出 → 收掉全部，避免留半截服务
        try:
            while True:
                for p in procs:
                    code = p.poll()
                    if code is not None:
                        for q in procs:
                            if q.poll() is None:
                                q.terminate()
                        if code == 0:
                            print("  服务已停止。", flush=True)
                            return
                        print(f"  [提示] 有服务异常退出（退出码 {code}），其它服务已一并停止。", flush=True)
                        sys.exit(1)
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n  正在停止 ...", flush=True)
            for q in procs:
                if q.poll() is None:
                    q.terminate()
            return

    if which == "console":
        # 兼容旧启动脚本：控制台已并入大脑，这里直接起大脑，不再单独占一个端口。
        print("[提示] 控制台已经并进大脑了，本次实际启动的是大脑（含 /admin.html）。")
        port, url = settings.BRAIN_PORT, f"http://127.0.0.1:{settings.BRAIN_PORT}/admin.html"
        from brain.app_brain import app
    else:
        port, url = settings.BRAIN_PORT, f"http://127.0.0.1:{settings.BRAIN_PORT}/"
        from brain.app_brain import app

    if _port_busy(port):
        print()
        print(f"[提示] 端口 {port} 上已经有服务在跑了，不用重复启动。")
        print(f"       直接用这个地址：{url}")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return

    host = settings.BRAIN_HOST
    try:
        # log_config=None：别让 uvicorn 把 root 的日志handler 清掉，否则写不进文件
        uvicorn.run(app, host=host, port=port, log_level="warning", log_config=None)
    except Exception:
        log = Path(settings.ROOT) / "data" / "logs" / "startup-error.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            print(f"[ERROR] 启动失败，详细错误已写到：{log}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
