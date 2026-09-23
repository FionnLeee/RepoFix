FROM node:22-bookworm-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@10.17.1
WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/control-api/package.json apps/control-api/package.json
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile
COPY apps apps
FROM base AS api
RUN pnpm --filter @repofix/api build
CMD ["sh", "-c", "pnpm --filter @repofix/api migrate && pnpm --filter @repofix/api start"]
FROM base AS web
ENV NEXT_TELEMETRY_DISABLED=1
ENV API_URL=http://api:3101
RUN pnpm --filter @repofix/web build
CMD ["pnpm", "--filter", "@repofix/web", "start"]
