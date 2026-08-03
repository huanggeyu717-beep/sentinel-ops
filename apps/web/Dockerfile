# 多阶段: node 构建静态资源 -> nginx 托管 (compose 里映射 5173:80)
FROM node:20-alpine AS build
WORKDIR /app
COPY apps/web/package.json ./package.json
RUN npm install
COPY apps/web/ ./
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
