'use strict';
/* Revue M6 : une proposition par carte, six zones (contexte, avant/après, motif, preuves, paternité, décision),
   raccourcis clavier, compteur de progression, export Excel. */

const R={statut:'en_attente',motif:'',page:'',titrePage:'',unite:'',motifs:{},cartes:[],idx:0,total:0,bilan:null,session:0,ignorees:new Set(),edition:false,erreur:null,forcer:false,propager:false,charge:false,fin:false};
const revueVue=$('#view-revue');
const DECIDABLES=['proposee','a_rediger','reportee'];
const STATUTS_REVUE=[['en_attente','En attente (proposées, à rédiger, reportées)'],['proposee','Proposées'],['a_rediger','À rédiger à la main'],['reportee','Reportées'],['appliquee','Appliquées'],['rejetee','Rejetées'],['archive','Archivées'],['','Toutes']];
const carteCourante=()=>R.cartes[R.idx]||null;
const decidable=c=>!!c&&DECIDABLES.includes(c.statut);

async function ouvrirRevue(params={}){
  if(params.statut)R.statut=params.statut;
  const filtres=['motif','page','unite'].some(k=>k in params);
  if(filtres||params.statut){R.motif=params.motif||'';R.page=params.page||'';R.titrePage=params.titrePage||'';R.unite=params.unite||''}
  R.edition=false;R.erreur=null;
  if(params.id){
    const c=await api('chaine/proposition/'+encodeURIComponent(params.id));
    R.cartes=[c,...R.cartes.filter(x=>x.id!==c.id)];R.idx=0;
  }else if(params.statut||filtres||!R.cartes.length){R.cartes=[];R.idx=0;R.fin=false;R.ignorees=new Set()}
  squeletteRevue();
  if(!R.cartes.length)await chargerLot();else {await chargerLot(true);}
  rendreRevue();
}
async function chargerLot(silencieux=false){
  const enAttente=R.statut==='en_attente';
  const offset=enAttente?0:R.cartes.length;
  const d=await api('chaine/propositions?'+qs({statut:R.statut==='toutes'?'':R.statut,offset,limite:enAttente?40:25,motif:R.motif,page:R.page,unite:R.unite}));
  R.bilan=d.bilan;R.total=d.total;R.motifs=d.motifs||{};rendreFiltreMotif();
  const vus=new Set(R.cartes.map(c=>c.id));
  const nouveaux=d.cartes.filter(c=>!vus.has(c.id)&&!R.ignorees.has(c.id));
  R.cartes=R.cartes.concat(nouveaux);
  R.fin=!nouveaux.length&&(enAttente?true:R.cartes.length>=d.total);
  if(!silencieux&&R.idx>=R.cartes.length)R.idx=Math.max(0,R.cartes.length-1);
}
function squeletteRevue(){
  revueVue.innerHTML=`${avisOccupe()}
  <div class="rv-barre"><label class="filtre">Afficher<select id="rv-statut">${STATUTS_REVUE.map(([v,t])=>`<option value="${v}"${v===R.statut||(v===''&&R.statut==='toutes')?' selected':''}>${esc(t)}</option>`).join('')}</select></label>
   <label class="filtre">Motif<select id="rv-motif"></select></label>
   <span id="rv-puces"></span>
   <div class="rv-progression" id="rv-progression"></div>
   <span class="espace"></span><a class="button" href="/api/chaine/export.xlsx" download>↓ Export Excel par URL</a></div>
  <div id="rv-suite"></div><div id="rv-corps" class="rv-corps"></div>`;
}
function rendreFiltreMotif(){
  const sel=$('#rv-motif');if(!sel)return;
  const total=Object.values(R.motifs).reduce((a,b)=>a+b,0);
  const codes=Object.keys(R.motifs);if(R.motif&&!codes.includes(R.motif))codes.push(R.motif);
  sel.innerHTML=`<option value="">Tous les motifs (${nb(total)})</option>`+codes.map(k=>`<option value="${esc(k)}"${k===R.motif?' selected':''}>${esc(lib('motif',k))} (${nb(R.motifs[k]||0)})</option>`).join('');
  const p=[];
  if(R.page)p.push(`<span class="puce">Page : ${esc(courtTexte(R.titrePage||'page choisie',36))} <button type="button" class="lien" data-rv-retirer="page" aria-label="Retirer le filtre de page">×</button></span>`);
  if(R.unite)p.push(`<span class="puce">Une seule unité <button type="button" class="lien" data-rv-retirer="unite" aria-label="Retirer le filtre d’unité">×</button></span>`);
  $('#rv-puces').innerHTML=p.join(' ');
}
const progressionHtml=()=>{
  const b=R.bilan?.statuts||{};
  const att=(b.proposee||0)+(b.a_rediger||0)+(b.reportee||0);
  const dec=(b.acceptee||0)+(b.appliquee||0)+(b.modifiee||0)+(b.rejetee||0)+(b.archive||0);
  return `<progress max="${Math.max(1,att+dec)}" value="${dec}" aria-label="Part des propositions décidées"></progress><span><b>${nb(dec)}</b> décidée(s) · <b>${nb(att)}</b> en attente · <b>${nb(R.session)}</b> décidée(s) dans cette séance</span>`;
};
const diffAvant=diff=>(diff||[]).map(s=>s.op==='egal'?esc(s.avant??s.texte):s.op==='supprime'?`<del>${esc(s.texte)}</del>`:'').join('');
const diffApres=diff=>(diff||[]).map(s=>s.op==='egal'?esc(s.texte):s.op==='ajoute'?`<ins>${esc(s.texte)}</ins>`:'').join('');
const PROBLEMES_REVUE={faits_absents:'Faits indisponibles pour cette unité',valeur_corrigee_absente:'Valeur corrigée absente',nli:'Le contrôle NLI ne confirme pas le texte',budget:'Différence au-delà du budget autorisé',diff_ratio:'Différence au-delà du budget autorisé'};
const detailProbleme=t=>String(t||'').replace(/^(\d+(?:\.\d+)?) de mots changés pour un budget de (\d+(?:\.\d+)?)\.?$/,(m,a,b)=>`${Math.round(100*a)} % des mots changés, au-delà du budget de ${Math.round(100*b)} %.`);
const problemesHtml=l=>(l||[]).map(p=>typeof p==='string'?`<li>${esc(humain(p))}</li>`:`<li>${esc(detailProbleme(humain(p.detail||p.message||PROBLEMES_REVUE[p.code]||String(p.code||'').replace(/_/g,' '))))}</li>`).join('');
function contexteHtml(c){
  const x=c.contexte||{},t=x.paragraphe||'';
  if(!t)return '<p class="muted">Paragraphe indisponible.</p>';
  const a=x.phrase_debut,b=x.phrase_fin;
  if(a==null||b==null||b<=a||b>t.length)return `<p class="para">${esc(t)}</p>`;
  return `<p class="para">${esc(t.slice(0,a))}<mark>${esc(t.slice(a,b))}</mark>${esc(t.slice(b))}</p>`;
}
function motifHtml(c){
  const m=c.motif||{},a=m.ancrage||{},k=c.controle||{};
  const ar=c.avant_apres||{};
  const budget=ar.budget!=null?` · budget ${Math.round(100*ar.budget)} %`:'';
  const l=[`<span class="rv-motif-t">${bd('motif',m.code)}</span>`,
    m.libelle_origine&&m.libelle_origine!==m.libelle?`<span>origine : ${esc(m.libelle_origine)}</span>`:'',
    a.date?`<span>Ancrage d’origine : <b>${esc(a.date_fr||jour(a.date))}</b> (${esc(lib('ancre',a.type))})</span>`:'<span class="muted">Aucune date d’ancrage connue</span>',
    ar.methode?`<span>Méthode : ${esc(lib('methode',ar.methode))}${ar.diff_ratio!=null?` · différence ${Math.round(100*ar.diff_ratio)} %${budget}`:''}</span>`:'',
    `<span>Criticité <b>${c.criticite!=null?Number(c.criticite).toFixed(2).replace('.',','):'—'}</b></span>`,
    k.ok===false?'<span class="bd warn">Contrôle non satisfait</span>':k.ok?'<span class="bd ok">Contrôle satisfait</span>':''];
  const faits=c.faits||{};
  const texteFait=f=>{
    if(typeof f!=='object'||!f)return String(f??'');
    if(f.avant!=null||f.apres!=null)return f.avant?`${f.avant} → ${f.apres}`:`ajout : ${f.apres}`;
    return f.verbalisation||[f.sujet||f.subject,libPredicat(f.predicat||f.predicate),f.objet||f.object].filter(Boolean).join(' · ');
  };
  const fl=(liste,titre)=>(liste||[]).length?`<span class="rv-faits"><i>${titre}</i> ${liste.map(f=>esc(courtTexte(texteFait(f),160))).join(' ; ')}</span>`:'';
  return `<div class="rv-motif-l">${l.join('')}</div>${fl(faits.modifies,'Faits modifiés')}${fl(faits.conserves,'Faits conservés')}${(k.problemes||[]).length?`<ul class="rv-pb">${problemesHtml(k.problemes)}</ul>`:''}`;
}
function preuvesHtml(c){
  const l=c.preuves||[];
  if(!l.length)return '<p class="muted">Aucune preuve associée à cette proposition.</p>';
  return `<ul class="rv-preuves">${l.map(p=>{
    // format actuel : verdict_nli = label NLI, score_nli, contribution ; ancien format : verdict_nli = rôle de la source
    const label=LIB.nli[p.verdict_nli]?p.verdict_nli:null,role=p.contribution||(!label&&p.verdict_nli?p.verdict_nli:null);
    const nli=label?`${bd('nli',label)}${p.score_nli!=null?` <small>${Number(p.score_nli).toFixed(2).replace('.',',')}</small>`:''}`:'';
    const lienInterne=p.url&&!/^https?:/.test(p.url);
    return `<li><div class="rv-p-t">${p.url&&/^https?:/.test(p.url)?`<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.domaine||p.url)}</a>`:esc(p.domaine||(lienInterne?'Page du site':'—'))} ${nli} ${role?`<span class="bd info">${esc(lib('contribution',role))}</span>`:''}</div><small>${p.date?esc(jour(p.date)):'date inconnue'}${p.origine?' · '+esc(lib('origine_preuve',p.origine)):''}</small>${p.passage&&!/^\s*[{[]/.test(p.passage)?`<blockquote>${esc(courtTexte(p.passage,420))}</blockquote>`:''}</li>`}).join('')}</ul>`;
}
function paterniteHtml(c){
  const p=c.paternite||{};
  if(!p.groupe)return '<p class="muted">Cette unité n’appartient à aucun groupe.</p>';
  const autres=(p.autres_pages||[]).filter(x=>x.unit_id!==c.unit_id);
  return `<dl class="rv-pat"><dt>Page parente</dt><dd>${p.est_parente?'<b>Cette page</b> est parente':esc(p.page_parente?.titre||p.page_parente?.url||'—')}</dd>
  <dt>Critère décisif</dt><dd>${esc(lib('paternite',p.critere))}${p.humain?' (vous)':''}</dd>
  <dt>Autres pages touchées</dt><dd>${autres.length?`<ul>${autres.map(x=>`<li>${esc(courtTexte(x.titre||x.url,50))} ${x.touchee?'<span class="bd ok">touchée</span>':x.statut?bd('proposition',x.statut):'<span class="bd mute">sans proposition</span>'}</li>`).join('')}</ul>`:'aucune'}</dd></dl>`;
}
function decisionHtml(c){
  const d=c.decision_detail||{},p=c.paternite||{},enfants=(p.enfants||[]).length;
  const bloque=!decidable(c);
  const dis=bloque?' data-bloque="1"':'';
  const aucunApres=!c.avant_apres?.apres;
  const arch=c.motif?.ancrage?.date_fr?` « à date du ${c.motif.ancrage.date_fr} »`:'';
  return `<div class="rv-decision">
   ${bloque?`<div class="rv-deja">Déjà traitée : ${bd('proposition',c.statut)}${d.note?` — ${esc(d.note)}`:''}${d.version_appliquee?' · nouvelle version locale de la page créée':''}${d.date?` · le ${esc(date(d.date))}`:''}</div>`:''}
   <div class="rv-opts"><label class="rv-note">Note (facultative)<input id="rv-note" maxlength="300" placeholder="Pourquoi cette décision"${bloque?' disabled':''}></label>
    ${enfants?`<label class="check-label"><input type="checkbox" id="rv-propager" ${R.propager?'checked':''}${bloque?' disabled':''}> Appliquer aussi aux ${nb(enfants)} unité(s) enfant(s)</label>`:'<span class="muted rv-sans-enfant">Aucune unité enfant : pas de propagation.</span>'}</div>
   <div class="rv-boutons">
    ${btnE('<u>A</u>ccepter',`data-decision="accepter"${dis}${aucunApres?' data-bloque="1"':''}`,'button primary')}
    ${btnE('<u>M</u>odifier',`data-decision="modifier"${dis}`)}
    ${btnE('<u>R</u>ejeter',`data-decision="rejeter"${dis}`)}
    ${btnE('Archive (<u>h</u>)',`data-decision="archive"${dis} title="Marquer comme archive${esc(arch)} : l’unité devient historique"`)}
    ${btnE('Re<u>p</u>orter',`data-decision="reporter"${dis}`)}
   </div></div>`;
}
function carteHtml(c){
  const ar=c.avant_apres||{},n=R.cartes.length;
  const erreur=R.erreur&&R.erreur.id===c.id?R.erreur:null;
  const apres=R.edition?`<textarea id="rv-texte" rows="5" aria-label="Texte final">${esc(ar.apres??ar.avant??'')}</textarea><div class="rv-edit-b">${btnE('Appliquer la modification','data-decision="valider-edition"','button primary')}<button type="button" class="button" data-decision="annuler-edition">Annuler</button><small>Ctrl + Entrée applique, Échap annule. Le texte est recontrôlé (NLI, budget de différence).</small></div>`
    :ar.apres!=null?`<p class="txt">${diffApres(ar.diff)}</p>`
    :`<div class="rv-aucun"><b>Aucun texte proposé.</b><p>${esc((c.controle?.problemes||[]).map(p=>p.detail||p.code).join(' ; ')||'Le texte est à rédiger à la main.')}</p><p>Utilisez « Modifier » pour saisir la phrase finale.</p></div>`;
  return `<article class="rv-carte" aria-label="Proposition ${R.idx+1} sur ${n}">
   <div class="rv-tete"><div class="rv-titre"><span class="rv-pos">${R.idx+1} / ${nb(n)}${R.fin?'':'+'}</span> ${bd('proposition',c.statut)}
     <strong>${esc(courtTexte(c.page?.titre||c.page?.url,80))}</strong> <small class="mono">${esc(courtTexte(c.page?.url,70))}</small></div>
    <div class="rv-nav"><button type="button" class="button" data-nav="-1" aria-label="Proposition précédente"${R.idx?'':' disabled'}>←</button><button type="button" class="button" data-nav="1" aria-label="Proposition suivante"${R.idx<n-1||!R.fin?'':' disabled'}>→</button>
     <button type="button" class="lien" data-unite="${esc(c.unit_id)}">Détail de l’unité</button></div></div>
   <section class="rv-z rv-motif" aria-label="Motif"><h3>Motif</h3>${motifHtml(c)}</section>
   <div class="rv-ba"><section class="rv-z rv-avant" aria-label="Avant"><h3>Avant</h3><p class="txt">${ar.avant!=null?(ar.apres!=null?diffAvant(ar.diff):esc(ar.avant)):'—'}</p></section>
    <section class="rv-z rv-apres${R.edition||R.erreur?' edition':''}" aria-label="Après"><h3>Après${R.edition?' (modification)':''}</h3>${apres}
     ${erreur?`<div class="rv-erreur" role="alert"><b>${esc(erreur.raison==='controle_echoue'?'Le contrôle refuse ce texte':erreur.raison==='application_impossible'?'Application impossible : passage introuvable ou ambigu dans la page':'Décision non appliquée')}</b>${erreur.controle?.problemes?.length?`<ul>${problemesHtml(erreur.controle.problemes)}</ul>`:''}${erreur.raison==='controle_echoue'?`<label class="check-label"><input type="checkbox" id="rv-forcer"${R.forcer?' checked':''}> Appliquer malgré tout (forcer)</label>`:''}</div>`:''}</section></div>
   <div class="rv-bas"><section class="rv-z rv-contexte" aria-label="Contexte"><h3>Contexte</h3>${contexteHtml(c)}</section>
    <section class="rv-z" aria-label="Preuves"><h3>Preuves <span class="vs-n">${(c.preuves||[]).length}</span></h3>${preuvesHtml(c)}</section>
    <section class="rv-z" aria-label="Paternité"><h3>Paternité</h3>${paterniteHtml(c)}</section></div>
   ${decisionHtml(c)}</article>`;
}
function rendreRevue(){
  if(!$('#rv-corps'))return;
  $('#rv-progression').innerHTML=progressionHtml();
  const e=CH.etat?.reste?.['m1.unites']?.a_faire||0;
  $('#rv-suite').innerHTML=e?`<div class="notice suite"><span><b>${nb(e)} version(s) de page</b> (créées par une décision acceptée ou un import) attendent leur décomposition en unités (M1 unités), puis la suite de la chaîne. Le site d’origine n’est jamais modifié.${CH.etat.prerequis?.['m1.unites']?' Bloquée : '+esc(CH.etat.prerequis['m1.unites']):''}</span>${btnE('Lancer M1 unités','data-lancer="m1.unites"')}</div>`:'';
  const c=carteCourante();
  $('#rv-corps').innerHTML=c?carteHtml(c):videHtml(R.statut==='en_attente'?'Plus rien à revoir':'Aucune proposition',R.statut==='en_attente'?'Toutes les propositions ont une décision, ou ont été reportées pendant cette séance. Changez le filtre pour revoir les reportées.':'Aucune carte ne correspond à ce filtre.');
  appliquerOccupe();
  if(R.edition)$('#rv-texte')?.focus();
  ajusterCarte();
  const m=$('#rv-corps .rv-contexte mark'),zone=m?.closest('.rv-z');
  if(m&&zone)zone.scrollTop=Math.max(0,m.offsetTop-zone.offsetTop-40);
}
/* La carte occupe exactement la hauteur restante de la fenêtre : les zones longues défilent à l'intérieur. */
function ajusterCarte(){
  const carte=$('#rv-corps .rv-carte');if(!carte||revueVue.hidden)return;
  const haut=carte.getBoundingClientRect().top+window.scrollY;
  const dispo=window.innerHeight-haut-14;
  carte.style.height=Math.max(520,dispo)+'px';
}
window.addEventListener('resize',debounce(ajusterCarte,100));
async function allerA(pas){
  if(R.edition&&!confirm('Abandonner la modification en cours ?'))return;
  R.edition=false;R.erreur=null;R.forcer=false;
  const i=R.idx+pas;
  if(i<0)return;
  if(i>=R.cartes.length-3&&!R.fin)try{await chargerLot(true)}catch(e){notify(e.message,true)}
  R.idx=Math.min(i,Math.max(0,R.cartes.length-1));
  rendreRevue();
}
async function decider(decision){
  const c=carteCourante();
  if(!c)return;
  if(decision==='modifier'){if(!decidable(c))return;R.edition=true;R.erreur=null;rendreRevue();return}
  if(decision==='annuler-edition'){R.edition=false;R.erreur=null;rendreRevue();return}
  let action=decision,texte;
  if(decision==='valider-edition'){action='modifier';texte=$('#rv-texte').value.trim();if(!texte){notify('Saisissez le texte final.',true);return}}
  if(!decidable(c))return;
  if(action==='accepter'&&!c.avant_apres?.apres){notify('Rien à accepter : utilisez « Modifier » pour rédiger la phrase.',true);return}
  const corps={id:c.id,decision:action,note:$('#rv-note')?.value.trim()||'',propager:!!$('#rv-propager')?.checked};
  if(texte!=null)corps.texte=texte;
  if($('#rv-forcer')?.checked)corps.forcer=true;
  R.propager=corps.propager;
  const r=await ecrire(()=>api('chaine/decision',corps));
  if(!r)return;
  if(r.ok===false){R.erreur=Object.assign({id:c.id},r);rendreRevue();notify(r.raison==='controle_echoue'?'Le contrôle refuse le texte : rien n’a été appliqué.':'Décision non appliquée : '+(r.raison||'raison inconnue'),true);return}
  R.erreur=null;R.edition=false;R.forcer=false;R.session++;
  const prop=(r.propagees||[]).length,ign=(r.ignorees||[]).length;
  notify(`${lib('decision',action)} : ${lib('proposition',r.status).toLowerCase()}${r.version_id?'. Nouvelle version locale créée':''}${prop?` · ${prop} unité(s) enfant(s) traitée(s)`:''}${ign?` · ${ign} ignorée(s)`:''}.`);
  const traitees=new Set([c.id,...(r.propagees||[]).map(x=>x.id||x.proposal_id||x)]);
  if(action==='reporter')R.ignorees.add(c.id);
  R.cartes=R.cartes.filter(x=>!traitees.has(x.id));
  if(R.idx>=R.cartes.length)R.idx=Math.max(0,R.cartes.length-1);
  if(R.cartes.length<4&&!R.fin)try{await chargerLot()}catch{}
  try{const d=await api('chaine/propositions?limite=1&statut=en_attente');R.bilan=d.bilan;CH.etat=await api('chaine/etat');await rafraichir(false)}catch{}
  rendreRevue();
}
revueVue.addEventListener('click',e=>{
  const x=e.target.closest('[data-rv-retirer]');if(x){R[x.dataset.rvRetirer]='';if(x.dataset.rvRetirer==='page')R.titrePage='';rechargerRevue();return}
  const d=e.target.closest('[data-decision]');if(d){decider(d.dataset.decision);return}
  const n=e.target.closest('[data-nav]');if(n){allerA(Number(n.dataset.nav));return}
  const u=e.target.closest('[data-unite]');if(u)ouvrirUnite(u.dataset.unite);
});
const rechargerRevue=()=>{R.cartes=[];R.idx=0;R.fin=false;R.edition=false;R.erreur=null;R.ignorees=new Set();quiet(async()=>{await chargerLot();rendreRevue()})};
revueVue.addEventListener('change',e=>{
  if(e.target.id==='rv-statut'){R.statut=e.target.value||'toutes';rechargerRevue()}
  if(e.target.id==='rv-motif'){R.motif=e.target.value;rechargerRevue()}
  if(e.target.id==='rv-forcer')R.forcer=e.target.checked;
});
document.addEventListener('keydown',e=>{
  if(CH.vue!=='revue'||volet.open||e.altKey||e.metaKey)return;
  const champ=e.target.matches('input,textarea,select');
  if(R.edition&&e.target.id==='rv-texte'){
    if(e.key==='Enter'&&e.ctrlKey){e.preventDefault();decider('valider-edition')}
    else if(e.key==='Escape'){e.preventDefault();decider('annuler-edition')}
    return;
  }
  if(champ||e.ctrlKey)return;
  const k=e.key.toLowerCase();
  const map={a:'accepter',m:'modifier',r:'rejeter',h:'archive',p:'reporter'};
  if(map[k]){e.preventDefault();if(!CH.occupe)decider(map[k]);else notify(MSG_OCCUPE,true)}
  else if(e.key==='ArrowRight'){e.preventDefault();allerA(1)}
  else if(e.key==='ArrowLeft'){e.preventDefault();allerA(-1)}
});
enregistrer('revue',{ouvrir:ouvrirRevue,
  actualiser:async()=>{if(R.edition||surChamp(revueVue)||CH.occupe)return;try{const d=await api('chaine/propositions?limite=1&statut=en_attente');R.bilan=d.bilan;$('#rv-progression').innerHTML=progressionHtml();$('#rv-suite')&&rendreRevue()}catch{}}});
