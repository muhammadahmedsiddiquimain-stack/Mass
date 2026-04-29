import json, math, os, ast, subprocess, datetime, urllib.parse, logging, re, secrets
from pathlib import Path
from flask import Flask, request, jsonify, session
import requests as req

MAX_HISTORY=20;MAX_INPUT=2000;MAX_FILE_SIZE=50000;EXEC_TIMEOUT=10;MAX_MEMORY=30;MAX_MSG_PER_HOUR=50
ALLOWED_CMDS={"ls","pwd","echo","date","whoami"}
IS_PROD=os.environ.get("RAILWAY_ENVIRONMENT") is not None
logging.basicConfig(level=logging.INFO,format="%(asctime)s [%(levelname)s] %(message)s")
log=logging.getLogger("AgentDeploy")
app=Flask(__name__)
app.secret_key=os.environ.get("SECRET_KEY",secrets.token_hex(32))
SESSIONS={}

def get_session_state():
    sid=session.get("sid")
    if not sid or sid not in SESSIONS:
        sid=secrets.token_hex(16);session["sid"]=sid
        SESSIONS[sid]={"agent":None,"provider_name":None,"memory":[],"msg_count":0,"hour_start":datetime.datetime.now().hour}
    return SESSIONS[sid]

def sanitize(text): return text.strip()[:MAX_INPUT]

def detect_injection(text):
    patterns=[r"ignore (previous|all) instructions",r"you are now",r"new system prompt",r"disregard",r"act as (a|an)",r"jailbreak"]
    return any(re.search(p,text.lower()) for p in patterns)

def rate_check(state):
    now_hour=datetime.datetime.now().hour
    if now_hour!=state["hour_start"]: state["msg_count"]=0;state["hour_start"]=now_hour
    if state["msg_count"]>=MAX_MSG_PER_HOUR: return False
    state["msg_count"]+=1;return Trueclass SessionMemory:
    def __init__(self,entries): self.entries=entries
    def add(self,content,tag="general"):
        e={"id":secrets.token_hex(4),"time":datetime.datetime.now().isoformat(),"tag":tag,"content":content[:400]}
        self.entries.append(e)
        if len(self.entries)>MAX_MEMORY: self.entries=self.entries[-MAX_MEMORY:]
        return f"Saved: {content[:60]}"
    def search(self,query):
        if not self.entries: return "No memories."
        matches=[e for e in self.entries if query.lower() in e["content"].lower()]
        return "\n".join(f"[{m['time'][:10]}] {m['content']}" for m in matches[-8:]) or f"Nothing for: {query}"
    def list_all(self):
        if not self.entries: return "No memories."
        return "\n".join(f"[{e['id']}] {e['time'][:10]} — {e['content'][:80]}" for e in self.entries[-15:])
    def delete(self,mid):
        before=len(self.entries);self.entries=[e for e in self.entries if e["id"]!=mid]
        return f"Deleted {mid}" if len(self.entries)<before else f"Not found: {mid}"
    def clear(self): self.entries.clear();return "Cleared."
    def to_context(self):
        if not self.entries: return ""
        return "Recent memories:\n"+"\n".join(f"- {e['content']}" for e in self.entries[-6:])

def tool_calculator(expression):
    try:
        tree=ast.parse(expression.strip(),mode="eval")
        allowed=(ast.Expression,ast.BinOp,ast.UnaryOp,ast.Constant,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Pow,ast.Mod,ast.FloorDiv,ast.USub,ast.UAdd,ast.Call,ast.Name)
        math_names=set(dir(math))
        for node in ast.walk(tree):
            if not isinstance(node,allowed): return f"Blocked: {type(node).__name__}"
            if isinstance(node,ast.Name) and node.id not in math_names: return f"Blocked: '{node.id}'"
        safe_g={k:getattr(math,k) for k in dir(math) if not k.startswith("_")}
        return f"Result: {eval(compile(tree,'<calc>','eval'),{'__builtins__':{}},safe_g)}"
    except SyntaxError: return "Invalid syntax"
    except Exception as e: return f"Error: {e}"

