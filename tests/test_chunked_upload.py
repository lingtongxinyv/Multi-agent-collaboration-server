"""分片上传 + 跨会话续传（**隔离版**，2026-10-14 从 /root/1012 迁入并改造）。

与原版的区别：原版连真实库 + 线上 :80（用真 admin 的签名 cookie），只能人工跑；
本版自己起隔离服务（临时 DSH_SUITE_DATA + 临时 DSH_DRIVE_ROOT），端口 8096。

重点：init/chunk/complete 全流程、断线续传不重传已完成片、幂等、缺片拒拼、
越界/超大分片拦截、匿名拒绝、管理端清理。

用法：~/dsh-suite/.venv/bin/python tests/test_chunked_upload.py
"""
import hashlib
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

ROOT = Path(tempfile.mkdtemp(prefix="chunkapi-"))
os.environ["DSH_SUITE_DATA"] = str(ROOT / "data")
os.environ["DSH_DRIVE_ROOT"] = str(ROOT / "users")
PORT = 8096
BASE = f"http://127.0.0.1:{PORT}"
CHUNK = 1024 * 1024                     # 1MB/片，便于造多片又跑得快

sys.path.insert(0, str(BACKEND))
from app import auth as A, db as D  # noqa: E402

assert str(D.DB_PATH).startswith(str(ROOT)), f"隔离失败: {D.DB_PATH}"
from app import drive as dr  # noqa: E402

assert str(dr.DRIVE_USERS_ROOT).startswith(str(ROOT)), f"隔离失败: {dr.DRIVE_USERS_ROOT}"
print(f"[env] DB={D.DB_PATH}\n      存储根={dr.DRIVE_USERS_ROOT}")

acc = D.get_db().create_account("chunk-admin", "pw-chunk-1", "admin", "分片测试管理员")
COOKIE = A._encode({"uid": acc["id"], "role": acc["role"],
                    "exp": time.time() + 3600, "iat": time.time()})
TMPDIR = "__chunk_test__"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


def req(method, path, json_body=None, data=None, headers=None, raw=False):
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


