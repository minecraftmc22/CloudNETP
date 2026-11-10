# syntax=docker/dockerfile:1

# CloudNETP 运行镜像：只带 Python 与运行期依赖，代码直接进镜像；
# 数据库与上传内容统一落在 /data（宿主机挂一个目录进来即可持久化）。
FROM python:3.12-slim

# 构建期可换源：默认阿里云，海外构建可传 --build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：requirements.txt 没变时重建不会重新装包
COPY requirements.txt ./
RUN python -m pip install --upgrade pip --index-url "$PIP_INDEX_URL" \
    && python -m pip install -r requirements.txt --index-url "$PIP_INDEX_URL"

COPY . .

# 数据目录与软链接
# - SQLite 默认连接串是 sqlite:///./dev.db（相对工作目录），STORAGE_DIR 留空时是 ./storage，
#   在这里把两者指到 /data，.env 不用为容器改任何一行也能持久化；
# - /data 在镜像里先建好并 chown，命名卷首次创建时会继承这个属主。
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/cloudnetp cloudnetp \
    && mkdir -p /data/storage \
    && rm -f /app/dev.db /app/dev.db-wal /app/dev.db-shm \
    && ln -sfn /data/dev.db /app/dev.db \
    && rm -rf /app/storage \
    && ln -sfn /data/storage /app/storage \
    && chown -R cloudnetp:cloudnetp /app /data /home/cloudnetp

USER cloudnetp

# 实际端口由 .env 的 PORT 决定（默认 8123），这里只是声明
EXPOSE 8123
VOLUME ["/data"]

# 不是 root 也别让容器空跑：接口能应答才算活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/version' % os.environ.get('PORT', '8123'), timeout=4)"

CMD ["python", "run.py"]
