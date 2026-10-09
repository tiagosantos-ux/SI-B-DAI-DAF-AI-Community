"""
DIA SI B Knowledge Base Portal
Prototype — calls Confluence API + SDC LLM Gateway (Claude)
"""

import os
import httpx

# Load .env — use absolute path so uvicorn worker processes find it
_env_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ[_k.strip()] = _v.strip()  # override, not setdefault
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import json
import asyncio

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://llm.sdc.siemens.cloud")
CLAUDE_MODEL = "claude-sonnet-4-6"

CONFLUENCE_SPACE = "SIDCRSS"
CONFLUENCE_ROOT_PAGE = "997393172"


def get_conf_config():
    return (
        os.environ.get("CONF_BASE_URL", "https://wiki.si.siemens.cloud"),
        os.environ.get("CONF_TOKEN", ""),
    )

SYSTEM_PROMPT = """You are the DIA SI B Knowledge Base assistant for the Siemens SI B division.

You answer questions by searching the team's official Confluence documentation space.

Space key: SIDCRSS
Root page ID: 997393172
Base URL: https://wiki.si.siemens.cloud

You will receive pre-fetched Confluence search results and page content in the user message.
Use that content to compose a clear, accurate answer.

Response rules:
- Answer in the same language the user asked (Portuguese or English)
- Lead with a direct 2-4 sentence answer
- Use bullet points for detail
- Always end with: Sources: [list of page titles and URLs found in the context]
- If the information is not in the provided context, say so clearly
- Adapt technical depth to the question: business questions → plain language; tool/data questions → technical detail
- Never invent information not present in the context
"""

app = FastAPI(title="DIA SI B Knowledge Base")


# ── Confluence helpers ─────────────────────────────────────────────────────────