def tool_web_search(query,max_results=5):
    try:
        url=f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
        r=req.get(url,headers={"User-Agent":"Agent/1.0"},timeout=8);data=r.json()
        results=[]
        if data.get("AbstractText"): results.append("Summary: "+data["AbstractText"])
        if data.get("Answer"): results.append("Answer: "+data["Answer"])
        for t in data.get("RelatedTopics",[])[:max_results]:
            if isinstance(t,dict) and t.get("Text"): results.append("- "+t["Text"])
        combined="\n".join(results) or "No results."
        if detect_injection(combined): return "[Blocked: suspicious content]"
        return combined
    except Exception as e: return f"Search error: {e}"

def tool_wikipedia(query):
    try:
        url=f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query.replace(' ','_'))}"
        r=req.get(url,headers={"User-Agent":"Agent/1.0"},timeout=8);d=r.json()
        parts=[]
        if d.get("title"): parts.append("Title: "+d["title"])
        if d.get("description"): parts.append("Desc: "+d["description"])
        if d.get("extract"): parts.append(d["extract"][:2000])
        result="\n".join(parts)
        if detect_injection(result): return "[Blocked]"
        return result
    except Exception as e: return f"Wikipedia error: {e}"

def tool_datetime(): 
    n=datetime.datetime.now();u=datetime.datetime.utcnow()
    return f"Local: {n.strftime('%Y-%m-%d %H:%M:%S')}\nUTC: {u.strftime('%Y-%m-%d %H:%M:%S')}\nDay: {n.strftime('%A')}"def make_dispatch(mem):
    return {"web_search":lambda i:tool_web_search(i["query"],i.get("max_results",5)),"wikipedia":lambda i:tool_wikipedia(i["query"]),"calculator":lambda i:tool_calculator(i["expression"]),"get_datetime":lambda i:tool_datetime(),"memory_add":lambda i:mem.add(i["content"],i.get("tag","general")),"memory_search":lambda i:mem.search(i["query"]),"memory_list":lambda i:mem.list_all(),"memory_delete":lambda i:mem.delete(i["memory_id"])}

