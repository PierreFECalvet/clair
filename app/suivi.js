'use strict';
/* Historique des appels API, détail d'un appel, panneaux de l'ancien parcours (archives).
   Les étapes, le bandeau de traitement, « À faire » et le suivi léger sont dans chaine.js. */

const fmtDuration=seconds=>{
  if(seconds==null||!isFinite(seconds))return '';
  const s=Math.max(0,Math.round(seconds)),h=Math.floor(s/3600),m=Math.floor(s%3600/60);
  return h?`${h} h ${String(m).padStart(2,'0')}`:m?`${m} min`:`${s} s`;
};
const fmtMs=ms=>ms==null?'—':ms<1000?`${ms} ms`:`${(ms/1000).toFixed(1).replace('.',',')} s`;
const since=iso=>iso?(Date.now()-new Date(iso).getTime())/1000:null;
const quiet=async fn=>{try{await fn()}catch(e){notify(e.message,true)}};
const el=(tag,cls,html)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(html!=null)node.innerHTML=html;return node};
const stageLabels={import:'Import des contenus',connexion:'Test des connexions',test:'Test des modèles',embeddings:'Embeddings des contenus',extraction:'Extraction des affirmations',prepare:'Extraction',deduplication:'Dédoublonnage',screening:'Précontrôle',estimation:'Estimation du budget',selection:'Sélection des affirmations',verification:'Recherche Ibou',queries:'Rédaction des questions Ibou',catchup:'Rattrapage',corrections:'Corrections',correct:'Corrections',expert:'Révision approfondie',expert_batch:'Révisions de la sélection',all:'Parcours complet',verify:'Vérification'};
const statusWords={success:'réussi',cache:'cache',error:'erreur',refused:'refusé',uncertain:'coût incertain',reserved:'réservé'};
const serviceWords={openai:'OpenAI',openrouter:'OpenRouter',lmstudio:'LM Studio',ibou:'Ibou',source:'Source citée'};

/* ---------- vue : historique des appels ---------- */
const pager=(data,name)=>`<span class="muted">${number(data.total)} appel(s) · ${data.offset+1}–${Math.min(data.offset+50,data.total)}</span><div><button class="button" data-${name}-step="-50" ${data.offset?'':'disabled'}>← Plus récents</button> <button class="button" data-${name}-step="50" ${data.offset+50<data.total?'':'disabled'}>Plus anciens →</button></div>`;
const viewHistory=el('section','view');viewHistory.id='view-history';viewHistory.hidden=true;
$('#view-activity').after(viewHistory);

