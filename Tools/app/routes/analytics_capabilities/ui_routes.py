from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.routes.analytics_capabilities.common import DEFAULT_SCHEMA


router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "capabilities": [
            "timeline.identity",
            "timeline.facts",
            "timeline.conversation",
            "caretaker_performance_rating",
            "caretaker_performance_dashboard", 
            "customer_brief_booking_only",
            "active_booking_recent_activity",
            "review_lead_communication",
            "review_number_communication_phone_only",
            "staff_activity",
            "staff_caretaker_activity_legacy_alias",
            "staff_profile",
            "whatsapp_conversation_inspector",
        ],
    }


def _field(name: str, typ: str = "text", default: Any = "", *, required: bool = False, help_text: str = "", options: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "type": typ, "default": default, "required": required, "help": help_text, "options": options or []}


CAPABILITY_CATALOG: dict[str, dict[str, Any]] = {
    "customer_brief": {
        "label": "Customer Brief",
        "method": "GET",
        "path": "/analytics/capabilities/customer-brief",
        "description": "Build a booking-scoped customer brief with user-friendly evidence, LLM copy mode, unrestricted full-conversation mode, or raw debug mode. Only booking_id is supported.",
        "fields": [
            _field("booking_id", "number", "", required=True, help_text="Only supported seed. Lead/user/person/email/phone lookup is intentionally removed."),
            _field("days", "number", 30, help_text="Conversation lookback days."),
            _field("llm", "checkbox", True, help_text="Adds copy-ready LLM prompt. No heuristic ratings are generated."),
            _field("display_mode", "select", "evidence", help_text="evidence is user-friendly; llm is prompt/context only; unrestricted returns full email/WhatsApp/call text plus call transcript; raw is full debug payload.", options=["evidence", "llm", "raw", "unrestricted"]),
        ],
        "hidden_defaults": {
            "output_format": "llm",
            "print_limit": 200,
            "max_text": 220,
            "max_llm_messages": 12,
            "max_llm_text_chars": 220,
            "verbose": False,
        },
        "example": {"booking_id": 56409, "days": 30, "llm": True, "display_mode": "evidence"},
    },
    "booking_review": {
        "label": "Booking Handling Review",
        "method": "GET",
        "path": "/analytics/capabilities/bookings/review",
        "description": "Cache-first booking handling review for one booking. Uses review_booking_communication and booking_communication_review cache; does not call LLM unless run_llm/force_refresh is true.",
        "fields": [
            _field("booking_id", "number", "", required=True, help_text="Single booking ID to review."),
            _field("customer_days", "number", 30, help_text="Customer communication lookback window."),
            _field("llm", "checkbox", True, help_text="Use booking handling review view. Cache-first by default."),
            _field("run_llm", "checkbox", False, help_text="Default false = read cache. Set true only to recompute/call LLM."),
            _field("display_mode", "select", "evidence", help_text="evidence is UI-friendly; llm includes prompt/context; raw uses customer-brief fallback only when llm=false.", options=["evidence", "llm", "raw"]),
            _field("use_cache", "checkbox", True, help_text="Return status='ok' cache by booking_id without rebuilding context."),
            _field("force_refresh", "checkbox", False, help_text="Explicitly recompute even when cache is ok."),
            _field("include_prompt", "checkbox", True, help_text="Include copy-ready prompt when available."),
            _field("model", "text", "gpt-5-mini"),
        ],
        "hidden_defaults": {
            "max_llm_messages": 12,
            "max_llm_text_chars": 220,
            "timeout_seconds": 45,
            "max_llm_seconds": 60,
        },
        "example": {"booking_id": 57756, "customer_days": 30, "llm": True, "run_llm": False, "display_mode": "evidence", "use_cache": True, "force_refresh": False, "include_prompt": True, "model": "gpt-5-mini"},
    },
    "review_lead_communication": {
        "label": "Review Lead Communication",
        "method": "GET",
        "path": "/analytics/capabilities/communication/review-lead",
        "description": "Review WhatsApp, email, calls, site visits, and travel cart booking attempts for one lead.",
        "fields": [
            _field("lead_id", "number", "", required=True),
            _field("days", "number", 90, help_text="How many days of communication to review."),
            _field("llm", "checkbox", True, help_text="Adds copy-ready LLM prompt. No heuristic ratings are generated."),
            _field("display_mode", "select", "evidence", help_text="evidence is neutral UI data; llm is prompt-only; raw is full rows.", options=["evidence", "llm", "raw", "unrestricted"]),
        ],
        "hidden_defaults": {"limit": 10000, "print_limit": 200, "max_text": 180, "include_prompt": True},
        "example": {"lead_id": 401676, "days": 90, "llm": True, "display_mode": "evidence"},
    },
    "review_number_communication": {
        "label": "Review Phone Communication",
        "method": "GET",
        "path": "/analytics/capabilities/communication/review-number",
        "description": "Review calls and WhatsApp for one phone only. No booking/lead/user/person/email expansion is performed.",
        "fields": [
            _field("phone", "text", "", required=True, help_text="Only supported seed. Accepts 10 digits or 12 digits; matches by last 10 digits."),
            _field("days", "number", 30),
            _field("from_date", "date", ""),
            _field("to_date", "date", ""),
            _field("llm", "checkbox", True, help_text="Adds copy-ready LLM prompt. No heuristic ratings are generated."),
            _field("display_mode", "select", "evidence", help_text="evidence is neutral UI data; llm is prompt-only; raw is full rows.", options=["evidence", "llm", "raw"]),
            _field("role", "select", "any", help_text="Use admin for staff/business numbers; counterparty for customer/contact numbers.", options=["any", "admin", "counterparty"]),
            _field("hide_automation", "checkbox", False),
        ],
        "hidden_defaults": {
            "limit": 10000,
            "print_limit": 200,
            "max_text": 220,
            "view": "clean",
            "focus": "all",
        },
        "example": {"phone": "7411146474", "days": 2, "role": "admin", "llm": True, "display_mode": "evidence"},
    },
    "staff_caretaker_activity": {
        "label": "Staff Activity Review",
        "method": "GET",
        "path": "/analytics/capabilities/staff/activity",
        "description": "Generic staff activity review. Backend resolves staff by phone/username/email and uses staging_user_account.team to choose Sales/Caretaker/Finance/Ops/etc collectors. The old caretaker endpoint remains only as a legacy alias and no longer forces Caretaker.",
        "fields": [
            _field("phone", "text", "", required=False, help_text="Staff phone. Accepts 10 or 12 digits; matches by last 10 digits."),
            _field("username", "text", "", required=False),
            _field("email", "text", "", required=False),
            _field(
                "role",
                "select",
                "auto",
                help_text="Use auto unless intentionally reviewing under a different role scope. DB team wins when available.",
                options=["auto", "Finance", "Caretaker", "Sales", "Onboarding", "Technical", "Ops Team", "Marketing"],
            ),
            _field("days", "number", 3, help_text="Activity lookback window."),
            _field("llm", "checkbox", True, help_text="Adds copy-ready LLM prompt. Evidence remains neutral."),
            _field("display_mode", "select", "evidence", help_text="evidence is UI-friendly; llm is prompt/context only; raw is full debug payload.", options=["evidence", "llm", "raw"]),
        ],
        "hidden_defaults": {"limit": 10000, "print_limit": 50, "max_text": 160},
        "example": {"phone": "7795550473", "role": "auto", "days": 3, "llm": True, "display_mode": "evidence"},
    },
    "whatsapp_inspector": {
        "label": "WhatsApp Conversation Inspector",
        "method": "GET",
        "path": "/analytics/capabilities/whatsapp/inspect",
        "description": "Inspect WhatsApp conversations by lead, phone, thread key or participant numbers.",
        "fields": [
            _field("lead_id", "number", ""),
            _field("phone", "text", ""),
            _field("actor", "text", ""),
            _field("thread_key", "text", ""),
            _field("remote_jid", "text", ""),
            _field("admin_number", "text", ""),
            _field("customer_number", "text", ""),
            _field("list_threads", "checkbox", False),
            _field("include_overall_summary", "checkbox", False),
            _field("limit", "number", 120),
        ],
        "example": {"lead_id": 401676, "list_threads": True},
    },
    "caretaker_performance_rating": {
    "label": "Caretaker Performance Rating",
    "method": "GET",
    "path": "/analytics/capabilities/staff/caretaker-performance/llm-rating",
    "description": "Build compact caretaker metrics from staff activity, send to LLM, parse score/risk/actions, and cache the review.",
    "fields": [
        _field("phone", "text", "", required=False, help_text="Caretaker phone. Use exactly one of phone, username, or email."),
        _field("username", "text", "", required=False),
        _field("email", "text", "", required=False),
        _field("days", "number", 30),
        _field("run_llm", "checkbox", False, help_text="False = cache/prompt only. True = call LLM."),
        _field("use_cache", "checkbox", True),
        _field("force_refresh", "checkbox", False),
        _field("include_prompt", "checkbox", True),
        _field("include_activity", "checkbox", False),
    ],
    "hidden_defaults": {
        "model": "gpt-5-mini",
        "timeout_seconds": 120,
        "limit": 10000,
        "print_limit": 80,
        "max_text": 180,
    },
    "example": {
        "phone": "8904567946",
        "days": 30,
        "run_llm": False,
        "use_cache": True,
        "force_refresh": False,
        "include_prompt": True,
        "include_activity": False,
    },
},

    
}


