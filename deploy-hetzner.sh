#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/chm-api"
REPO_URL="https://github.com/your-username/your-repo.git"
BRANCH="main"
DOMAIN="api.example.com"
EMAIL="you@example.com"

sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git ufw

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker "$USER"

sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

sudo mkdir -p "$APP_DIR"
cd "$APP_DIR"
if [ ! -d .git ]; then
  sudo git clone -b "$BRANCH" "$REPO_URL" .
fi
sudo git pull origin "$BRANCH"

cat > .env <<EOF
API_KEY=replace-with-strong-random-secret
REQUIRE_API_KEY=true
CORS_ORIGINS=["https://your-frontend.example.com"]
TRUSTED_HOSTS=["localhost","127.0.0.1","$DOMAIN"]
MAX_GEOJSON_BYTES=1000000
MAX_AOI_AREA_KM2=1200
AOI_SQUARE_SIDE_KM=30
MAX_VERTICES=50000
MAX_TILES_PER_REQUEST=16
DOWNLOAD_WORKERS=2
MAX_CONCURRENT_CHM_JOBS=1
MAX_PENDING_CHM_JOBS=6
RATE_LIMIT_PER_MINUTE=30
EOF

sudo sed -i "s/api.example.com/$DOMAIN/g" Caddyfile
sudo sed -i "s/you@example.com/$EMAIL/g" Caddyfile

sudo docker compose up -d --build

printf '\nDeployment complete.\n'
printf 'Open https://%s/health to verify the service.\n' "$DOMAIN"