/* ---------- historique des appels ---------- */
let historyKind='all',historyQuery='',historyOffset=0,historyData=null;
viewHistory.innerHTML=`<div class="toolbar"><div class="tabs" id="history-tabs">${[['all','Tous'],['llm','Modèles'],['ibou','Ibou'],['source','Sources citées'],['error','Erreurs']].map(([k,t])=>`<button data-history-kind="${k}">${t}</button>`).join('')}</div><input class="search" id="history-search" placeholder="Requête, identifiant d’affirmation, modèle, erreur…" aria-label="Rechercher dans l’historique"></div>
<section class="card padded"><h2>Synthèse</h2><div id="history-summary"></div></section>
<section class="card below"><div id="history-table"></div></section><div class="toolbar below" id="history-pages"></div>
<section class="card padded below"><h2>Traitements lancés</h2><p>Chaque lancement, ses étapes et son issue. Conservé après redémarrage.</p><div id="history-runs"></div></section>`;
async function loadHistory(){
  $$('#history-tabs button').forEach(b=>b.classList.toggle('selected',b.dataset.historyKind===historyKind));
  const params=new URLSearchParams({offset:historyOffset});if(historyKind!=='all')params.set('kind',historyKind);if(historyQuery)params.set('q',historyQuery);
  const d=historyData=await api('history?'+params);
  const groups={};for(const r of d.summary){const key=r.service+'|'+r.operation;const g=groups[key]||(groups[key]={service:r.service,operation:r.operation,calls:0,errors:0,units:0,tin:0,tout:0,ms:0,timed:0,last:''});g.calls+=r.calls;if(['error','uncertain','refused'].includes(r.status))g.errors+=r.calls;g.units+=r.units;g.tin+=r.tokens_in;g.tout+=r.tokens_out;if(r.avg_ms!=null){g.ms+=r.avg_ms*r.calls;g.timed+=r.calls}if(r.last>g.last)g.last=r.last}
  const list=Object.values(groups).sort((a,b)=>b.calls-a.calls);
  $('#history-summary').innerHTML=list.length?`<div class="table-wrap"><table><thead><tr><th>Service</th><th>Opération</th><th>Appels</th><th>Erreurs</th><th>Durée moyenne</th><th>Tokens entrée / sortie</th><th>Unités Ibou</th><th>Dernier</th></tr></thead><tbody>${list.map(g=>`<tr><td>${esc(serviceWords[g.service]||g.service)}</td><td>${esc(g.operation||'—')}</td><td>${number(g.calls)}</td><td>${g.errors?`<span class="danger">${number(g.errors)}</span>`:'0'}</td><td>${g.timed?fmtMs(Math.round(g.ms/g.timed)):'—'}</td><td>${g.tin||g.tout?`${number(g.tin)} / ${number(g.tout)}`:'—'}</td><td>${g.units?number(g.units):'—'}</td><td>${date(g.last)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">Aucun appel enregistré pour ce filtre. Le journal commence au premier traitement lancé après cette mise à jour ; les appels Ibou antérieurs y ont été repris.</p>';
  $('#history-table').innerHTML=d.rows.length?`<div class="table-wrap"><table><thead><tr><th>Date</th><th>Service</th><th>Ce qui est parti</th><th>État</th><th>Durée</th><th>Coût</th></tr></thead><tbody>${d.rows.map(r=>`<tr class="clickable" data-log="${r.id}" tabindex="0"><td>${date(r.created)}</td><td>${esc(serviceWords[r.service]||r.service)}<small>${esc(r.operation||'')}${r.model?' · '+esc(r.model):''}</small></td><td>${esc(logSubject(r))}${r.claim_text?`<small>Affirmation : ${esc(r.claim_text)}</small>`:''}${r.error?`<small class="danger">${esc(r.error)}</small>`:''}</td><td><span class="badge call-${esc(r.status)}">${esc(statusWords[r.status]||r.status)}</span>${r.http_status?`<small>HTTP ${r.http_status}</small>`:''}</td><td>${fmtMs(r.duration_ms)}</td><td>${r.units?r.units+' u.':r.tokens_in!=null?`${number(r.tokens_in)} → ${number(r.tokens_out||0)} tokens`:'—'}</td></tr>`).join('')}</tbody></table></div>`:empty('≡','Aucun appel dans cette vue.','Changez de filtre ou lancez un traitement.');
  $('#history-pages').innerHTML=d.total?pager(d,'history'):'';
  $('#history-runs').innerHTML=d.runs.length?d.runs.map(run=>{let trail=[];try{trail=JSON.parse(run.stages||'[]')}catch{}
    return `<div class="event"><strong>${esc(stageLabels[run.kind]||run.kind)}</strong> · <span class="badge run-${esc(run.status)}">${esc({running:'en cours',completed:'terminé',paused:'interrompu',error:'erreur',interrupted:'serveur arrêté'}[run.status]||run.status)}</span><small>${date(run.started)}${run.ended?` → ${date(run.ended)} · ${fmtDuration((new Date(run.ended)-new Date(run.started))/1000)}`:''}</small>${trail.filter(t=>t.stage!==run.kind).length?`<small>${trail.filter(t=>t.stage!==run.kind).map(t=>`${esc(stageLabels[t.stage]||t.stage)}${t.total?` ${number(t.done||0)}/${number(t.total)}`:''}`).join(' › ')}</small>`:''}${run.message&&run.status!=='completed'?`<small class="danger">${esc(run.message)}</small>`:''}</div>`}).join(''):'<p class="muted">Aucun traitement enregistré depuis cette mise à jour.</p>';
}
function logSubject(r){
  if(r.service==='ibou'||r.service==='source')return r.request||'/'+r.endpoint;
  try{const d=JSON.parse(r.request);return d.claim||d.reference||(d.chunk?'Chunk : '+d.chunk:'')||d.quote||(d.a?d.a+' ↔ '+d.b:'')||d.test||`${number(d.taille_totale||0)} caractères envoyés`}catch{return r.request||''}
}
viewHistory.addEventListener('click',e=>{
  const b=e.target.closest('button');if(!b)return;
  if(b.dataset.historyKind){historyKind=b.dataset.historyKind;historyOffset=0;quiet(loadHistory);return}
  if(b.dataset.historyStep){historyOffset=Math.max(0,historyOffset+Number(b.dataset.historyStep));quiet(loadHistory)}
});
let historyTimer;$('#history-search').oninput=e=>{clearTimeout(historyTimer);historyTimer=setTimeout(()=>{historyQuery=e.target.value.trim();historyOffset=0;quiet(loadHistory)},350)};
const logDialog=el('dialog');logDialog.id='log-dialog';document.body.append(logDialog);
async function openLog(id){
  const r=await api('history/'+id);let request=r.request;try{request=JSON.stringify(JSON.parse(r.request),null,2)}catch{}
  let response=r.response;try{response=JSON.stringify(JSON.parse(r.response),null,2)}catch{}
  logDialog.innerHTML=`<div class="dialog-heading"><div><div class="eyebrow">${esc(serviceWords[r.service]||r.service)} · ${esc(r.operation||r.endpoint)}</div><h2>${date(r.created)}</h2></div><button class="icon-button close-dialog" aria-label="Fermer">×</button></div>
  <div class="log-facts"><span class="badge call-${esc(r.status)}">${esc(statusWords[r.status]||r.status)}</span>${r.http_status?`<span>HTTP ${r.http_status}</span>`:''}<span>${fmtMs(r.duration_ms)}</span>${r.model?`<span>${esc(r.model)}</span>`:''}${r.units?`<span>${r.units} unité(s)</span>`:''}${r.tokens_in!=null?`<span>${number(r.tokens_in)} → ${number(r.tokens_out||0)} tokens</span>`:''}<span class="mono">${esc(r.endpoint)}</span></div>
  ${r.error?`<p class="danger">${esc(r.error)}</p>`:''}
  ${r.claim?`<div class="notice"><strong>Affirmation concernée</strong> ${badge(r.claim.status)}<br><button class="text-button" data-claim="${r.claim.id}">${esc(r.claim.text)} ↗</button>${r.verdict?`<p><strong>Enregistré après cet appel :</strong> ${esc(labels[r.verdict.status]||r.verdict.status)} — ${esc(r.verdict.reason||'')}</p>`:''}</div>`:''}
  <h3>${r.service==='ibou'?'Requête envoyée':r.service==='source'?'Adresse consultée':'Contenu envoyé (champs longs résumés par leur taille)'}</h3><pre class="log-body">${esc(request)}</pre>
  ${r.ibou?`<h3>Résultats renvoyés · ${r.ibou.count}</h3>${r.ibou.results.map(x=>`<div class="proof"><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title||x.url)} ↗</a><small>${esc(x.host)}${x.language?' · '+esc(x.language):''} · ${x.passages} passage(s)</small><blockquote>${esc(x.excerpt)}</blockquote></div>`).join('')||'<p>Aucun résultat.</p>'}`:response?`<h3>Réponse reçue${r.response.length>=4000?' (tronquée à 4 000 caractères)':''}</h3><pre class="log-body">${esc(response)}</pre>`:''}`;
  if(!logDialog.open)logDialog.showModal();
}
document.addEventListener('click',e=>{const row=e.target.closest('[data-log]');if(row)quiet(()=>openLog(row.dataset.log))});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches('[data-log]'))e.target.click()});
logDialog.addEventListener('click',e=>{if(e.target.closest('[data-claim]'))logDialog.close()});

