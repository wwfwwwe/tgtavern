# ⚠️ 严禁商用，本套工具全部开源，免费。
# 如果你是花钱买的、花钱找人部署的、或者在用别人收费的服务——你被骗了。请找对方退款，并举报。
FROM python:3.12-slim

# 国内网络可以在 docker-compose.yml 里填镜像（见 README）
ARG PIP_INDEX_URL=
ARG PLAYWRIGHT_DOWNLOAD_HOST=
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST} \
    PYTHONUNBUFFERED=1 TGT_DATA=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium-headless-shell
COPY app/ /app/
