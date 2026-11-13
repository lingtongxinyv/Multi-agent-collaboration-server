"""网盘接口端到端测试（**隔离版**，2026-10-14 从 /root/1012 迁入并改造）。

与原版的区别：原版连真实库 + 线上 :8010（用真 admin 的签名 cookie），只能人工跑；
本版自己起一个隔离服务（临时 DSH_SUITE_DATA + 临时 DSH_DRIVE_ROOT），
用临时库里新建的 admin 账号，测完即销毁 —— 可以进 run_all.sh 与面板一键自检。

覆盖：列表/配额、上传下载、Range（含尾部）、路径穿越防护、中文与 emoji 文件名、
重命名/分享/预览、匿名拒绝、管理员视图、清理无残留。

用法：~/dsh-suite/.venv/bin/python tests/test_drive_api.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"

ROOT = Path(tempfile.mkdtemp(prefix="driveapi-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")
os.environ["DSH_DRIVE_ROOT"] = str(ROOT / "users")
PORT = 8098                       # 避开 link 套件的 8099-8103 与元测试的 8097
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(BACKEND))
from app import auth as A, db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败：连到了 {D.DB_PATH}！"
from app import drive as dr  # noqa: E402

assert str(dr.DRIVE_USERS_ROOT).startswith(str(ROOT)), \
    f"隔离失败：存储根指向 {dr.DRIVE_USERS_ROOT}！"
print(f"[env] DB={D.DB_PATH}\n      存储根={dr.DRIVE_USERS_ROOT}")

acc = D.get_db().create_account("drive-admin", "pw-drive-1", "admin", "网盘测试管理员")
COOKIE = A._encode({"uid": acc["id"], "role": acc["role"],
                    "exp": time.time() + 3600, "iat": time.time()})

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def req(method, path, json_body=None, headers=None, raw=False, data=None):
    hdrs = {"Cookie": f"panel_session={COOKIE}"}
    hdrs.update(headers or {})
    body = data
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            rb = resp.read()
            return resp.status, (rb if raw else (json.loads(rb) if rb else None))
    except urllib.error.HTTPError as e:
        rb = e.read()
        try:
            return e.code, json.loads(rb)
        except Exception:                               # noqa: BLE001
            return e.code, rb.decode("utf-8", "replace")[:200]


env = {**os.environ}
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                        "--port", str(PORT), "--log-level", "warning"],
                       cwd=str(BACKEND), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/drive/list", timeout=1)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(0.5)

try:
    print("\n② 列表与配额")
    st, r = req("GET", "/api/drive/list")
    check("列表 200", st == 200, f"{st} {str(r)[:120]}")
    check("含配额字段", isinstance(r, dict) and "quota_mb" in r and "max_file_mb" in r, str(r)[:150])

    print("\n③ 建目录 + 流式上传")
    st, r = req("POST", "/api/drive/mkdir", json_body={"path": "__selftest__"})
    check("创建目录", st == 200, f"{st} {r}")
    payload = ("BellHub 网盘隔离自测载荷 —— 中文与符号 ✅\n" * 1500).encode()
    b = "----selftest"
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"path\"\r\n\r\n"
            f"__selftest__/hello.txt\r\n--{b}\r\nContent-Disposition: form-data; "
            f"name=\"file\"; filename=\"hello.txt\"\r\nContent-Type: text/plain\r\n\r\n"
            ).encode() + payload + f"\r\n--{b}--\r\n".encode()
    st, r = req("POST", "/api/drive/upload", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    check("上传成功且字节数一致", st == 200 and r.get("size") == len(payload), f"{st} {str(r)[:150]}")
    check("占用已更新", isinstance(r, dict) and r.get("used_mb", 0) > 0, str(r)[:120])

    print("\n④ 下载 / Range / 内容一致性")
    st, got = req("GET", "/api/drive/download?path=__selftest__/hello.txt", raw=True)
    check("整文件下载字节一致", st == 200 and got == payload,
          f"{st} len={len(got) if isinstance(got, bytes) else got}")
    st, part = req("GET", "/api/drive/download?path=__selftest__/hello.txt",
                   headers={"Range": "bytes=0-9"}, raw=True)
    check("Range 206 且切片正确", st == 206 and isinstance(part, bytes) and part == payload[:10], f"{st}")
    st, part2 = req("GET", "/api/drive/download?path=__selftest__/hello.txt",
                    headers={"Range": "bytes=-8"}, raw=True)
    check("尾部 Range 正确", st == 206 and part2 == payload[-8:], f"{st}")

    print("\n⑤ 路径穿越（越权）防护")
    for bad in ["../panel.db", "__selftest__/../../panel.db", "/etc/passwd", "..", "a/./../.."]:
        st, _ = req("GET", f"/api/drive/download?path={urllib.parse.quote(bad)}")
        check(f"下载拒绝 {bad!r}", st in (400, 404), f"HTTP {st}")
    st, _ = req("GET", "/api/drive/list?path=..%2F..")
    check("列表拒绝越界", st == 400, f"HTTP {st}")

    print("\n⑤b 中文/emoji 文件名（回归：曾因 latin-1 报头编码导致下载 500）")
    for fname in ["中文测试.txt", "带空格 与符号(1).bin", "emoji🎉文件.dat"]:
        data = ("中文内容测试 " + fname).encode("utf-8") * 500
        bb = "----cn"
        bd = (f"--{bb}\r\nContent-Disposition: form-data; name=\"path\"\r\n\r\n"
              f"{fname}\r\n--{bb}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
              ).encode("utf-8") + data + f"\r\n--{bb}--\r\n".encode()
        st, r = req("POST", "/api/drive/upload", data=bd,
                    headers={"Content-Type": f"multipart/form-data; boundary={bb}"})
        check(f"上传 {fname!r}", st == 200, f"{st} {str(r)[:120]}")
        st, got = req("GET", f"/api/drive/download?path={urllib.parse.quote(fname)}", raw=True)
        check(f"下载 {fname!r} 内容一致", st == 200 and got == data, f"HTTP {st}")
        st, _ = req("POST", "/api/drive/delete", json_body={"paths": [fname]})
        check(f"清理 {fname!r}", st == 200, f"{st}")

    print("\n⑥ 重命名 / 分享 / 预览")
    st, r = req("POST", "/api/drive/rename",
                json_body={"path": "__selftest__/hello.txt", "new_name": "renamed.txt"})
    check("重命名", st == 200, f"{st} {r}")
    st, r = req("POST", "/api/drive/share",
                json_body={"path": "__selftest__/renamed.txt", "note": "隔离自测"})
    sid = r.get("id") if isinstance(r, dict) else None
    check("创建分享", st == 200 and sid, f"{st} {r}")
    st, got = req("GET", f"/api/drive/shared/{sid}", raw=True)
    check("经分享下载内容一致", st == 200 and got == payload, f"{st}")
    st, got = req("GET", "/api/drive/preview?path=__selftest__/renamed.txt", raw=True)
    check("预览内容一致", st == 200 and got == payload, f"{st}")

    print("\n⑦ 未登录访问应被拒绝（管理员定的策略：分享仅登录可见）")
    anon = urllib.request.build_opener()
    for p in ["/api/drive/list", f"/api/drive/shared/{sid}"]:
        try:
            anon.open(urllib.request.Request(BASE + p), timeout=30)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check(f"匿名访问 {p} 被拒", code == 401, f"HTTP {code}")

    print("\n⑧ 管理员视图")
    st, r = req("GET", "/api/admin/drive")
    check("管理员视图 200", st == 200, f"{st} {str(r)[:120]}")
    check("列出用户（隔离库里就是自己）",
          isinstance(r, dict) and len(r.get("users", [])) >= 1, str(r)[:200])

    print("\n⑨ 清理")
    if sid:
        st, _ = req("DELETE", f"/api/drive/share/{sid}")
        check("撤销分享", st == 200, f"HTTP {st}")
    st, r = req("POST", "/api/drive/delete", json_body={"paths": ["__selftest__"]})
    check("删除测试目录", st == 200 and r.get("deleted") == 1, f"{st} {r}")
    st, r = req("GET", "/api/drive/list")
    check("无残留", "__selftest__" not in [i["name"] for i in (r.get("items") or [])])
    check("占用归零", r.get("used_mb") == 0.0, f"used={r.get('used_mb')}")
    real_dir = Path.home() / "dsh-users" / "drive-admin"
    check("没有写进真实存储根", not real_dir.exists(), str(real_dir))
finally:
    srv.terminate()
    try:
        srv.wait(timeout=8)
    except subprocess.TimeoutExpired:
        srv.kill()

print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项: " + "、".join(FAIL))
    sys.exit(1)
print("全部通过 ✅")
