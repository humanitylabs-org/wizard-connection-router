FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 WIZARD_ROUTER_PORT=8802
LABEL org.opencontainers.image.source="https://github.com/humanitylabs-org/wizard-connection-router" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
RUN mkdir /data && chown 1000:1000 /data
COPY server.py SKILL.md LICENSE ./
COPY mcp/ ./mcp/
COPY static/ ./static/
USER 1000:1000
EXPOSE 8802
CMD ["python3", "server.py"]
HEALTHCHECK --interval=30s --timeout=5s CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8802/health',timeout=3).read()"]
