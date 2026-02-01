# 軽量なPythonイメージを使用
FROM python:3.10-slim

# システム依存関係のインストール
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ワークディレクトリの設定
WORKDIR /app

# 依存ライブラリのコピーとインストール
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ソースコードのコピー
COPY . .

# 実行権限の付与
RUN chmod +x entrypoint.sh

# Streamlitのポート開放
EXPOSE 8501

# 環境変数のデフォルト値（Docker内からホストのAnvilにアクセスするため）
# Mac/Windowsの場合は host.docker.internal が使える
ENV ANVIL_RPC_URL="http://host.docker.internal:8545"

# 同時起動スクリプトを実行
CMD ["./entrypoint.sh"]
