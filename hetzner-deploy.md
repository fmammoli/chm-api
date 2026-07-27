# Hetzner deployment template

This file gives you a copy-paste deployment path for a small Ubuntu VPS.

## 1. Prepare the server

SSH into the Hetzner VPS and run:

```bash
chmod +x deploy-hetzner.sh
./deploy-hetzner.sh
```

## 2. Edit the domain settings

Before running the script, update these values inside the script:

- `REPO_URL` with your GitHub repository URL
- `BRANCH` if you use a branch other than `main`
- `DOMAIN` with the domain you will use for the API
- `EMAIL` with an email address for Let’s Encrypt

## 3. Update Caddy

The script will replace the placeholder domain in [Caddyfile](Caddyfile) automatically.

## 4. Verify deployment

After the script finishes, check:

```bash
curl https://your-domain/health
```

If you want to inspect the containers:

```bash
docker compose ps
```
