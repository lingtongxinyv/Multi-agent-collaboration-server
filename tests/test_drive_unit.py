"""drive.py 阶段一自测：临时 DB + 临时存储根，绝不触碰真实用户数据。"""
import io, shutil, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "panel" / "backend"
sys.path.insert(0, str(BACKEND))

tmp = Path(tempfile.mkdtemp(prefix="drivetest_")) / "run"
tmp.mkdir(parents=True, exist_ok=True)
errors = []
print(f"① 临时环境: {tmp}")

# —— 关键：DB_PATH 由 DSH_SUITE_DATA 环境变量决定（db.py:21），必须在导入前设置 ——
import os
os.environ["DSH_SUITE_DATA"] = str(tmp / "data")
from app import db as dbmod
print(f"   DB_PATH 已重定向 → {dbmod.DB_PATH}")
assert str(dbmod.DB_PATH).startswith(str(tmp)), "隔离失败：未指向临时目录，拒绝继续！"

from app import drive as dr
dr.DRIVE_USERS_ROOT = tmp / "users"   # 进程内直改（单元测试同进程）
print(f"   存储根已重定向 → {dr.DRIVE_USERS_ROOT}")

database = dbmod.get_db()
acc = database.create_account("tester", "pw123456", "user")
print(f"   测试账号建立: {acc['username']} quota_drive_mb={acc.get('quota_drive_mb')} used_drive_mb={acc.get('used_drive_mb')}")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"   {'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))

print("\n② 数据库迁移（新增列是否落地）")
cols = {r["name"] for r in database.query("PRAGMA table_info(accounts)")}
check("accounts.quota_drive_mb 存在", "quota_drive_mb" in cols)
check("accounts.used_drive_mb 存在", "used_drive_mb" in cols)
tables = {r["name"] for r in database.query("SELECT name FROM sqlite_master WHERE type='table'")}
check("drive_shares 表存在", "drive_shares" in tables)
check("默认配额 1024MB", acc.get("quota_drive_mb") == 1024, str(acc.get("quota_drive_mb")))

print("\n③ 路径穿越防护（核心安全）")
root = dr.drive_root_for("tester")
print(f"   网盘根: {root} 权限={oct(root.stat().st_mode)[-3:]}")
for bad in ["../secret", "a/../../etc/passwd", "..", "./x", "a/./b",
            "a/./../../etc/passwd", "../../../etc/passwd", "sub/../../otheruser/drive/x",
            "/etc/passwd", "a//b", "a/..", "..\\..\\windows", ".hidden/./../.."]:
    try:
        dr._resolve(root, bad); check(f"拒绝 {bad!r}", False, "竟然放行")
    except ValueError: check(f"拒绝 {bad!r}", True)
for good in ["a.txt", "sub/dir/f.txt", "中文 名.pdf", ""]:
    try:
        dr._resolve(root, good); check(f"放行 {good!r}", True)
    except Exception as e: check(f"放行 {good!r}", False, str(e))

print("\n④ 目录与文件读写")
dr.mkdir("tester", "docs")
(root / "docs" / "hello.txt").write_text("你好 BellHub", encoding="utf-8")
info = dr.file_info("tester", "docs/hello.txt")
check("file_info 读到中文文件", info["name"] == "hello.txt" and info["size"] > 0, str(info))
check("iter_file 整读正确", b"".join(dr.iter_file("tester", "docs/hello.txt")).decode() == "你好 BellHub")
raw_all = (root / "docs" / "hello.txt").read_bytes()
check("Range 断点续传(字节级)", b"".join(dr.iter_file("tester", "docs/hello.txt", 3, 5)) == raw_all[3:8])
check("Range 起点越界返回空", b"".join(dr.iter_file("tester", "docs/hello.txt", 9999, 5)) == b"")

print("\n⑤ 列目录")
lst = dr.list_dir("tester", "")
check("列出 items", [i["name"] for i in lst["items"]] == ["docs"], str(lst["items"]))
check("返回配额信息", lst["used_mb"] >= 0 and lst["quota_mb"] == 1024, str(lst))

