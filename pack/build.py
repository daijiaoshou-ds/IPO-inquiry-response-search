# -*- coding: utf-8 -*-
"""生成给读者下载的离线发布包。

    .venv\\Scripts\\python.exe pack\\build.py

产物：pack\\_out\\问询检索_v<版本>_<年月>.zip   （当前约 95 MB）

思路：
  * 数据库里【不带】FTS 触发索引和 4 个二级索引，读者第一次运行时自建（实测 6~10 秒），
    光这一步就把下载量从 112 MB 压到 50 MB；
  * 运行时用 python.org 的 embeddable 包（python.exe 有 PSF 签名），
    不用 PyInstaller，避开 SmartScreen 和杀软误报；
  * 环境/库（fastapi、uvicorn）用本机 pip 装到 python\\Lib\\site-packages，
    同是 CPython 3.12，cp312 的 wheel 通用。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.6.2"
RELEASE = time.strftime("%Y-%m")      # 数据截止月份，也是发布日期
AUTHOR = "呆叫兽"
NAME = "问询检索"
# 3.12.11 之后 python.org 只发 security-only 的源码版，不再提供 Windows 二进制，
# 所以能拿到 embed-amd64.zip 的最新版就是 3.12.10（已逐个 HEAD 验过）。
PY_VER = "3.12.10"

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "pack" / "_out"
CACHE = ROOT / "pack" / "_cache"
SRC_DB = ROOT / "data" / "queries.db"

# 服务端真正用到的文件；pdfread / build_db 这些不进包
APP_FILES = ["__init__.py", "server.py", "search.py", "extract.py", "normalize.py",
             "engine_rust.py",
             # 上传整份函用：ingest 分派 pdf/docx，pdfread 是 PDF 逐行还原
             "ingest.py", "pdfread.py",
             # 导出 Excel 用：server 在模块级 import，缺了它连服务都起不来
             "guide.py"]
DROP_INDEXES = ["idx_q_align", "idx_q_src", "idx_q_norm", "idx_q_letter"]


def human(n: int) -> str:
    return "%.1f MB" % (n / 1048576)


_T_STEP = [time.time()]


def step(msg: str) -> None:
    """每步开头打时间。构建要几分钟，卡住时得能一眼看出卡在哪一步——
    上一版没有这个，7 分钟没输出只能靠猜。"""
    print("\n== %s   [+%.0fs]" % (msg, time.time() - _T_STEP[0]))


def download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1024 * 1024:
        print("   已缓存：" + dest.name)
        return True
    print("   下载：" + url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    sys.stdout.write("\r   %3d%%  %.1f/%.1f MB" % (done * 100 // total,
                                                                  done / 1048576, total / 1048576))
                    sys.stdout.flush()
        print()
        return True
    except Exception as exc:  # noqa: BLE001
        print("\n   失败：" + str(exc)[:160])
        dest.unlink(missing_ok=True)
        return False


def db_facts(path: Path) -> dict:
    """从库里取几个真实数字，填进使用说明（免得每次加板块都要手动改文案）。"""
    con = sqlite3.connect(str(path))
    try:

        def one(sql):
            return con.execute(sql).fetchone()[0]

        boards = [r[0] for r in con.execute(
            "SELECT board, COUNT(*) c FROM letter GROUP BY board ORDER BY c DESC")]
        return {
            "__LETTERS__": "{:,}".format(one("SELECT COUNT(*) FROM letter")),
            "__COMPANIES__": "{:,}".format(one("SELECT COUNT(DISTINCT company) FROM letter")),
            "__QUERIES__": "{:,}".format(one("SELECT COUNT(*) FROM query")),
            "__BOARDS__": " · ".join(boards),
        }
    finally:
        con.close()


def strip_db(dst: Path) -> None:
    """复制一份数据库，去掉能重建的 FTS 与二级索引。"""
    if dst.exists():
        dst.unlink()
    con = sqlite3.connect(str(SRC_DB))
    try:
        con.execute("VACUUM INTO '" + str(dst).replace("'", "''") + "'")
    finally:
        con.close()
    con = sqlite3.connect(str(dst))
    try:
        con.execute("DROP TABLE IF EXISTS query_fts")
        for name in DROP_INDEXES:
            con.execute("DROP INDEX IF EXISTS " + name)
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("VACUUM")
    finally:
        con.close()


def main() -> None:
    t_all = time.time()
    pkg = OUT / ("%s_v%s" % (NAME, VERSION))
    if pkg.exists():
        # 不直接 rmtree：上一版目录里有两三千个文件，会被"批量删除"保护拦下来，
        # 构建在第一步就 abort（2026-10-29 复打时撞过一次）。改名挪开最省事，
        # 顺手给个 _stale 前缀，攒多了自己删也行。
        stale = OUT / ("_stale_" + pkg.name + time.strftime("_%H%M%S"))
        pkg.rename(stale)
        print("   上一版目录改名为 " + stale.name + "（可手动删除）")
        shutil.rmtree(stale, ignore_errors=True)
    pkg.mkdir(parents=True)
    print("%s v%s  (数据截至 %s)  ->  %s" % (NAME, VERSION, RELEASE, pkg))

    # ---- 1. 便携 Python ------------------------------------------------
    step("1/7 取便携 Python " + PY_VER)
    pyzip = CACHE / ("python-%s-embed-amd64.zip" % PY_VER)
    if not download("https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip"
                    % (PY_VER, PY_VER), pyzip):
        sys.exit("下载便携 Python 失败，检查网络后重跑")
    pydir = pkg / "python"
    with zipfile.ZipFile(pyzip) as z:
        z.extractall(pydir)
    # 让解释器能找到我们装的库（不启用 import site，避免读到读者机器上的用户级包）
    pth = next(pydir.glob("python3*._pth"))
    pth.write_text("python312.zip\n.\nLib\\site-packages\n", encoding="ascii")
    print("   解释器：" + pydir.name + "  " + human(int(sum(f.stat().st_size for f in pydir.rglob("*") if f.is_file()))))

    # ---- 2. 依赖 -------------------------------------------------------
    step("2/7 安装依赖到包内")
    target = pydir / "Lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    # 必须走 subprocess（而不是 os.system）：本项目的路径含中文，
    # os.system 会让 cmd 用 OEM 码页重新解析一遍，中文路径直接报"文件名语法不正确"。
    # 版本钉死成"当前 venv 里装的那一套"：
    #   ① 读者跑到的和作者本机一致，出问题可复现；
    #   ② 版本没变，pip 直接命中本地 wheel 缓存，不会去下"今天最新"的那个。
    # 2026-10-29 的 v1.6 构建就卡死在这里：pip 解析到 pillow 13.0.0（venv 里是 12.3.0），
    # 7.3 MB 那个 wheel 下载不动，整个构建停了 7 分钟没有任何输出。
    # 升级依赖是另一件事，不该顺带发生在打包时。
    constraints = CACHE / "constraints.txt"
    freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                            capture_output=True, text=True, encoding="utf-8").stdout
    constraints.write_text(freeze, encoding="utf-8")
    # 本地 wheelhouse：某些 wheel 从 files.pythonhosted.org 下来只有 20 KB/s
    # （2026-10-29 实测 pillow 6.9 MB 要 5 分钟，pip 就干等在那儿）。
    # 把那个 wheel 丢进 pack/_cache/wheels/ 就会走本地文件，不再碰网络。
    house = CACHE / "wheels"
    house.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--no-warn-script-location",
           "--progress-bar", "off", "--disable-pip-version-check",
           "--timeout", "20", "--retries", "2",
           "--find-links", str(house),
           "-c", str(constraints), "--target", str(target), "fastapi", "uvicorn",
           # 网页上传整份函要用的解析组件（pdfplumber 依赖 pdfminer.six + Pillow，
           # python-docx 依赖 lxml，合计约 37 MB 解压后）
           # openpyxl 是导出《参考答案指引》用的；server 会 import guide.py，缺了直接起不来
           "pdfplumber", "python-docx", "openpyxl",
           # 上传接口用了 File()/Form()，FastAPI 要求 python-multipart。
           # 缺了不是"上传不能用"，而是 app 在定义路由时就抛 RuntimeError，整个服务起不来
           # ——v1.5 的离线包就栽在这（本机 venv 里早有这个包，所以本机一直没暴露）。
           "python-multipart"]
    print("   " + " ".join(cmd[3:]))
    if subprocess.run(cmd).returncode != 0:
        sys.exit("pip install 失败")
    print("   依赖安装完成：" + human(int(sum(f.stat().st_size for f in target.rglob("*") if f.is_file()))))

    # ---- 3. 代码 -------------------------------------------------------
    step("3/7 拷服务端代码")
    appdir = pkg / "app"
    (appdir / "src").mkdir(parents=True)
    for name in APP_FILES:
        shutil.copy2(ROOT / "src" / name, appdir / "src" / name)
    (appdir / "src" / "webui").mkdir()
    shutil.copy2(ROOT / "src" / "webui" / "index.html", appdir / "src" / "webui" / "index.html")

    # 页面里注入版本/截止日期（源文件保持通用写法）
    page = appdir / "src" / "webui" / "index.html"
    html = page.read_text(encoding="utf-8")
    html = re.sub(r'(<span id="about-date">)[^<]*(</span>)',
                  lambda m: m.group(1) + RELEASE + m.group(2), html)
    page.write_text(html, encoding="utf-8")

    shutil.copy2(ROOT / "pack" / "run_offline.py", appdir / "run.py")

    # Rust 检索内核：静态链接的 exe（约 2.4 MB），缺了也能跑——会自动回退 Python 引擎
    rust_exe = ROOT / "rust" / "search-core" / "target" / "release" / "search-core.exe"
    if rust_exe.exists():
        shutil.copy2(rust_exe, pkg / "search-core.exe")
        print("   src/ + webui + run.py + search-core.exe（%s）" % human(rust_exe.stat().st_size))
    else:
        print("   src/ + webui + run.py")
        print("   !! 没找到 search-core.exe，先跑：cd rust\\search-core && cargo build --release")
        print("   !! 这一版将只带 Python 引擎（可用，但每次检索 0.5 秒而不是 0.04 秒）")

    # ---- 4. 数据库 -----------------------------------------------------
    step("4/7 生成瘦身数据库（去掉 FTS 与二级索引）")
    dbdir = pkg / "data"
    dbdir.mkdir()
    t0 = time.time()
    strip_db(dbdir / "queries.db")
    print("   queries.db = %s  (%.0fs，读者第一次运行会自建索引)"
          % (human((dbdir / "queries.db").stat().st_size), time.time() - t0))

    # ---- 5. 启动器与说明 -----------------------------------------------
    step("5/7 写 启动.bat / 使用说明.html")
    bat = (
        "@echo off\r\n"
        "setlocal\r\n"
        "title " + NAME + " v" + VERSION + "\r\n"
        "cd /d \"%~dp0\"\r\n"
        "chcp 65001 >nul\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        "set PYTHONUTF8=1\r\n"
        "\"%~dp0python\\python.exe\" \"%~dp0app\\run.py\"\r\n"
        "echo.\r\n"
        "pause\r\n"
    )
    (pkg / "启动.bat").write_bytes(bat.encode("utf-8"))
    facts = db_facts(dbdir / "queries.db")
    facts["__RELEASE__"] = RELEASE
    guide = (ROOT / "pack" / "使用说明.html").read_text(encoding="utf-8")
    for k, v in facts.items():
        guide = guide.replace(k, v)
    (pkg / "使用说明.html").write_text(guide, encoding="utf-8")
    print("   启动.bat（CRLF）、使用说明.html（板块 %s，%s 条）"
          % (facts["__BOARDS__"], facts["__QUERIES__"]))

    # ---- 6. 体积汇总 ---------------------------------------------------
    step("6/7 体积")
    files = [f for f in pkg.rglob("*") if f.is_file()]
    raw = sum(f.stat().st_size for f in files)
    print("   解压后 %.1f MB  共 %d 个文件" % (raw / 1048576, len(files)))

    # ---- 7. 打包 -------------------------------------------------------
    step("7/7 压缩成 zip")
    zip_path = OUT / ("%s_v%s_%s.zip" % (NAME, VERSION, RELEASE))
    t0 = time.time()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(files):
            z.write(f, str(Path(pkg.name) / f.relative_to(pkg)).replace("\\", "/"))
    print("   %s  =  %s  (压缩 %.0fs)"
          % (zip_path.name, human(zip_path.stat().st_size), time.time() - t0))
    print("\n全部完成，用时 %.0fs" % (time.time() - t_all))
    print("上传网盘的就是：%s" % zip_path)


if __name__ == "__main__":
    main()
