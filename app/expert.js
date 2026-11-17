'use strict';
const expertSetting=document.createElement('label');expertSetting.innerHTML='Modèle OpenAI pour les révisions approfondies<input id="expert-model" list="expert-models" placeholder="Identifiant exact d’un modèle plus avancé accessible à votre compte"><datalist id="expert-models"></datalist><small>Indépendant du modèle d’extraction et de LM Studio. Utilise votre clé OpenAI ; choix explicite, aucun remplacement automatique de modèle.</small>';
$('#model').closest('label').after(expertSetting);
const listExpert=document.createElement('button');listExpert.type='button';listExpert.className='button';listExpert.textContent='Lister les modèles OpenAI pour la révision';expertSetting.after(listExpert);
const saveBeforeExpert=saveSettings;
saveSettings=async function(){await saveBeforeExpert();await api('config',{review_model:$('#expert-model').value.trim()})};
listExpert.onclick=e=>action(async()=>{await saveSettings();const r=await api('test',{target:'expert'});$('#expert-models').innerHTML=r.models.map(m=>`<option value="${esc(m)}"></option>`).join('');notify('Modèles OpenAI chargés. Choisissez celui à utiliser pour les révisions approfondies.')},e.currentTarget);
let expertLoaded=false,expertCompletion='',pendingExpertClaim=null;
const batchButton=document.createElement('button');batchButton.className='button primary';batchButton.id='propose-selected';selectionBar.querySelector('.bulk-heading-actions').append(batchButton);
const batchPanel=document.createElement('section');batchPanel.className='notice';selectionBar.after(batchPanel);
const batchTab=document.createElement('button');batchTab.dataset.filter='expert_batch';batchTab.textContent='Dernière sélection traitée';$('#claim-tabs').append(batchTab);
const visibleBeforeBatch=visibleClaims;
visibleClaims=function(){if(filter!=='expert_batch')return visibleBeforeBatch();const ids=new Set(state.expert_batch?.ids||[]);const q=$('#search').value.toLowerCase();return state.claims.filter(c=>ids.has(c.id)&&(c.text+' '+c.period).toLowerCase().includes(q))};
const selectedBeforeBatch=selectedCount;
selectedCount=function(){selectedBeforeBatch();batchButton.textContent=`Proposer des corrections pour la sélection (${selection.size})`;batchButton.disabled=!selection.size||state?.job.status==='running'};
batchButton.textContent='Proposer des corrections pour la sélection (0)';batchButton.disabled=true;
batchButton.onclick=e=>action(async()=>{
 if(!selection.size)throw Error('Coche au moins une affirmation.');
 if(!state.config.review_model||!state.config.openai_key_set){go('settings');throw Error('Enregistrez la clé OpenAI et le modèle de révision approfondie.')}
 const ids=[...selection];await api('expert-batch',{ids});pendingExpertClaim=null;filter='expert_batch';go('claims');
 $$('#claim-tabs button').forEach(b=>b.classList.toggle('selected',b.dataset.filter===filter));
 notify(`Révision de ${ids.length} affirmation(s) lancée, une par une. Les propositions resteront à examiner.`);
},e.currentTarget);
batchPanel.addEventListener('click',e=>{
 const button=e.target.closest('[data-resume-expert-batch]');if(!button)return;
 action(async()=>{if(!state.config.openai_key_set){go('settings');throw Error('Renseignez la clé OpenAI pour reprendre ce lot.')}await api('expert-batch-resume',{id:button.dataset.resumeExpertBatch});notify('Reprise des seules affirmations restantes.')},button);
});
let batchCompletion='';
const renderBeforeExpert=render;
render=function(){
 renderBeforeExpert();if(!state)return;
 if(!expertLoaded){$('#expert-model').value=state.config.review_model||'';expertLoaded=true}
 const j=state.job;
 const batch=state.expert_batch;
 batchTab.hidden=!batch;
 batchPanel.innerHTML=batch?`<strong>Corrections de la sélection : ${batch.done} / ${batch.total} analysées</strong><p>Modèle OpenAI : ${esc(batch.model)} · ${esc({pending:'En attente',running:'En cours',paused:'Suspendu',completed:'Terminé'}[batch.status]||batch.status)}. Ouvre une affirmation par son titre pour lire sa proposition. Les contenus restent inchangés.</p>${batch.error?`<p class="danger">${esc(batch.error)}</p>`:''}${batch.done<batch.total?`<button class="button" data-resume-expert-batch="${esc(batch.id)}" ${j.status==='running'?'disabled':''}>Reprendre les ${batch.total-batch.done} restantes</button>`:''}<p class="muted">Un appel au modèle avancé par affirmation, avec son contexte et ses sources. Consomme des crédits OpenAI, aucune unité Ibou. Les résultats déjà terminés de ce lot sont conservés à la reprise.</p>`:'<p>Coche les affirmations à traiter, puis clique sur « Proposer des corrections pour la sélection ». Un appel au modèle OpenAI avancé par affirmation ; aucune modification automatique ni consommation Ibou.</p>';
 if(batch?.status==='completed'&&batchCompletion!==batch.id){batchCompletion=batch.id;notify(`Les ${batch.total} révisions de la sélection sont terminées. Consulte « Dernière sélection traitée ».`)}
 if(j.kind==='expert'&&['paused','error'].includes(j.status))pendingExpertClaim=null;
 if(j.kind==='expert'&&j.status==='completed'&&expertCompletion!==j.last_check){
  expertCompletion=j.last_check;
  if(pendingExpertClaim){const id=pendingExpertClaim;pendingExpertClaim=null;action(()=>detail(id));notify('La proposition est prête : examine-la avant de préparer une modification.')}
  else notify('Révision approfondie terminée. Rouvrez l’affirmation pour consulter le résultat.');
 }
};
document.addEventListener('click',e=>{
 const button=e.target.closest('[data-propose-correction]');if(!button)return;
 action(async()=>{
  if(state.job.status==='running')throw Error('Un traitement est déjà en cours.');
  if(!state.config.review_model||!state.config.openai_key_set){go('settings');throw Error('Enregistrez la clé OpenAI et le modèle de révision approfondie.')}
  const id=button.dataset.proposeCorrection;
  await api('expert-review',{id,context:'',proposal:'',urls:[]});pendingExpertClaim=id;
  notify('Le modèle prépare une proposition à partir du passage et des sources de l’article. Le résultat s’ouvrira ici.');
 },button);
});
function expertReport(row){
 let r;try{r=JSON.parse(row.result)}catch{return ''}
 const verdict={supported:'Étayé par les références',contradicted:'Contredit par les références',unestablished:'Les références ne permettent pas de conclure'};
 const ready=r.eligible_for_patches;
 return `<section class="proof"><h3>${ready?'Une correction peut être préparée':'Analyse terminée — correction à compléter'}</h3>
 <p><strong>Aucun texte n’a été modifié.</strong> ${ready?'Lis la proposition ci-dessous. Si elle te convient, prépare les modifications.':'Le modèle a proposé une formulation, mais les contrôles ne permettent pas encore de préparer son remplacement dans les articles.'}</p>
 <p>${esc(r.assessment)}</p>
 <div class="notice"><strong>Ce qui a été analysé</strong><p>${r.user_proposal?'Ta correction proposée et la phrase originale.':'Proposition automatique à partir de la phrase originale, de son contexte et des preuves disponibles.'}</p><p>${r.user_context?'Ton contexte supplémentaire a été transmis.':'Contexte extrait automatiquement du corpus.'}</p></div>
 ${r.user_proposal?`<p><strong>Ta proposition</strong><br>${esc(r.user_proposal)}</p>`:''}
 <label>Proposition du modèle — pas encore appliquée<textarea rows="4" readonly>${esc(r.proposed_correction)}</textarea></label>
 ${ready?`<button class="button primary" data-expert-patches="${esc(row.id)}">Cette proposition me convient : préparer les modifications</button><p>Tu pourras ensuite examiner chaque passage dans Corrections avant de l’appliquer.</p>`:`<p><strong>Prochaine action :</strong> dans le formulaire ci-dessous, précise le contexte, colle ta correction et ajoute les sources utiles, puis relance l’analyse de cette seule affirmation.</p>`}
 ${!r.validation_version?'<p class="muted">Rapport produit avant la correction du contrôle des citations locales. Il est conservé pour l’historique ; une nouvelle analyse utilisera le contrôle corrigé.</p>':''}
 <details><summary>Voir les justifications, les sources et les questions restantes</summary>
 <small>OpenAI · ${esc(row.model)} · ${date(row.created)}</small><p><strong>Affirmation contextualisée</strong><br>${esc(r.contextualized_claim)}</p>
 ${(r.points||[]).map(p=>`<div class="notice"><strong>${esc(verdict[p.verdict]||p.verdict)} — ${esc(p.claim)}</strong><small>${p.scope==='context'?'Contrôle de ce que dit le document':'Contrôle factuel'}</small><p>${esc(p.reason)}</p>${(p.citations||[]).map(c=>`<blockquote>${esc(c.quote)}</blockquote>`).join('')}</div>`).join('')}
 <label>Prompt de vérification réutilisable<textarea rows="5" readonly>${esc(r.verification_prompt)}</textarea></label>
 ${(r.remaining_questions||[]).map(q=>`<p>Question restante : ${esc(q)}</p>`).join('')}
 ${(r.search_queries||[]).map(q=>`<p>Recherche suggérée (non exécutée) : ${esc(q)}</p>`).join('')}
 ${(r.source_report||[]).map(x=>`<p class="muted">${esc(x.url)} · ${x.status==='ok'?'source accessible':esc(x.error||'source inaccessible')}</p>`).join('')}
 </details></section>`;
}
const detailBeforeExpert=detail;
detail=async function(id){
 await detailBeforeExpert(id);const data=await api('claim/'+id);if(!$('#detail-dialog').open)return;
 let last={};try{last=JSON.parse(data.expert_reviews?.[0]?.result||'{}')}catch{}
 const panel=document.createElement('section');panel.className='notice';panel.innerHTML=`<h3>Demander une correction de cette affirmation</h3><p>Le modèle peut proposer une correction sans texte à rédiger de votre côté. Vous pouvez aussi préciser le sujet ou soumettre votre propre formulation. Il reçoit automatiquement les citations originales, jusqu’à trois documents du corpus, les références de l’article et les preuves consultées. Il distinguera fidélité au texte, faits établis et nuances non démontrées.</p><p class="muted">Modèle : ${esc(state.config.review_model||'à choisir dans Connexions & modèles')}. Appel OpenAI payant ; aucun appel Ibou ni recherche web automatique. Les URL indiquées sont consultées directement.</p><label>Contexte supplémentaire (facultatif)<textarea id="expert-context" rows="3" maxlength="12000" placeholder="Ex. La fuite désigne les Google leaks de 2024 ; usage désigne ici une utilisation technique, pas les clics des internautes.">${esc(last.user_context||'')}</textarea></label><label>Ta correction proposée (facultative : laisse vide pour une proposition du modèle)<textarea id="expert-proposal" rows="4" maxlength="12000">${esc(last.user_proposal||'')}</textarea></label><label>Sources supplémentaires (facultatives, une URL par ligne, 10 maximum)<textarea id="expert-urls" rows="3" placeholder="https://…"></textarea></label><div class="form-actions"><button class="button" id="expert-see-context">Voir le contexte du corpus</button><button class="button primary" id="expert-launch" ${state.job.status==='running'?'disabled':''}>Proposer une correction avec OpenAI</button></div><div id="expert-context-preview"></div>`;
 $('#claim-detail').prepend(panel);
 $('#expert-see-context').onclick=e=>action(async()=>{const c=await api('expert-context/'+id);const pre=document.createElement('pre');pre.textContent=JSON.stringify(c,null,2);$('#expert-context-preview').replaceChildren(pre)},e.currentTarget);
 $('#expert-launch').onclick=e=>action(async()=>{if(!state.config.review_model||!state.config.openai_key_set){$('#detail-dialog').close();go('settings');throw Error('Enregistrez la clé OpenAI et le modèle de révision approfondie.')}await api('expert-review',{id,context:$('#expert-context').value,proposal:$('#expert-proposal').value,urls:$('#expert-urls').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean)});pendingExpertClaim=id;$('#detail-dialog').close();notify('Révision approfondie lancée. Le rapport sera disponible en rouvrant cette affirmation.')},e.currentTarget);
 const reports=document.createElement('section');reports.innerHTML=(data.expert_reviews||[]).map(expertReport).join('');panel.before(reports);
 reports.onclick=e=>{const b=e.target.closest('[data-expert-patches]');if(b)action(async()=>{await api('expert-patches',{id:b.dataset.expertPatches});$('#detail-dialog').close();go('corrections');notify('Préparation de propositions locales. Aucune modification appliquée automatiquement.')},b)};
};