def multipart(fields, files):
    b = "----chunktest"
    parts = []
    for k, v in fields.items():
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fn, content) in files.items():
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


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
    print("\n① 准备：建测试目录与 5MB 测试文件（1MB/片 → 5 片）")
    req("POST", "/api/drive/mkdir", json_body={"path": TMPDIR})
    payload = bytes((i * 13 + 7) % 251 for i in range(5 * 1024 * 1024 - 3))
    sha = hashlib.sha256(payload).hexdigest()
    total_chunks = (len(payload) + CHUNK - 1) // CHUNK

    print("\n② init：创建会话")
    st, r = req("POST", "/api/drive/upload/init",
                json_body={"path": f"{TMPDIR}/chunked.bin", "size": len(payload),
                           "sha256": sha, "chunk_size": CHUNK})
    sid = r.get("sid") if isinstance(r, dict) else None
    check("init 200 且返回 sid", st == 200 and sid, f"{st} {str(r)[:150]}")
    check("total_chunks 正确", r.get("total_chunks") == total_chunks, str(r.get("total_chunks")))
    check("新会话 resumed=False", r.get("resumed") is False)
    check("initial uploaded 为空", r.get("uploaded") == [], str(r.get("uploaded")))

    def put_chunk(i):
        piece = payload[i * CHUNK:(i + 1) * CHUNK]
        body, ctype = multipart({"sid": sid, "index": str(i)}, {"data": (f"{i}.part", piece)})
        return req("POST", "/api/drive/upload/chunk", data=body, headers={"Content-Type": ctype})

    print("\n③ 上传前 2 片（模拟传到一半断线）")
    for i in range(2):
        st, r = put_chunk(i)
        if i == 0:
            check("首片上传 200", st == 200, f"{st} {str(r)[:120]}")
            check("received 计数正确", r.get("received") == 1, str(r))

    print("\n④ 查询会话状态")
    st, r = req("GET", f"/api/drive/upload/status?sid={sid}")
    check("status 200", st == 200, f"{st}")
    check("已收分片为 [0,1]", r.get("uploaded") == [0, 1], str(r.get("uploaded")))

    print("\n⑤ 断线重连：重新 init 同一文件应命中会话")
    st, r2 = req("POST", "/api/drive/upload/init",
                 json_body={"path": f"{TMPDIR}/chunked.bin", "size": len(payload),
                            "sha256": sha, "chunk_size": CHUNK})
    check("命中同一会话", r2.get("sid") == sid, f"{r2.get('sid')} vs {sid}")
    check("resumed=True", r2.get("resumed") is True)
    check("已传分片保持 [0,1]", r2.get("uploaded") == [0, 1], str(r2.get("uploaded")))

    print("\n⑥ 幂等：重传第 0 片不应改变状态")
    st, r = put_chunk(0)
    check("重传成功", st == 200, f"{st}")
    st, r = req("GET", f"/api/drive/upload/status?sid={sid}")
    check("仍只有 2 片（未被重复计数）", r.get("received") == 2, str(r.get("received")))

    print("\n⑦ 缺片时不得拼接")
    st, r = req("POST", "/api/drive/upload/complete", json_body={"sid": sid})
    check("缺片拼接被拒（400）", st == 400, f"{st} {str(r)[:120]}")
    check("错误信息含缺失片数", "缺失" in str(r.get("detail", "")), str(r)[:150])

    print("\n⑧ 续传剩余分片 → 完成拼接")
    for i in range(2, total_chunks):
        st, r = put_chunk(i)
    check("补齐全部分片", st == 200, f"{st} {str(r)[:120]}")
    st, r = req("POST", "/api/drive/upload/complete", json_body={"sid": sid})
    check("complete 200", st == 200, f"{st} {str(r)[:150]}")
    check("返回大小一致", r.get("size") == len(payload), str(r.get("size")))
    check("used_mb 已更新", r.get("used_mb", 0) > 0, str(r)[:120])

    print("\n⑨ 下载拼接结果并比对 sha256")
    st, got = req("GET", f"/api/drive/download?path={urllib.parse.quote(TMPDIR + '/chunked.bin')}",
                  raw=True)
    check("下载 200", st == 200, f"{st}")
    check("内容哈希与源一致", isinstance(got, bytes) and hashlib.sha256(got).hexdigest() == sha,
          f"{hashlib.sha256(got).hexdigest()[:16] if isinstance(got, bytes) else got}")

    print("\n⑩ 会话已清理")
    st, _ = req("GET", f"/api/drive/upload/status?sid={sid}")
    check("会话不存在（404）", st == 404, f"{st}")

    print("\n⑪ 安全：越界索引 / 超大分片 / 跨会话")
    st, r = req("POST", "/api/drive/upload/init",
                json_body={"path": f"{TMPDIR}/sec.bin", "size": 3 * CHUNK, "chunk_size": CHUNK})
    sid2 = r["sid"]
    body, ctype = multipart({"sid": sid2, "index": "99"}, {"data": ("x", b"a" * 100)})
    st, r = req("POST", "/api/drive/upload/chunk", data=body, headers={"Content-Type": ctype})
    check("越界索引被拒（400）", st == 400, f"{st} {str(r)[:100]}")
    body, ctype = multipart({"sid": sid2, "index": "0"}, {"data": ("x", b"a" * (CHUNK + 5000))})
    st, r = req("POST", "/api/drive/upload/chunk", data=body, headers={"Content-Type": ctype})
    check("超大分片被拒（400）", st == 400, f"{st} {str(r)[:100]}")
    st, r = req("POST", "/api/drive/upload/complete", json_body={"sid": "0" * 32})
    check("伪造 sid 被拒", st in (403, 404), f"{st} {str(r)[:100]}")
    st, r = req("GET", "/api/drive/upload/status?sid=" + "0" * 32)
    check("伪造 sid 查状态被拒", st == 404, f"{st}")

    print("\n⑫ 匿名访问（未登录）全部拒绝")
    anon = urllib.request.build_opener()
    for path, method, extra in [("/api/drive/upload/init", "POST",
                                 {"Content-Type": "application/json"}),
                                ("/api/drive/upload/status?sid=x", "GET", {})]:
        try:
            body = json.dumps({"path": "x", "size": 10}).encode() if method == "POST" else None
            anon.open(urllib.request.Request(BASE + path, data=body, method=method, headers=extra),
                      timeout=20)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check(f"匿名 {path} 被拒", code == 401, f"HTTP {code}")

    print("\n⑬ 管理端：查看与清理未完成上传")
    st, r = req("GET", "/api/admin/drive/uploads")
    check("管理端列出未完成上传 200", st == 200, f"{st}")
    check("含刚建的会话", any(u["sid"] == sid2 for u in r.get("uploads", [])), str(r)[:150])
    st, r = req("POST", "/api/admin/drive/uploads/cleanup?days=0")
    check("立即清理成功", st == 200 and r.get("removed", 0) >= 1, f"{st} {str(r)[:120]}")
    st, r = req("GET", "/api/admin/drive/uploads")
    check("清理后列表为空", len(r.get("uploads", [])) == 0, str(r)[:120])

    print("\n⑭ 清理测试残留")
    st, r = req("POST", "/api/drive/delete", json_body={"paths": [TMPDIR]})
    check("删除测试目录", st == 200 and r.get("deleted", 0) >= 1, f"{st} {r}")
    st, r = req("GET", "/api/drive/list")
    check("无残留", TMPDIR not in [i["name"] for i in (r.get("items") or [])])
    check("占用归零", r.get("used_mb") == 0.0, f"used={r.get('used_mb')}")
    real_dir = Path.home() / "dsh-users" / "chunk-admin"
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
