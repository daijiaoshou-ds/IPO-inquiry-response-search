# -*- coding: utf-8 -*-
"""把本机的检索服务挂到公网，给同事一个能直接打开的网址。

用法（项目根目录下）：
    .venv\\Scripts\\python.exe deploy\\tunnel.py

两种模式，自动判断：

  * 做过「固定网址」设置（tools\\named_tunnel.json 存在）
        -> 跑 Cloudflare 命名隧道，网址永远是你买的那个域名；
  * 没设置过
        -> 跑 Cloudflare 快速隧道，网址是 https://xxx.trycloudflare.com，
           每次重启都会变。

不管哪种模式，都会：缺 cloudflared 就先下、本机服务没跑就起起来、把网址抄进剪贴板。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
CF = TOOLS / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
STATE = TOOLS / "named_tunnel.json"      # 固定网址的配置（域名 / 隧道 id）
CONFIG = TOOLS / "cloudflared.yml"       # 每次运行时按当前端口重新生成

ASSET = ("cloudflared-windows-amd64.exe" if os.name == "nt" else "cloudflared-linux-amd64")
MIRRORS = [
    "https://github.com/cloudflare/cloudflared/releases/latest/download/" + ASSET,
    "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download/" + ASSET,
    "https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/" + ASSET,
]
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


# ------------------------------------------------------------------ 固定网址

def load_state() -> dict | None:
    """读 tools\\named_tunnel.json；没有或坏了都当成"没设置过"。"""
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data.get("uuid") or not data.get("hostname"):
        return None
    return data


def write_config(state: dict, port: int) -> Path:
    """生成 cloudflared 的配置：进来的域名打到本机 127.0.0.1:port。

    每次启动都重写，是为了端口能改（--port）而配置不会和实际对不上。
    """
    TOOLS.mkdir(exist_ok=True)
    cred = Path.home() / ".cloudflared" / (state["uuid"] + ".json")
    CONFIG.write_text(
        "# 由 deploy/tunnel.py 自动生成，别手改（改端口用 --port）\n"
        "tunnel: {uuid}\n"
        "credentials-file: {cred}\n"
        "ingress:\n"
        "  - hostname: {host}\n"
        "    service: http://127.0.0.1:{port}\n"
        "  - service: http_status:404\n".format(
            uuid=state["uuid"],
            cred=str(cred).replace("\\", "/"),
            host=state["hostname"],
            port=port,
        ),
        encoding="utf-8",
    )
    return CONFIG


# ------------------------------------------------------------------ 通用

def download(dest: Path) -> bool:
    """下载 cloudflared；第一个源失败就换下一个。"""
    for url in MIRRORS:
        print("  源：" + url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("content-length") or 0)
                done, t0 = 0, time.time()
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        secs = max(0.001, time.time() - t0)
                        if total:
                            sys.stdout.write("\r  %3d%%  %6.1f/%6.1f MB  %5.0f KB/s    "
                                             % (done * 100 // total, done / 1048576,
                                                total / 1048576, done / 1024 / secs))
                        else:
                            sys.stdout.write("\r  %6.1f MB  %5.0f KB/s    "
                                             % (done / 1048576, done / 1024 / secs))
                        sys.stdout.flush()
            print()
            if dest.stat().st_size > 1024 * 1024:
                return True
            print("  文件太小，换下一个源")
        except Exception as exc:  # noqa: BLE001 - 网络问题一律换源重试
            print("\n  失败：" + str(exc)[:140])
        dest.unlink(missing_ok=True)
    return False


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def copy_to_clipboard(text: str) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), check=True)
        return True
    except Exception:
        return False


def banner(url: str, auth: str, fixed: bool) -> None:
    user, _, pwd = auth.partition(":")
    head = "网站已经开好（固定网址，不会变）" if fixed else "网站已经开好，把下面两行发给同事就行"
    out = ["", "  ================================================================",
           "   " + head, "", "     网址：" + url]
    if auth:
        out.append("     口令：" + user + " / " + pwd)
    if fixed:
        out += ["", "   这个窗口一关，网站就下线（电脑得开着、别睡眠）。"]
    else:
        out += ["", "   这个窗口一关，网站就下线；网址每次重启都会变。"]
    out += ["  ================================================================", ""]
    print("\n".join(out))
    TOOLS.mkdir(exist_ok=True)
    (TOOLS / "public_url.txt").write_text(url + "\n", encoding="utf-8")
    print("  已写入 tools\\public_url.txt；"
          + ("已复制到剪贴板。" if copy_to_clipboard(url) else "复制剪贴板失败，请手动复制上面那行。"))


def ensure_server(args, host: str):
    """本机服务没在跑就起一个；返回 Popen 或 None。"""
    if port_open(host, args.port):
        print("[1/3] 本机 %s:%d 已经在跑了，直接用" % (host, args.port))
        if args.auth:
            print("      注意：正在跑的那个服务不一定带口令，确认一下它启动时有没有 --auth")
        return None
    if args.no_server:
        sys.exit("本机 %s:%d 没有监听，先去跑 src.server" % (host, args.port))
    print("[1/3] 启动本机服务 %s:%d ..." % (host, args.port))
    cmd = [sys.executable, "-m", "src.server", "--host", host, "--port", str(args.port)]
    if args.auth:
        cmd += ["--auth", args.auth]
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    for _ in range(120):
        if port_open(host, args.port):
            time.sleep(0.5)
            return proc
        time.sleep(0.5)
    sys.exit("服务没起来。先单独跑一次看报错："
             ".venv\\Scripts\\python.exe -m src.server")


def ensure_cloudflared() -> None:
    if CF.exists():
        print("[2/3] cloudflared 已就位")
        return
    print("[2/3] 第一次运行，需要下载 cloudflared（约 53 MB，国内大概 5~10 分钟）")
    TOOLS.mkdir(exist_ok=True)
    if not download(CF):
        sys.exit("下载失败。可以手动下载 " + ASSET + " 另存为 " + str(CF))
    if os.name != "nt":
        try:
            os.chmod(CF, 0o755)
        except OSError:
            pass


def pump(proc_cf, on_line, tail: list) -> None:
    """把 cloudflared 的输出逐行喂给 on_line，同时留最近 20 行备查。

    留 tail 是为了失败时能说清楚为什么——否则隧道一退出，屏幕上只有一句
    "退出码 1"，什么线索都没有。
    """
    for line in proc_cf.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        tail.append(line)
        if len(tail) > 20:
            del tail[0]
        on_line(line)


def wait_forever(proc_cf, proc_server, tail: list | None = None) -> None:
    try:
        while proc_cf.poll() is None:
            time.sleep(0.3)
        print("隧道自己退出了（退出码 %s）。" % proc_cf.returncode)
        if tail and proc_cf.returncode:
            print("  ---- cloudflared 最后几行 ----")
            for line in tail[-12:]:
                print("  " + line)
    except KeyboardInterrupt:
        pass
    finally:
        for p in (proc_cf, proc_server):
            if p is not None and p.poll() is None:
                p.terminate()
        print("已关闭。")


def main() -> None:
    ap = argparse.ArgumentParser(description="把本机检索服务挂到公网")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--auth", default=os.environ.get("AUTH", ""),
                    help="访问口令，格式 用户名:密码；留空表示不要口令")
    ap.add_argument("--no-server", action="store_true",
                    help="假定本机服务已经在跑，不要再起一个")
    ap.add_argument("--quick", action="store_true",
                    help="即使已经配好固定网址，也改用临时网址（.trycloudflare.com）")
    args = ap.parse_args()

    host = "127.0.0.1"
    state = None if args.quick else load_state()

    proc_server = ensure_server(args, host)
    ensure_cloudflared()

    if state is not None:
        cfg = write_config(state, args.port)
        print("[3/3] 走固定隧道：%s -> https://%s" % (state.get("name", "wenxun"), state["hostname"]))
        proc_cf = subprocess.Popen(
            # 传 UUID 而不是隧道名：传名字 cloudflared 会去调 API（需要 cert.pem），
            # 传 UUID 直接用配置里的 credentials-file，离线也能起。
            [str(CF), "tunnel", "--no-autoupdate", "--config", str(cfg), "run", state["uuid"]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        shown = {"done": False}
        tail: list = []

        def on_fixed(line: str) -> None:
            if not shown["done"] and "Registered tunnel connection" in line:
                shown["done"] = True
                print("      云边连接已建立。")
                banner("https://" + state["hostname"], args.auth, fixed=True)
            elif " ERR " in line or " WRN " in line:
                print("  " + line)

        threading.Thread(target=pump, args=(proc_cf, on_fixed, tail), daemon=True).start()

        def fallback() -> None:
            time.sleep(20)
            if not shown["done"]:
                shown["done"] = True
                banner("https://" + state["hostname"], args.auth, fixed=True)

        threading.Thread(target=fallback, daemon=True).start()
        wait_forever(proc_cf, proc_server, tail)
        return

    print("[3/3] 走临时隧道（网址每次都会变；想固定见 docs/部署指南.md 方案 B）")
    proc_cf = subprocess.Popen(
        [str(CF), "tunnel", "--no-autoupdate", "--url", "http://%s:%d" % (host, args.port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    found = {"url": None}
    tail: list = []

    def on_line(line: str) -> None:
        m = URL_RE.search(line)
        if m and not found["url"]:
            found["url"] = m.group(0)
            banner(m.group(0), args.auth, fixed=False)
        elif not found["url"] and (" ERR " in line or " WRN " in line):
            print("  " + line)

    threading.Thread(target=pump, args=(proc_cf, on_line, tail), daemon=True).start()
    wait_forever(proc_cf, proc_server, tail)


if __name__ == "__main__":
    main()