@router.get("/catalog")
def capability_catalog() -> dict[str, Any]:
    return {
        "schema": DEFAULT_SCHEMA,
        "base_path": "/analytics/capabilities",
        "default_view": "evidence",
        "note": "Number review is phone-only; Customer Brief is booking_id-only; Staff Activity uses exactly one staff seed (phone, username, or email), defaults to 3 days, and defaults role=auto from staging_user_account.team. UI returns neutral evidence; LLM should produce ratings, risks, reasons and next actions from the copied prompt.",
        "capabilities": CAPABILITY_CATALOG,
    }


UI_HTML = r'''
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Analytics Capabilities UI</title>
<style>
body{font-family:Arial,sans-serif;margin:0;background:#f7f7f8;color:#111827}header{padding:18px 24px;background:#111827;color:white}main{max-width:1240px;margin:0 auto;padding:20px}.grid{display:grid;grid-template-columns:390px 1fr;gap:18px}.card{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}label{display:block;font-size:13px;font-weight:700;margin-top:12px}input,select,textarea{width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:8px;padding:9px;font-size:14px}input[type=checkbox]{width:auto;transform:scale(1.15);margin-right:8px}button{border:0;border-radius:8px;padding:9px 12px;cursor:pointer;font-weight:700}.primary{background:#2563eb;color:white}.secondary{background:#e5e7eb;color:#111827}.danger{background:#fee2e2;color:#991b1b}.actions,.copybar{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}pre{white-space:pre-wrap;word-break:break-word;background:#0b1020;color:#e5e7eb;padding:14px;border-radius:10px;max-height:620px;overflow:auto}.hint,small{color:#6b7280;font-size:12px}.pill{display:inline-block;background:#eef2ff;color:#3730a3;padding:3px 8px;border-radius:999px;font-size:12px;margin:3px}.metric{display:inline-block;border:1px solid #e5e7eb;border-radius:10px;padding:8px 10px;margin:4px;background:#fafafa}.timeline{border-top:1px solid #e5e7eb;margin-top:12px}.event{padding:10px 0;border-bottom:1px solid #e5e7eb}.event .meta{font-size:12px;color:#6b7280;margin-bottom:4px}.event .text{font-size:14px;line-height:1.35}@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head>
<body><header><h2 style="margin:0">BPAI Analytics UI <span class="pill">schema: AnalyticsEngine</span></h2><div class="small" style="color:#d1d5db">Customer Brief is booking_id-only. Use display_mode=unrestricted for full conversations and call transcripts. Staff Activity uses phone, username, or email and role=auto from staging_user_account.team. Ratings, risks, reasons and next actions should be generated by LLM from Copy for LLM.</div></header><main><div class="grid"><section class="card"><label>Capability</label><select id="capability"></select><div id="description" class="hint"></div><div id="fields"></div><div class="actions"><button class="primary" onclick="runCapability()">Run</button><button class="secondary" onclick="fillExample()">Fill example</button><button class="secondary" onclick="copyRequest()">Copy request</button><button class="danger" onclick="clearForm()">Clear</button></div><label>Request preview</label><pre id="requestPreview">{}</pre></section><section class="card"><h3 style="margin-top:0">Response</h3><div class="copybar"><button class="secondary" onclick="copyTimeline()">Copy Timeline</button><button class="secondary" onclick="copyForLLM()">Copy for LLM</button><button class="secondary" onclick="copyDebugJson()">Copy Debug JSON</button></div><div class="hint">Copy for LLM includes the full reviewer prompt plus required evidence, so it can be pasted directly into ChatGPT/another LLM.</div><div id="status" class="small"></div><div id="evidenceBox"></div><label id="promptLabel" style="display:none">LLM prompt</label><textarea id="promptBox" rows="12" style="display:none" readonly></textarea><label>Raw response</label><pre id="responseBox">Run a capability to see output.</pre>
<button class="primary" onclick="getLLMRating()" style="background:#7c3aed">⭐ Get LLM Rating</button>
<div id="llmRatingSection" style="display:none; margin-top:16px">
  <div style="display:flex; justify-content:space-between; align-items:baseline; margin:0 0 8px 0;">
    <h3 style="margin:0">⭐ LLM Rating &amp; Summary</h3>
    <span id="llmRatingValue" style="font-weight:bold; background:#eef2ff; padding:4px 12px; border-radius:20px; font-size:14px"></span>
  </div>
  <div id="llmRatingBox"></div>
</div></div></section></div></main>
<script>
let catalog=null,activeResponse=null,activeRequest=null,externalQueryParams={};
function normalizeEndpointPath(value){let endpoint=String(value||'').trim();try{endpoint=decodeURIComponent(endpoint)}catch(e){}if(!endpoint)return'';const prefix='/analytics/capabilities';if(endpoint.startsWith(prefix))endpoint=endpoint.slice(prefix.length)||'/';return endpoint}
function capabilityKeyFromEndpoint(endpoint){const normalized=normalizeEndpointPath(endpoint);if(!normalized||!catalog||!catalog.capabilities)return'';const aliases={'/bookings/followup-review':'/bookings/review','/bookings/follow-up-review':'/bookings/review','/booking/review':'/bookings/review'};const wanted=aliases[normalized]||normalized;for(const [key,cap] of Object.entries(catalog.capabilities)){const capPath=normalizeEndpointPath(cap.path);if(capPath===wanted||capPath===normalized||cap.path===endpoint)return key}return''}
function coerceUrlBool(value){return ['1','true','yes','y','on'].includes(String(value||'').trim().toLowerCase())}
async function init(){const res=await fetch('/analytics/capabilities/catalog');catalog=await res.json();const sel=document.getElementById('capability');Object.entries(catalog.capabilities).forEach(([key,cap])=>{const opt=document.createElement('option');opt.value=key;opt.textContent=cap.label;sel.appendChild(opt)});sel.addEventListener('change',()=>{externalQueryParams={};renderForm()});applyUrlParamsFromLocation()}
function applyUrlParamsFromLocation(){const urlParams=new URLSearchParams(window.location.search);const endpoint=urlParams.get('endpoint')||'';const selectedKey=capabilityKeyFromEndpoint(endpoint);const sel=document.getElementById('capability');if(selectedKey)sel.value=selectedKey;renderForm();const key=sel.value,cap=catalog.capabilities[key];externalQueryParams={};urlParams.forEach((value,name)=>{if(name==='endpoint')return;let mappedName=name,mappedValue=value;if(key==='booking_review'&&name==='booking_ids')mappedName='booking_id';const field=(cap.fields||[]).find(f=>f.name===mappedName);if(field){const el=document.getElementById('field_'+mappedName);if(!el)return;if(field.type==='checkbox')el.checked=coerceUrlBool(mappedValue);else el.value=mappedValue}else{externalQueryParams[mappedName]=mappedValue}});updatePreview();if(endpoint&&selectedKey){setTimeout(()=>runCapability(),0)}}
function renderForm(){const key=document.getElementById('capability').value,cap=catalog.capabilities[key];document.getElementById('description').textContent=cap.description||'';const box=document.getElementById('fields');box.innerHTML='';cap.fields.forEach(f=>{const wrap=document.createElement('div'),label=document.createElement('label');label.textContent=f.name+(f.required?' *':'');wrap.appendChild(label);let input;if(f.type==='select'){input=document.createElement('select');(f.options||[]).forEach(o=>{const opt=document.createElement('option');opt.value=o;opt.textContent=o;input.appendChild(opt)});input.value=f.default??''}else if(f.type==='checkbox'){input=document.createElement('input');input.type='checkbox';input.checked=Boolean(f.default)}else{input=document.createElement('input');input.type=f.type||'text';input.value=f.default??''}input.id='field_'+f.name;input.addEventListener('input',updatePreview);input.addEventListener('change',updatePreview);wrap.appendChild(input);if(f.help){const hint=document.createElement('div');hint.className='hint';hint.textContent=f.help;wrap.appendChild(hint)}box.appendChild(wrap)});updatePreview()}
function getParams(){const key=document.getElementById('capability').value,cap=catalog.capabilities[key],params={...(cap.hidden_defaults||{})};cap.fields.forEach(f=>{const el=document.getElementById('field_'+f.name);if(!el)return;let value=f.type==='checkbox'?el.checked:el.value;if(value===''||value===null||value===undefined)return;if(f.type==='number')value=Number(value);params[f.name]=value});Object.entries(externalQueryParams||{}).forEach(([k,v])=>{params[k]=v});if(key==='customer_brief'&&String(params.display_mode||'').toLowerCase()==='unrestricted'){params.output_format='unrestricted';delete params.max_llm_messages;delete params.max_llm_text_chars}return params}
function buildRequest(){const key=document.getElementById('capability').value,cap=catalog.capabilities[key],params=getParams(),qs=new URLSearchParams();Object.entries(params).forEach(([k,v])=>qs.append(k,String(v)));return{method:cap.method||'GET',path:cap.path,params,url:cap.path+(qs.toString()?'?'+qs.toString():'')}}
function updatePreview(){activeRequest=buildRequest();document.getElementById('requestPreview').textContent=JSON.stringify(activeRequest,null,2)}
function fillExample(){const key=document.getElementById('capability').value,cap=catalog.capabilities[key];clearForm(false);Object.entries(cap.example||{}).forEach(([k,v])=>{const el=document.getElementById('field_'+k);if(!el)return;if(el.type==='checkbox')el.checked=Boolean(v);else el.value=v});updatePreview()}
function clearForm(update=true){const key=document.getElementById('capability').value,cap=catalog.capabilities[key];cap.fields.forEach(f=>{const el=document.getElementById('field_'+f.name);if(!el)return;if(el.type==='checkbox')el.checked=false;else el.value=''});if(update)updatePreview()}
async function runCapability(){updatePreview();document.getElementById('status').textContent='Running.';document.getElementById('responseBox').textContent='';document.getElementById('evidenceBox').innerHTML='';document.getElementById('promptBox').style.display='none';document.getElementById('promptLabel').style.display='none';document.getElementById('llmRatingSection').style.display='none';document.getElementById('llmRatingBox').innerHTML='';document.getElementById('llmRatingValue').textContent='';try{const res=await fetch(activeRequest.url,{headers:{accept:'application/json'}});const text=await res.text();try{activeResponse=JSON.parse(text)}catch{activeResponse={raw:text}}document.getElementById('status').textContent=`HTTP ${res.status}`;document.getElementById('responseBox').textContent=JSON.stringify(activeResponse,null,2);renderEvidence(activeResponse);const prompt=findPrompt(activeResponse);if(prompt){const p=document.getElementById('promptBox');p.value=prompt;p.style.display='block';document.getElementById('promptLabel').style.display='block'}}catch(e){activeResponse={error:String(e)};document.getElementById('status').textContent='Request failed';document.getElementById('responseBox').textContent=JSON.stringify(activeResponse,null,2)}}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function showVal(v){if(v===null||v===undefined)return'';if(typeof v==='object')return JSON.stringify(v);return String(v)}
function renderBookingFollowupTable(r){const box=document.getElementById('evidenceBox');function table(headers,rows){let out='<table style="border-collapse:collapse;width:100%;background:white;margin-top:10px"><thead><tr>';headers.forEach(h=>out+=`<th style="border:1px solid #e5e7eb;padding:8px;text-align:left;background:#f3f4f6">${esc(h)}</th>`);out+='</tr></thead><tbody>';(rows||[]).forEach(row=>{out+='<tr>';headers.forEach(h=>out+=`<td style="border:1px solid #e5e7eb;padding:8px;vertical-align:top">${esc(showVal(row[h]))}</td>`);out+='</tr>'});out+='</tbody></table>';return out}let html=`<div class="card" style="box-shadow:none;background:#fcfcfd"><h3 style="margin-top:0">${esc(r.title||'Booking Follow-up Review')}</h3>`;html+=`<div class="metric"><b>Reviewed</b>: ${esc(r.reviewed_booking_count||0)}</div><div class="metric"><b>Recent days</b>: ${esc(r.recent_days||'')}</div><div class="metric"><b>Customer days</b>: ${esc(r.customer_days||'')}</div><div class="metric"><b>LLM</b>: ${esc(r.run_llm)}</div>`;html+='<h4>Follow-up action table</h4>';html+=table(['booking_id','priority_score','owner_team','action','evidence','risk','score'],r.action_rows||[]);html+='<h4>Booking summary</h4>';html+=table(['booking_id','score','risk','main_reason','actions','status','error'],r.summary_rows||[]);html+='</div>';box.innerHTML=html}
function timelineLine(ev){const base=ev.line||`${ev.t||ev.time||''} | ${ev.ch||ev.channel||''} | ${ev.flow||ev.role_flow||''} | ${ev.status||''} | ${ev.summary||ev.subject||ev.text||ev.transcript||''}`;const transcript=ev.transcript;if(transcript&&transcript!==ev.text&&String(base).indexOf(String(transcript))===-1)return base+'\nTranscript: '+transcript;return base}
function unrestrictedPayload(r){return (r&&r.payload)||r||{}}
function unrestrictedConversation(r){const payload=unrestrictedPayload(r);if(payload&&payload.conversation)return payload.conversation;if(r&&r.llm_context&&r.llm_context.conversation)return r.llm_context.conversation;if(r&&r.conversation)return r.conversation;return{}}
function unrestrictedTimeline(r){const conv=unrestrictedConversation(r);const rows=conv.timeline||conv.recent_messages||[];return Array.isArray(rows)?rows:[]}
function renderUnrestricted(r){const box=document.getElementById('evidenceBox'),payload=unrestrictedPayload(r),conv=unrestrictedConversation(r),timeline=unrestrictedTimeline(r),booking=(payload&&payload.booking)||{},support=(payload&&payload.support)||{};let html=`<div class="card" style="box-shadow:none;background:#fcfcfd"><h3 style="margin-top:0">Customer Brief - unrestricted</h3>`;html+=`<div class="metric"><b>mode</b>: unrestricted</div><div class="metric"><b>view</b>: ${esc((r&&r.view)||'unrestricted')}</div><div class="metric"><b>version</b>: ${esc((payload&&payload.context_version)||(r&&r.context_version)||'')}</div><div class="metric"><b>messages shown</b>: ${esc(timeline.length)}</div>`;if(conv.recent_message_count!==undefined)html+=`<div class="metric"><b>conversation count</b>: ${esc(conv.recent_message_count)}</div>`;if(conv.included_count!==undefined)html+=`<div class="metric"><b>included</b>: ${esc(conv.included_count)}</div>`;if(conv.count!==undefined)html+=`<div class="metric"><b>count</b>: ${esc(conv.count)}</div>`;if(booking&&Object.keys(booking).length){html+='<div class="timeline"><h4 style="margin:12px 0 6px">Booking</h4>';['booking_id','status','booking_status','current_state','property_id','travel_from_date','travel_to_date','check_in_time','check_out_time'].forEach(k=>{if(booking[k]!==undefined&&booking[k]!==null&&booking[k]!=='')html+=`<span class="metric"><b>${esc(k)}</b>: ${esc(showVal(booking[k]))}</span>`});html+='</div>'}if(support&&Object.keys(support).length){html+='<div class="timeline"><h4 style="margin:12px 0 6px">Support</h4>';['total_ticket_count','open_ticket_count','closed_ticket_count'].forEach(k=>{if(support[k]!==undefined&&support[k]!==null&&support[k]!=='')html+=`<span class="metric"><b>${esc(k)}</b>: ${esc(showVal(support[k]))}</span>`});html+='</div>'}html+='<div class="timeline"><h4 style="margin:12px 0 6px">Full Conversation Timeline</h4>';if(!timeline.length){html+='<div class="event"><div class="text">No conversation rows found in payload.conversation.recent_messages for this booking/window.</div></div>'}timeline.forEach(ev=>{const meta=[ev.t||ev.time,ev.ch||ev.channel,ev.flow||ev.role_flow,ev.status,ev.direction,ev.agent||ev.actor||ev.role||ev.agent_role].filter(Boolean).join(' | ');let body='';if(ev.line){body=ev.line}else{const parts=[];if(ev.subject)parts.push('Subject: '+ev.subject);if(ev.text)parts.push(ev.text);if(ev.transcript&&ev.transcript!==ev.text)parts.push('Transcript: '+ev.transcript);if(ev.audio_url)parts.push('Audio: '+ev.audio_url);body=parts.join('\n')}html+=`<div class="event"><div class="meta">${esc(meta)}</div><div class="text" style="white-space:pre-wrap">${esc(body||timelineLine(ev))}</div></div>`});html+='</div></div>';box.innerHTML=html}
function renderLeadRating(r){const box=document.getElementById('evidenceBox');function metric(label,value){if(value===null||value===undefined||value==='')return'';return `<span class="metric"><b>${esc(label)}</b>: ${esc(showVal(value))}</span>`}function table(headers,rows){let out='<table style="border-collapse:collapse;width:100%;background:white;margin-top:10px"><thead><tr>';headers.forEach(h=>out+=`<th style="border:1px solid #e5e7eb;padding:8px;text-align:left;background:#f3f4f6">${esc(h)}</th>`);out+='</tr></thead><tbody>';(rows||[]).forEach(row=>{out+='<tr>';headers.forEach(h=>out+=`<td style="border:1px solid #e5e7eb;padding:8px;vertical-align:top">${esc(showVal(row[h]))}</td>`);out+='</tr>'});out+='</tbody></table>';return out}let html=`<div class="card" style="box-shadow:none;background:#fcfcfd"><h3 style="margin-top:0">${esc(r.title||'Lead Communication Review')}</h3>`;html+=metric('Lead ID',r.lead_id)+metric('Cached',r.cached)+metric('Cache status',r.cache_status)+metric('Overall score',r.overall_score)+metric('Priority',r.overall_priority_score)+metric('Lead handling',r.lead_handling_score)+metric('Customer score',r.customer_perspective_score)+metric('Risk',r.overall_risk||r.risk);if(r.main_reason)html+=`<div class="metric" style="display:block"><b>Main reason</b>: ${esc(r.main_reason)}</div>`;if(r.lead_summary)html+=`<div class="timeline"><h4 style="margin:12px 0 6px">Lead summary</h4><pre style="max-height:220px">${esc(JSON.stringify(r.lead_summary,null,2))}</pre></div>`;if(r.action_rows&&r.action_rows.length){html+='<div class="timeline"><h4 style="margin:12px 0 6px">Immediate next actions</h4>'+table(['priority_score','owner_team','action','evidence'],r.action_rows)+'</div>'}if(r.stakeholder_scores&&r.stakeholder_scores.length){html+='<div class="timeline"><h4 style="margin:12px 0 6px">Stakeholder scores</h4>'+table(['stakeholder_team','score','priority_score','phase','handled','gaps','evidence'],r.stakeholder_scores)+'</div>'}if(r.actor_scores&&r.actor_scores.length){html+='<div class="timeline"><h4 style="margin:12px 0 6px">Actor scores</h4>'+table(['actor_entity','role_team','score','priority_score','action','evidence'],r.actor_scores)+'</div>'}if(r.review_text)html+=`<div class="timeline"><h4 style="margin:12px 0 6px">Review text</h4><pre>${esc(r.review_text)}</pre></div>`;html+='</div>';box.innerHTML=html}
function isLeadReviewUiRequest(){return activeRequest&&String(activeRequest.path||'').includes('/communication/review-lead')}
function isCustomerBriefUiRequest(){return activeRequest&&String(activeRequest.path||'').includes('/customer-brief')}
function isRequestedUnrestricted(){return activeRequest&&activeRequest.params&&String(activeRequest.params.display_mode||'').toLowerCase()==='unrestricted'}
function shouldRenderCustomerBriefUnrestricted(r){if(!r)return false;if(isLeadReviewUiRequest())return false;const requested=isRequestedUnrestricted();const view=String(r.view||'').toLowerCase();const display=String(r.display_mode||'').toLowerCase();const payload=unrestrictedPayload(r);const ctx=String((payload&&payload.context_version)||(r&&r.context_version)||'').toLowerCase();if(isCustomerBriefUiRequest())return requested||view==='unrestricted'||display==='unrestricted'||ctx.includes('customer_brief');return (requested||view==='unrestricted'||display==='unrestricted'||ctx.includes('unrestricted'))&&(ctx.includes('customer_brief')||Boolean(payload&&payload.customer&&payload.conversation))}
function isEvidenceLikeResponse(r){if(!r||typeof r!=='object')return false;if(r.view==='evidence')return true;const view=String(r.view||'').toLowerCase();return view==='unrestricted'&&Boolean(r.timeline||r.metrics||r.sections||r.summary_cards)}
function renderEvidence(r){const box=document.getElementById('evidenceBox');if(r&&(r.view==='lead_llm_rating'||r.review_text||r.overall_score!==undefined||r.overall_priority_score!==undefined)){renderLeadRating(r);return}if(r&&r.view==='booking_followup_table'){renderBookingFollowupTable(r);return}if(shouldRenderCustomerBriefUnrestricted(r)){renderUnrestricted(r);return}if(!isEvidenceLikeResponse(r)){box.innerHTML='';return}let title=r.title||'Evidence';if(String(r.view||'').toLowerCase()==='unrestricted'&&String(title).toLowerCase().indexOf('unrestricted')===-1){title=title+' - unrestricted'}let html=`<div class="card" style="box-shadow:none;background:#fcfcfd"><h3 style="margin-top:0">${esc(title)}</h3>`;
const summaryCards=(r.summary_cards||[]).filter(c=>!(String(c.label||'').toLowerCase()==='review scope'&&String(r.title||'').toLowerCase().includes(String(c.value||'').toLowerCase())));
if(summaryCards.length){html+='<div style="margin-top:8px">';summaryCards.forEach(c=>{html+=`<div class="metric" style="max-width:100%;display:block"><b>${esc(c.label)}</b>: ${esc(c.value)}</div>`});html+='</div>'}else if(r.metrics){const m=r.metrics||{},calls=m.calls||{},metricBits=[];Object.entries(m.channels||{}).forEach(([k,v])=>{
  if(String(k).toLowerCase()==='call' && calls.total!==undefined) return;
  metricBits.push(`<span class="metric"><b>${esc(k)}</b>: ${esc(v)}</span>`);
});metricBits.push(`<span class="metric"><b>rows</b>: ${esc(r.row_count||m.rows||0)}</span>`);if(calls.total!==undefined)metricBits.push(`<span class="metric"><b>calls</b>: ${esc(calls.total||0)}</span>`);if(calls.connected!==undefined)metricBits.push(`<span class="metric"><b>connected</b>: ${esc(calls.connected||0)}</span>`);if(calls.missed!==undefined)metricBits.push(`<span class="metric"><b>missed</b>: ${esc(calls.missed||0)}</span>`);if(calls.talk_time_sec!==undefined)metricBits.push(`<span class="metric"><b>talk sec</b>: ${esc(calls.talk_time_sec||0)}</span>`);if(metricBits.length)html+=`<div>${metricBits.join('')}</div>`}
function eventBody(row){let line=row&&row.line?String(row.line):'';if(!line){const bits=Object.entries(row||{}).filter(([k,v])=>v!==null&&v!==undefined&&v!==''&&!(Array.isArray(v)&&v.length===0)).map(([k,v])=>`<b>${esc(k)}</b>=${esc(showVal(v))}`);line=bits.join(' · ')}const transcript=row&&row.transcript;if(transcript&&transcript!==row.text&&line.indexOf(String(transcript))===-1){line+=(line?'\n':'')+'Transcript: '+String(transcript)}return line}
function renderRows(rows){let out='';(rows||[]).forEach(row=>{const body=eventBody(row);if(body)out+=`<div class="event"><div class="text" style="white-space:pre-wrap">${body}</div></div>`});return out}
(r.sections||[]).forEach(sec=>{html+=`<div class="timeline"><h4 style="margin:12px 0 6px">${esc(sec.title||'Section')}</h4>`;(sec.items||[]).forEach(item=>{html+=`<span class="metric"><b>${esc(item.label)}</b>: ${esc(item.value)}</span>`});if(sec.rows&&sec.rows.length){html+=renderRows(sec.rows)}html+='</div>'});
html+='<div class="timeline"><h4 style="margin:12px 0 6px">Timeline</h4>';(r.timeline||[]).forEach(ev=>{const line=eventBody(ev)||timelineLine(ev);html+=`<div class="event"><div class="text" style="white-space:pre-wrap">${line}</div></div>`});html+='</div></div>';box.innerHTML=html}
function findPrompt(obj){if(!obj||typeof obj!=='object')return'';if(obj.llm_prompt)return obj.llm_prompt;if(obj.copy_blocks&&obj.copy_blocks.llm_prompt)return obj.copy_blocks.llm_prompt;if(obj.reviews&&obj.reviews.length&&obj.reviews[0].llm_prompt)return obj.reviews.map(r=>`Booking ${r.booking_id}\n\n${r.llm_prompt||''}`).join('\n\n---\n\n');return''}
function findTimeline(obj){if(!obj||typeof obj!=='object')return'';if(isRequestedUnrestricted()&&Array.isArray(obj.rows)&&obj.rows.length)return obj.rows.map(timelineLine).join('\n');if(obj.copy_blocks&&obj.copy_blocks.timeline_text)return obj.copy_blocks.timeline_text;const candidates=[obj.rows,obj.timeline,obj.conversation&&obj.conversation.timeline,obj.conversation&&obj.conversation.recent_messages,obj.payload&&obj.payload.conversation&&obj.payload.conversation.timeline,obj.payload&&obj.payload.conversation&&obj.payload.conversation.recent_messages,obj.llm_context&&obj.llm_context.conversation&&obj.llm_context.conversation.timeline,obj.llm_context&&obj.llm_context.conversation&&obj.llm_context.conversation.recent_messages];for(const rows of candidates){if(Array.isArray(rows)&&rows.length)return rows.map(timelineLine).join('\n')}return''}
async function copyText(text){await navigator.clipboard.writeText(text||'');document.getElementById('status').textContent='Copied.'}
function copyRequest(){copyText(JSON.stringify(activeRequest||buildRequest(),null,2))}
function copyTimeline(){copyText(findTimeline(activeResponse))}
function copyForLLM(){copyText(findPrompt(activeResponse)||JSON.stringify((activeResponse&&activeResponse.llm_context)||activeResponse||{},null,2))}
function copyDebugJson(){copyText((activeResponse&&activeResponse.copy_blocks&&activeResponse.copy_blocks.response_json)||JSON.stringify(activeResponse||{},null,2))}
async function getLLMRating(){
  if(!activeResponse){document.getElementById('status').textContent='Run a capability first.';return}
  const prompt=findPrompt(activeResponse);
  if(!prompt){document.getElementById('status').textContent='No LLM prompt found. Use display_mode=evidence or llm.';return}
  const section=document.getElementById('llmRatingSection'), box=document.getElementById('llmRatingBox');
  const ratingSpan = document.getElementById('llmRatingValue');
  section.style.display='block';
  ratingSpan.textContent = '...';
  box.innerHTML='<div style="padding:14px;color:#6b7280;border:1px solid #e5e7eb;border-radius:8px">⏳ Generating LLM rating & summary...</div>';
  try{
    const res=await fetch('/analytics/capabilities/llm_rating',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({system_prompt:prompt,payload:activeResponse})
    });
    const data=await res.json();
    const text=data.result||data.response||data.content||data.text||data.output||JSON.stringify(data,null,2);
    
    // --- improved rating extraction ---
    let rating = '';
    const ratingPatterns = [
        /RATING:\s*(10|[0-9](?:\.[0-9])?)\s*\/\s*10/i,            // forced footer "RATING: 7/10"
        /(?:rating|score)\s*:?\s*(10|[0-9](?:\.[0-9])?)\/10/i,    // "Rating: 7/10"
        /\*\*\s*(10|[0-9](?:\.[0-9])?)\s*\/\s*10\s*\*\*/,         // bold **7/10**
        /\b(10|[0-9](?:\.[0-9])?)\s*\/\s*10\b/,                   // bare 7/10
        /\b(10|[0-9](?:\.[0-9])?)\s+out\s+of\s+10\b/i,            // "7 out of 10"
    ];
    for (const pat of ratingPatterns) {
    const m = text.match(pat);
    if (m) { rating = m[1] + '/10'; break; }
    }

    let priority = '';
    const priorityPatterns = [
        /PRIORITY:\s*(10|[0-9](?:\.[0-9])?)\s*\/\s*10\s*[—-]\s*(Low|Medium|High|Critical)/i,
        /priority\s*:?\s*(10|[0-9](?:\.[0-9])?)\s*\/\s*10/i,
        /priority score\s*:?\s*(10|[0-9](?:\.[0-9])?)\s*\/\s*10/i,
    ];

    for (const pat of priorityPatterns) {
        const m = text.match(pat);
        if (m) {
            priority = m[1] + '/10' + (m[2] ? ' ' + m[2] : '');
            break;
        }
    }

    if (!rating) rating = '?/10';
    if (!priority) priority = '?/10';

    ratingSpan.textContent = `Rating: ${rating} | Priority: ${priority}`;
    
    const fullSummaryText = text;
    box.innerHTML = '<pre style="white-space:pre-wrap;word-break:break-word;background:#0b1020;color:#e5e7eb;padding:14px;border-radius:10px;max-height:620px;overflow:auto">'+esc(text)+'</pre>';
    document.getElementById('status').textContent='LLM rating ready.';
  }catch(e){
    ratingSpan.textContent = 'error';
    box.innerHTML='<div style="color:#991b1b;padding:10px;border:1px solid #fca5a5;border-radius:8px">Error calling LLM API: '+esc(String(e))+'</div>';
    document.getElementById('status').textContent='LLM API call failed.';
  }
}
init();
</script></body></html>
'''
@router.post("/llm_rating")
async def llm_rating_proxy(payload: dict[str, Any]) -> Any:
    import httpx
    timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post("https://app.bpai.info/api/bpai/run_llm", json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail=f"LLM upstream timed out: {type(e).__name__}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"LLM upstream returned {e.response.status_code}: {e.response.text[:300]}",
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"LLM proxy error: {type(e).__name__}: {str(e)}")

@router.get("/ui", response_class=HTMLResponse)
def capabilities_ui() -> HTMLResponse:
    return HTMLResponse(UI_HTML) 