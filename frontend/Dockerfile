# syntax=docker/dockerfile:1.7
# AEVAF frontend: Next.js standalone build served by Node on Alpine.
ARG NODE_VERSION=20

# ----------------------------------------------------------------- stage 1: dependencies
FROM node:${NODE_VERSION}-alpine AS deps
WORKDIR /app
# package-lock.json is optional (the glob makes the COPY succeed without it).
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# --------------------------------------------------------- stage 2: build the standalone app
FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /app

# NEXT_PUBLIC_* values are compiled into the browser bundle, so they are build arguments.
# "/api/v1" is same-origin: the nginx proxy forwards it to the backend.
ARG NEXT_PUBLIC_API_URL=/api/v1
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN mkdir -p public && npm run build

# ----------------------------------------------------------------- stage 3: slim runner
FROM node:${NODE_VERSION}-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -S -g 10001 nextjs && adduser -S -u 10001 -G nextjs nextjs

COPY --from=build --chown=nextjs:nextjs /app/public ./public
COPY --from=build --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nextjs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
