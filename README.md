# TAWCA Conference System

This repository contains a Streamlit app for conference registration, QR check-in, attendance, and certificate generation.

Quick start (local):

1. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the app locally:

```bash
streamlit run app.py
```

Deployment options:

- Streamlit Community Cloud: push this repo to GitHub and connect it to Streamlit Cloud.
- Docker: build the provided Dockerfile and run the container.
- Heroku: use the provided `Procfile` and `start.sh` to run the app.

See the `Dockerfile`, `Procfile`, and `.streamlit/config.toml` for configuration details.

Security note: update `ADMIN_USERNAME` and `ADMIN_PASSWORD` via environment variables in production.
# TAWCAEVENTSYSTEM
This is TAWCA Event system Used to manage Event Upload Photo and have the data Base of the participants Sending of Whatsapp invitations