print("\n⑥ 重命名")
dr.rename_path("tester", "docs/hello.txt", "world.txt")
check("重命名成功", (root / "docs" / "world.txt").exists())
for bad in ["../x", "a/b", "", ".."]:
    try:
        dr.rename_path("tester", "docs/world.txt", bad); check(f"拒绝新名 {bad!r}", False)
    except (ValueError, FileExistsError): check(f"拒绝新名 {bad!r}", True)

print("\n⑦ 流式上传 + 配额强制")
small = dr.save_stream("tester", "up.bin", io.BytesIO(b"A" * 500_000))
check("上传 500KB 成功", small["size"] == 500_000, str(small))
check("used_drive_mb 已更新", dr.get_quotas(database.get_account_by_name("tester"))[0] > 0)
database.update_account_quiet(acc["id"], {"quota_drive_mb": 1, "used_drive_mb": 0})
try:
    dr.save_stream("tester", "big.bin", io.BytesIO(b"x" * (2 * 1024 * 1024)))
    check("超配额被拒绝", False, "竟然写成功")
except PermissionError as e:
    check("超配额被拒绝（1MB 配额写 2MB）", True, str(e)[:70])
check("失败后无残留分片", not any((root / ".tmp").glob("*.part")))

print("\n⑧ 单文件上限")
database.update_account_quiet(acc["id"], {"quota_drive_mb": 1024, "used_drive_mb": 0})
database.set_setting("drive_max_file_mb", "1")
try:
    dr.save_stream("tester", "huge.bin", io.BytesIO(b"y" * (2 * 1024 * 1024)))
    check("超单文件上限被拒绝", False, "竟然写成功")
except ValueError as e:
    check("超单文件上限被拒绝（上限1MB传2MB）", True, str(e)[:60])

print("\n⑨ 分享记录")
sh = dr.create_share("tester", "up.bin", note="测试分享")
check("创建分享", sh and sh["relpath"] == "up.bin", str(sh))
check("查询分享", len(database.list_drive_shares("tester")) == 1)
ok, why = dr.share_is_valid(sh); check("分享有效", ok, why)
database.delete_drive_share(sh["id"], "tester")
check("撤销分享", database.get_drive_share(sh["id"]) is None)

print("\n⑩ 删除与占用")
dr.delete_paths("tester", ["docs"])
check("目录递归删除", not (root / "docs").exists())
dr.delete_paths("tester", ["up.bin"])
check("清空后占用归零", dr.usage_mb("tester") == 0.0, str(dr.usage_mb("tester")))

# ══════════════════════════════════════════════════════════════
# 分片上传与跨会话续传
# ══════════════════════════════════════════════════════════════
print("\n⑪ 分片上传：会话创建与分片校验")
acct = database.get_account_by_name("tester")
database.update_account_quiet(acct["id"], {"quota_drive_mb": 1024, "used_drive_mb": 0})
CS = 1024 * 256                       # 测试用小分片（256KB），便于造多片
payload = bytes((i * 7 + 3) % 251 for i in range(CS * 3 + 12345))   # 3 整片 + 尾片
total_chunks = (len(payload) + CS - 1) // CS
sess = dr.upload_init(acct, "big.bin", len(payload), chunk_size=CS)
check("init 返回 sid", bool(sess.get("sid")), str(sess)[:120])
check("总片数正确", sess["total_chunks"] == total_chunks, f"{sess['total_chunks']} vs {total_chunks}")
check("新会话 resumed=False", sess["resumed"] is False)
check("非直传（>8MB 判定为 false 才对，此处小文件直传）", sess["direct"] is True)

print("\n⑫ 分片越界与超大分片（安全）")
sid = sess["sid"]
try:
    dr.save_chunk(acct, sid, total_chunks, b"x" * 10); check("越界索引被拒", False, "放行了")
except ValueError as e: check("越界索引被拒", True)
try:
    dr.save_chunk(acct, sid, -1, b"x" * 10); check("负索引被拒", False, "放行了")
except ValueError: check("负索引被拒", True)
try:
    dr.save_chunk(acct, sid, 0, b"x" * (CS + 1)); check("超大分片被拒", False, "放行了")
except ValueError: check("超大分片被拒", True)
try:
    dr.save_chunk(acct, sid, 0, b""); check("空分片被拒", False, "放行了")
