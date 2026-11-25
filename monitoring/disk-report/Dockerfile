FROM python:3.12-alpine
RUN apk add --no-cache rclone coreutils
WORKDIR /app
COPY collector.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 9105
CMD ["python3", "-I", "/app/collector.py"]
