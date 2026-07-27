# Cloud-init for Hetzner

Use this file in the Hetzner server creation wizard under the cloud-init section.

## What it does

- installs Docker CE and the Docker Compose plugin
- installs Git and UFW
- opens SSH, HTTP, and HTTPS ports
- clones your repository into `/opt/chm-api`
- creates a basic `.env` file
- starts the Docker stack with Caddy

## Before using it

Edit these values in [cloud-init.yaml](cloud-init.yaml):

- `REPO_URL`
- `BRANCH`
- `DOMAIN`
- `EMAIL`
- `API_KEY`

## Notes

The script assumes your repository already contains the Docker files used by this project:

- [Dockerfile](Dockerfile)
- [docker-compose.yml](docker-compose.yml)
- [Caddyfile](Caddyfile)
