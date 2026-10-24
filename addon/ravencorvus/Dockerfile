ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
# scripts/build-addon.sh copies pyproject.toml and src/ next to this file.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
