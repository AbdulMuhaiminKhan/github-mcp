# Runs the MCP server over stdio. Claude Desktop config:
#   "command": "docker", "args": ["run", "-i", "--rm", "-e", "GITHUB_TOKEN", "-e", "GITHUB_MCP_TOOLSET", "github-mcp"]
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home mcp
USER mcp
ENV GITHUB_MCP_TOOLSET=v2 PYTHONUNBUFFERED=1
ENTRYPOINT ["github-mcp"]
