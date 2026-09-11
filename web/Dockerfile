# Interface React.
#
# Build multi-etage : les ~200 Mo de node_modules servent a produire ~250 Ko de
# fichiers statiques et n'ont rien a faire dans l'image de production.

FROM node:22-alpine AS builder
WORKDIR /app

# Les dependances d'abord : tant que le manifeste ne bouge pas, cette couche
# reste en cache et un deploiement ne rebuild que le code applicatif.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Port non privilegie : le conteneur n'a aucune raison de reclamer le droit
# d'ouvrir un port < 1024.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
