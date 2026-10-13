FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Git AI: needed to read the refs/notes/ai notes (`git ai stats --json`)
RUN curl -fsSL https://usegitai.com/install.sh | bash || echo "WARNING: Git AI not installed; phase 3 will have no data"
ENV PATH="/root/.git-ai/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml ./
COPY aimetrics ./aimetrics
COPY db ./db
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "aimetrics"]
CMD ["serve"]
