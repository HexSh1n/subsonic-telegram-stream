# subsonic-telegram-stream

A self-hosted Subsonic-compatible music server backed by Telegram channels. It indexes audio files posted in one or more Telegram channels and serves them over the Subsonic API (`v1.16.1`, JSON) for desktop and mobile players such as **Feishin**, **Symfonium**, **Tempus**, **Substreamer**, and **foobar2000** (`foo_navidrome`).

Audio is streamed directly from Telegram with HTTP `Range` support (instant seeking without downloading the entire file) and cached on disk using an LRU eviction policy.

## Features

- **Direct Telegram Streaming:** Byte-range streaming via Telethon with local LRU disk caching for tracks and cover art.
- **Live Channel Sync:** Incremental indexing on startup plus real-time updates for new posts, edits, and deletions.
- **Metadata & Artwork Enrichment:** Background enrichment via Deezer, iTunes, and Wikipedia — resolves albums, track numbers, release years, genres, album covers, artist portraits, biographies, and related artists.
- **Multi-Client Playback Support:**
  - Server-side play queue synchronization (`getPlayQueue` / `savePlayQueue`, including automatic updates from `scrobble`).
  - Live playback status (`getNowPlaying`), local scrobbling (`recent` / `frequent` album lists), and favorites (`star` / `unstar`).
  - Multi-channel folder browsing (`getMusicFolders`, `getIndexes`, `getMusicDirectory`).
- **Proxy Support:** Optional MTProto, SOCKS5, SOCKS4, or HTTP proxy configuration for Telegram connections.

## Requirements

- Python 3.10+
- A Telegram account subscribed to the target channel(s)
- Telegram API credentials (`api_id` and `api_hash`) from [my.telegram.org](https://my.telegram.org)

## Installation

```bash
git clone https://github.com/HexSh1n/subsonic-telegram-stream.git
cd subsonic-telegram-stream

python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your settings:

```yaml
telegram:
  api_id: 123456
  api_hash: "your_api_hash"
  session: "tg-stream"
  channels:
    - "@your_music_channel"
  # Optional proxy (remove if not needed):
  # proxy:
  #   type: socks5          # mtproto | socks5 | socks4 | http
  #   server: 127.0.0.1
  #   port: 1080
  #   secret: ""            # required for mtproto

server:
  host: "0.0.0.0"
  port: 8080

users:
  - username: "admin"
    password: "change_me"

cache:
  dir: "./cache"
  max_bytes: 10737418240    # 10 GiB
```

## Telegram Authentication

Authenticate once to generate the local `<session>.session` file:

```bash
# Recommended: QR code login (Telegram -> Settings -> Devices -> Link Desktop Device)
python -m app.login_qr

# Alternative: phone number + verification code
python -m app.login
```

Keep the generated `.session` file private and backed up — deleting it requires signing in again.

## Running

```bash
python -m app
```

The Subsonic API will be available at `http://<host>:<port>/rest/`. Place the server behind a reverse proxy (e.g., Caddy or Nginx) with HTTPS for external/mobile access.

To manually trigger a channel rescan from a client, use the client's library scan action (`startScan`).

## Supported Subsonic Endpoints

| Category | Endpoints |
|---|---|
| System & Auth | `ping`, `getLicense`, `getUser`, `getUsers`, `getOpenSubsonicExtensions`, `getScanStatus`, `startScan` |
| Browsing | `getMusicFolders`, `getIndexes`, `getMusicDirectory`, `getArtists`, `getArtist`, `getArtistInfo`, `getArtistInfo2`, `getAlbum`, `getSong` |
| Lists & Search | `getAlbumList`, `getAlbumList2`, `getRandomSongs`, `getGenres`, `getSongsByGenre`, `search`, `search2`, `search3` |
| Media | `stream`, `download`, `getCoverArt` |
| User State | `star`, `unstar`, `getStarred`, `getStarred2`, `scrobble`, `getNowPlaying`, `getPlayQueue`, `savePlayQueue`, `getPlayQueueByIndex`, `savePlayQueueByIndex` |

## Project Structure

```text
app/
├── main.py                # FastAPI entrypoint, startup indexing, background enrichment & cache janitor
├── config.py              # YAML configuration loader and path resolution
├── runtime.py             # Shared application runtime state
├── login.py / login_qr.py # Interactive Telegram authentication utilities
├── telegram/              # Telethon client wrapper and range-based media streamer
├── library/               # SQLite database layer, channel indexer, and live event handlers
├── metadata/              # Deezer, iTunes, and Wikipedia metadata/artwork enrichment
├── subsonic/              # Subsonic auth, serializers, REST router, and streaming handlers
└── cache/                 # Disk LRU cache manager for audio and cover art
```

## Storage Layout

- `data/library.db` — SQLite library index, play history, favorites, artist metadata, and play queue
- `cache/tracks/` — Cached audio files (evicted via LRU when exceeding `cache.max_bytes`)
- `cache/covers/` — Cached album and artist artwork
- `<session>.session` — Telethon session file

## License

MIT
