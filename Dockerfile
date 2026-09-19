# A-Quant Lab：单镜像跑 API + 前端（本地部署 / 试运行）
#
# 为什么单镜像：这份部署的用途是让人**看到它跑起来**，不是生产发布。
# 生产形态（API 与前端分开、令牌纪律）见 ADR-013 与 Q0 报告，
# 目前**尚未**落实——容器里用的是演示主体开关，不能当作身份认证。

# ---------- 阶段 1：构建前端 ----------
FROM node:22-bookworm-slim AS web
WORKDIR /web
# 先只拷依赖清单，利用层缓存：源码改动不会让依赖重装
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

# ---------- 阶段 2：运行 ----------
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 依赖在 pip 层装好，源码改动不会触发重装
RUN pip install --no-cache-dir \
      'fastapi>=0.110' 'uvicorn>=0.29' 'pydantic>=2.0' \
      'PyYAML>=6.0' 'httpx>=0.27' 'pytest>=8.0' 'jsonschema>=4.20' \
      'baostock>=0.8'

WORKDIR /app
# 整个仓库都进镜像：apps/api 启动时会用 tests/ 里的合成夹具自举一份快照，
# 因此容器起来就有可看的数据，不需要挂载任何东西。
COPY . /app
COPY --from=web /web/dist /app/apps/web/dist

# 领域代码在 src/ 下。镜像里没有 pytest.ini 的 pythonpath 设置生效，
# 因此必须显式给 PYTHONPATH——否则 apps/api 一行 import 就炸，
# 而错误信息只是"No module named 'aquant'"，看不出是路径问题。
ENV PYTHONPATH=/app/src:/app

# 数据目录：SQLite 与快照都落在这里，挂卷即可持久化
ENV AQUANT_DATA_DIR=/data
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=3).status==200 else 1)"

# 单进程同时提供 API 与前端静态文件：
# 前端用相对路径请求 /api，与 API 同源，因此不需要 CORS，
# 也不需要为容器额外配一个反向代理。
CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "apps/api", "--host", "0.0.0.0", "--port", "8000"]
