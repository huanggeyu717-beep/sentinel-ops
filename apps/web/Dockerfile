# 多阶段: node 构建静态资源 -> nginx 托管 (compose 里映射 5173:80)
FROM node:20-alpine AS build
WORKDIR /app
# 锁文件必须一起 COPY 并用 npm ci: 原来只 COPY package.json + npm install,
# 服务器上现解现装的依赖版本可以与本机、与 CI 都不同 (SPEC-009 第四节)。
# npm ci 在锁文件与 package.json 不同步时直接失败, 不静默回退 —— W5 已把
# CI 里的 `npm ci || npm install` 兜底去掉, 这里同一条纪律。
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
