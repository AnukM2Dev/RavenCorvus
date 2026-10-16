FROM python:3.13-alpine

RUN apk add --no-cache bash jq curl ca-certificates

# cloudflared: pick the binary for this build architecture
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64)  CF_ARCH=amd64 ;; \
      aarch64) CF_ARCH=arm64 ;; \
      *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"; \
    chmod +x /usr/local/bin/cloudflared; \
    /usr/local/bin/cloudflared --version

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
