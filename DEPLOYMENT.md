# 🚀 AI Data Cleaner - Deployment Guide

This guide explains how to deploy the AI Data Cleaner API and Interactive Dashboard to production environments using **Docker**, **Render**, **Railway**, **Hugging Face Spaces**, or **Cloud Virtual Machines (AWS / GCP / DigitalOcean)**.

---

## 📋 Prerequisites & Environment Variables

| Variable | Required | Description |
|---|---|---|
| `PORT` | Optional (default: `8000`) | Server listening port |
| `ENVIRONMENT` | Optional (default: `development`) | Environment mode: `development` or `production` |
| `ALLOWED_ORIGINS` | Production: Required | Comma-separated trusted origins (e.g. `https://datarefine.example.com`). Wildcard `*` rejected in production |
| `GEMINI_API_KEY` | Optional | Google Gemini API key (defaults to free-tier `gemini-2.5-flash`) |
| `ALLOW_SERVER_GEMINI_FALLBACK` | Optional (default: `true`) | Set to `false` in public multi-tenant environments to require user BYOK keys |
| `AI_RATE_LIMIT_PER_MINUTE` | Optional (default: `10`) | Maximum AI planning requests per client IP per minute |
| `AI_SERVER_KEY_LIMIT_PER_HOUR` | Optional (default: `20`) | Maximum server-key AI plans per client IP per hour |
| `GLOBAL_RATE_LIMIT_PER_MINUTE` | Optional (default: `120`) | General API request rate limit per client IP per minute |
| `OPENAI_API_KEY` | Optional | OpenAI API key (for GPT-4o-mini or Groq/vLLM endpoints) |
| `ANTHROPIC_API_KEY` | Optional | Anthropic API key (for Claude 3.5 Haiku / Sonnet) |
| `OLLAMA_BASE_URL` | Optional | Local/air-gapped Ollama URL (default: `http://localhost:11434`) |

> [!NOTE]
> Even if no LLM API key is provided, the dashboard still functions completely using the built-in deterministic **Mock Provider** and rule-based heuristic guardrails.

---

## 🐳 Option 1: Docker & Docker Compose (Recommended)

### Using Docker Compose
1. Clone the repository and navigate to the project directory:
   ```bash
   git clone https://github.com/Ok-Sanjeev/ai-data-cleaner.git
   cd ai-data-cleaner
   ```
2. (Optional) Set your API keys in `.env` or environment:
   ```bash
   export GEMINI_API_KEY="your-gemini-key"
   ```
3. Build and run the container:
   ```bash
   docker compose up -d --build
   ```
4. Access the dashboard:
   👉 **`http://localhost:8000`**

### Using Standalone Docker
```bash
# Build the Docker image
docker build -t ai-data-cleaner .

# Run the container
docker run -d -p 8000:8000 \
  -e GEMINI_API_KEY="your-key" \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/runs:/app/runs \
  --name ai-cleaner ai-data-cleaner
```

---

## 🟣 Option 2: 1-Click Deploy on Render.com

