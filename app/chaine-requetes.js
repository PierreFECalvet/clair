'use strict';
/* Requêtes Ibou : requêtes par cadre (lecture, contrôle, édition), appels déjà faits, budget et lancement de M5. */

const Q={onglet:'requetes',offset:0,limite:40,f:{statut:'',kind:'',critique:'',repli:'0',q:''},data:null,edition:null,appels:null,aOffset:0,aF:{ancien:'0',statut:''},budgets:[]};
const requetesVue=$('#view-requetes');
const chipsCadre=c=>cadreHtml(c);
const controlesHtml=r=>{
  const k=r.checks||{},pb=r.problemes_lisibles||(k.problemes||[]).map(x=>typeof x==='string'?humain(x):(x.detail||String(x.code||'')));
  const score=k.score!=null?k.score:r.humanity;
  return `${k.ok===false||pb.length?`<span class="bd warn">${pb.length||1} problème(s)</span>`:k.ok?'<span class="bd ok">Contrôles réussis</span>':'<span class="bd mute">Non contrôlée</span>'}${score!=null?`<small>Naturel : ${Number(score).toFixed(2)}</small>`:''}${pb.length?`<ul class="rq-pb">${pb.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}${Object.keys(k.composantes||{}).length?`<details class="rq-comp"><summary>Détail du score de naturel</summary><div class="kvs">${lisible(k.composantes)}</div></details>`:''}`;
};
async function ouvrirRequetes(params={}){
  if(params.onglet==='budget'){Q.onglet='requetes'}
  if(params.statut!==undefined&&params.onglet!=='budget'){Q.f.statut=params.statut;Q.f.repli='0';Q.offset=0}
  if(params.onglet==='appels')Q.onglet='appels';
  squeletteRequetes();
  await chargerBudget();
  if(Q.onglet==='appels')await chargerAppels();else await chargerRequetes();
  if(params.onglet==='budget')$('#q-plafond')?.focus();
}
async function chargerBudget(){
  try{const d=await api('chaine/verifications?limite=1');Q.budgets=d.budgets||[]}catch{Q.budgets=[]}
  rendreBudget();
}
function rendreBudget(){
  const z=$('#q-budget');if(!z)return;
  const e=CH.etat||{},raison=e.prerequis?.m5,pret=e.reste?.m5?.a_faire||0,tard=e.reste?.m5?.plus_tard||0;
  const b=Q.budgets[0];
  z.innerHTML=`<div class="bud-champs"><label class="inline">Plafond (unités)<input type="number" id="q-plafond" min="1" max="1000000" value="${b?b.cap:100}"></label>
   ${btnE('Lancer M5','data-m5="1"'+(raison?' data-bloque="1"':''),'button primary')}
   <label class="inline"><input type="checkbox" id="q-plustard"> inclure la file « plus tard » (${nb(tard)})</label>
   <button type="button" class="button" id="q-solde"${CH.etat?.moteurs?.ibou?.cle?'':' disabled title="Clé Ibou manquante : renseignez-la dans Moteurs et réglages, onglet Connexions."'}>Consulter le solde</button><span class="muted" id="q-solde-r">${Q.solde!=null?esc(Q.solde):''}</span></div>
   <div class="bud-etat"><span><b>${nb(pret)}</b> requête(s) critique(s) prête(s)</span>${b?`<span>Dernier budget : <b>${nb(b.used)} / ${nb(b.cap)}</b> unités utilisées (${date(b.created)})</span>`:'<span>Aucun budget engagé.</span>'}
   ${raison?`<span class="bloque">M5 bloquée : ${esc(humain(raison))}</span>`:'<span class="ok">M5 peut partir : critiques d’abord, arrêt au plafond, reprise possible.</span>'}</div>`;
  appliquerOccupe();
}
function squeletteRequetes(){
  const f=Q.f,n=CH.etat?.compteurs?.m4?.en_attente_decomposition||0;
  requetesVue.innerHTML=`${avisOccupe()}
  <section class="card bud" id="q-budget" aria-label="Budget Ibou"></section>
  <div class="tabs onglets" role="tablist"><button type="button" role="tab" class="${Q.onglet==='requetes'?'selected':''}" aria-selected="${Q.onglet==='requetes'}" data-q-onglet="requetes">Requêtes par cadre</button><button type="button" role="tab" class="${Q.onglet==='appels'?'selected':''}" aria-selected="${Q.onglet==='appels'}" data-q-onglet="appels">Appels déjà faits</button></div>
  ${Q.onglet==='requetes'?`${n?`<div class="note"><b>${nb(n)} unité(s) n’ont pas de requête</b> : elles attendent leur décomposition (M1 faits avec un LLM). Sans fait ni cadre, aucune requête n’est générée ; le repli « Est-ce que l’unité… ? » est désactivé et M5 refuse de l’envoyer.</div>`:''}
  <div class="barre filtres"><input class="recherche" id="rq-q" type="search" placeholder="Rechercher dans les requêtes" aria-label="Rechercher une requête" value="${esc(f.q)}">
   ${selectF('statut','Statut',optionsDe('requete_statut',['prete','a_revoir','plus_tard','envoyee','abandonnee']),f.statut)}
   ${selectF('kind','Type',optionsDe('requete_kind',['ouverte','controle']),f.kind)}
   ${selectF('critique','Criticité',[['','Toutes'],['1','Critiques'],['0','Non critiques']],f.critique)}
   ${selectF('repli','Replis',[['0','Masqués'],['1','Seulement les replis'],['','Inclus']],f.repli)}</div>
  <section class="card"><div id="rq-liste"></div></section><div id="rq-pager"></div>`
  :`<div class="barre filtres">${selectF('ancien','Origine',[['0','Appels de M5'],['1','Ancien parcours (migrés)'],['','Tous']],Q.aF.ancien)}${selectF('astatut','Statut',[['','Tous'],...['success','cache','erreur','debit','solde_epuise','refuse','indisponible','incertain'].map(v=>[v,lib('appel',v)])],Q.aF.statut)}</div>
  <section class="card"><div id="ap-liste"></div></section><div id="ap-pager"></div>`}`;
  rendreBudget();
}
async function chargerRequetes(){
  const f=Q.f;
  Q.data=await api('chaine/requetes?'+qs({offset:Q.offset,limite:Q.limite,statut:f.statut,kind:f.kind,critique:f.critique,repli:f.repli,q:f.q}));
  rendreRequetes();
}
function ligneRequete(r){
  const ed=Q.edition&&Q.edition.id===r.id;
  const texte=ed?`<input class="rq-input" id="rq-edit" value="${esc(Q.edition.texte??r.text)}" maxlength="500" aria-label="Texte de la requête"><div class="rq-edit-b">${btnE('Enregistrer','data-rq-enreg="'+esc(r.id)+'"','button primary')}<button type="button" class="button" data-rq-annuler="1">Annuler</button><small>Mêmes contrôles qu’à la génération : 4 à 30 mots, accents, pas d’underscore ni de date ISO, une ou deux questions, sujet du cadre nommé.</small></div>${(Q.edition.problemes||[]).length?`<div class="rq-refus" role="alert"><b>Refusée par les contrôles, rien n’a changé :</b><ul>${Q.edition.problemes.map(p=>`<li>${esc(p)}</li>`).join('')}</ul></div>`:''}`
    :`<div class="rq-t">${esc(r.text)}</div>`;
  return `<tr class="${ed?'edition':''}" data-id="${esc(r.id)}"><td class="rq-texte">${texte}<div class="rq-cadre">${chipsCadre(r.cadre)}</div>${r.edited?'<small class="muted">Corrigée à la main</small>':''}</td>
   <td>${esc(libGabarit(r.template))}<small>Cadre issu de : ${esc(lib('origine_cadre',r.cadre?.origine))}</small>${r.cadre?.gabarit_brut&&r.cadre.gabarit_brut!==r.text?`<details class="rq-comp"><summary>Gabarit brut</summary><small>${esc(r.cadre.gabarit_brut)}</small></details>`:''}</td>
   <td class="num"><button type="button" class="lien" data-vue="unites" data-params='${esc(JSON.stringify({groupe:r.group_id}))}' title="Voir les unités couvertes">${nb(r.unites)}</button></td>
   <td>${controlesHtml(r)}</td>
   <td>${r.critical?'<span class="bd bad">Critique</span>':'<span class="bd mute">Non critique</span>'}<small>priorité ${r.priority!=null?Number(r.priority).toFixed(2).replace('.',','):'—'}</small></td>
   <td>${bd('requete_statut',r.status)}${r.envoyable?'<small class="ok">Partira au prochain lancement de M5</small>':(r.status==='a_revoir'?`<small class="rouge" title="${esc(r.raison_non_envoyable||'')}">Ne partira pas tant que vous ne l’avez pas corrigée (voir les contrôles).</small>`:`<small>${esc(humain(r.raison_non_envoyable||'Non envoyable'))}</small>`)}${r.appels?`<small>${nb(r.appels)} appel(s)</small>`:''}</td>
   <td>${ed?'':btnE('Modifier','data-rq-edit="'+esc(r.id)+'"','button')}</td></tr>`;
}
function rendreRequetes(){
  const d=Q.data;if(!d||!$('#rq-liste'))return;
  $('#rq-liste').innerHTML=d.requetes.length?`<div class="table-wrap"><table class="dense requetes"><thead><tr><th>Requête et cadre</th><th>Gabarit</th><th class="num">Unités</th><th>Contrôles</th><th>Priorité</th><th>Statut</th><th></th></tr></thead><tbody>${d.requetes.map(ligneRequete).join('')}</tbody></table></div>`:videHtml('Aucune requête dans cette vue','Les requêtes sont générées par M4 pour les unités qui ont un cadre (faits). Changez les filtres ou lancez M4.');
  $('#rq-pager').innerHTML=pagerHtml('requetes',d.total,d.offset,d.limite);
  appliquerOccupe();
  if(Q.edition)$('#rq-edit')?.focus();
}
async function chargerAppels(){
  Q.appels=await api('chaine/verifications?'+qs({offset:Q.aOffset,limite:40,ancien:Q.aF.ancien,statut:Q.aF.statut}));
  Q.budgets=Q.appels.budgets||Q.budgets;rendreBudget();rendreAppels();
}
function rendreAppels(){
  const d=Q.appels;if(!d||!$('#ap-liste'))return;
  $('#ap-liste').innerHTML=d.appels.length?`<div class="table-wrap"><table class="dense lignes"><thead><tr><th>Date</th><th>Requête envoyée</th><th>Appel</th><th class="num">Unités</th><th class="num">Résultats</th><th class="num">Preuves retenues</th><th>Origine</th></tr></thead><tbody>${d.appels.map(a=>`<tr tabindex="0" role="button" data-appel="${esc(a.id)}"><td>${esc(date(a.created))}</td><td class="texte"><span>${esc(courtTexte(a.query,170))}</span>${a.critical?'<small><b class="rouge">requête critique</b></small>':''}</td><td>/${esc(a.endpoint)}${a.max_results?` · ${a.max_results}`:''}<br>${bd('appel',a.status)}${a.http_status?`<small>HTTP ${a.http_status}</small>`:''}${a.duration_ms!=null?`<small>${fmtMs(a.duration_ms)}</small>`:''}</td><td class="num">${nb(a.units)}${a.units_remaining!=null?`<small>reste ${nb(a.units_remaining)}</small>`:''}</td><td class="num">${a.resultats!=null?nb(a.resultats):'—'}</td><td class="num">${a.preuves?`${nb(a.preuves_retenues)} / ${nb(a.preuves)}`:'—'}</td><td>${a.legacy_call_id?'Ancien parcours':'M5'}</td></tr>`).join('')}</tbody></table></div>`:videHtml('Aucun appel dans cette vue','Les appels Ibou apparaissent ici avec leurs résultats, les preuves retenues ou écartées et le verdict.');
  $('#ap-pager').innerHTML=pagerHtml('requetes',d.total,d.offset,d.limite);
}
const dialogueAppel=el('dialog','volet large');dialogueAppel.id='dialogue-appel';document.body.append(dialogueAppel);
dialogueAppel.addEventListener('click',e=>{if(e.target===dialogueAppel||e.target.closest('.close-dialog')||e.target.closest('[data-vue]'))dialogueAppel.close();const u=e.target.closest('[data-unite]');if(u){dialogueAppel.close();ouvrirUnite(u.dataset.unite)}});
async function ouvrirAppel(id){
  dialogueAppel.innerHTML='<div class="volet-tete"><h2>Chargement…</h2><button class="icon-button close-dialog" aria-label="Fermer">×</button></div>';
  if(!dialogueAppel.open)dialogueAppel.showModal();
  let d;
  try{d=await api('chaine/appel/'+encodeURIComponent(id))}catch(e){dialogueAppel.close();notify(e.message,true);return}
  const a=d.appel,r=d.requete;
  const preuve=p=>`<div class="preuve ${p.kept?'retenue':'ecartee'}"><div class="preuve-t">${/^https?:/.test(p.url||'')?`<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.title||p.domain||p.url)}</a>`:esc(p.title||p.domain||'—')} ${p.kept?'<span class="bd info">Passage candidat</span>':'<span class="bd bad">Écartée</span>'} ${!p.kept&&p.reject_reason?`<b class="motif-rejet">${esc(lib('rejet',p.reject_reason))}</b>`:''} ${p.nli?bd('nli',p.nli):''}</div><small>${esc(p.domain||'')}${p.page_date?' · '+esc(jour(p.page_date)):''}${p.relevance!=null?' · pertinence '+Number(p.relevance).toFixed(2):''}${p.nli_score!=null?' · NLI '+Number(p.nli_score).toFixed(2):''}</small>${p.text?`<blockquote>${esc(courtTexte(p.text,700))}</blockquote>`:''}${(p.selections||[]).map(x=>`<div class="bloc-g"><b>${esc(x.affirmation||'')}</b><p><span class="bd ${x.retenue?'ok':'warn'}">${x.retenue?'Preuve retenue':'Non retenue'} : ${esc(x.position)}</span> ${esc(x.motif||'')}</p>${x.citation?`<blockquote>${esc(x.citation)}</blockquote>`:''}</div>`).join('')}</div>`;
  const ancien=!a.query_id;
  dialogueAppel.innerHTML=`<div class="volet-tete"><div><div class="eyebrow">APPEL IBOU · ${esc(date(a.created))}</div><h2>${esc(courtTexte(a.query,140))}</h2></div><button class="icon-button close-dialog" aria-label="Fermer">×</button></div>
   <div class="volet-badges">${bd('appel',a.status)} <span class="bd mute">${nb(a.units)} unité(s) Ibou</span> ${a.http_status?`<span class="bd mute">HTTP ${a.http_status}</span>`:''} ${d.resultats.length?`<span class="bd mute">${nb(d.resultats.length)} résultat(s)</span>`:''} <span class="bd mute">${ancien?'Ancien parcours':'M5'}</span></div>
   <div class="volet-corps">
   ${r?`<section><h3>Requête et cadre</h3><div class="rq-t">${esc(r.text)}</div><div class="rq-cadre">${cadreHtml(r.cadre)}</div><small>${esc(libGabarit(r.template))}${r.critical?' · <b class="rouge">critique</b>':''}</small></section>`
     :`<div class="explication"><strong>Appel de l’ancien parcours</strong><p>Il n’est rattaché à aucune requête de la chaîne : ses preuves ne sont pas ventilées en retenues et écartées. Son contenu brut est dans l’<button type="button" class="lien" data-vue="history">historique des appels</button>.</p></div>`}
   ${d.unites.length?`<section><h3>Unités vérifiées et verdict <span class="vs-n">${d.unites.length}</span></h3>${d.unites.map(u=>`<div class="bloc-g"><button type="button" class="lien texte-b" data-unite="${esc(u.id)}">${esc(courtTexte(u.text,200))}</button><small>${u.verdict?bd('verdict',u.verdict):'<span class="bd mute">Pas de verdict</span>'} ${bd('statut',u.status||'aucun')} · ${esc(courtTexte(u.title||'',50))}</small></div>`).join('')}</section>`:''}
   ${r||d.preuves.length?`<section><h3>Passages de cet appel <span class="vs-n info">${d.retenues} candidat(s)</span> <span class="vs-n">${d.ecartees} écartée(s)</span></h3>${d.preuves.length?`<div class="preuves">${d.preuves.map(preuve).join('')}</div>`:'<p class="muted">Aucune preuve enregistrée pour cet appel (résultats écartés avant l’analyse, ou appel repris du cache).</p>'}</section>`:''}
   ${d.resultats.length&&!d.preuves.length?`<section><h3>Résultats renvoyés par Ibou</h3><ul class="resultats">${d.resultats.map(x=>`<li>${/^https?:/.test(x.url||'')?`<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.titre||x.url)}</a>`:esc(x.titre||'—')} <small>${esc(x.domaine||'')}</small></li>`).join('')}</ul></section>`:''}
   </div>`;
}
requetesVue.addEventListener('click',e=>{
  const o=e.target.closest('[data-q-onglet]');if(o){Q.onglet=o.dataset.qOnglet;Q.offset=0;Q.aOffset=0;quiet(()=>ouvrirRequetes({}));return}
  const ed=e.target.closest('[data-rq-edit]');if(ed){const r=Q.data.requetes.find(x=>x.id===ed.dataset.rqEdit);Q.edition={id:r.id,texte:r.text,problemes:[]};rendreRequetes();return}
  if(e.target.closest('[data-rq-annuler]')){Q.edition=null;rendreRequetes();return}
  const en=e.target.closest('[data-rq-enreg]');
  if(en){
    const texte=$('#rq-edit').value.trim();Q.edition.texte=texte;
    ecrire(async()=>{
      const r=await api('chaine/requete',{id:en.dataset.rqEnreg,texte});
      if(!r.ok){Q.edition.problemes=r.problemes_lisibles||r.problemes||['Requête refusée par les contrôles.'];rendreRequetes();notify('Requête refusée par les contrôles : rien n’a changé.',true);return}
      Q.edition=null;notify('Requête enregistrée après contrôle : elle est prête et partira telle quelle.');
      await chargerRequetes();CH.etat=await api('chaine/etat');await rafraichir(false);
    },en);return;
  }
  const m5=e.target.closest('[data-m5]');
  if(m5)ecrire(async()=>{
    const budget=Number($('#q-plafond').value);
    if(!Number.isInteger(budget)||budget<=0)throw Error('Indiquez un plafond entier supérieur à 0 : c’est le nombre maximal d’unités Ibou dépensées.');
    await api('chaine/lancer',{etape:'m5',budget,plus_tard:!!$('#q-plustard').checked});
    notify(`M5 lancée avec un plafond de ${nb(budget)} unité(s) : critiques d’abord, arrêt au plafond.`);await rafraichir(true);
  },m5);
  if(e.target.id==='q-solde')quiet(async()=>{
    e.target.disabled=true;
    try{const r=await api('chaine/solde');Q.solde=r.disponible?`Solde Ibou : ${nb(r.solde)} unité(s) disponibles (consulté à ${new Date().toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit'})}).`:r.raison;$('#q-solde-r').textContent=Q.solde}
    finally{e.target.disabled=false}
  });
  const ap=e.target.closest('tr[data-appel]');if(ap)ouvrirAppel(ap.dataset.appel);
});
requetesVue.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches('tr[data-appel]'))ouvrirAppel(e.target.dataset.appel);if(e.key==='Escape'&&Q.edition&&e.target.id==='rq-edit'){Q.edition=null;rendreRequetes()}if(e.key==='Enter'&&e.target.id==='rq-edit')$('[data-rq-enreg]')?.click()});
requetesVue.addEventListener('change',e=>{
  const f=e.target.closest('[data-filtre]');if(!f)return;
  const nom=f.dataset.filtre;
  if(nom==='ancien'||nom==='astatut'){Q.aF[nom==='astatut'?'statut':'ancien']=f.value;Q.aOffset=0;quiet(chargerAppels)}
  else{Q.f[nom]=f.value;Q.offset=0;quiet(chargerRequetes)}
});
requetesVue.addEventListener('input',debounce(e=>{if(e.target.id==='rq-q'){Q.f.q=e.target.value.trim();Q.offset=0;quiet(chargerRequetes)}},300));
enregistrer('requetes',{ouvrir:ouvrirRequetes,
  page:pas=>{if(Q.onglet==='appels'){Q.aOffset=Math.max(0,Q.aOffset+pas*40);quiet(chargerAppels)}else{Q.offset=Math.max(0,Q.offset+pas*Q.limite);quiet(chargerRequetes)}},
  actualiser:async()=>{if(surChamp(requetesVue)||Q.edition||dialogueAppel.open)return;rendreBudget();if(Q.onglet==='appels')await chargerAppels();else await chargerRequetes()}});
