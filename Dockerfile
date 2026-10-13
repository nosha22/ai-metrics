FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Git AI: necessário para ler as notas refs/notes/ai (`git ai stats --json`)
RUN curl -fsSL https://usegitai.com/install.sh | bash || echo "AVISO: Git AI não instalado; a Fase 3 fica sem dados"
ENV PATH="/root/.git-ai/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml ./
COPY aimetrics ./aimetrics
COPY db ./db
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "aimetrics"]
CMD ["serve"]
