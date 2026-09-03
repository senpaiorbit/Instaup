from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_client(config, logger=None):
    from instagrapi import Client

    client = Client()
    client.request_timeout = 10
    client.session_retry_total = 2

    session_path = PROJECT_ROOT / "session.json"
    try:
        client.load_settings(str(session_path))
    except Exception as e:
        msg = f"Failed to load session from {session_path}: {e}"
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return None

    try:
        client.account_info()
    except Exception as e:
        msg = f"Session verification failed (account_info): {e}"
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return None

    client.delay_range = config.get("delay_range", [1, 3])
    return client


def save_session(client, path="session.json"):
    out = Path(path)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    client.dump_settings(str(out))
