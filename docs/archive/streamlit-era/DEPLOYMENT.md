# Deployment Guide

This guide covers deploying the Research Paper AI Assistant to different platforms.

---

## Quick Start - Local Deployment

Already running locally? Here's the checklist:

```bash
# 1. Setup
cp .env.example .env
# Edit .env with your API keys

# 2. Install
pip install -r requirements.txt

# 3. Run
streamlit run home.py

# Open: http://localhost:8501
```

---

## Cloud Deployment Options

### Option 1: Streamlit Cloud (Recommended for Quick Setup)

**Pros**: Free tier, auto-deploys from GitHub, easiest setup
**Cons**: Limited resources, requires public GitHub repo

#### Steps:

1. **Push to GitHub** (see GITHUB_SETUP.md)

2. **Go to Streamlit Cloud**:
   - Visit: https://share.streamlit.io
   - Click "Create new app"
   - Connect GitHub account
   - Select: `your-repo/research-paper-assistant`
   - Branch: `main`
   - Path: `home.py`

3. **Add Secrets**:
   - In Streamlit Cloud dashboard, click "Edit secrets"
   - Add your environment variables:
   ```toml
   GROQ_API_KEY = "your_key_here"
   GITHUB_TOKEN = "your_token_here"
   LANGCHAIN_API_KEY = "your_key_here"
   ```

4. **Done!** App deploys automatically on every push

**Cost**: Free tier (with limitations) or $10/month Pro

---

### Option 2: Heroku Deployment

**Pros**: Good free tier historically, easy deployment
**Cons**: Heroku phased out free tier (Nov 2022)

#### If using Heroku Paid:

1. **Install Heroku CLI**:
   ```bash
   # Windows: Download from https://devcenter.heroku.com/articles/heroku-cli
   heroku login
   ```

2. **Create Procfile**:
   ```
   web: sh setup.sh && streamlit run home.py
   ```

3. **Create setup.sh**:
   ```bash
   #!/bin/bash
   mkdir -p ~/.streamlit/
   echo "[server]
   headless = true
   port = $PORT
   enableCORS = false
   " > ~/.streamlit/config.toml
   ```

4. **Deploy**:
   ```bash
   heroku create your-app-name
   heroku config:set GROQ_API_KEY=your_key
   heroku config:set GITHUB_TOKEN=your_token
   git push heroku main
   ```

---

### Option 3: Docker + Cloud Run (Google Cloud)

**Pros**: Scalable, cost-effective, flexible
**Cons**: Requires Docker knowledge

#### Create Dockerfile:

```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

CMD ["streamlit", "run", "home.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

#### Create .dockerignore:

```
.git
.gitignore
__pycache__
*.pyc
.pytest_cache
.venv
venv
.env
data/sessions.db
data/chroma
*.log
```

#### Deploy to Google Cloud Run:

```bash
# 1. Install Google Cloud CLI: https://cloud.google.com/sdk/docs/install

# 2. Authenticate
gcloud auth login

# 3. Set project
gcloud config set project YOUR_PROJECT_ID

# 4. Build and deploy
gcloud run deploy research-paper-assistant \
  --source . \
  --platform managed \
  --region us-central1 \
  --memory 2Gi \
  --timeout 3600 \
  --set-env-vars GROQ_API_KEY=your_key,GITHUB_TOKEN=your_token
```

**Cost**: $0.40/month + $0.00002500 per request

---

### Option 4: AWS Deployment

#### Using Elastic Beanstalk:

```bash
# 1. Install EB CLI
pip install awsebcli

# 2. Initialize
eb init -p python-3.9 research-paper-assistant

# 3. Create environment
eb create research-paper-prod

# 4. Set environment variables
eb setenv GROQ_API_KEY=your_key GITHUB_TOKEN=your_token

# 5. Deploy
git push

# 6. Open
eb open
```

#### Or use EC2 (more manual but flexible):

```bash
# 1. Launch EC2 instance (Ubuntu 20.04)
# 2. SSH into instance
ssh -i your-key.pem ubuntu@your-instance-ip

# 3. Setup
sudo apt update
sudo apt install python3-pip git
git clone https://github.com/YOUR_USERNAME/research-paper-assistant.git
cd research-paper-assistant

# 4. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 5. Install and setup
pip install -r requirements.txt
cp .env.example .env
# Edit .env with API keys

# 6. Install Nginx (reverse proxy)
sudo apt install nginx

# 7. Create Systemd service
sudo nano /etc/systemd/system/streamlit.service
```

Create systemd service file:
```ini
[Unit]
Description=Streamlit App
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/research-paper-assistant
ExecStart=/home/ubuntu/research-paper-assistant/venv/bin/streamlit run home.py --server.port 8501
Restart=always

