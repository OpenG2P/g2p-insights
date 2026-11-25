# Mock Master Data Service for the local test stack.
# Stdlib only — no pip install, so this builds in seconds and adds ~0 weight.
FROM python:3.11-alpine

WORKDIR /app
COPY app.py .

EXPOSE 8080
CMD ["python", "-u", "app.py"]
