# Streamlit Cloud Deployment Guide

Deploy your Research Paper AI Assistant to Streamlit Cloud in minutes!

---

## Why Streamlit Cloud?

✅ **Free tier** available  
✅ **Auto-deploy** from GitHub (push → auto-deployed)  
✅ **HTTPS** included  
✅ **Custom domain** support  
✅ **No infrastructure** needed  
✅ **Perfect for** Streamlit apps  

---

## Step-by-Step Deployment

### Step 1: Push to GitHub

First, ensure your code is on GitHub (see GITHUB_SETUP.md):

```bash
cd d:\PYTHON\Research_paper_project_with_API

# If not already a git repo:
git init

# Add all files
git add .

# Commit
git commit -m "Initial commit: Research Paper AI Assistant"

# Add remote (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/research-paper-assistant.git
git branch -M main
git push -u origin main
```

**Verify**: Visit https://github.com/YOUR_USERNAME/research-paper-assistant

---

### Step 2: Create Streamlit Cloud Account

1. Go to: **https://share.streamlit.io**
2. Click **"Sign Up"**
3. Choose: **"Sign up with GitHub"**
4. Authorize Streamlit to access your GitHub account
5. ✅ Account created!

---

### Step 3: Deploy Your App

1. Go to **https://share.streamlit.io**
2. Click **"Create new app"**
3. Fill in deployment details:

   ```
   Repository: your-username/research-paper-assistant
   Branch: main
   File path: home.py
   ```

4. Click **"Deploy"**

**Status Page Opens**: https://share.streamlit.io/your-username/research-paper-assistant

---

### Step 4: Add API Keys (Secrets)

⚠️ **Important**: Never commit API keys to GitHub!

1. Go to your app dashboard
2. Click **⋮ (three dots)** → **Edit secrets**
3. Add your environment variables:

```toml
# Groq API Key
GROQ_API_KEY = "gsk_..."

# GitHub Token (optional but recommended)
GITHUB_TOKEN = "github_pat_..."

# LangChain (optional)
LANGCHAIN_API_KEY = "lsv2_..."

# Other configs
LANGCHAIN_PROJECT = "ResearchPaper-Assistant"
ENV = "production"
```

4. Click **Save**
5. Streamlit auto-restarts with your secrets ✅

---

### Step 5: Wait for Deployment

**First deploy** takes 2-5 minutes:

- 📦 Installing dependencies
- 📥 Downloading embedding models (~400MB)
- 🚀 Starting app

**Status**: Shows in real-time on the dashboard

Once complete: **App is LIVE! 🎉**

---

## Your Live App URL

```
https://share.streamlit.io/YOUR_USERNAME/research-paper-assistant
```

Share this link with anyone!

---

## Auto-Deploy on Push

Now whenever you push to GitHub:

```bash
# Make changes locally
echo "# Updates" >> README.md

# Push to GitHub
git add .
git commit -m "Update documentation"
git push origin main
```

**Streamlit Cloud automatically redeploys!** ✅

No manual deployment needed.

---

## Custom Domain (Optional)

Want your own domain?

1. In app settings, click **"Customize domain"**
2. Follow domain verification steps
3. Map your domain (e.g., `research-assistant.yourdomain.com`)

**Cost**: Free (you pay for domain only)

---

## Troubleshooting

### App takes too long to load

**Problem**: First run downloads embedding models (~400MB)

**Solutions**:
```bash
# Add this to requirements.txt to pre-download models
sentence-transformers>=2.2.0
```

Or pre-cache in Streamlit config:

Create `.streamlit/config.toml`:
```toml
[client]
showErrorDetails = true

[server]
maxUploadSize = 200
enableXsrfProtection = true
```

### "ModuleNotFoundError"

**Problem**: Missing dependency

**Solution**:
1. Update `requirements.txt`
2. Push to GitHub
3. Streamlit redeploys with new packages ✅

### API Keys not working

**Problem**: Secrets not loading

**Solution**:
1. Go to app settings
2. Click **"Reboot app"**
3. Wait 30 seconds
4. Try again

### App crashes on large repos

**Problem**: Memory limit (1GB default)

**Solution**:
1. Use Pro plan ($10/month)
2. Or optimize code:
   - Reduce `max_files` in Constructor (40 → 20)
   - Cache more aggressively
   - Use smaller embedding model

