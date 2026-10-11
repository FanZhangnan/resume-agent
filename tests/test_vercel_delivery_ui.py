"""Offline interaction checks for delivery controls and untrusted summary content."""

from _bootstrap import PROJECT_ROOT

import json
import re
import shutil
import subprocess


PAGE = PROJECT_ROOT / "webui" / "static" / "vercel_app.html"


def test_delivery_interactions_without_browser_or_credentials():
    if not shutil.which("node"):
        print("SKIP delivery interactions (node not installed)")
        return
    html = PAGE.read_text(encoding="utf-8")
    script = re.search(r'<script nonce="__CSP_NONCE__">(.*?)</script>', html, re.S).group(1)
    # Expose existing application functions in the test VM instead of booting HTTP requests.
    script = script.replace("boot();\n})();", """
    globalThis.ui = {
      runState, PHASES, renderRunState, renderDeliverySummary, renderReasoningOptions,
      startRun, retryRun, downloadResumeWord, openRun, applyStatus, syncMockControl,
      setConfig(config, model, reasoning){ publicConfig = config; selectedModel = model; selectedReasoning = reasoning; },
      getReasoning(){ return selectedReasoning; }
    };
    })();
    """)
    program = r'''
const assert = require("node:assert/strict");
const vm = require("node:vm");
class Element {
  constructor(tag = "div"){
    this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {};
    this._text = ""; this.value = ""; this.checked = false; this.disabled = false;
    this.firstChild = {nodeValue:""};
    this.classes = new Set();
    this.classList = {
      toggle:(name,on) => { const next = on === undefined ? !this.classes.has(name) : on;
        if(next) this.classes.add(name); else this.classes.delete(name); return next; },
      add:name => this.classes.add(name), remove:name => this.classes.delete(name),
      contains:name => this.classes.has(name)
    };
  }
  set textContent(value){ this._text = String(value); this.children = []; }
  get textContent(){ return this._text + this.children.map(child => child.textContent).join(""); }
  append(...children){ this.children.push(...children); }
  appendChild(child){ this.children.push(child); }
  addEventListener(name,handler){ this.listeners[name] = handler; }
  setAttribute(name,value){ this[name] = value; }
  removeAttribute(name){ delete this[name]; }
  focus(){ this.focused = true; }
  click(){ if(this.tagName === "a") downloads.push(this); else if(this.listeners.click) return this.listeners.click(); }
}
const elements = new Map();
const get = id => { if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
const document = {
  hidden:false, getElementById:get, createElement:tag => new Element(tag),
  querySelector:selector => get(selector), querySelectorAll:() => [], addEventListener:() => {}
};
class FormData {
  constructor(){ this.entries = new Map(); }
  append(key,value){ this.entries.set(key,value); }
  get(key){ return this.entries.get(key); }
  has(key){ return this.entries.has(key); }
}
const requests = [], downloads = [];
let nextStatus = {};
let wordResponse = {ok:true, blob:async () => ({})};
const fetch = async (url,options = {}) => {
  requests.push({url,...options});
  if(url.endsWith("/resume.docx")) return wordResponse;
  let data = url === "/api/runs" && options.method === "POST"
    ? {run_id:"fresh", model:"auto", reasoning:"", mock:get("mockMode").checked, byok:Boolean(options.body.get("api_key"))}
    : url === "/api/runs" ? {runs:[]} : nextStatus;
  return {ok:true,json:async () => data};
};
const context = {document,fetch,FormData,URL:{createObjectURL:() => "blob:test",revokeObjectURL:() => {}},
  setTimeout:() => 1,clearTimeout:() => {},console,Date,Blob:class {},};
vm.createContext(context);
vm.runInContext(SCRIPT,context);
const {ui} = context;
const config = {service:{available:true,provider_name:"示例服务"}, model_options:[
  {id:"auto",reasoning_levels:[""],default_reasoning:""},
  {id:"deep",reasoning_levels:["high","xhigh"],default_reasoning:"high"}
]};
const ready = () => Object.assign(ui.runState, {phase:"terminal",runId:"old",status:"completed",
  safeToDeliver:true,unresolvedFixes:[],resumeStruct:{},report:"匹配度：71/100",deliverySummary:null,
  startFailed:false,mock:false,byok:false});
const settle = async () => { for(let i=0;i<12;i++) await Promise.resolve(); };
(async () => {
  ui.setConfig(config,"auto","high");
  ui.renderReasoningOptions("");
  assert.equal(ui.getReasoning(),"");
  assert(get("reasoning-control").classList.contains("hidden"));
  assert.equal(get("effort-seg").children.length,0);
  ui.setConfig(config,"deep",""); ui.renderReasoningOptions("xhigh");
  assert.equal(ui.getReasoning(),"xhigh");
  assert(!get("reasoning-control").classList.contains("hidden"));
  assert.equal(get("effort-seg").children[0].textContent,"深入");
  ui.setConfig(config,"auto",""); ui.renderReasoningOptions("");
  ready();
  ui.renderRunState();
  assert(!get("downloadWordBtn").disabled); // Old runs do not need the additive summary.
  assert(!get("summaryFallback").classList.contains("hidden"));
  assert.equal(get("scoreValue").textContent,"71");
  const hostile = '<img src=x onerror="alert(1)">';
  ui.runState.deliverySummary = {match_score:83,strengths:[hostile],gaps:[hostile],priority_actions:[hostile],
    verified:true,changes:[{section:hostile,before:hostile,after:hostile,reason:hostile}]};
  ui.renderRunState();
  assert.equal(get("scoreValue").textContent,"83");
  assert.equal(get("summaryStrengths").children[0].textContent,hostile);
  assert(get("summaryChanges").textContent.includes(hostile));
  function assertPlainTree(node){
    assert(!["img","script"].includes(node.tagName));
    assert.equal(node.innerHTML,undefined);
    node.children.forEach(assertPlainTree);
  }
  assertPlainTree(get("summaryChanges")); assertPlainTree(get("summaryStrengths"));
  for(const changes of [{status:"partial"},{status:"failed"},{status:"cancelled"},
      {status:"deadline_exceeded"},{status:"running"},{status:"pending"},{safeToDeliver:false},{unresolvedFixes:["待核对"]},{resumeStruct:null}]){
    ready(); Object.assign(ui.runState,changes); ui.renderRunState();
    assert(get("downloadWordBtn").disabled);
    assert(get("downloadLayoutWordBtn").disabled);
    const before = requests.length; await ui.downloadResumeWord(); assert.equal(requests.length,before);
  }
  ready(); await ui.downloadResumeWord();
  assert.equal(requests.at(-1).url,"/api/runs/old/resume.docx");
  assert.equal(downloads.at(-1).download,"优化简历.docx");
  const successfulDownloads = downloads.length;
  wordResponse = {ok:false}; await ui.downloadResumeWord();
  assert.equal(downloads.length,successfulDownloads);
  assert(get("errorBanner").textContent.includes("Word 暂时无法下载"));
  wordResponse = {ok:true,blob:async () => { ui.runState.runId = "other"; return {}; }};
  ready(); await ui.downloadResumeWord(); assert.equal(downloads.length,successfulDownloads);
  ready(); ui.setConfig({...config,service:{available:false,message:"维护中 https://provider.example.invalid/v1 sk-synthetic-key"}},"auto","");
  get("mockMode").checked = false; ui.renderRunState();
  assert(get("startBtn").disabled); assert(!get("demoBtn").disabled);
  assert(get("serviceBanner").textContent.includes("维护中"));
  assert(!get("serviceBanner").textContent.includes("provider.example.invalid"));
  assert(!get("serviceBanner").textContent.includes("sk-synthetic-key"));
  get("mockMode").checked = true; ui.syncMockControl();
  assert(!get("startBtn").disabled); assert(get("mockBanner").textContent.includes("示例数据"));
  ready(); ui.runState.mock = true; ui.renderRunState();
  assert(get("summaryVerified").textContent.includes("示例数据"));
  ui.setConfig({...config,service:{available:true,site_funded:false,message:"请使用自有密钥"}},"auto","");
  get("mockMode").checked = false; ui.renderRunState();
  assert(!get("startBtn").disabled); // BYOK works without a funded site key.
  ui.setConfig({model_options:config.model_options},"auto",""); ui.renderRunState();
  assert(!get("startBtn").disabled); // Older config without service still works.
  ready(); ui.runState.byok = true; get("byok-key").value = "";
  const beforeRetry = requests.length;
  ui.retryRun(); assert.equal(requests.length,beforeRetry);
  assert(get("byok-key").focused); assert(get("errorBanner").textContent.includes("重新填写"));
  get("resume-text").value = "当前编辑后的简历"; get("jdText").value = "当前 JD";
  get("byok-key").value = "fresh-test-key";
  nextStatus = {run_id:"fresh",status:"completed",safe_to_deliver:true,unresolved_fixes:[],optimized_resume_struct:{},mock:false,byok:true};
  ui.retryRun(); await settle();
  const submission = requests.find(request => request.method === "POST");
  assert.equal(submission.body.get("resume_text"),"当前编辑后的简历");
  assert.equal(submission.body.get("jd_text"),"当前 JD");
  assert.equal(submission.body.get("api_key"),"fresh-test-key");
  assert(!submission.body.has("reasoning"));
  assert.equal(get("byok-key").value,"");
  ui.runState.deliverySummary = {strengths:["旧摘要"]};
  nextStatus = {run_id:"historical",status:"completed",safe_to_deliver:true,unresolved_fixes:[],optimized_resume_struct:{},report:"旧报告",mock:null};
  await ui.openRun("historical",{status:"completed",mock:true});
  assert.equal(ui.runState.deliverySummary,null);
  assert.equal(ui.runState.mock,null);
  assert(get("run-meta").textContent.includes("模式未知"));
  assert(!get("run-meta").textContent.includes("真实分析"));
  assert(get("summaryVerified").textContent.includes("模式未知"));
  assert(!get("summaryFallback").classList.contains("hidden"));
  assert(!get("downloadWordBtn").disabled);
  assert.equal(get("resume-text").value,"当前编辑后的简历");
  assert.equal(get("byok-key").value,"");
  await ui.openRun("historical",{status:"completed"});
  assert.equal(ui.runState.mock,null);
  assert(get("run-meta").textContent.includes("模式未知"));
  ui.applyStatus({run_id:"active-mock",status:"running",mock:true,byok:false});
  assert.equal(ui.runState.mock,true);
  assert(get("run-meta").textContent.includes("离线演示（Mock）"));
  assert(get("summaryVerified").textContent.includes("示例数据"));
  ui.applyStatus({status:"running",mock:false});
  assert(get("run-meta").textContent.includes("真实分析"));
  ui.applyStatus({status:"completed",mock:null});
  assert.equal(ui.runState.mock,null);
  assert(get("run-meta").textContent.includes("模式未知"));
  ui.runState.reasoning = "high"; ui.applyStatus({reasoning:"",status:"completed"});
  assert.equal(ui.runState.reasoning,"");
  console.log("PASS offline UI interactions: summaries, injection safety, legacy runs, maintenance/Mock/BYOK, empty reasoning, gated Word, retry and stale downloads");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("SCRIPT", json.dumps(script))
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout.strip())


if __name__ == "__main__":
    test_delivery_interactions_without_browser_or_credentials()