[Install]
WantedBy=multi-user.target
```

Then start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable streamlit
sudo systemctl start streamlit
sudo systemctl status streamlit
```

---

### Option 5: Azure Container Instances

```bash
# 1. Build Docker image
docker build -t research-paper-app .

# 2. Tag for Azure Registry
docker tag research-paper-app YOUR_REGISTRY.azurecr.io/research-paper-app

# 3. Push to Azure
docker push YOUR_REGISTRY.azurecr.io/research-paper-app

# 4. Deploy
az container create \
  --resource-group myResourceGroup \
  --name research-paper-app \
  --image YOUR_REGISTRY.azurecr.io/research-paper-app \
  --registry-login-server YOUR_REGISTRY.azurecr.io \
  --registry-username YOUR_USERNAME \
  --registry-password YOUR_PASSWORD \
  --environment-variables GROQ_API_KEY=your_key GITHUB_TOKEN=your_token
```

---

## Deployment Comparison

| Platform | Cost | Setup Time | Scalability | Best For |
|----------|------|-----------|-------------|----------|
| **Streamlit Cloud** | Free/10$/mo | 5 min | Low | Quick demos |
| **Heroku** | $7-25/mo | 10 min | Medium | Small apps |
| **Google Cloud Run** | Pay-per-use | 15 min | High | Production |
| **AWS** | Variable | 30 min | Very High | Enterprise |
| **Azure** | Pay-per-use | 20 min | High | Enterprise |
| **VPS (DigitalOcean/Linode)** | $4-30/mo | 30 min | High | Full control |

---

## Production Checklist

Before deploying to production:

- [ ] `.env.example` created and documented
- [ ] `.gitignore` configured (no API keys in repo)
- [ ] API keys stored in cloud provider secrets
- [ ] Database persists across restarts (✅ SQLite + volume mount)
- [ ] Vector store backed up (ChromaDB can be exported)
- [ ] Error logging configured
- [ ] Rate limiting implemented (GitHub API handled ✅)
- [ ] HTTPS enabled (cloud providers handle this)
- [ ] Security headers configured
- [ ] Memory/CPU limits set appropriately
- [ ] Monitoring/alerts setup

---

## Recommended Setup for Your Project

**For best cost-effectiveness and ease:**

```
Development: Local (your machine)
              ↓
Staging: Streamlit Cloud (free)
              ↓
Production: Google Cloud Run (scalable, $0.40/mo base)
```

**Steps**:

1. Push code to GitHub ✅
2. Deploy to Streamlit Cloud for testing ✅
3. Scale to Cloud Run when ready ✅

---

## Database & Storage Considerations

### SQLite (Current - works for small deployments)
- ✅ Works with Streamlit Cloud
- ✅ Works with Cloud Run
- ⚠️ Not suitable for distributed deployments

### For Production Scaling:
- **PostgreSQL** - Better for multi-instance deployments
- **MongoDB** - Good for document storage
- **DynamoDB** - Serverless option on AWS

### Vector Store Persistence:
- ChromaDB data should be on persistent volume
- Or use managed vector DB (Pinecone, Weaviate)

---

## Environment Variables in Production

**Never expose:**
```bash
GROQ_API_KEY=xxx
GITHUB_TOKEN=xxx
LANGCHAIN_API_KEY=xxx
```

**Instead use:**
- Streamlit Cloud: Secrets dashboard
- Google Cloud: Secret Manager
- AWS: Systems Manager Parameter Store
- Heroku: Config Vars
- Azure: Key Vault

---

## Quick Deployment Command Reference

```bash
# Streamlit Cloud (auto-deploy from GitHub)
# Just push to GitHub, it deploys automatically

# Google Cloud Run
gcloud run deploy research-paper-assistant --source .

# Docker locally
docker build -t research-paper-app .
docker run -e GROQ_API_KEY=xxx -p 8501:8501 research-paper-app

# Systemd (VPS)
systemctl restart streamlit
```

---

## Support & Monitoring

### Monitor App Health:
- Streamlit Cloud: Dashboard analytics ✅
- Cloud Run: Cloud Logging + Monitoring
- Self-hosted: Prometheus + Grafana

### Common Issues:
- **Timeout**: Increase timeout in Streamlit config
- **Memory**: Increase allocated memory in cloud provider
- **Cold start**: Use warmer tier or keep-alive ping
- **API Rate Limits**: Implement caching (already done ✅)

---

**Which deployment option interests you most?** I can provide more detailed setup for your choice! 🚀
