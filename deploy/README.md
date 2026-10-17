# deploy/ —— 把检索服务部署出去

面向"以后回来翻的自己"的技术版说明；给同事看的那份在 `docs/部署指南.md`。

## 三种跑法

| 跑法 | 命令 | 什么时候用 |
| --- | --- | --- |
| 本机 | `启动网站.bat` | 自己用 |
| 公网隧道 | `公网访问.bat`（= `deploy/tunnel.py`） | 今天就要给同事用，电脑能一直开着 |
| 容器 | `docker compose -f deploy/docker-compose.yml up -d --build` | 有一台服务器 |

## 环境变量

服务端只认这几个，CLI 参数同名（`--db` `--host` `--port` `--auth`），
环境变量优先级低于命令行。

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `QX_DB` | `data/queries.db` | 数据库路径 |
| `HOST` | `127.0.0.1` | 监听地址；容器里设 `0.0.0.0` |
| `PORT` | `8765` | 监听端口；多数 PaaS 用它注入端口 |
| `AUTH` | 空 | `用户名:密码`，非空则开启 HTTP Basic 认证 |
| `DB_URL` | 空 | 仅容器入口用：本地没库时从该直链下载（支持 `.gz`） |

## 数据库怎么过去

数据库 420 MB，是可再生的（`scripts/rebuild_db.bat` 能重建），所以**不进镜像、不进 git**。

```bash
# 整理成一个干净的单文件（顺便校验 integrity_check）
.venv/Scripts/python.exe deploy/pack_db.py --gz

# 传到服务器
scp data/queries_pack.db.gz user@server:/opt/wenxun/data/

# 或者让容器自己去下（放到任意直链，比如对象存储、Hugging Face dataset）
docker run -d -p 8765:8765 -e DB_URL=https://.../queries_pack.db.gz wenxun
```

为什么不能直接 `scp data/queries.db`：它是 WAL 模式，万一有个没合并的 `-wal`，
拷走的 `.db` 会缺最近的写入。`VACUUM INTO` 出来的副本是单文件 +
`journal_mode=delete`，落在只读文件系统上也能开——Web 服务只读、从不写库。

实测（420.2 MB 的库）：`integrity_check=ok`，整理 9.6~12.5 s 产出 409.7 MB；
再 `--gz` 花 15 s，得到 **112.1 MB（3.7x）**。注意体积收益几乎全在 gzip 那一步——
VACUUM 自己只减了 10 MB（freelist 本来就是 0），它的价值是"干净的单文件"。

## 为什么这里没有 pdfplumber

`src/extract.py` 原来在模块顶层 `from .pdfread import ...`，而 `pdfread` 会拉起
pdfplumber（一整条 PDF 解析栈，几十 MB）。但 Web 服务只用得上 `split_user_input`，
根本不需要解析 PDF。现在改成 `extract.iter_lines()` 内部延迟导入，于是：

- `deploy/requirements.txt` 只有 `fastapi` + `uvicorn`；
- 镜像小、构建快，少一类装不上的依赖；
- 抽取能力没变——改完当场拿 `问询函_创业板/博实结科技_r1.pdf` 验证过（8 页 / 2 题 / 2 条 query）。

要单独用 `is_hei` / `page_count`，直接从 `src.pdfread` 取。

## 有没有实测过

- `启动网站.bat` / `公网访问.bat` / `deploy/tunnel.py`：**实测过**。公网地址上 `GET /` 200、
  `POST /api/search` 200（0.97 s）；带口令时无凭据返回 401，凭据正确才 200。
- `deploy/pack_db.py`：**实测过**，420.2 MB → 409.7 MB → `.gz` 112.1 MB（3.7x）。
- **踩过一个大坑**：自动生成的 `公网访问.bat` 是 **LF 换行**，双击后 cmd.exe 每行吃掉几个
  字符，刷一屏 "xxx 不是内部或外部命令"，最后 python 报 `can't open file '...=D:\\...\.venv\\Scripts\\python.exe'`。
  根因：本机 `core.autocrlf=true`，git 提交时把 CRLF 存成 LF，**但工具新写出来的文件不会被自动转**——
  `启动网站.bat` 是早先 checkout 出来的，所以它没事，新写的两个全中招。
  现在 `.gitattributes` 里 `*.bat text eol=crlf` 钉死了。**以后改 .bat 一定要保证 CRLF**
  （别用会写 LF 的编辑器；实在不行就用 python 把 \n 全换成 \r\n）。
- **Docker 那条路没在本机验证过** —— 这台机器上没装 Docker，`docker build` 没跑成。
  Dockerfile 是照着官方 python:3.12-slim 写的，第一次构建请自己盯一眼输出。

## 关于免费托管

2025 年的免费 Python 托管基本都关门了（Hugging Face 的 Docker Space 现在要 PRO、
Koyeb 停了新用户、Render 从国内网络 TLS 都连不上）。逐个平台的实测结论见
`docs/部署指南.md` §4。