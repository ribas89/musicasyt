# MusicasYT

MusicasYT is an on-demand playlist worker for [Musicas](https://github.com/ribas89/musicas). It accepts supported playlist URLs, downloads audio, stores files and playlist state in per-user folders, and provides an HTTP API for synchronization and job status.

## Requirements

- Docker Compose
- A shared user-library directory
- An existing Docker network named `proxy`
- Traefik for external HTTPS access

The container runs as `1000:1000`. Mount the shared user-library directory at `/users`.

## Configuration

Copy `.env.example` to `.env` and set the values for your deployment. Generate an API key with:

```bash
openssl rand -hex 32
```

Set the generated value as `MUSICASYT_API_KEY`. Use the same key in the backend that calls this service. Keep it server-side and do not expose it to browsers.

## Docker Compose

```yaml
services:
  musicasyt:
    image: ghcr.io/ribas89/musicasyt:latest
    container_name: musicasyt
    user: "1000:1000"
    env_file:
      - .env
    volumes:
      - ./data:/data
      - /path/to/user/library:/users
    restart: unless-stopped
    networks:
      - proxy
    labels:
      - traefik.enable=true
      - traefik.docker.network=proxy
      - traefik.http.routers.musicasyt.rule=Host(`app.example.com`)
      - traefik.http.routers.musicasyt.service=musicasyt
      - traefik.http.services.musicasyt.loadbalancer.server.port=${MUSICASYT_PORT:-8080}

networks:
  proxy:
    external: true
    name: proxy
```

Before starting the service:

1. Replace `/path/to/user/library` with the host path of the shared user library.
2. Replace `app.example.com` with the hostname used to access the service.
3. Make sure the external Docker network `proxy` exists and Traefik is connected to it.
4. Make sure the user folders already exist and are writable by UID:GID `1000:1000`.

Start the service with:

```bash
docker compose up -d
```

The backend should call the service through its HTTPS hostname and send the same `MUSICASYT_API_KEY` in the `X-API-Key` header.

## Environment example

```dotenv
MUSICASYT_HOST=0.0.0.0
MUSICASYT_PORT=8080
MUSICASYT_API_KEY=replace-with-a-long-random-secret
MAX_QUEUED_JOBS=20
LOG_LEVEL=INFO
MUSICASYT_COOKIES=
MUSICASYT_USER_AGENT=
```

`MUSICASYT_HOST` and `MUSICASYT_PORT` control the HTTP listener inside the container. Keep the host at `0.0.0.0` when using Docker. If you change the port, update the Traefik service port in `docker-compose.yml` to match.

`MAX_QUEUED_JOBS` sets the maximum number of active and queued jobs. The default is `20`.

`LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. The default is `INFO`. Set `LOG_LEVEL=DEBUG` in `.env` and recreate the container to enable detailed diagnostics.

In `DEBUG` mode, the worker logs the complete parsed `get_playlist()` result as formatted JSON, playlist and track processing, and the full verbose output from the media downloader, including extractor activity, format selection, retries, progress, conversion, metadata and errors. Authentication headers, cookie values, and raw HTTP request or response bodies are never written to the logs.

```bash
docker compose up -d --build --force-recreate musicasyt
docker logs -f musicasyt
```

`MUSICASYT_COOKIES` is optional. When set, it must point to a Netscape/Mozilla `cookies.txt` file inside the container, for example `/data/cookies.txt`. Place the file in the host `./data` directory so it is available through the existing `/data` mount. The same cookie file is used by the playlist client and the downloader; the required request authentication is prepared automatically. No separate authentication file is needed.

`MUSICASYT_USER_AGENT` is optional. When set, the same User-Agent is used by the playlist client and the downloader. Leave it empty to keep their default values.

## API

- `GET /status` reports startup and worker status.
- `POST /sync` starts or returns a synchronization job. Send `X-API-Key` and a JSON body containing `username` and `playlist_url`.
- `GET /jobs/{username}/{playlist_id}` returns the in-memory job status.
- `DELETE /playlists/{username}/{playlist_id}` removes a registered playlist and its folder.

Job state is held in memory. Playlist metadata is stored in `musicasyt.json` in each user's library, so the last known playlist information remains available after a worker restart.

