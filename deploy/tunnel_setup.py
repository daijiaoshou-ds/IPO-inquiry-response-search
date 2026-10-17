# -*- coding: utf-8 -*-
"""「固定网址」一次性设置：把你的域名和 Cloudflare 命名隧道绑起来。

用法（项目根目录下）：
    .venv\\Scripts\\python.exe deploy\\tunnel_setup.py

只有两件事得你自己做，我做不了：
  1. 买好域名；
  2. 在 Cloudflare 里「添加站点」，再把域名的 NS 改成它给的那两个，
     一直等到 Cloudflare 上状态变成 Active（详见 docs/部署指南.md 第 2 节）。

之后这个脚本自动做完：登录授权 -> 建隧道 -> 把域名指到隧道 -> 写好本地配置。
全部只跑一次；以后双击 公网访问.bat 就会自动走这个固定网址。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tunnel import CF, ROOT, STATE, load_state, write_config  # noqa: E402

CERT = Path.home() / ".cloudflared" / "cert.pem"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", re.I)


def run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return (proc.stdout or "") + (proc.stderr or "")


def find_tunnel(name: str) -> str:
    """cloudflared tunnel list 里按名字找 UUID；找不到返回空串。"""
    out = run([str(CF), "tunnel", "list"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name and UUID_RE.fullmatch(parts[0]):
            return parts[0]
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="把域名绑到 Cloudflare 命名隧道（一次性设置）")
    ap.add_argument("--hostname", default="", help="完整域名，例如 wenxun.zhangsan.com")
    ap.add_argument("--name", default="wenxun", help="隧道名，默认 wenxun")
    ap.add_argument("--dry-run", action="store_true", help="只检查环境并打印计划，不动 Cloudflare")
    ap.add_argument("--force", action="store_true", help="已经设置过也重做")
    args = ap.parse_args()

    print()
    print("  ================================================================")
    print("   固定网址 · 一次性设置")
    print("  ================================================================")
    print()

    if not CF.exists():
        raise SystemExit("找不到 " + str(CF) + "。\n先双击一次 公网访问.bat，它会自动把 cloudflared 下下来。")

    state = load_state()
    if state and not args.force:
        print("  已经设置过了：https://%s" % state["hostname"])
        print("  要换域名重做，加 --force 再跑一次。")
        return

    # ---- 第 1 步：域名 -------------------------------------------------
    hostname = args.hostname.strip()
    if not hostname:
        print("  你买的域名是什么？例如 wenxun.zhangsan.com（只输入域名，不要 https://）")
        hostname = input("  域名 > ").strip()
    hostname = re.sub(r"^https?://", "", hostname).strip("/ ").lower()
    if not HOST_RE.match(hostname):
        raise SystemExit('"%s" 看起来不是一个完整域名。要写全：子域名 + 主域名，例如 wenxun.zhangsan.com' % hostname)

    if args.dry_run:
        print("  [dry-run] 环境检查通过：")
        print("     cloudflared = %s" % CF)
        print("     域名        = %s" % hostname)
        print("     隧道名      = %s" % args.name)
        print("     Cloudflare 授权文件 = %s"
              % ("已有" if CERT.exists() else "还没有（真正跑的时候会让你点 Authorize）"))
        cfg = write_config({"uuid": "00000000-0000-0000-0000-000000000000",
                            "hostname": hostname}, 8765)
        print("     将会生成的配置内容：")
        for line in cfg.read_text(encoding="utf-8").splitlines():
            print("       " + line)
        return

    # ---- 第 2 步：登录 Cloudflare --------------------------------------
    if not CERT.exists():
        print()
        print("  第 1/3 步：登录 Cloudflare")
        print("  浏览器会弹出来，登录后选你的那个域名，点【Authorize】授权。")
        print("  如果浏览器没自己打开，把窗口里那条链接复制到浏览器里也行。")
        print()
        subprocess.run([str(CF), "tunnel", "login"], check=False)
    if not CERT.exists():
        raise SystemExit(
            "  没拿到授权文件（%s）。\n"
            "  多半是这两个原因：\n"
            "    1) 浏览器里没有点 Authorize；\n"
            "    2) 域名还没在 Cloudflare 里变成 Active（NS 没生效）。\n"
            "  处理好之后重新跑这个脚本。" % CERT)

    # ---- 第 3 步：建隧道 ----------------------------------------------
    print()
    print("  第 2/3 步：创建隧道 %s" % args.name)
    uuid = find_tunnel(args.name)
    if uuid:
        print("     已经有了，直接复用：%s" % uuid)
    else:
        out = run([str(CF), "tunnel", "create", args.name])
        m = UUID_RE.search(out)
        if not m:
            print(out)
            raise SystemExit("  创建隧道失败。把上面这段发给作者看看。")
        uuid = m.group(0)
        print("     建好了：%s" % uuid)

    cred = Path.home() / ".cloudflared" / (uuid + ".json")
    if not cred.exists():
        raise SystemExit("  隧道建好了，但没找到密钥文件 %s。\n  重跑一次 --force 试试。" % cred)

    # ---- 第 4 步：绑域名 ----------------------------------------------
    print()
    print("  第 3/3 步：把 https://%s 指到这条隧道" % hostname)
    out = run([str(CF), "tunnel", "route", "dns", args.name, hostname])
    if "already exists" in out.lower():
        print("     已有同名解析，覆盖它")
        out = run([str(CF), "tunnel", "route", "dns", "--overwrite-dns", args.name, hostname])
    if "error" in out.lower() or "failed" in out.lower() or "incorrect" in out.lower():
        print(out)
        raise SystemExit("  绑定域名失败。常见原因：这个域名不在当前 Cloudflare 账号里，"
                         "或者填的不是完整域名。")

    # ---- 保存 ---------------------------------------------------------
    state = {"name": args.name, "uuid": uuid, "hostname": hostname}
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_config(state, 8765)

    print()
    print("  ================================================================")
    print("   设置完成，你的固定网址是：")
    print()
    print("      https://" + hostname)
    print()
    print("   第一次要等 1~5 分钟域名才通（Cloudflare 要下发证书和配置）。")
    print("   以后每天双击【公网访问.bat】，它会自动走这条隧道，网址不再变。")
    print("  ================================================================")
    print()


if __name__ == "__main__":
    main()
