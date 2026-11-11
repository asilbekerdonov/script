FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tashkent \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && mv /root/.cache/ms-playwright /ms-playwright \
 && chmod -R 755 /ms-playwright

# браузеры лежат в общем месте, доступном непривилегированному пользователю
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY lms_auto.py ./

RUN useradd -m -u 10001 lms \
 && mkdir -p /app/screenshots \
 && chown -R lms:lms /app
USER lms

CMD ["python", "lms_auto.py"]
