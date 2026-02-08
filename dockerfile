FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh

EXPOSE 8501

# Host Anvil access from container (Mac/Windows: host.docker.internal)
ENV ANVIL_RPC_URL="http://host.docker.internal:8545"

CMD ["./entrypoint.sh"]
