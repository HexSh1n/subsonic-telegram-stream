from .main import app  # noqa: F401


def _run() -> None:
    import uvicorn

    from .config import load_config

    cfg = load_config()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")


if __name__ == "__main__":
    _run()