except ValueError: check("空分片被拒", True)
try:
    dr.save_chunk(acct, sid, 0, b"x" * (CS - 1)); check("非末片大小不符被拒", False, "放行了")
except ValueError: check("非末片大小不符被拒", True)

print("\n⑬ 跨用户访问他人会话（安全）")
other = database.create_account("intruder", "pw12345678", "user")
try:
    dr.save_chunk(other, sid, 0, b"x" * CS); check("他人写入被拒", False, "放行了")
except PermissionError: check("他人写入被拒", True)
try:
    dr.complete_upload(other, sid); check("他人拼接被拒", False, "放行了")
except PermissionError: check("他人拼接被拒", True)

print("\n⑭ 分片不完整时拒绝拼接")
try:
    dr.complete_upload(acct, sid); check("缺片时拒绝拼接", False, "竟然成功")
except ValueError as e: check("缺片时拒绝拼接", True, str(e)[:60])

print("\n⑮ 上传全部分片 → 拼接成功且内容一致")
for i in range(total_chunks):
    piece = payload[i * CS:(i + 1) * CS]
    r = dr.save_chunk(acct, sid, i, piece)
    if i == 0:
        check("首片 received=1", r["received"] == 1, str(r))
res = dr.complete_upload(acct, sid)
check("拼接返回大小一致", res["size"] == len(payload), str(res)[:100])
written = (root / "big.bin").read_bytes()
check("磁盘内容与源完全一致", written == payload, f"{len(written)} vs {len(payload)}")
check("会话已清理", database.get_drive_upload(sid) is None)
check("分片目录已清理", not (root / ".upload" / sid).exists())
check("占用已更新", dr.usage_mb("tester") > 0)

print("\n⑯ 跨会话续传：① 路径+大小 命中")
sess2 = dr.upload_init(acct, "resume.bin", len(payload), chunk_size=CS)
sid2 = sess2["sid"]
for i in range(2):                                  # 只传前 2 片就"断线"
    dr.save_chunk(acct, sid2, i, payload[i * CS:(i + 1) * CS])
again = dr.upload_init(acct, "resume.bin", len(payload), chunk_size=CS)
check("重发 init 命中同一会话", again["sid"] == sid2, f"{again['sid']} vs {sid2}")
check("resumed=True", again["resumed"] is True)
check("已传分片列表正确", again["uploaded"] == [0, 1], str(again["uploaded"]))
for i in range(2, total_chunks):                    # 只补传剩余分片
    dr.save_chunk(acct, sid2, i, payload[i * CS:(i + 1) * CS])
res2 = dr.complete_upload(acct, sid2)
check("续传后拼接成功", (root / "resume.bin").read_bytes() == payload, str(res2)[:80])

print("\n⑰ 跨会话续传：② 改名后靠哈希命中")
sess3 = dr.upload_init(acct, "hashA.bin", len(payload), sha256="deadbeef" * 8, chunk_size=CS)
sid3 = sess3["sid"]
dr.save_chunk(acct, sid3, 0, payload[:CS])
hit = dr.upload_init(acct, "hashB.bin", len(payload), sha256="deadbeef" * 8, chunk_size=CS)
check("换路径后按哈希命中旧会话", hit["sid"] == sid3, f"{hit['sid']} vs {sid3}")
check("哈希命中标记 resumed", hit["resumed"] is True)
dr.cancel_upload(acct, sid3)
check("取消上传后会话消失", database.get_drive_upload(sid3) is None)

print("\n⑱ 超配额会话不得创建 / 拼接复核")
# 注意：update_account_quiet 只改库，不会更新内存里的 acct 字典；
# 真实环境每次请求都由 auth.current_account 重新读库，所以这里也必须重新取。
database.update_account_quiet(acct["id"], {"quota_drive_mb": 1, "used_drive_mb": 0})
database.set_setting("drive_max_file_mb", "1024")     # 先排除单文件上限干扰（⑧ 曾把它设成 1MB）
acct = database.get_account_by_name("tester")
try:
    dr.upload_init(acct, "toobig.bin", 5 * 1024 * 1024); check("超配额 init 被拒", False, "放行了")
