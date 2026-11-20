FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY logos ./logos
COPY dashboard ./dashboard
ENV HOST=0.0.0.0 PORT=8080
# MCP por HTTP. Para el dashboard, otro servicio con: python -m dashboard.app
CMD ["python", "-m", "logos.servidor_mcp", "--http"]
