FROM python:3.12-slim
RUN pip install --no-cache-dir anthropic==1.15.0 && mkdir -p /output /logs/verifier
COPY . /tests/
RUN chmod +x /tests/test.sh
