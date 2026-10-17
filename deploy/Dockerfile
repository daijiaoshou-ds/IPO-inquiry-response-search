# 问询回复检索 —— 服务端镜像
#
# 只装 Web 服务需要的东西；数据库不进镜像，运行时挂进来（见 docker-compose.yml），
# 因为它是 420 MB 的可再生产物，塞进镜像会让每次构建都白传一遍。
#
# 构建（在项目根目录）：
#     docker build -f deploy/Dockerfile -t wenxun .
# 运行：
#     docker run -d --name wenxun -p 8765:8765 \
#       -v "$PWD/data:/data:ro" -e AUTH=user:pass wenxun
#
# 数据库也可以让容器自己去下（适合云平台），设 DB_URL 指到一个直链即可。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    QX_DB=/data/queries.db \
    HOST=0.0.0.0 \
    PORT=8765

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gzip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt

COPY src /app/src
COPY deploy/entrypoint.sh /app/deploy/entrypoint.sh
RUN chmod +x /app/deploy/entrypoint.sh

EXPOSE 8765
CMD ["/app/deploy/entrypoint.sh"]
