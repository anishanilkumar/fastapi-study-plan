FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29"
COPY app.py study.html sw.js manifest.webmanifest ./
COPY icons ./icons
COPY courses ./courses
COPY fonts ./fonts
# Progress lives in /data; mount a volume there. Put a config.json in it too.
ENV STUDY_DB=/data/study.db STUDY_CONFIG=/data/config.json STUDY_HOST=0.0.0.0 STUDY_PORT=8781
VOLUME /data
EXPOSE 8781
CMD ["python", "app.py"]
