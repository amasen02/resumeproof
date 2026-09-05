FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

RUN useradd --create-home resumeproof && mkdir /data && chown resumeproof:resumeproof /data
USER resumeproof
ENTRYPOINT ["resumeproof"]
CMD ["demo", "--output-dir", "/data"]
