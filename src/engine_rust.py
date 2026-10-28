# -*- coding: utf-8 -*-
"""Rust 检索内核的 sidecar 客户端。

为什么是独立进程而不是 PyO3：
  - Rust 崩了不会带崩网页服务；Python 侧还能立刻回退到自己的引擎
  - 部署只多一个 exe，不用管 Python 3.12.10 / 3.12.14 的 ABI 是否对得上
  - 索引 mmap 一次就常驻，不用每个请求重新打开
代价：每次查询多一次 IPC（约 0.2 ms，相对 8 ms 的检索可以忽略）。

协议：一行一个 JSON。
  请求 {"id":1,"board":"科创板"|null,"round":null,"top":10,
        "parts":[{"text":"原始分句","core":"...","loose":"...","strict":"...","clauses":["..."]}]}
  响应 {"id":1,"ms":7.8,"parts":[{"hits":[{"q_id":"..","score":0.93,"sim":0.9,"lcs":12,"exact":true}]}]}
  出错 {"id":1,"error":"..."}

注意：**归一化不在这里做**，core/loose/strict/clauses 由 Python 的
normalize.py 算好后传进去，Rust 侧不碰中文处理。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 相对位置在开发目录和发布包里不一样，两处都找：
#   开发：<项目根>/src/engine_rust.py  → ROOT 就是项目根
#   发布：<包根>/app/src/engine_rust.py → ROOT 是 app/，要看上一级才是包根
# 所以对 ROOT 和 ROOT.parent 都找一遍，别写死单一路径。
def _cands(*names: str) -> tuple:
    out = []
    for base in (ROOT, ROOT.parent):
        for n in names:
            out.append(base / n)
    return tuple(out)


EXE_CANDIDATES = _cands(
    "search-core.exe",
    "rust/search-core.exe",
    "rust/search-core/target/release/search-core.exe",
)
INDEX_CANDIDATES = _cands("index/search.index", "eval/search.index", "search.index")
DB_CANDIDATES = _cands("data/queries.db")


def _find(candidates, what: str) -> Path:
    for p in candidates:
        if p.exists():
            return p
    # 都不在就返回第一个，让上层报"找不到 xxx"时带上一个具体路径
    return candidates[0]


class RustEngine:
    """常驻的 search-core --serve 进程。线程安全（内部串行化）。"""

    def __init__(self, proc: subprocess.Popen):
        self._p = proc
        self._lock = threading.Lock()
        self._seq = 0

    # ------------------------------------------------------------ 生命周期
    @classmethod
    def open(cls, exe: str | Path | None = None, index: str | Path | None = None,
             db: str | Path | None = None) -> "RustEngine":
        exe = Path(exe or os.environ.get("QB_RUST_EXE") or _find(EXE_CANDIDATES, "search-core.exe"))
        index = Path(index or os.environ.get("QB_INDEX") or _find(INDEX_CANDIDATES, "search.index"))
        db = Path(db or os.environ.get("QB_DB") or _find(DB_CANDIDATES, "data/queries.db"))
        if not exe.exists():
            raise RuntimeError("找不到 Rust 内核：" + str(exe))
        if not index.exists():
            raise RuntimeError("找不到索引：" + str(index)
                               + "（先跑 search-core --build-index）")
        if not db.exists():
            raise RuntimeError("找不到数据库：" + str(db))
        proc = subprocess.Popen(
            [str(exe), "--serve", "--index", str(index), "--db", str(db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(ROOT),
        )
        return cls(proc)

    def alive(self) -> bool:
        return self._p.poll() is None

    def close(self) -> None:
        try:
            if self._p.stdin:
                self._p.stdin.close()
        except Exception:
            pass
        try:
            self._p.wait(timeout=3)
        except Exception:
            try:
                self._p.kill()
            except Exception:
                pass

    # ------------------------------------------------------------ 检索
    def search(self, parts: list[dict], board: str | None = None,
               round_no: int | None = None, top: int = 10) -> list[list[dict]]:
        """返回每个分句的 hits 列表。parts 里的 core/loose/strict/clauses 必须已算好。"""
        if not self.alive():
            raise RuntimeError("Rust 内核进程已退出")
        with self._lock:
            self._seq += 1
            req = {"id": self._seq, "board": board, "round": round_no,
                   "top": top, "parts": parts}
            self._p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._p.stdin.flush()
            line = self._p.stdout.readline()
        if not line:
            raise RuntimeError("Rust 内核无响应（进程退出，code=%s）" % self._p.poll())
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError("Rust 内核报错：" + str(resp["error"]))
        got = resp.get("parts") or []
        if len(got) != len(parts):
            raise RuntimeError("Rust 内核返回的分句数不对：%d != %d" % (len(got), len(parts)))
        return [p.get("hits") or [] for p in got]


_ENGINE: RustEngine | None = None
_LOADED = False
_LOCK = threading.Lock()


def get_engine() -> RustEngine | None:
    """取全局引擎；不可用返回 None（调用方回退 Python）。

    QB_ENGINE: auto（默认，能用就用）/ rust（必须用，失败也返回 None 并报一次）
               / py（强制 Python）
    """
    global _ENGINE, _LOADED
    mode = (os.environ.get("QB_ENGINE") or "auto").strip().lower()
    if mode in ("py", "python", "off", "0"):
        return None
    with _LOCK:
        if _LOADED:
            if _ENGINE is not None and not _ENGINE.alive():
                _ENGINE = None
                _LOADED = True
            return _ENGINE
        _LOADED = True
        try:
            _ENGINE = RustEngine.open()
            print("[engine] Rust 内核已启动：" + str(_find(INDEX_CANDIDATES, "search.index"))
                  + "（QB_ENGINE=py 可强制回退 Python）", flush=True)
        except Exception as e:  # 缺文件、启动失败都只提示一次，不影响服务
            print("[engine] Rust 内核不可用，使用 Python 引擎：" + str(e), flush=True)
            _ENGINE = None
        return _ENGINE
