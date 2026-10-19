# Cloud copy of the text CRM: inbox, inbound webhook, daily texts. It never opens a browser;
# the PC pushes CDK and inventory data to /feed/<feed_token> once a day.
# Replies are drafted by the Codex CLI on its own ChatGPT login, kept on the volume. Sign it in once with
# fly ssh console -C "codex login --device-auth"
FROM python:3.12-slim
WORKDIR /app
# Without tzdata, TZ is silently ignored and send hours run on UTC.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata nodejs npm \
    && npm install -g @openai/codex@0.153.3 && npm cache clean --force \
    && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY textcrm ./textcrm
RUN pip install --no-cache-dir anthropic
ENV TZ=America/New_York CODEX_HOME=/app/private/codex
VOLUME /app/private
EXPOSE 8787
CMD ["sh", "-c", "mkdir -p \"$CODEX_HOME\" && exec python -u -m textcrm run"]
