FROM python:3.12-slim

WORKDIR /app
COPY server.py /app/server.py
COPY deploy.env /data/deploy.env

EXPOSE 19418
CMD ["python3", "/app/server.py"]