1. Fork or push this repository to your GitHub account.
2. Sign in to [Render.com](https://render.com) and click **New + > Blueprint**.
3. Select your repository. Render will automatically detect `render.yaml`.
4. Add your `GEMINI_API_KEY` in the Environment Variables section.
5. Click **Apply**. Your app will build and deploy on a free or standard Render web service with automatic HTTPS!

---

## 🚂 Option 3: 1-Click Deploy on Railway.app

1. Sign in to [Railway.app](https://railway.app).
2. Click **New Project > Deploy from GitHub repo**.
3. Select `ai-data-cleaner`. Railway detects `railway.json` and `Dockerfile`.
4. Under **Variables**, add `GEMINI_API_KEY` (and optional `OPENAI_API_KEY`).
5. Click **Deploy**. Railway provisions a live public domain (e.g. `https://ai-data-cleaner-production.up.railway.app`).

---

## 🤗 Option 4: Deploy on Hugging Face Spaces

1. Create a new Space on [Hugging Face](https://huggingface.co/spaces).
2. Choose **Docker** as the Space SDK and blank template.
3. Push the contents of this repository to the Space's Git repository.
4. In Space **Settings > Variables and secrets**, add your `GEMINI_API_KEY` secret.
5. Hugging Face will automatically build the `Dockerfile` and serve the dashboard on port `8000`.

---

## 💻 Option 5: Deploy on Linux VM (Ubuntu / Debian / AWS EC2)

1. Connect to your server and clone:
   ```bash
   git clone https://github.com/Ok-Sanjeev/ai-data-cleaner.git /opt/ai-data-cleaner
   cd /opt/ai-data-cleaner
   ```
2. Setup Python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. Create a systemd service (`/etc/systemd/system/aicleaner.service`):
   ```ini
   [Unit]
   Description=AI Data Cleaner Service
   After=network.target

   [Service]
   User=ubuntu
   WorkingDirectory=/opt/ai-data-cleaner
   ExecStart=/opt/ai-data-cleaner/venv/bin/python server.py --host 0.0.0.0 --port 8000
   Restart=always
   Environment=PORT=8000
   Environment=GEMINI_API_KEY=your-api-key

   [Install]
   WantedBy=multi-user.target
   ```
4. Start and enable service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable aicleaner
   sudo systemctl start aicleaner
   ```

---

## 🔒 Security, Authentication & Multi-Tenant Isolation

DataRefine includes enterprise-grade security hardening in [`server.py`](server.py), [`session_store.py`](session_store.py), and [`auth_store.py`](auth_store.py):

- **Production Authentication & Argon2id**: User registration, email/password login, secure token issuance, and password reset flows with Argon2id password hashing (`time_cost=2`, `memory_cost=65536` [64 MiB], `parallelism=1`).
- **Optional Google OAuth 2.0**: Full OpenID Connect flow with CSRF state verification and server-side Google ID token signature verification via `google.oauth2.id_token`.
- **Multi-Tenant Data Confinement**: Each user's uploaded datasets are isolated into dedicated user directories (`data/uploads/<user_id>/`). Path resolution strictly checks ownership, preventing cross-tenant reads or profiling.
- **Session & Checkpoint Isolation**: Sessions, checkpoints, undo/redo states, and reports in `sessions_catalog.db` and on disk are tagged with user ownership. Unauthorized cross-user access attempts are rejected with HTTP 403 Forbidden.
- **Anonymous Demo Mode**: Public visitors can explore demo datasets with strict limits (10 MB upload limit, 10,000 row limit, 1-hour session TTL), while registered accounts enjoy full platform capacity (50 MB upload limit, 500,000 row limit, 24-hour session TTL).
- **Encrypted Preferences & Zero-Leakage API Keys**: User-provided LLM API keys are encrypted at rest using AES-128-CBC / HMAC-SHA256 (Fernet) and never returned in plaintext to the browser or logged in telemetry.
- **Strict File Root Jail (`ALLOWED_DATA_DIRS`)**: Client-supplied dataset file paths are validated by `resolve_safe_dataset_path()` and strictly confined to approved `data/` subdirectories, blocking path traversal and arbitrary file reads.
- **CORS Policy & Security Headers**: Strict HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, and Permissions-Policy response headers protect against clickjacking and MIME-sniffing.

---

## ⚙️ Environment Variables Reference

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | *(None)* | Default server Gemini API key. |
| `SECRET_KEY` | *(Auto-generated)* | Cryptographic master key for Fernet preference encryption and token signatures. Set to a strong random hex string in production. |
| `AUTH_TOKEN_EXPIRE_SECONDS` | `604800` (7 days) | Validity duration for Bearer authentication tokens. |
| `GOOGLE_CLIENT_ID` | *(Optional)* | Google Cloud OAuth 2.0 Client ID for Google Sign-In. |
| `GOOGLE_CLIENT_SECRET` | *(Optional)* | Google Cloud OAuth 2.0 Client Secret. |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/api/auth/google/callback` | Authorized redirect URI for Google OAuth callback. |
| `SESSION_MAX_AGE_SECONDS` | `86400` (24 hours) | Inactive TTL for authenticated user cleaning sessions. |
| `DEMO_SESSION_MAX_AGE_SECONDS`| `3600` (1 hour) | Inactive TTL for anonymous guest cleaning sessions. |
| `MAX_UPLOAD_SIZE_BYTES` | `52428800` (50 MB) | Maximum upload file size for registered accounts. |
| `MAX_DEMO_UPLOAD_SIZE_BYTES` | `10485760` (10 MB) | Maximum upload file size for anonymous guest visitors. |
| `MAX_DATASET_ROWS` | `500000` | Maximum rows allowed for dataset profiling. |
| `MAX_DEMO_DATASET_ROWS` | `10000` | Maximum rows allowed for anonymous uploaded dataset profiling. |
| `ALLOWED_ORIGINS` | `*` | Comma-separated list of permitted CORS origins. |
| `ENVIRONMENT` | `development` | Set to `production` in live deployments. |

---

## 🔍 Healthcheck & Verification

Verify the deployment is healthy:
```bash
curl -f http://localhost:8000/api/health
```
Response:
```json
{
  "status": "healthy",
  "service": "AI Data Cleaner",
  "version": "2.0.0",
  "supported_providers": ["gemini", "openai", "anthropic", "ollama", "mock"]
}
```
Interactive API docs are available at **`/docs`** (Swagger UI).
