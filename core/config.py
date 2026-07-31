"""
Where the API key and the model name come from.

The key is deliberately NOT entered in the UI. It's read from disk (or the
environment) so it never appears on screen, never sits in a browser form, and
never has to be re-typed on every run. Resolution order, first hit wins:

    1. ANTHROPIC_API_KEY environment variable
    2. config.json in the app folder      -> {"api_key": "sk-ant-..."}
    3. .env in the app folder             -> ANTHROPIC_API_KEY=sk-ant-...

The model works the same way, with `model` in config.json as the default, and
the UI free to override it for a single run without editing any file.

A NOTE ON KEEPING THE KEY SAFE
==============================
config.json and .env hold a live credential in plain text. Anyone with the
folder has the key. So:
  - don't commit either file to git (add both to .gitignore)
  - don't hand the folder to someone else with the key still in it
  - if the key leaks, revoke it at console.anthropic.com rather than editing
    it out and hoping
The app never prints the key, and only ever shows its last four characters so
you can tell WHICH key is loaded without exposing it.
"""

import json
import os
import re
from pathlib import Path

# Fallback when nothing configures a model. Kept in one place so there is no
# model name hardcoded anywhere else in the codebase.
DEFAULT_MODEL = "claude-sonnet-4-6"

CONFIG_FILENAME = "config.json"
DOTENV_FILENAME = ".env"


def app_dir():
    """The folder holding app.py -- one level up from core/."""
    return Path(__file__).resolve().parent.parent


def _read_config_json(folder):
    path = Path(folder) / CONFIG_FILENAME
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else {}), path
    except (json.JSONDecodeError, OSError):
        return {}, path


def _read_dotenv(folder):
    """Minimal KEY=VALUE parser -- enough for a key file, no dependency."""
    path = Path(folder) / DOTENV_FILENAME
    if not path.exists():
        return {}, None
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}, path
    return values, path


def _read_streamlit_secrets():
    """
    (api_key, model) from Streamlit secrets, or (None, None).

    When the app is hosted, there is no config.json to edit and no shell to set
    an environment variable in -- the key is pasted into the host's secrets
    manager instead. Streamlit exposes those through st.secrets, which is a
    place plain os.environ never looks, so a hosted deployment would report
    "no API key found" however correctly the secret was set.

    Accessing st.secrets raises if no secrets file exists at all, which is the
    normal case when running locally, so every failure here is silent.
    """
    try:
        import streamlit as st
    except Exception:
        return None, None
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY") or st.secrets.get("api_key")
        model = st.secrets.get("ANTHROPIC_MODEL") or st.secrets.get("model")
        key = str(key).strip() if key else None
        model = str(model).strip() if model else None
        return (key or None), (model or None)
    except Exception:
        return None, None


def describe_sources(folder=None):
    """
    Where a key was looked for and whether one was there -- shown in the UI so
    a failed deployment can be diagnosed without guessing. Never returns the
    key itself.
    """
    folder = folder or app_dir()
    cfg, _ = _read_config_json(folder)
    env_file, _ = _read_dotenv(folder)
    secret_key, _ = _read_streamlit_secrets()
    return [
        ("ANTHROPIC_API_KEY environment variable",
         bool(str(os.environ.get("ANTHROPIC_API_KEY") or "").strip())),
        ("Streamlit secrets (hosted apps)", bool(secret_key)),
        (f"{CONFIG_FILENAME} in the app folder", bool(str(cfg.get("api_key") or "").strip())),
        (f"{DOTENV_FILENAME} in the app folder",
         bool(str(env_file.get("ANTHROPIC_API_KEY") or "").strip())),
    ]


def load_settings(folder=None):
    """
    Returns a dict:
        {"api_key": str|None, "api_key_source": str|None,
         "model": str, "model_source": str}
    """
    folder = folder or app_dir()
    cfg, cfg_path = _read_config_json(folder)
    env_file, env_path = _read_dotenv(folder)

    api_key, key_source = None, None
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    secret_key, secret_model = _read_streamlit_secrets()
    if env_key and env_key.strip():
        api_key, key_source = env_key.strip(), "ANTHROPIC_API_KEY environment variable"
    elif secret_key:
        api_key, key_source = secret_key, "Streamlit secrets"
    elif str(cfg.get("api_key") or "").strip():
        api_key, key_source = str(cfg["api_key"]).strip(), f"{CONFIG_FILENAME}"
    elif str(env_file.get("ANTHROPIC_API_KEY") or "").strip():
        api_key = str(env_file["ANTHROPIC_API_KEY"]).strip()
        key_source = DOTENV_FILENAME

    # A key pasted with surrounding quotes or a stray newline is a common
    # copy-paste slip and would otherwise fail with a confusing 401.
    if api_key:
        api_key = api_key.strip().strip('"').strip("'")
        api_key = re.sub(r'\s+', '', api_key)

    model, model_source = DEFAULT_MODEL, "built-in default"
    if secret_model:
        model, model_source = secret_model, "Streamlit secrets"
    elif str(cfg.get("model") or "").strip():
        model, model_source = str(cfg["model"]).strip(), CONFIG_FILENAME
    elif str(env_file.get("ANTHROPIC_MODEL") or "").strip():
        model, model_source = str(env_file["ANTHROPIC_MODEL"]).strip(), DOTENV_FILENAME

    return {
        "api_key": api_key,
        "api_key_source": key_source,
        "model": model,
        "model_source": model_source,
        "config_path": str(cfg_path) if cfg_path else str(Path(folder) / CONFIG_FILENAME),
    }


def masked(api_key):
    """'sk-ant-...a1B2' -- enough to identify a key, not enough to use it."""
    if not api_key:
        return None
    tail = api_key[-4:] if len(api_key) >= 4 else "?"
    return f"…{tail}"


def save_model(model, folder=None):
    """
    Persist a model choice into config.json, leaving the api_key untouched.
    Returns True on success.
    """
    folder = folder or app_dir()
    path = Path(folder) / CONFIG_FILENAME
    cfg, _ = _read_config_json(folder)
    cfg["model"] = model
    try:
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def list_models(api_key=None):
    """
    Model IDs available to this key, newest first, straight from the API.

    Asking the API beats hardcoding a list: model names change, and a stale
    hardcoded dropdown would offer models that no longer exist while hiding
    ones that do. Returns [] if the SDK is missing, the key is bad, or the
    call fails -- the caller then falls back to free-text entry.
    """
    if not api_key:
        return []
    try:
        import anthropic
    except ImportError:
        return []
    try:
        client = anthropic.Anthropic(api_key=api_key)
        page = client.models.list(limit=50)
        out = []
        for m in getattr(page, "data", []) or []:
            mid = getattr(m, "id", None)
            if mid:
                out.append(mid)
        return out
    except Exception:
        return []
