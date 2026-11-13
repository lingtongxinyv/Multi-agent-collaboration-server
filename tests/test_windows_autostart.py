"""Windows 自启回归测试：必须**不需要管理员权限**。

背景（2026-10-14 管理员实机踩到）：
    D:\\...>bellhub-connector autostart
    错误: 拒绝访问。
    ✗ 创建失败
根因：`schtasks /SC ONLOGON` 建计划任务在普通用户下会 ERROR_ACCESS_DENIED。
修法：改用当前用户「启动」文件夹 + VBS（窗口样式 0 = 后台无黑框），完全不需要管理员。

本测试在 Linux 上跑，但把 platform/subprocess/APPDATA 全部换成假的，
因此能真正验证"Windows 分支不调用 schtasks 创建任务、且写出了正确的 VBS"。

用法：~/dsh-suite/.venv/bin/python tests/test_windows_autostart.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKERS = REPO / "workers"

ROOT = Path(tempfile.mkdtemp(prefix="winautostart-"))
FAKE_APPDATA = ROOT / "AppData" / "Roaming"
FAKE_APPDATA.mkdir(parents=True)
os.environ["APPDATA"] = str(FAKE_APPDATA)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeOS:
    """只影响被测模块的 os 垫片：name 装成 nt，其余属性透传真实 os。

    ⚠ 不能直接写 `os.name = "nt"` —— 那会让全局 pathlib 实例化 WindowsPath，
    在 Linux 上立刻 NotImplementedError（本测试第一版就踩了这个）。
    """

    name = "nt"

    def __getattr__(self, key):
        return getattr(os, key)


def patch_windows(mod):
    """把模块伪装成 Windows，并拦下所有 subprocess 调用以便断言。"""
    calls = []
    mod.platform.system = lambda: "Windows"          # type: ignore[assignment]
    mod.os = FakeOS()                                # type: ignore[assignment]

    class FakeCompleted:
        returncode = 1                               # 真实环境下 schtasks 会失败
        stdout = ""
        stderr = "错误: 拒绝访问。"

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
        return FakeCompleted()

    mod.subprocess.run = fake_run                     # type: ignore[assignment]
    return calls


class Args:
    def __init__(self, remove: bool = False):
        self.remove = remove


print("[1] 连接器：Windows autostart 不再依赖 schtasks")
conn = load(WORKERS / "bellhub_connector.py", "conn_mod")
calls = patch_windows(conn)
startup = conn.windows_startup_dir()
check("启动目录指向当前用户的 Startup（基于 APPDATA）",
      str(startup).startswith(str(FAKE_APPDATA)) and str(startup).endswith("Startup"),
      str(startup))

rc = conn.cmd_autostart(Args(remove=False))
vbs = startup / "bellhub-connector.vbs"
check("返回成功", rc == 0, str(rc))
check("写出了 VBS 启动项", vbs.is_file(), str(vbs))
body = vbs.read_text(encoding="utf-8") if vbs.is_file() else ""
check("VBS 用窗口样式 0（后台无黑框）", ', 0, False' in body, body)
check("VBS 调用了 run 模式", "run" in body, body)
created = [c for c in calls if len(c) > 1 and c[0].endswith("schtasks") and "/Create" in c]
check("创建时**没有**调用 schtasks /Create（那步就是『拒绝访问』的来源）",
      not created, str(created))

rc = conn.cmd_autostart(Args(remove=True))
check("--remove 返回成功", rc == 0, str(rc))
check("--remove 删掉了 VBS", not vbs.exists())

print("\n[2] 设备 agent：同样处理")
agent = load(WORKERS / "device_agent.py", "agent_mod")
calls2 = patch_windows(agent)
startup2 = agent.windows_startup_dir()
check("启动目录同样基于 APPDATA", str(startup2).startswith(str(FAKE_APPDATA)), str(startup2))
rc = agent.cmd_autostart(False)
avbs = startup2 / "bellhub-device-agent.vbs"
check("写出了设备 agent 的 VBS", avbs.is_file(), str(avbs))
abody = avbs.read_text(encoding="utf-8") if avbs.is_file() else ""
check("设备 VBS 同样是后台无窗口", ', 0, False' in abody, abody)
check("设备侧创建时也没调 schtasks /Create",
      not [c for c in calls2 if len(c) > 1 and c[0].endswith("schtasks") and "/Create" in c])
rc = agent.cmd_autostart(True)
check("设备 --remove 生效", rc == 0 and not avbs.exists())

print("\n[3] VBS 引号转义正确（路径含空格也能跑）")
calls3 = patch_windows(conn)
# 用一个含空格的假可执行路径，验证内嵌双引号被写成两个双引号
conn._self_command = lambda: '"C:\\Program Files\\BellHub\\bellhub-connector.exe" run'
conn.cmd_autostart(Args(remove=False))
body3 = vbs.read_text(encoding="utf-8")
check("含空格路径的引号被正确转义",
      '"""C:\\Program Files\\BellHub\\bellhub-connector.exe"" run"' in body3, body3)

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
