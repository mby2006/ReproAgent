FROM node:24-alpine

ARG NPM_REGISTRY=https://registry.npmjs.org

RUN npm install -g pnpm@12.18.0 --registry=${NPM_REGISTRY} \
    && pnpm config set registry ${NPM_REGISTRY}

WORKDIR /app

COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/package.json
RUN pnpm install --frozen-lockfile

COPY apps/web apps/web

WORKDIR /app/apps/web
EXPOSE 3000
CMD ["pnpm", "dev", "--hostname", "0.0.0.0"]