/* ---------- détail d'une affirmation : ses échanges ---------- */
const detailBeforeSuivi=detail;
detail=async function(id){
  await detailBeforeSuivi(id);const d=await api('history?claim='+encodeURIComponent(id));if(!$('#detail-dialog').open)return;
  const box=el('section','',`<h3>Échanges avec les services · ${d.total}</h3>${d.rows.map(r=>`<div class="proof log-line" data-log="${r.id}" tabindex="0" role="button"><strong>${date(r.created)} · ${esc(serviceWords[r.service]||r.service)} · ${esc(r.operation||r.endpoint)}</strong> <span class="badge call-${esc(r.status)}">${esc(statusWords[r.status]||r.status)}</span><small>${fmtMs(r.duration_ms)}${r.units?` · ${r.units} u.`:''}${r.tokens_in!=null?` · ${number(r.tokens_in)} → ${number(r.tokens_out||0)} tokens`:''}${r.service==='ibou'?' · '+esc(r.request):''}</small>${r.error?`<small class="danger">${esc(r.error)}</small>`:''}</div>`).join('')||'<p class="muted">Aucun appel journalisé pour cette affirmation (le journal commence à cette mise à jour).</p>'}`);
  $('#claim-detail').append(box);
};


/* ---------- ancien parcours : panneaux regroupés dans les archives ---------- */
const stepPanels=el('details','card step-panels','<summary>Anciens panneaux de lancement <small>dédoublonnage, précontrôle par lot</small></summary>');
$('#metrics').after(stepPanels);
stepPanels.append(watchPanel,dedupPanel,screeningPanel);
for(const panel of [dedupPanel,screeningPanel])panel.classList.remove('card','below');
$('#view-contents').prepend(issuesPanel);issuesPanel.classList.remove('below');
$('.pipeline-card')?.remove();
const renderBeforeSuivi=render;
render=function(){
  renderBeforeSuivi();if(!state?.progress)return;
  const j=state.job;watchPanel.hidden=!(j.last_error||j.retry_pending||j.list_completed_at||['paused','error'].includes(j.status));
  watchPanel.classList.add('watch-compact');
  $('#calls').closest('section').querySelector('p').innerHTML='Vue détaillée, avec requêtes et résultats : <button class="text-button" data-vue="history">Historique des appels ↗</button>';
};