TOOLS_SCHEMA=[
    {"name":"web_search","description":"Search the web.","input_schema":{"type":"object","properties":{"query":{"type":"string"},"max_results":{"type":"integer"}},"required":["query"]}},
    {"name":"wikipedia","description":"Look up Wikipedia.","input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"calculator","description":"Math expressions.","input_schema":{"type":"object","properties":{"expression":{"type":"string"}},"required":["expression"]}},
    {"name":"get_datetime","description":"Current date/time.","input_schema":{"type":"object","properties":{}}},
    {"name":"memory_add","description":"Save to memory.","input_schema":{"type":"object","properties":{"content":{"type":"string"},"tag":{"type":"string"}},"required":["content"]}},
    {"name":"memory_search","description":"Search memory.","input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"memory_list","description":"List all memories.","input_schema":{"type":"object","properties":{}}},
    {"name":"memory_delete","description":"Delete a memory.","input_schema":{"type":"object","properties":{"memory_id":{"type":"string"}},"required":["memory_id"]}},
]

def build_system(mem):
    base="You are a powerful AI Agent. Tools: web_search, wikipedia, calculator, get_datetime, memory_add, memory_search, memory_list, memory_delete. Be helpful and concise."
    ctx=mem.to_context()
    return base+(f"\n\n{ctx}" if ctx else "")

class GroqProvider:
    NAME="Groq";MODEL="llama-3.3-70b-versatile"
    def __init__(self,k): self.k=k
    def _tools(self): return [{"type":"function","function":{"name":t["name"],"description":t["description"],"parameters":t["input_schema"]}} for t in TOOLS_SCHEMA]
    def _msgs(self,h,mem):
        msgs=[{"role":"system","content":build_system(mem)}]
        for m in h:
            r,c=m["role"],m["content"]
            if isinstance(c,str): msgs.append({"role":r,"content":c})
            elif isinstance(c,list):
                for b in c:
                    if b.get("type")=="text" and b.get("text"): msgs.append({"role":r,"content":b["text"]})
                    elif b.get("type")=="tool_use": msgs.append({"role":"assistant","content":None,"tool_calls":[{"id":b["id"],"type":"function","function":{"name":b["name"],"arguments":json.dumps(b.get("input",{}))}}]})
                    elif b.get("type")=="tool_result": msgs.append({"role":"tool","tool_call_id":b["tool_use_id"],"content":str(b.get("content",""))})
        return msgs
    def call(self,h,mem):
        r=req.post("https://api.groq.com/openai/v1/chat/completions",headers={"Authorization":f"Bearer {self.k}","Content-Type":"application/json"},json={"model":self.MODEL,"messages":self._msgs(h,mem),"tools":self._tools(),"max_tokens":4096},timeout=60)
        if r.status_code!=200: raise Exception(f"Groq {r.status_code}: {r.text[:200]}")
        d=r.json();msg=d["choices"][0]["message"];fin=d["choices"][0]["finish_reason"]
        texts=[msg["content"]] if msg.get("content") else []
        tools=[{"id":tc["id"],"name":tc["function"]["name"],"input":json.loads(tc["function"]["arguments"] or "{}")} for tc in (msg.get("tool_calls") or [])]
        return texts,tools,fin=="stop" and not tools

class GeminiProvider:
    NAME="Gemini";MODEL="gemini-2.0-flash"
    def __init__(self,k): self.k=k
    def _tools(self): return [{"function_declarations":[{"name":t["name"],"description":t["description"],"parameters":t["input_schema"]} for t in TOOLS_SCHEMA]}]
    def _msgs(self,h):
        out=[]
        for m in h:
            role="user" if m["role"]=="user" else "model";c=m["content"]
            if isinstance(c,str): out.append({"role":role,"parts":[{"text":c}]})
            elif isinstance(c,list):
                parts=[]
                for b in c:
                    if b.get("type")=="text" and b.get("text"): parts.append({"text":b["text"]})
                    elif b.get("type")=="tool_use": parts.append({"functionCall":{"name":b["name"],"args":b.get("input",{})}})
                    elif b.get("type")=="tool_result": parts.append({"functionResponse":{"name":b.get("tool_use_id","tool"),"response":{"content":b.get("content","")}}})
                if parts: out.append({"role":role,"parts":parts})
        return out
    def call(self,h,mem):
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{self.MODEL}:generateContent?key={self.k}"
        r=req.post(url,json={"system_instruction":{"parts":[{"text":build_system(mem)}]},"contents":self._msgs(h),"tools":self._tools()},timeout=60)
        if r.status_code!=200: raise Exception(f"Gemini {r.status_code}: {r.text[:200]}")
        d=r.json();parts=d["candidates"][0]["content"].get("parts",[]);fin=d["candidates"][0].get("finishReason","")
        texts=[];tools=[]
        for p in parts:
            if "text" in p: texts.append(p["text"])
            if "functionCall" in p: fc=p["functionCall"];tools.append({"id":fc["name"]+"_id","name":fc["name"],"input":fc.get("args",{})})
        return texts,tools,fin in("STOP","MAX_TOKENS") and not tools

class OpenRouterProvider:
    NAME="OpenRouter";MODEL="meta-llama/llama-3.3-70b-instruct:free"
    def __init__(self,k): self.k=k
    def _tools(self): return [{"type":"function","function":{"name":t["name"],"description":t["description"],"parameters":t["input_schema"]}} for t in TOOLS_SCHEMA]
    def _msgs(self,h,mem):
        msgs=[{"role":"system","content":build_system(mem)}]
        for m in h:
            r,c=m["role"],m["content"]
            if isinstance(c,str): msgs.append({"role":r,"content":c})
            elif isinstance(c,list):
                for b in c:
                    if b.get("type")=="text" and b.get("text"): msgs.append({"role":r,"content":b["text"]})
                    elif b.get("type")=="tool_use": msgs.append({"role":"assistant","content":None,"tool_calls":[{"id":b["id"],"type":"function","function":{"name":b["name"],"arguments":json.dumps(b.get("input",{}))}}]})
                    elif b.get("type")=="tool_result": msgs.append({"role":"tool","tool_call_id":b["tool_use_id"],"content":str(b.get("content",""))})
        return msgs
    def call(self,h,mem):
        r=req.post("https://openrouter.ai/api/v1/chat/completions",headers={"Authorization":f"Bearer {self.k}","Content-Type":"application/json","HTTP-Referer":"https://ai-agent","X-Title":"AIAgent"},json={"model":self.MODEL,"messages":self._msgs(h,mem),"tools":self._tools(),"max_tokens":4096},timeout=60)
        if r.status_code!=200: raise Exception(f"OpenRouter {r.status_code}: {r.text[:200]}")
        d=r.json();msg=d["choices"][0]["message"];fin=d["choices"][0]["finish_reason"]
        texts=[msg["content"]] if msg.get("content") else []
        tools=[{"id":tc["id"],"name":tc["function"]["name"],"input":json.loads(tc["function"]["arguments"] or "{}")} for tc in (msg.get("tool_calls") or [])]
        return texts,tools,fin=="stop" and not tools

class Agent:
    def __init__(self,provider,mem): self.provider=provider;self.mem=mem;self.history=[];self.dispatch=make_dispatch(mem)
    def _trim(self):
        if len(self.history)>MAX_HISTORY: self.history=self.history[-MAX_HISTORY:]
    def chat(self,msg):
        msg=sanitize(msg)
        if not msg: return "Empty."
        self.history.append({"role":"user","content":msg});self._trim()
        for _ in range(10):
            try: texts,tool_calls,done=self.provider.call(self.history,self.mem)
            except Exception as e: return f"API error: {e}"
            blocks=[]
            for t in texts:
                if t: blocks.append({"type":"text","text":t})
            for tc in tool_calls: blocks.append({"type":"tool_use","id":tc["id"],"name":tc["name"],"input":tc["input"]})
            self.history.append({"role":"assistant","content":blocks});self._trim()
            if done or not tool_calls: return "\n".join(t for t in texts if t).strip() or "(no response)"
            results=[]
            for tc in tool_calls:
                handler=self.dispatch.get(tc["name"])
                try: result=handler(tc["input"]) if handler else f"Unknown: {tc['name']}"
                except Exception as e: result=f"Tool error: {e}"
                results.append({"type":"tool_result","tool_use_id":tc["id"],"content":result})
            self.history.append({"role":"user","content":results});self._trim()
        return "Max iterations reached."
    def reset(self): self.history=[]
@app.route("/")
def index(): return HTML_PAGE

@app.route("/api/setup",methods=["POST"])
def setup():
    data=request.json or {};provider=data.get("provider","").strip();key=data.get("key","").strip()
    env_keys={"groq":os.environ.get("GROQ_KEY",""),"gemini":os.environ.get("GEMINI_KEY",""),"openrouter":os.environ.get("OPENROUTER_KEY","")}
    if not key and env_keys.get(provider): key=env_keys[provider]
    if not key: return jsonify({"error":"API key required"}),400
    if len(key)<20 or len(key)>200: return jsonify({"error":"Invalid key format"}),400
    providers={"groq":GroqProvider,"gemini":GeminiProvider,"openrouter":OpenRouterProvider}
    cls=providers.get(provider)
    if not cls: return jsonify({"error":"Unknown provider"}),400
    try:
        state=get_session_state();p=cls(key);mem=SessionMemory(state["memory"])
        state["agent"]=Agent(p,mem);state["provider_name"]=p.NAME
        return jsonify({"ok":True,"provider":p.NAME,"model":p.MODEL})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/chat",methods=["POST"])
def chat():
    state=get_session_state()
    if not state["agent"]: return jsonify({"error":"Not configured"}),400
    if not rate_check(state): return jsonify({"error":"Rate limit: 50 msg/hour"}),429
    data=request.json or {};msg=data.get("message","").strip()
    if not msg: return jsonify({"error":"Empty"}),400
    try: return jsonify({"response":state["agent"].chat(msg)})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/reset",methods=["POST"])
def reset():
    state=get_session_state()
    if state["agent"]: state["agent"].reset()
    return jsonify({"ok":True})

@app.route("/api/memory",methods=["GET"])
def get_memory():
    state=get_session_state()
    if not state["agent"]: return jsonify({"memory":"Not connected."})
    return jsonify({"memory":state["agent"].mem.list_all()})

@app.route("/api/memory/clear",methods=["POST"])
def clear_memory():
    state=get_session_state()
    if state["agent"]: state["agent"].mem.clear()
    return jsonify({"ok":True})

@app.route("/api/status",methods=["GET"])
def status():
    state=get_session_state()
    return jsonify({"provider":state["provider_name"] or "None","configured":state["agent"] is not None,"msg_count":state["msg_count"]})

@app.route("/api/env_providers",methods=["GET"])
def env_providers():
    available=[]
    if os.environ.get("GROQ_KEY"): available.append("groq")
    if os.environ.get("GEMINI_KEY"): available.append("gemini")
    if os.environ.get("OPENROUTER_KEY"): available.append("openrouter")
    return jsonify({"available":available})

HTML_PAGE="""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
<meta name="theme-color" content="#0a0a0f"/>
<title>AI Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0a0f;--surface:#12121a;--surface2:#1a1a26;--border:#2a2a3d;--accent:#7c6af7;--accent2:#a78bfa;--green:#4ade80;--red:#f87171;--text:#e2e2f0;--muted:#6b6b8a;--user-bg:#1e1b4b}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;overflow:hidden}
#setup{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;z-index:100;overflow-y:auto}
#setup.hidden{display:none}
.card{width:100%;max-width:380px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:28px;display:flex;flex-direction:column;gap:18px}
.title{font-size:22px;font-weight:600;color:var(--accent2);text-align:center}
.sub{font-size:13px;color:var(--muted);text-align:center}
.label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.pgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.pb{background:var(--surface2);border:2px solid transparent;border-radius:12px;padding:12px 6px;cursor:pointer;font-size:12px;font-weight:500;color:var(--muted);text-align:center;transition:all .2s}
.pb.active{border-color:var(--accent);color:var(--accent2);background:#1e1b4b}
.pi{font-size:20px;display:block;margin-bottom:4px}
input[type=password]{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:14px 16px;color:var(--text);font-family:monospace;font-size:13px;outline:none}
input:focus{border-color:var(--accent)}
.btn{background:var(--accent);border:none;border-radius:12px;padding:15px;color:#fff;font-size:15px;font-weight:600;cursor:pointer;width:100%}
.btn:disabled{opacity:.5}
.note{background:var(--surface2);border-radius:10px;padding:12px;font-size:12px;color:var(--muted);line-height:1.6}
a{color:var(--accent2);font-size:12px}
#main{height:100%;display:flex;flex-direction:column}
#main.hidden{display:none}
.hdr{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.hdl{display:flex;align-items:center;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green)}
.aname{font-size:16px;font-weight:600}
.ptag{font-size:11px;color:var(--muted);font-family:monospace}
.hbtns{display:flex;gap:8px}
.ib{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:8px 12px;cursor:pointer;font-size:14px;color:var(--muted)}
#messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:88%;display:flex;flex-direction:column;gap:4px}
.msg.user{align-self:flex-end;align-items:flex-end}
.msg.agent{align-self:flex-start}
.bubble{padding:12px 15px;border-radius:18px;font-size:14px;line-height:1.6;word-break:break-word;white-space:pre-wrap}
.msg.user .bubble{background:var(--user-bg);border-bottom-right-radius:4px;color:#c4b5fd}
.msg.agent .bubble{background:var(--surface);border:1px solid var(--border);border-bottom-left-radius:4px}
.mt{font-size:10px;color:var(--muted);font-family:monospace;padding:0 4px}
.thinking{align-self:flex-start;display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:18px;border-bottom-left-radius:4px}
.dots{display:flex;gap:4px}
.d{width:6px;height:6px;border-radius:50%;background:var(--accent);animation:bounce 1.2s infinite}
.d:nth-child(2){animation-delay:.2s}.d:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
.welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;gap:12px;opacity:.5;padding:20px;text-align:center}
.ia{background:var(--surface);border-top:1px solid var(--border);padding:12px 16px;padding-bottom:max(12px,env(safe-area-inset-bottom));flex-shrink:0}
.ir{display:flex;gap:10px;align-items:flex-end}
#input{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:12px 16px;color:var(--text);font-size:14px;outline:none;resize:none;max-height:120px;min-height:44px;line-height:1.5}
#input:focus{border-color:var(--accent)}
#send{background:var(--accent);border:none;border-radius:50%;width:44px;height:44px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}
#send:disabled{opacity:.4}
#send svg{width:20px;height:20px;fill:#fff}
#mp{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;display:flex;flex-direction:column;justify-content:flex-end}
#mp.hidden{display:none}
.ms{background:var(--surface);border-radius:20px 20px 0 0;padding:20px;max-height:70vh;display:flex;flex-direction:column;gap:12px}
.mh{display:flex;justify-content:space-between;align-items:center}
.mc{overflow-y:auto;font-family:monospace;font-size:12px;color:var(--muted);line-height:1.8;flex:1}
.mcl{background:var(--red);border:none;border-radius:10px;padding:12px;color:#fff;font-weight:600;cursor:pointer;width:100%}
.toast{position:fixed;bottom:100px;left:50%;transform:translateX(-50%);background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:10px 18px;font-size:13px;color:var(--text);z-index:999;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}
</style>
</head>
<body>
<div id="setup">
<div class="card">
<div class="title">⚡ AI Agent</div>
<div class="sub">Secure • Private • No data stored</div>
<div><div class="label">Provider</div>
<div class="pgrid">
<button class="pb active" data-p="groq" onclick="selP(this)"><span class="pi">⚡</span>Groq</button>
<button class="pb" data-p="gemini" onclick="selP(this)"><span class="pi">✦</span>Gemini</button>
<button class="pb" data-p="openrouter" onclick="selP(this)"><span class="pi">◈</span>OpenRouter</button>
</div></div>
<div><div class="label">API Key</div><input type="password" id="ak" placeholder="Paste API key..."/></div>
<a id="kl" href="https://console.groq.com" target="_blank">🔗 Get Groq API Key (Free)</a>
<div class="note">🔒 Key sirf requests ke liye use hoti hai — server pe save nahi hoti.</div>
<button class="btn" id="cb" onclick="conn()">Connect</button>
</div></div>
<div id="main" class="hidden">
<div class="hdr">
<div class="hdl"><div class="dot"></div>
<div><div class="aname">AI Agent</div><div class="ptag" id="pl">—</div></div></div>
<div class="hbtns">
<button class="ib" onclick="showMem()">🧠</button>
<button class="ib" onclick="resetChat()">↺</button>
<button class="ib" onclick="sw()">⚙</button>
</div></div>
<div id="messages">
<div class="welcome" id="wlc">
<div style="font-size:48px">🤖</div>
<div style="font-size:16px;font-weight:600">Agent Ready</div>
<div style="font-size:13px;color:var(--muted)">Web search, math, memory aur bohat kuch.</div>
</div></div>
<div class="ia"><div class="ir">
<textarea id="input" placeholder="Message..." rows="1" onkeydown="hk(event)" oninput="ar(this)"></textarea>
<button id="send" onclick="send()"><svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg></button>
</div></div></div>
<div id="mp" class="hidden" onclick="hm(event)">
<div class="ms">
<div class="mh"><div style="font-size:16px;font-weight:600">🧠 Memory</div>
<button style="background:var(--surface2);border:none;border-radius:8px;padding:6px 12px;color:var(--text);cursor:pointer" onclick="hm()">Close</button></div>
<div class="mc" id="mc">Loading...</div>
<button class="mcl" onclick="clrMem()">Clear All</button>
</div></div>
<div class="toast" id="toast"></div>
<script>
let sp='groq';
const pl={groq:'https://console.groq.com',gemini:'https://aistudio.google.com',openrouter:'https://openrouter.ai'};
const ll={groq:'Get Groq API Key (Free)',gemini:'Get Gemini API Key (Free)',openrouter:'Get OpenRouter API Key (Free)'};
function selP(el){document.querySelectorAll('.pb').forEach(b=>b.classList.remove('active'));el.classList.add('active');sp=el.dataset.p;document.getElementById('kl').href=pl[sp];document.getElementById('kl').textContent='🔗 '+ll[sp]}
async function conn(){
  const key=document.getElementById('ak').value.trim();
  if(!key){toast('Enter API key');return}
  const btn=document.getElementById('cb');btn.disabled=true;btn.textContent='Connecting...';
  try{const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:sp,key})});
  const d=await r.json();
  if(d.ok){document.getElementById('setup').classList.add('hidden');document.getElementById('main').classList.remove('hidden');document.getElementById('pl').textContent=d.provider+' · '+d.model;toast('Connected')}
  else toast(d.error||'Failed')}catch(e){toast('Error')}
  btn.disabled=false;btn.textContent='Connect'}
function sw(){document.getElementById('setup').classList.remove('hidden');document.getElementById('main').classList.add('hidden');document.getElementById('ak').value=''}
function ar(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}
function hk(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}
function now(){return new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}
function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function addMsg(role,text){
  const w=document.getElementById('wlc');if(w)w.remove();
  const msgs=document.getElementById('messages');
  const div=document.createElement('div');div.className='msg '+role;
  div.innerHTML=`<div class="bubble">${esc(text)}</div><div class="mt">${now()}</div>`;
  msgs.appendChild(div);msgs.scrollTop=msgs.scrollHeight}
function showT(){
  const msgs=document.getElementById('messages');
  const div=document.createElement('div');div.id='th';div.className='thinking';
  div.innerHTML='<div class="dots"><div class="d"></div><div class="d"></div><div class="d"></div></div><span style="font-size:13px;color:var(--muted)">Thinking...</span>';
  msgs.appendChild(div);msgs.scrollTop=msgs.scrollHeight}
function hideT(){const t=document.getElementById('th');if(t)t.remove()}
async function send(){
  const inp=document.getElementById('input');const msg=inp.value.trim();if(!msg)return;
  inp.value='';inp.style.height='auto';document.getElementById('send').disabled=true;
  addMsg('user',msg);showT();
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  const d=await r.json();hideT();
  addMsg('agent',d.response||'Error: '+(d.error||'Unknown'))}
  catch(e){hideT();addMsg('agent','Connection error.')}
  document.getElementById('send').disabled=false}
async function resetChat(){await fetch('/api/reset',{method:'POST'});document.getElementById('messages').innerHTML='';toast('Cleared')}
async function showMem(){document.getElementById('mp').classList.remove('hidden');const r=await fetch('/api/memory');const d=await r.json();document.getElementById('mc').textContent=d.memory||'No memories.'}
function hm(e){if(!e||e.target===document.getElementById('mp'))document.getElementById('mp').classList.add('hidden')}
async function clrMem(){if(!confirm('Delete all?'))return;await fetch('/api/memory/clear',{method:'POST'});document.getElementById('mc').textContent='No memories.';toast('Cleared')}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}
</script>
</body>
</html>"""

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    print(f"\nAI Agent running on port {port}")
    app.run(host="0.0.0.0",port=port,debug=False)