---

## Streamlit Cloud Tiers

### Free Tier ✅
- ✅ 1 app
- ✅ 1GB memory
- ✅ Monthly restarts
- ✅ Public apps only

**Cost**: FREE

### Pro Tier
- ✅ 3 apps
- ✅ 3GB memory
- ✅ No restarts
- ✅ Private apps
- ✅ Custom domain
- ✅ Priority support

**Cost**: $10/month

---

## Monitoring Your App

### View App Logs

1. In Streamlit Cloud dashboard
2. Click on your app
3. Scroll down to **"Logs"** section
4. View real-time output

### Track Usage

1. Dashboard shows: Users, page views, errors
2. Performance metrics built-in
3. Email alerts for crashes

---

## Best Practices

### 1. Use Environment Variables

```python
import os

groq_key = os.getenv("GROQ_API_KEY")
github_token = os.getenv("GITHUB_TOKEN")
```

Never hardcode keys!

### 2. Cache Expensive Operations

```python
@st.cache_resource
def load_model():
    return HuggingFaceEmbeddings(...)

embeddings = load_model()  # Loaded once, reused
```

Already implemented ✅

### 3. Handle Large Files

```python
@st.cache_data(ttl=3600)
def load_data():
    return expensive_operation()
```

Already implemented ✅

### 4. Optimize Dependencies

```bash
# Keep requirements.txt clean
# Remove unused packages
pip freeze > requirements.txt
```

Check your `requirements.txt` for unused packages.

### 5. Use .gitignore

Already created ✅

```
.env          # Never commit secrets
data/         # Large data files
venv/         # Virtual environment
__pycache__/  # Python cache
```

---

## Update Your App

### Push changes to GitHub

```bash
# Make any changes
git add .
git commit -m "Update feature X"
git push origin main
```

### Streamlit Cloud redeploys automatically!

Check dashboard → **"Deployed"** status updates

---

## Restart Your App

**If needed**:
1. Go to app dashboard
2. Click **⋮ → Reboot app**
3. App restarts with fresh environment

---

## Delete Your App

1. Dashboard → Your app
2. Click **⋮ → Delete app**
3. Confirm deletion

---

## Limits & Quotas

| Metric | Free Tier | Pro Tier |
|--------|-----------|----------|
| Memory | 1 GB | 3 GB |
| Storage | N/A | N/A |
| CPU | Shared | Shared |
| Timeout | 2 hours | 2 hours |
| Apps | 1 | 3 |
| Private Apps | ❌ | ✅ |
| Custom Domain | ❌ | ✅ |

---

## Example: Complete Flow

```bash
# 1. Make changes locally
echo "# New feature" >> CHANGELOG.md

# 2. Test locally
streamlit run home.py
# ✅ Works? Great!

# 3. Push to GitHub
git add .
git commit -m "Add new feature"
git push origin main

# 4. Streamlit Cloud auto-deploys
# 📱 Check: https://share.streamlit.io/your-username/research-paper-assistant

# 5. Updates live immediately! 🎉
```

---

## Need Help?

- **Streamlit Docs**: https://docs.streamlit.io
- **Streamlit Cloud Docs**: https://docs.streamlit.io/streamlit-cloud
- **Community**: https://discuss.streamlit.io
- **Status**: https://status.streamlit.io

---

## Deployment Summary

| Step | Time | Status |
|------|------|--------|
| 1. Push to GitHub | 5 min | ✅ |
| 2. Create Streamlit account | 5 min | ✅ |
| 3. Deploy from GitHub | 2-5 min | ✅ |
| 4. Add API keys | 2 min | ✅ |
| **Total** | **15 min** | **LIVE!** |

---

## Your Deployed App

**URL**: `https://share.streamlit.io/YOUR_USERNAME/research-paper-assistant`

**Features**:
- ✅ Constructor: Generate papers from GitHub
- ✅ Deconstructor: Analyze research papers
- ✅ Persistent chat history
- ✅ PDF generation
- ✅ Vector search
- ✅ Auto-scaling

---

**Ready to deploy?** 🚀

1. Push to GitHub (GITHUB_SETUP.md)
2. Create Streamlit Cloud account (share.streamlit.io)
3. Deploy and add secrets
4. **Done!**

Your app is live in 15 minutes! 🎉
