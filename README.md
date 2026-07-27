# CHM Crop API

FastAPI service that accepts a GeoJSON AOI, crops canopy height map tiles from the WRI/Meta CHM v2 dataset, and returns a GeoTIFF stream for OpenLayers.

## Scope

- Indonesia-only data policy is enforced.
- Validation uses an exact Indonesia country polygon from a local boundary file.
- If input is outside Indonesia, API returns: `it only shows data from indonesia`.

## Run locally

1. Install dependencies:

```bash
uv sync
```

2. Copy environment config:

```bash
cp .env.example .env
```

3. Set a strong API key in `.env`.

	You can use `CHM_API_KEY`, `API_KEY`, or `CANOPY_API_KEY` for the FastAPI server key.

4. Start API:

```bash
uv run fastapi dev app/main.py
```

## API

### Health

- `GET /health`

### Crop canopy raster

- `POST /api/v1/chm/crop`
- Header: `X-API-Key: <your key>`
- AOI behavior: input AOI is used as-is and must comply with the configured square size (default 20 km x 20 km).
- Body:

```json
{
	"geojson": {
		"type": "FeatureCollection",
		"features": [
			{
				"type": "Feature",
				"properties": {},
				"geometry": {
					"type": "Polygon",
					"coordinates": [[[106.7, -6.4], [107.2, -6.4], [107.2, -6.0], [106.7, -6.0], [106.7, -6.4]]]
				}
			}
		]
	}
}
```

Response:

- Content type: `image/tiff`
- Headers include raster metadata:
	- `X-Raster-CRS`
	- `X-Raster-Bounds`
	- `Content-Disposition: inline; filename="canopy_height_output.tif"`

### Crop CTrees AGB raster

- `POST /api/v1/ctrees/agb/crop`
- Header: `X-API-Key: <your key>`
- AOI behavior: input AOI is used as-is and must comply with the configured square size (default 20 km x 20 km).
- Body:

```json
{
	"year": 2025,
	"variable": "agb",
	"geojson": {
		"type": "FeatureCollection",
		"features": [
			{
				"type": "Feature",
				"properties": {},
				"geometry": {
					"type": "Polygon",
					"coordinates": [[[106.7, -6.4], [107.2, -6.4], [107.2, -6.0], [106.7, -6.0], [106.7, -6.4]]]
				}
			}
		]
	}
}
```

Response:

- Content type: `image/tiff`
- Output CRS: `EPSG:3857` (Web Mercator)
- Headers include raster metadata:
	- `X-Raster-CRS`
	- `X-Raster-Bounds`
	- `X-Raster-Year`
	- `X-Raster-Variable`
	- `Content-Disposition: inline; filename="ctrees_agb_<variable>_<year>.tif"`

## Security and performance defaults

- API key authentication.
- In-memory per-IP rate limit.
- Input validation: geometry type, geometry validity, bounds, payload size, AOI size, vertex count, tile count.
- AOI rule: input polygon is not rewritten; API validates it matches the configured square side length (default `20`).
- To change the default footprint for all deployments, edit the default in [app/config.py](app/config.py) and push the change.
- Temporary files are cleaned after response.
- Tile index metadata is cached in memory with TTL.

## GeoTIFF output guarantees

The crop endpoint now emits map-scale robust GeoTIFF output intended for OpenLayers WebGL GeoTIFF layers:

- Tiled output with 512x512 internal blocks.
- DEFLATE compression.
- Explicit NoData propagation from source tiles through crop, merge, and export.
- Internal overviews at multiple levels for zoomed-out rendering.
- Band statistics tags for min/max visibility checks.

## Validate output metadata

1. Create a baseline output (optional):

uv run fastapi dev app/main.py
curl -sS -X POST http://localhost:8000/api/v1/chm/crop -H "Content-Type: application/json" -H "X-API-Key: <key>" -d '{"geojson":{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[120.5,-9.8],[120.6,-9.8],[120.6,-9.9],[120.5,-9.9],[120.5,-9.8]]]},"properties":{}}}' -o /tmp/chm_after.tif

2. Run metadata checks and before/after comparison:

/Users/femama/Developer/beyond_carbon/chm-api/.venv/bin/python scripts/validate_raster_metadata.py /tmp/chm_after.tif --before /tmp/chm_before.tif

3. Optional GDAL verification (if gdalinfo is in conda env):

conda run -n bc-workshop gdalinfo /tmp/chm_after.tif

Expected output characteristics:

- Tiled raster (`Block=512x512`)
- Compression (`COMPRESSION=DEFLATE`)
- Explicit `NoData Value`
- Multiple `Overviews`
- Valid CRS and pixel transform

## Migration note

Existing previously generated outputs should be reprocessed if they do not include overviews or are not tiled, because zoomed-out rendering performance and visibility depend on those pyramid levels.

## Tooling notes

- `rasterio` must be built against a GDAL version with COG support (GDAL >= 3.1 recommended).
- If COG driver translation is unavailable at runtime, the API falls back to tiled/compressed GeoTIFF while preserving NoData and overviews.

## Deploy

- Set environment variables from `.env.example`.
- Use `fastapi run` in production.
- Put this API behind TLS and a managed gateway/WAF.
- For multi-instance deployments, replace in-memory rate limiting with a shared store (for example, Redis).

### Docker deployment

Build and run the API locally with Docker Compose:

```bash
docker compose up --build
```

The service will be available at http://localhost:8000/health.

### View logs

To view the API container logs locally:

```bash
docker compose logs -f chm-api
```

To follow the recent logs from the server:

```bash
sudo docker compose logs --tail=100 -f chm-api
```

You can also inspect the Caddy proxy logs if HTTPS or routing is failing:

```bash
sudo docker compose logs -f caddy
```

If you enabled the browser-based log viewer, open it in your browser at:

```text
http://<your-server-ip>/logs-ui
```

This route shows the recent application logs in the UI. If you are testing locally, use:

```text
http://localhost:8000/logs-ui
```

### Deploy changes

The easiest way to deploy the latest code from this repository to your Hetzner server is:

```bash
./scripts/update-server.sh
```

That script connects to the configured server, pulls the latest commit from GitHub, and rebuilds/restarts the Docker containers. The script is defined in [scripts/update-server.sh](scripts/update-server.sh).

If you prefer to deploy manually on the server, run:

```bash
cd /opt/chm-api
sudo git pull origin main
sudo docker compose up -d --build
```

Keep your runtime secrets in the server's `.env` file and do not overwrite it during deployment.

### Reverse proxy and HTTPS

The compose setup now includes a Caddy container that listens on ports 80 and 443 and forwards traffic to the FastAPI app. This is the easiest HTTPS path for a Hetzner VPS:

1. Point your domain to the VPS IP.
2. Edit [Caddyfile](Caddyfile) and replace `:80` with your actual domain, for example `api.example.com`.
3. Start the stack with Docker Compose.
4. Caddy will automatically request and renew a TLS certificate from Let's Encrypt.

This avoids the manual certificate work that usually makes HTTPS setup painful.

For a Hetzner VPS, copy the repository, create a `.env` file with your runtime settings, and run the same compose command on the server.
