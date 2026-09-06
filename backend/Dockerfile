# Playwright's own image ships Chromium plus every system library it needs,
# matching the pinned SDK version below — far less fragile than apt-installing
# browser dependencies by hand.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Render sets $PORT at runtime; 8000 is just the local-dev fallback.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
