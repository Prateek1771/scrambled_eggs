# Multi-stage so the runtime image carries the built app and nothing else -- no
# source, no dev dependencies, no package manager.

FROM node:22-alpine AS deps
WORKDIR /app
# Dependencies first, so editing a component does not reinstall the world.
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# The only public variable the bundle needs. Everything else the browser has to
# know -- including where the database lives -- is fetched from the API at
# runtime, because NEXT_PUBLIC_* values are inlined at build time and would
# otherwise pin this image to the machine that built it.
ARG NEXT_PUBLIC_API_URL=http://localhost:8080
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# `output: "standalone"` in next.config.ts emits a self-contained server plus
# only the node_modules it actually uses.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