def confluence_headers():
    _, token = get_conf_config()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def confluence_search(query: str, limit: int = 5) -> list[dict]:
    base_url, _ = get_conf_config()
    cql = f'space = "{CONFLUENCE_SPACE}" AND type = page AND (title ~ "{query}" OR text ~ "{query}") ORDER BY lastModified DESC'
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        r = await client.get(
            f"{base_url}/rest/api/content/search",
            headers=confluence_headers(),
            params={"cql": cql, "limit": limit, "expand": "excerpt"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        results = []
        for item in data.get("results", []):
            results.append({
                "id": item["id"],
                "title": item["title"],
                "url": f"{base_url}/spaces/{CONFLUENCE_SPACE}/pages/{item['id']}",
                "excerpt": item.get("excerpt", ""),
            })
        return results


async def confluence_get_page(page_id: str, max_chars: int = 3000) -> str:
    base_url, _ = get_conf_config()
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        r = await client.get(
            f"{base_url}/rest/api/content/{page_id}",
            headers=confluence_headers(),
            params={"expand": "body.storage"},
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        # Strip HTML tags simply
        import re
        raw = data.get("body", {}).get("storage", {}).get("value", "")
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]


async def build_context(query: str) -> tuple[str, list[dict]]:
    """Search Confluence and fetch top page content. Returns (context_text, sources)."""
    results = await confluence_search(query, limit=4)
    if not results:
        return "No relevant pages found in Confluence.", []

    context_parts = []
    sources = []

    # Fetch content from top 2 results
    fetch_tasks = [confluence_get_page(r["id"]) for r in results[:2]]
    page_contents = await asyncio.gather(*fetch_tasks)

    for result, content in zip(results[:2], page_contents):
        if content:
            context_parts.append(
                f"=== Page: {result['title']} ===\nURL: {result['url']}\n\n{content}\n"
            )
            sources.append(result)

    # Add remaining results as references only
    for result in results[2:]:
        context_parts.append(
            f"=== Also found: {result['title']} ===\nURL: {result['url']}\nExcerpt: {result.get('excerpt', '')}\n"
        )
        sources.append(result)

    return "\n\n".join(context_parts), sources


# ── LLM call ──────────────────────────────────────────────────────────────────

async def ask_claude_stream(question: str, context: str):
    """Generator that streams Claude response tokens."""
    user_message = f"""Question: {question}

Confluence context:
{context}

Answer the question based on the context above."""

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "stream": True,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    }

    headers = {
        "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        async with client.stream(
            "POST",
            f"{ANTHROPIC_BASE_URL}/v1/messages",
            headers=headers,
            json=payload,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield delta.get("text", "")
                    except json.JSONDecodeError:
                        pass


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML_PAGE)


@app.post("/ask")
async def ask(request: Request, question: str = Form(...)):
    context, sources = await build_context(question)

    async def event_stream():
        # First send sources as a metadata event
        yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"
        # Then stream the answer
        async for token in ask_claude_stream(question, context):
            yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── HTML (single-file, no external assets) ────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DIA SI B · Knowledge Base</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f6fa;
    color: #1a1a2e;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  header {
    background: #009999;
    color: white;
    padding: 16px 32px;
    display: flex;
    align-items: center;
    gap: 14px;
    box-shadow: 0 2px 8px rgba(0,0,0,.15);
  }
  header svg { flex-shrink: 0; }
  header h1 { font-size: 1.2rem; font-weight: 600; letter-spacing: .3px; }
  header span { font-size: .85rem; opacity: .85; }

  main {
    flex: 1;
    max-width: 860px;
    width: 100%;
    margin: 40px auto;
    padding: 0 20px;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .search-box {
    background: white;
    border-radius: 12px;
    padding: 24px;
    box-shadow: 0 2px 12px rgba(0,0,0,.07);
  }
  .search-box label {
    display: block;
    font-size: .85rem;
    font-weight: 600;
    color: #555;
    margin-bottom: 10px;
    text-transform: uppercase;
    letter-spacing: .5px;
  }
  .input-row {
    display: flex;
    gap: 10px;
  }
  input[type=text] {
    flex: 1;
    border: 1.5px solid #ddd;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 1rem;
    outline: none;
    transition: border-color .2s;
  }
  input[type=text]:focus { border-color: #009999; }

  button {
    background: #009999;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 12px 22px;
    font-size: .95rem;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    transition: background .2s, opacity .2s;
  }
  button:hover { background: #007a7a; }
  button:disabled { opacity: .6; cursor: default; }

  .suggestions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 14px;
  }
  .chip {
    background: #e8f5f5;
    color: #007a7a;
    border: 1px solid #b2dfdf;
    border-radius: 20px;
    padding: 6px 14px;
    font-size: .82rem;
    cursor: pointer;
    transition: background .15s;
  }
  .chip:hover { background: #c5e8e8; }

  .answer-box {
    background: white;
    border-radius: 12px;
    padding: 28px;
    box-shadow: 0 2px 12px rgba(0,0,0,.07);
    display: none;
  }
  .answer-box.visible { display: block; }

  .answer-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid #eee;
  }
  .answer-header .q-label {
    font-size: .78rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .5px;
    color: #888;
  }
  .answer-header .q-text {
    font-size: .95rem;
    color: #333;
    font-style: italic;
  }

  #answer-content {
    font-size: .97rem;
    line-height: 1.75;
    color: #222;
    white-space: pre-wrap;
    min-height: 40px;
  }

  .sources-section {
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid #eee;
  }
  .sources-section h4 {
    font-size: .78rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .5px;
    color: #888;
    margin-bottom: 10px;
  }
  .source-item {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  .source-item a {
    font-size: .88rem;
    color: #009999;
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-color .15s;
  }
  .source-item a:hover { border-color: #009999; }

  .spinner {
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid #ddd;
    border-top-color: #009999;
    border-radius: 50%;
    animation: spin .7s linear infinite;
    vertical-align: middle;
    margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .status-bar {
    font-size: .82rem;
    color: #888;
    margin-bottom: 10px;
    height: 18px;
  }

  footer {
    text-align: center;
    font-size: .78rem;
    color: #aaa;
    padding: 20px;
  }
</style>
</head>
<body>

<header>
  <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
    <rect width="28" height="28" rx="6" fill="white" fill-opacity=".2"/>
    <path d="M8 14h12M14 8v12" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
  </svg>
  <div>
    <h1>DIA SI B · Knowledge Base</h1>
    <span>Powered by Confluence + Claude (Siemens SDC)</span>
  </div>
</header>

<main>
  <div class="search-box">
    <label>Ask anything about DIA SI B</label>
    <div class="input-row">
      <input type="text" id="question" placeholder="e.g. How does the Forecast report work?" autofocus />
      <button id="ask-btn" onclick="ask()">Ask</button>
    </div>
    <div class="suggestions">
      <span class="chip" onclick="fill('What is the DIA SI B team responsible for?')">What is DIA SI B?</span>
      <span class="chip" onclick="fill('How does the Forecast report work?')">Forecast report</span>
      <span class="chip" onclick="fill('How does the SI B Forecast Collection Tool work?')">Collection Tool</span>
      <span class="chip" onclick="fill('What are the available Power BI reports?')">Power BI reports</span>
      <span class="chip" onclick="fill('What is the data flow from Snowflake to Power BI?')">Data flow</span>
      <span class="chip" onclick="fill('How does Row Level Security work in the reports?')">RLS / Security</span>
    </div>
  </div>

  <div class="answer-box" id="answer-box">
    <div class="answer-header">
      <div>
        <div class="q-label">Question</div>
        <div class="q-text" id="q-display"></div>
      </div>
    </div>
    <div class="status-bar" id="status-bar"></div>
    <div id="answer-content"></div>
    <div class="sources-section" id="sources-section" style="display:none">
      <h4>Sources</h4>
      <div id="sources-list"></div>
    </div>
  </div>
</main>

<footer>DIA SI B · Internal use only · Data sourced from Confluence (SIDCRSS)</footer>

<script>
function fill(text) {
  document.getElementById('question').value = text;
  document.getElementById('question').focus();
}

document.getElementById('question').addEventListener('keydown', e => {
  if (e.key === 'Enter') ask();
});

async function ask() {
  const question = document.getElementById('question').value.trim();
  if (!question) return;

  const btn = document.getElementById('ask-btn');
  const box = document.getElementById('answer-box');
  const content = document.getElementById('answer-content');
  const statusBar = document.getElementById('status-bar');
  const sourcesSection = document.getElementById('sources-section');
  const sourcesList = document.getElementById('sources-list');

  btn.disabled = true;
  box.classList.add('visible');
  document.getElementById('q-display').textContent = question;
  content.textContent = '';
  sourcesList.innerHTML = '';
  sourcesSection.style.display = 'none';
  statusBar.innerHTML = '<span class="spinner"></span> Searching Confluence...';

  const form = new FormData();
  form.append('question', question);

  try {
    const response = await fetch('/ask', { method: 'POST', body: form });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let answerStarted = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\\n\\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (!raw) continue;

        try {
          const event = JSON.parse(raw);

          if (event.type === 'sources') {
            statusBar.innerHTML = '<span class="spinner"></span> Generating answer...';
            if (event.sources && event.sources.length > 0) {
              sourcesList.innerHTML = event.sources.map(s =>
                `<div class="source-item">
                  <span>📄</span>
                  <a href="${s.url}" target="_blank">${s.title}</a>
                </div>`
              ).join('');
              sourcesSection.style.display = 'block';
            }
          }

          if (event.type === 'token') {
            if (!answerStarted) {
              statusBar.innerHTML = '';
              answerStarted = true;
            }
            content.textContent += event.text;
            box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }

          if (event.type === 'done') {
            statusBar.innerHTML = '';
          }
        } catch (e) { /* skip malformed events */ }
      }
    }
  } catch (err) {
    statusBar.innerHTML = '';
    content.textContent = 'Error connecting to the server. Please try again.';
  }

  btn.disabled = false;
}
</script>
</body>
</html>
"""
