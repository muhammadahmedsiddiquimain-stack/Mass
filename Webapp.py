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
