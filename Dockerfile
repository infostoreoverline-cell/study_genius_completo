FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STUDYGENIUS_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-xetex texlive-latex-extra texlive-fonts-recommended texlive-lang-italian \
    fonts-texgyre fonts-lmodern graphviz && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY studygenius ./studygenius
RUN pip install --no-cache-dir -r requirements.txt . && \
    useradd --uid 10001 --create-home studygenius && mkdir -p /data && chown studygenius:studygenius /data
USER studygenius
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=4)"
CMD ["python", "-m", "uvicorn", "studygenius.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
