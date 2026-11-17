'use strict';
const runButton=document.createElement('button');runButton.className='button primary';runButton.type='button';runButton.textContent='▶ Tout lancer';
$('.bulk-heading-actions').prepend(runButton);$('#new-content').classList.remove('primary');
const runDialog=document.createElement('dialog');document.body.append(runDialog);
runDialog.innerHTML=`<div class="dialog-heading"><div><div class="eyebrow">UN SEUL PARCOURS</div><h2>Lancer tout le traitement</h2></div><button type="button" class="icon-button close-dialog" aria-label="Fermer">×</button></div>
<p>Les contenus déjà importés sont conservés. Les étapes terminées sont réutilisées.</p>
<ol class="run-steps"><li>Tester réellement le modèle et les embeddings</li><li>Extraire les affirmations atomiques</li><li>Dédoublonner globalement les informations avec confirmation du LLM</li><li>Précontrôler le corpus et les sources citées avec le modèle</li><li>Rechercher uniquement les faits externes restants, dans le budget choisi (0 = sans Ibou)</li><li>Préparer les propositions de correction si demandé</li></ol>
<div id="run-summary" class="notice"></div>
<label>Plafond Ibou pour ce lancement (unités)<input id="run-cap" type="number" min="0" max="1000000" step="1" value="100" required></label>
<p class="muted">Arrêt automatique au plafond. Une reprise ouvre une nouvelle campagne avec ce plafond ; les consommations précédentes restent dans le journal.</p>
<label class="check-label"><input id="run-corrections" type="checkbox" checked> Préparer aussi les propositions de correction</label>
<label class="check-label"><input id="run-selection" type="checkbox" checked> Après extraction, me laisser choisir les affirmations avant de dépenser des unités Ibou</label>
<div class="notice">OpenAI peut facturer la génération et les embeddings dès le test initial. Les unités Ibou sont distinctes. Les propositions de correction restent à examiner et à appliquer manuellement ; les nouvelles versions pourront ensuite repasser par « Tout lancer ».</div>
<p id="run-error" class="danger" role="alert"></p><div class="form-actions"><button class="button" type="button" id="run-balance">Consulter le solde Ibou</button><button class="button primary" type="button" id="run-submit">Tout lancer avec ce plafond →</button></div><p id="run-balance-result" class="muted"></p>`;
runButton.onclick=()=>{
  if(!state)return;
  if(state.job.status==='running'){notify('Un traitement est déjà en cours.');return}
  if(!state.pages.length){notify('Importez d’abord vos contenus.');return}
  if(!state.config.model||(state.config.provider==='openai'&&!state.config.openai_key_set)){go('settings');notify('Renseignez le modèle et les clés, puis enregistrez les connexions.');return}
  const e=state.estimate;
  $('#run-summary').textContent=e.unprepared?`${state.pages.length} version(s) · ${e.unprepared} chunks à extraire. L’estimation précise Ibou sera calculée et consignée avant les recherches payantes. Le plafond ci-dessous s’applique dans tous les cas.`:`${e.unscreened||0} affirmations à précontrôler. ${e.search||0} recherches déjà identifiées : maximum ${e.maximum} u. Ce montant reste provisoire tant que le précontrôle est incomplet.`;
  $('#run-error').textContent='';runDialog.showModal();
};
$('#run-balance').onclick=e=>action(async()=>{$('#run-balance-result').textContent=(await api('test',{target:'ibou'})).message},e.currentTarget);
$('#run-submit').onclick=async()=>{
  const button=$('#run-submit');button.disabled=true;$('#run-error').textContent='';
  try{
    const value=$('#run-cap').value;const budget=Number(value);
    if(!value||!Number.isInteger(budget)||budget<0||budget>1000000)throw Error('Saisissez un plafond entier entre 0 et 1 000 000 unités.');
    await api('run-all',{budget,corrections:$('#run-corrections').checked,select_claims:$('#run-selection').checked});runDialog.close();go('overview');await refresh();notify('Parcours lancé : le test des modèles est la première étape.');
  }catch(e){$('#run-error').textContent=e.message}finally{button.disabled=false}
};
$('#test-llm').textContent='Enregistrer & lister les modèles';
const realTest=document.createElement('button');realTest.type='button';realTest.className='button';realTest.textContent='Tester réellement les modèles';realTest.id='test-real-models';$('#test-llm').after(realTest);
const testNote=document.createElement('p');testNote.className='muted';testNote.textContent='Ce test effectue une courte génération et un embedding si activé. Il peut consommer des crédits OpenAI. Le résultat s’affiche dans le bandeau et le journal.';realTest.after(testNote);
realTest.onclick=e=>action(async()=>{await saveSettings();await api('test-models',{});notify('Test réel lancé. Consultez le bandeau de progression.')},e.currentTarget);
const renderBeforeRun=render;
render=function(){
  renderBeforeRun();if(!state)return;
  const job=state.job;runButton.disabled=job.status==='running';
  runButton.textContent=job.status==='paused'||job.status==='error'?'▶ Reprendre le parcours':'▶ Tout lancer';
  $('#job-banner').hidden=job.status==='idle';
  const stages={deduplication:'Dédoublonnage global par LLM',connexion:'Test des connexions',embeddings:'Embeddings des contenus',extraction:'Extraction et dédoublonnage',estimation:'Estimation du budget',verification:'Vérification des affirmations',corrections:'Propositions de correction',import:'Import des contenus',prepare:'Préparation',test:'Test des modèles',all:'Parcours complet'};
  $('#job-progress').textContent=[stages[job.stage]||'',job.total?`${job.done} / ${job.total} éléments de cette étape`:'',job.status==='running'?'En cours':job.status==='completed'?'Terminé':'Interrompu · données conservées'].filter(Boolean).join(' — ');
};