except PermissionError as e: check("超配额 init 被拒", True, str(e)[:60])
# 会话已建但配额后被调小 → 拼接时必须再拦一次
database.update_account_quiet(acct["id"], {"quota_drive_mb": 1024, "used_drive_mb": 0})
s4 = dr.upload_init(database.get_account_by_name("tester"), "quota_race.bin", len(payload), chunk_size=CS)
for i in range(s4["total_chunks"]):
    dr.save_chunk(acct, s4["sid"], i, payload[i * CS:(i + 1) * CS])
# 载荷约 0.8MB：配额必须压到 0.5MB 以下才能触发复核（否则在配额内，成功是正确行为）
database.update_account_quiet(acct["id"], {"quota_drive_mb": 1, "used_drive_mb": 1.0})
try:
    dr.complete_upload(database.get_account_by_name("tester"), s4["sid"])
    check("拼接时配额复核生效", False, "竟然成功")
except PermissionError as e:
    check("拼接时配额复核生效", True, str(e)[:60])
check("被拒后会话仍保留（可续传）", database.get_drive_upload(s4["sid"]) is not None)

print("\n⑲ 清理：过期半成品与孤儿目录")
import time as _t
database.update_account_quiet(acct["id"], {"quota_drive_mb": 1024, "used_drive_mb": 0})
database.set_setting("drive_max_file_mb", "1024")     # 恢复（⑧ 曾设成 1MB）
# 重建一个时间较旧的会话，确保它进入过期集合
s_old = dr.upload_init(acct, "expire_me.bin", len(payload), chunk_size=CS)
dr.save_chunk(acct, s_old["sid"], 0, payload[:CS])
with database.tx() as c:
    c.execute("UPDATE drive_uploads SET updated_at=? WHERE sid=?",
              (_t.time() - 8 * 86400, s_old["sid"]))
with database.tx() as c:      # 把会话时间改到 8 天前
    c.execute("UPDATE drive_uploads SET updated_at=? WHERE sid=?",
              (_t.time() - 8 * 86400, s4["sid"]))
clean = dr.cleanup_expired_uploads(7)
check("过期会话被清理", clean["removed"] >= 1, str(clean))
check("过期会话目录被删除", not (root / ".upload" / s_old["sid"]).exists())
orphan = root / ".upload" / "orphan_dir_xyz"
orphan.mkdir(parents=True, exist_ok=True)
(orphan / "0.part").write_bytes(b"z" * 100)
old = _t.time() - 8 * 86400
os.utime(orphan, (old, old))
clean2 = dr.cleanup_expired_uploads(7)
check("孤儿目录被回收", clean2["orphans"] >= 1, str(clean2))
check("孤儿目录已删除", not orphan.exists())

print("\n⑲b 回归：分片暂存不得计入用户配额（曾实测发现设计缺陷）")
database.update_account_quiet(acct["id"], {"quota_drive_mb": 1024, "used_drive_mb": 0})
dr.refresh_drive_usage("tester")
base_used = dr.usage_mb("tester")
s5 = dr.upload_init(database.get_account_by_name("tester"), "pending.bin",
                    len(payload), chunk_size=CS)
for i in range(s5["total_chunks"]):
    dr.save_chunk(database.get_account_by_name("tester"), s5["sid"], i,
                  payload[i * CS:(i + 1) * CS])
after = dr.usage_mb("tester")
check("传完全部分片但未拼接时，占用不变", abs(after - base_used) < 0.01,
      f"{base_used} → {after}（.upload 应被排除）")
updir = root / ".upload" / s5["sid"]
check("分片确实存在于磁盘", updir.exists() and any(updir.glob("*.part")))
dr.cancel_upload(database.get_account_by_name("tester"), s5["sid"])
check("取消后分片目录被清理", not updir.exists())

print("\n⑳ 回归：一次性直传仍然可用")
small2 = dr.save_stream("tester", "direct.txt", io.BytesIO(b"direct upload ok"))
check("直传接口未受影响", small2["size"] == len(b"direct upload ok"), str(small2))


shutil.rmtree(tmp, ignore_errors=True)
print(f"\n结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    print("失败项: " + "、".join(FAIL)); sys.exit(1)
print("全部通过 ✅（真实用户数据未被触碰）")
