FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py AGENT_PROFILE.md ./
COPY mori_together ./mori_together
RUN useradd -m mori && mkdir data && chown -R mori:mori /app
USER mori
EXPOSE 8001
CMD ["python", "agent.py"]
