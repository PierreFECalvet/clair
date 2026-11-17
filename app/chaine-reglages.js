'use strict';
/* Évaluation (mesures et seuils du protocole) et Moteurs et réglages (moteurs, seuils, prédicats, connexions). */

/* ---------- Évaluation ---------- */
const E={data:null,pages:null};
const evalVue=$('#view-evaluation');
const fmtMesure=v=>v==null?'—':Number.isInteger(v)?number(v):Number(v).toFixed(3).replace('.',',');
async function ouvrirEvaluation(){
  const [d,p]=await Promise.all([api('chaine/evaluation'),E.pages?Promise.resolve(null):api('chaine/pages?dernieres=1&limite=200')]);
  E.data=d;if(p)E.pages=p.pages;
  rendreEvaluation();
}
function groupesMesures(mesures){
  const g=new Map();
  for(const m of mesures){if(!g.has(m.methodologie))g.set(m.methodologie,[]);g.get(m.methodologie).push(m)}
  return [...g.entries()];
}
function rendreEvaluation(){
  const d=E.data;if(!d)return;
  const mesures=d.mesures||[],tenues=mesures.filter(m=>m.ok===true).length,non=mesures.filter(m=>m.ok===false).length,nm=mesures.filter(m=>m.ok==null).length;
  const ref=d.reference||{};
  const tableau=groupesMesures(mesures).map(([nom,l])=>{
    const t=l.filter(m=>m.ok===true).length,n=l.filter(m=>m.ok===false).length,x=l.filter(m=>m.ok==null).length;
    return `<details class="vs mes"${n?' open':''}><summary>${esc(nom)} <span class="vs-n ok">${t} tenue(s)</span> <span class="vs-n ${n?'ko':''}">${n} non tenue(s)</span> <span class="vs-n">${x} non mesurable(s)</span></summary>
    <table class="mini mesures"><thead><tr><th>Mesure</th><th class="num">Valeur</th><th class="num">Seuil</th><th>Résultat</th></tr></thead><tbody>${l.map(m=>`<tr><td>${esc(m.mesure)}${m.a_saisir?' <span class="bd info">À saisir</span>':''}${m.note?`<small>${esc(courtTexte(m.note,160))}</small>`:''}</td><td class="num">${fmtMesure(m.valeur)}</td><td class="num">${m.seuil==null?'—':(m.sens==='max'?'≤ ':'≥ ')+fmtMesure(m.seuil)}</td><td>${m.ok===true?'<span class="bd ok">Tenu</span>':m.ok===false?'<span class="bd bad">Non tenu</span>':'<span class="bd mute">Non mesurable</span>'}</td></tr>`).join('')}</tbody></table></details>`;
  }).join('');
  const saisir=Object.entries(d.a_saisir||{}).map(([cle,s])=>{
    if(cle==='m4.note_humaine')return `<form class="saisie" data-saisie="${cle}"><label>${esc(s.libelle)}<small>${nb(s.notes)} note(s) saisie(s)</small><input type="number" min="1" max="5" step="1" required name="valeur" aria-label="Note de 1 à 5"></label>${btnE('Enregistrer','type="submit"')}</form>`;
    if(cle==='m5.accord')return `<form class="saisie" data-saisie="${cle}"><label>${esc(s.libelle)}<small>${nb(s.notes)} contrôle(s) saisi(s)</small></label><div class="saisie-l"><select name="etiquette" aria-label="Verdict contrôlé"><option value="confirmee">Confirmée</option><option value="corrigee">Corrigée</option></select><select name="valeur" aria-label="Accord"><option value="true">D’accord avec le verdict</option><option value="false">Pas d’accord</option></select>${btnE('Enregistrer','type="submit"')}</div></form>`;
    return `<p class="muted">${esc(s.libelle)} : ${nb(s.notes)} saisie(s)</p>`;
  }).join('');
  const ap=d.apprentissage||{};
  evalVue.innerHTML=`${avisOccupe()}<div class="eval-grille"><section class="card padded-s"><div class="bloc-tete"><h2>Mesures et seuils du protocole</h2><span class="aide">${tenues} tenue(s) · ${non} non tenue(s) · ${nm} non mesurable(s)</span></div>${mesures.length?tableau:videHtml('Aucune mesure','Chargez un jeu de référence pour calculer les mesures.')}</section>
   <div class="eval-cote">
    <section class="card padded-s"><h3>Jeu de référence</h3><p>${ref.chargee?`<span class="bd ok">Chargé</span> ${esc(ref.nom||'')} · ${nb(ref.pages)} page(s)`:'<span class="bd warn">Aucun jeu de référence chargé</span> Sans lui, les mesures de qualité ne sont pas calculables.'}</p>
     <label class="fichier">Fichier JSON du jeu de référence<input type="file" id="ref-fichier" accept=".json,application/json"></label>${btnE('Charger le jeu de référence','id="ref-charger"')}<p class="aide">Format : voir docs/reference_exemple.json.</p></section>
    <section class="card padded-s"><h3>Gabarit d’annotation</h3><p class="aide">Fichier JSON pré-rempli à relire pour les pages cochées (20 au plus).</p>
     <div class="liste-cases" id="gab-pages">${(E.pages||[]).map(p=>`<label class="check-label"><input type="checkbox" value="${esc(p.version_id)}"> ${esc(courtTexte(p.title,44))}</label>`).join('')||'<span class="muted">Aucune page.</span>'}</div><button type="button" class="button" id="gab-telecharger">↓ Télécharger le gabarit</button></section>
    <section class="card padded-s"><h3>Mesures à saisir à la main</h3>${saisir||'<p class="muted">Rien à saisir.</p>'}</section>
    <section class="card padded-s"><h3>Apprentissage de la revue</h3><p class="aide">${nb(ap.total_decisions)} décision(s) prise(s) en revue ; sans modification : ${ap.taux_sans_modification!=null?Math.round(100*ap.taux_sans_modification)+' %':'—'}. ${nb(ap.acceptees)} acceptée(s), ${nb(ap.modifiees)} modifiée(s), ${nb(ap.rejetees)} rejetée(s), ${nb(ap.archivees)} archivée(s).${ap.budget_suggere!=null?` Budget de différence suggéré : ${esc(ap.budget_suggere)}.`:''}</p></section></div></div>`;
  appliquerOccupe();
}
evalVue.addEventListener('submit',e=>{
  const f=e.target.closest('[data-saisie]');if(!f)return;e.preventDefault();
  const cle=f.dataset.saisie,v=f.elements.valeur.value;
  const corps=cle==='m5.accord'?{action:'saisir',mesure:cle,valeur:v==='true',etiquette:f.elements.etiquette.value}:{action:'saisir',mesure:cle,valeur:Number(v)};
  ecrire(async()=>{await api('chaine/evaluation',corps);notify('Mesure enregistrée.');await ouvrirEvaluation()},e.submitter);
});
evalVue.addEventListener('click',e=>{
  if(e.target.id==='ref-charger'){
    const fichier=$('#ref-fichier').files[0];
    if(!fichier){notify('Choisissez d’abord un fichier JSON.',true);return}
    ecrire(async()=>{
      let ref;try{ref=JSON.parse(await fichier.text())}catch{throw Error('Ce fichier n’est pas un JSON valide.')}
      const r=await api('chaine/evaluation',{action:'charger',reference:ref});
      notify(`Jeu de référence chargé : ${r.nom||'sans nom'}, ${nb(r.pages)} page(s).`);await ouvrirEvaluation();
    },e.target);return;
  }
  if(e.target.id==='gab-telecharger')quiet(async()=>{
    const ids=$$('#gab-pages input:checked').map(x=>x.value);
    if(!ids.length)throw Error('Cochez au moins une page.');
    if(ids.length>20)throw Error('20 pages au plus par gabarit.');
    const d=await api('chaine/evaluation/gabarit?versions='+encodeURIComponent(ids.join(',')));
    telecharger(JSON.stringify(d,null,2),'gabarit-annotation.json','application/json');notify('Gabarit téléchargé.');
  });
});
function telecharger(contenu,nom,type){
  const url=URL.createObjectURL(new Blob([contenu],{type}));const a=document.createElement('a');a.href=url;a.download=nom;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
enregistrer('evaluation',{ouvrir:ouvrirEvaluation,actualiser:async()=>{if(!surChamp(evalVue))await ouvrirEvaluation()}});

/* ---------- Moteurs et réglages ---------- */
const M={onglet:'moteurs',pred:null,regl:null,seuilQ:'',focus:null,predEdit:null,predErreur:null};
const moteursVue=$('#view-moteurs');
const ROLES={phrases:['Découpe des pages en phrases','sat'],analyse:['Analyse linguistique (lemmes, dépendances)','spacy'],entites:['Entités nommées','entites'],nli:['Implication entre phrases : contrôles, relations, preuves','nli'],embeddings:['Vecteurs : voisins, sujets, dédoublonnage','embeddings'],reordonner:['Classement des passages renvoyés par Ibou','reordonner'],perplexite:['Naturel des requêtes (score)','perplexite'],classer:['Type de page','classer'],relations:['Extraction des faits (GLiREL ou LLM)','relations']};
const ORDRE_FONCTIONS=['phrases','analyse','entites','nli','embeddings','reordonner','perplexite','classer','relations'];
const ONGLETS_M=[['moteurs','Moteurs'],['seuils','Seuils'],['predicats','Prédicats'],['connexions','Connexions']];
async function ouvrirMoteurs(params={}){
  if(params.onglet)M.onglet=params.onglet;
  M.focus=params.focus||null;if(M.focus)M.onglet='moteurs';
  const [r,e]=await Promise.all([chargerReglages(true),api('chaine/etat')]);
  CH.etat=e;M.regl=r;
  rendreOngletsM();await rendreContenuM();
}
function rendreOngletsM(){
  $('#moteurs-onglets').innerHTML=ONGLETS_M.map(([k,t])=>`<button type="button" role="tab" aria-selected="${k===M.onglet}" class="${k===M.onglet?'selected':''}" data-m-onglet="${k}">${t}</button>`).join('');
  $('#moteurs-connexions').hidden=M.onglet!=='connexions';
  $('#moteurs-contenu').hidden=M.onglet==='connexions';
}
async function rendreContenuM(){
  const z=$('#moteurs-contenu');
  if(M.onglet==='connexions'){z.innerHTML='';return}
  if(M.onglet==='moteurs')z.innerHTML=htmlMoteurs();
  else if(M.onglet==='seuils')z.innerHTML=htmlSeuils();
  else{if(!M.pred)M.pred=await api('chaine/predicats');z.innerHTML=htmlPredicats()}
  appliquerOccupe();
  if(M.focus&&M.onglet==='moteurs'){const c=z.querySelector(`[data-regl="${M.focus}"]`);if(c){c.closest('.interrupteur')?.classList.add('repere');c.focus()}M.focus=null}
}
const DOUBLE_PASSE={
  forte:'Les sorties du LLM (unités, faits, valeurs corrigées, réécritures) sont contrôlées par le NLI local, distinct du LLM.',
  faible:'Faute de NLI local, le LLM contrôle ses propres sorties (réglage autorisé ci-dessous) : contrôle moins fiable, exécutions marquées « double passe faible ».',
  refusee:'Le seul NLI disponible est le LLM lui-même : contrôler ses propres sorties est refusé. Les unités par LLM et les faits s’arrêtent, M5 ne corrige pas automatiquement, M6 laisse la réécriture « à rédiger ».',
  impossible:'Aucun NLI : les sorties du LLM ne peuvent pas être contrôlées, et M3 et M5 sont limités.'
};
function htmlLlm(e,r){
  const llm=e.moteurs?.llm||{},cfg=CH.pulse?.config||{},dp=llm.double_passe||e.moteurs?.fonctions?.nli?.double_passe;
  const nli=e.moteurs?.fonctions?.nli||{};
  const configure=!!cfg.model;
  const typeBd=llm.type==='local'?'<span class="bd ok">Local</span>':'<span class="bd warn">Tiers · API payante</span>';
  const etatLlm=!configure?'<span class="bd bad">Aucun LLM configuré</span>':llm.disponible?'<span class="bd ok">Utilisable par la chaîne</span>':`<span class="bd bad">Refusé par la chaîne</span>`;
  const dpBd=`<span class="bd ${dp==='forte'?'ok':dp==='faible'?'warn':'bad'}">${esc(lib('double_passe',dp))}</span>`;
  const inter=(cle,titre,autorise,cout,actif)=>`<label class="interrupteur${actif?' actif':''}"><input type="checkbox" role="switch" data-regl="${cle}" data-ecrit ${actif?'checked':''} ${CH.occupe?'disabled':''}><span class="int-corps"><b>${titre}</b><small><i>Autorise :</i> ${autorise}</small><small><i>Ce que ça coûte :</i> ${cout}</small></span><span class="int-etat">${actif?'Autorisé':'Refusé'}</span></label>`;
  return `<section class="card padded-s llm-bloc"><div class="llm-grille">
   <div><h3>LLM configuré</h3><p class="llm-nom">${configure?`<b>${esc(lib('fournisseur',cfg.provider))}</b> · ${esc(cfg.model)} ${typeBd}`:'—'}</p><p>${etatLlm}${configure&&!llm.disponible?` <small>${esc(humain(llm.raison))}</small>`:''}</p><button type="button" class="lien" data-m-onglet="connexions">Changer de fournisseur ou de modèle (Connexions)</button></div>
   <div><h3>Double passe ${dpBd}</h3><p class="aide">${esc(DOUBLE_PASSE[dp]||'')}</p>${dp==='forte'&&nli.actif?`<small>NLI : ${esc(nli.actif)}</small>`:''}${llm.raison_double_passe&&dp!=='forte'?`<small class="raison-dp">${esc(humain(llm.raison_double_passe))}</small>`:''}</div>
   <div class="interrupteurs">
    ${inter('llm_tiers_autorise','Autoriser un LLM tiers pour les étapes « LLM local »','unités par LLM, faits, reformulation des requêtes, réécriture des propositions et NLI de repli avec un fournisseur tiers (OpenAI).','les textes de vos pages partent chez ce fournisseur et chaque appel est facturé en tokens. Refusé : ces étapes s’arrêtent ou se replient sur les règles tant que LM Studio n’est pas configuré.',!!r.llm_tiers_autorise)}
    ${inter('nli_par_llm','Autoriser la double passe faible','sans NLI local, le LLM vérifie lui-même ses unités, faits, valeurs corrigées et réécritures.','un contrôle moins fiable (le LLM juge sa propre sortie) ; les exécutions concernées sont marquées « double passe faible ». Refusé : ces étapes s’arrêtent tant que le NLI local (installer_modeles.cmd) n’est pas installé.',!!r.nli_par_llm)}
   </div></div></section>`;
}
function htmlMoteurs(){
  const e=CH.etat,m=e.moteurs,r=M.regl,f=m.fonctions||{};
  const lignes=ORDRE_FONCTIONS.map(nom=>{
    const [role,cleModele]=ROLES[nom];
    let actif,dispo=[],raison='',charge=false,version='';
    if(nom==='relations'){actif=m.extracteur_relations||'aucun';raison=e.prerequis?.['m1.faits']||'';dispo=[]}
    else{const x=f[nom]||{};actif=x.actif||'aucun';dispo=x.disponible||[];raison=x.raison_repli||'';charge=!!x.charge;version=x.version||''}
    const raisons=String(raison).split(' ; ').filter(Boolean);
    const choix=['auto','local','llm','regles'];
    return `<tr><td><strong>${esc(lib('fonction',nom))}</strong><small>${esc(role)}</small></td>
     <td>${actif==='aucun'?'<span class="bd bad">Aucun moteur utilisable</span>':`<span class="bd ok">${esc(fmtMoteur(actif))}</span>`}${charge?'<small>modèle chargé en mémoire</small>':''}</td>
     <td>${dispo.length?dispo.map(x=>`<span class="chip">${esc(fmtMoteur(x))}</span>`).join(' '):'<span class="muted">aucun</span>'}</td>
     <td class="raison">${raisons.length?raisons.map(x=>`<div>${esc(humain(x))}</div>`).join(''):'<span class="muted">Aucun repli en cours.</span>'}</td>
     <td><input class="modele" data-modele="${cleModele}" value="${esc(r.modeles?.[cleModele]||'')}" ${CH.occupe?'disabled':''} data-ecrit aria-label="Modèle ${esc(lib('fonction',nom))}">${version?`<small>version ${esc(version)}</small>`:''}</td>
     <td><select data-moteur="${nom}" data-ecrit ${CH.occupe?'disabled':''} aria-label="Moteur ${esc(lib('fonction',nom))}">${choix.map(c=>`<option value="${c}"${(r.moteurs?.[nom]||'auto')===c?' selected':''}>${esc(lib('moteur',c))}</option>`).join('')}</select></td></tr>`;
  }).join('');
  return `${avisOccupe()}${htmlLlm(e,r)}<section class="card padded-s"><div class="resume-m"><span>Ibou : ${m.ibou?.cle?'<span class="bd ok">Clé présente</span>':'<span class="bd bad">Clé manquante</span>'}</span>
   <label class="inline">Extraction des unités<select data-regl="option_unites" data-ecrit ${CH.occupe?'disabled':''}>${r.choix.option_unites.map(o=>`<option value="${o}"${r.option_unites===o?' selected':''}>${o==='auto'?'Automatique (C si LLM, sinon A)':o==='A'?'A · règles':o==='B'?'B · propositionizer':'C · LLM local contraint'}</option>`).join('')}</select></label>
   <label class="inline check-label"><input type="checkbox" data-regl="repli_requetes_unite" data-ecrit ${r.repli_requetes_unite?'checked':''} ${CH.occupe?'disabled':''}> Repli « Est-ce que l’unité… ? » (déconseillé)</label></div></section>
  <section class="card"><div class="table-wrap"><table class="dense moteurs"><thead><tr><th>Fonction</th><th>Moteur actif</th><th>Replis disponibles</th><th>Raison du repli</th><th>Modèle</th><th>Niveau autorisé</th></tr></thead><tbody>${lignes}</tbody></table></div></section>
  <p class="aide">« Niveau autorisé » plafonne le moteur : automatique = le meilleur disponible ; modèle local ; LLM = saute les modèles locaux ; règles seules. Si le moteur choisi est absent, l’étape se replie sur le suivant ou s’arrête en disant pourquoi.</p>`;
}
const nomSeuil=n=>{const l=String(n).split('.');l.shift();return (l.join(' · ')||n).replace(/_/g,' ').replace(/^./,c=>c.toUpperCase())};
const AUTRES_REGLAGES={'m2:llm':'M2 · typologie par LLM (1 = activée)','m2:auteurs_experts':'M2 · auteurs reconnus comme experts','m6.pages_datees':'M6 · types de page qui reçoivent une mention de mise à jour','m6.format_lien':'M6 · format des liens internes insérés','m1.fcm.moteur':'M1 · moteur du fuzzy c-means','m1.reduction.methode':'M1 · méthode de réduction de dimension','m1:entites:wikidata':'M1 · liaison des entités à Wikidata','gsc:fichier':'Export Google Search Console (chemin du fichier)'};
const typeSeuil=s=>Array.isArray(s.defaut)?'liste':typeof s.defaut==='boolean'?'bool':typeof s.defaut==='number'?'nombre':'texte';
const NOMS_GROUPES={m1:'M1 · pages, unités, faits, sujets',dedup:'Dédoublonnage',m2:'M2 · sources et typologie',m3:'M3 · voisins et paternité',m4:'M4 · statuts et requêtes',m5:'M5 · criticité et Ibou',m6:'M6 · propositions',site:'Site'};
function htmlSeuils(){
  const r=M.regl,q=M.seuilQ.toLowerCase(),g=new Map();
  for(const s of r.seuils){if(q&&!s.nom.toLowerCase().includes(q)&&!nomSeuil(s.nom).toLowerCase().includes(q))continue;const k=s.nom.split('.')[0];if(!g.has(k))g.set(k,[]);g.get(k).push(s)}
  const ordre=[...g.keys()].sort((a,b)=>String(a).localeCompare(String(b),'fr',{numeric:true}));
  const champ=s=>{
    const t=typeSeuil(s),n=esc(s.nom);
    if(t==='bool')return `<input type="checkbox" data-seuil="${n}" data-type="bool" ${s.valeur?'checked':''} data-ecrit ${CH.occupe?'disabled':''} aria-label="${n}">`;
    if(t==='nombre')return `<input type="number" step="any" data-seuil="${n}" data-type="nombre" value="${esc(s.valeur)}" data-ecrit ${CH.occupe?'disabled':''} aria-label="${n}">`;
    return `<input type="text" data-seuil="${n}" data-type="${t}" value="${esc(Array.isArray(s.valeur)?s.valeur.join(', '):s.valeur)}" data-ecrit ${CH.occupe?'disabled':''} aria-label="${n}">`;
  };
  const autres=Object.entries(r.autres||{});
  return `${avisOccupe()}<div class="barre"><input class="recherche" id="seuils-q" type="search" placeholder="Filtrer les seuils (ex. critique, dédoublonnage)" value="${esc(M.seuilQ)}" aria-label="Filtrer les seuils"><span class="aide">${nb(r.seuils.filter(s=>s.surcharge).length)} seuil(s) modifié(s) sur ${nb(r.seuils.length)}. Une valeur invalide est refusée avec la raison.</span></div>
  ${ordre.map(k=>`<details class="vs seuils"${q||k==='m5'?' open':''}><summary>${esc(NOMS_GROUPES[k]||k)} <span class="vs-n">${g.get(k).length}</span></summary><table class="mini seuil-t"><thead><tr><th>Seuil</th><th>Valeur</th><th>Défaut</th><th></th></tr></thead><tbody>${g.get(k).map(s=>`<tr class="${s.surcharge?'surcharge':''}"><td title="Nom technique : ${esc(s.nom)}">${esc(nomSeuil(s.nom))}</td><td>${champ(s)}<small class="seuil-err" data-err="${esc(s.nom)}"></small></td><td>${esc(Array.isArray(s.defaut)?s.defaut.join(', '):String(s.defaut))}</td><td>${s.surcharge?btnE('Rétablir',`data-retablir="${esc(s.nom)}"`,'button mini'):''}</td></tr>`).join('')}</tbody></table></details>`).join('')||videHtml('Aucun seuil ne correspond','')}
  ${autres.length&&!q?`<details class="vs seuils"><summary>Autres réglages des modules <span class="vs-n">${autres.length}</span></summary><table class="mini seuil-t"><tbody>${autres.map(([k,v])=>`<tr><td title="Nom technique : ${esc(k)}">${esc(AUTRES_REGLAGES[k]||k)}</td><td><input type="text" data-autre="${esc(k)}" value="${esc(v??'')}" placeholder="(défaut)" data-ecrit ${CH.occupe?'disabled':''} aria-label="${esc(k)}"></td></tr>`).join('')}</tbody></table></details>`:''}`;
}
const CLASSES_PRED=['immuable','etat_courant','annonce'],GABARITS_PRED=['etat_courant','connaissance_generale','annonce'];
function lignePredicat(x){
  if(M.predEdit===x.nom)return `<tr class="edition" data-pred="${esc(x.nom)}"><td><input class="pred-libelle" value="${esc(x.libelle)}" maxlength="120" aria-label="Libellé"><small title="${esc(x.nom)}">${esc(courtTexte(x.verbalisation,60))}</small></td>
    <td><select class="pred-classe" aria-label="Classe">${CLASSES_PRED.map(c=>`<option value="${c}"${c===x.classe?' selected':''}>${esc(lib('classe',c))}</option>`).join('')}</select></td>
    <td class="num"><input class="pred-vol" type="number" min="1" max="36500" step="1" value="${x.volatilite_jours??''}" placeholder="aucune" aria-label="Volatilité en jours"></td>
    <td>${esc(x.type_objet)}</td>
    <td><select class="pred-gabarit" aria-label="Gabarit de requête">${GABARITS_PRED.map(g=>`<option value="${g}"${g===x.gabarit?' selected':''}>${esc(libGabarit(g))}</option>`).join('')}</select></td>
    <td><label class="check-label"><input type="checkbox" class="pred-critique"${x.critique?' checked':''}> Critique</label></td>
    <td class="pred-actions">${btnE('Enregistrer',`data-pred-enreg="${esc(x.nom)}"`,'button primary petit')} <button type="button" class="button petit" data-pred-annuler="1">Annuler</button>${M.predErreur?`<div class="rq-refus" role="alert">${esc(M.predErreur)}</div>`:''}</td></tr>`;
  return `<tr data-pred="${esc(x.nom)}"><td><strong>${esc(x.libelle)}</strong><small title="${esc(x.nom)}">${esc(courtTexte(x.verbalisation,60))}</small></td><td>${esc(lib('classe',x.classe))}</td><td class="num">${x.volatilite_jours==null?'—':nb(x.volatilite_jours)}</td><td>${esc(x.type_objet)}</td><td>${esc(libGabarit(x.gabarit))}</td><td>${x.critique?'<span class="bd bad">Critique</span>':'—'}</td><td>${M.predEdit?'':btnE('Modifier',`data-pred-edit="${esc(x.nom)}"`,'button petit')}</td></tr>`;
}
function htmlPredicats(){
  const p=M.pred;
  return `${avisOccupe()}<div class="barre"><span class="aide">Liste fermée <b>${esc(p.version)}</b> · ${nb(p.predicats.length)} prédicats. Chaque modification est contrôlée par le serveur et crée une nouvelle version de la liste (l’ancienne est conservée).</span><span class="espace"></span>
   <label class="fichier">Remplacer toute la liste par un fichier JSON<input type="file" id="pred-fichier" accept=".json,application/json"></label>${btnE('Remplacer la liste','id="pred-remplacer"')}</div>
  <section class="card"><div class="table-wrap"><table class="dense predicats"><thead><tr><th>Prédicat <small>(forme vérifiée)</small></th><th>Classe</th><th class="num">Volatilité (jours)</th><th>Type d’objet</th><th>Gabarit de requête</th><th>Critique</th><th></th></tr></thead><tbody>${p.predicats.map(lignePredicat).join('')}</tbody></table></div></section>`;
}
async function enregistrerReglage(corps,bouton){
  return ecrire(async()=>{const r=await api('chaine/reglages',corps);CH.reglages=r.reglages;M.regl=r.reglages;CH.etat=await api('chaine/etat');return r},bouton);
}
moteursVue.addEventListener('click',e=>{
  const pe=e.target.closest('[data-pred-edit]');if(pe){M.predEdit=pe.dataset.predEdit;M.predErreur=null;rendreContenuM().then(()=>$('.pred-libelle')?.focus());return}
  if(e.target.closest('[data-pred-annuler]')){M.predEdit=null;M.predErreur=null;rendreContenuM();return}
  const pg=e.target.closest('[data-pred-enreg]');
  if(pg){
    const tr=pg.closest('tr'),vol=tr.querySelector('.pred-vol').value.trim();
    const corps={nom:pg.dataset.predEnreg,libelle:tr.querySelector('.pred-libelle').value.trim(),classe:tr.querySelector('.pred-classe').value,
      volatilite_jours:vol===''?null:Number(vol),critique:tr.querySelector('.pred-critique').checked,gabarit:tr.querySelector('.pred-gabarit').value};
    if(vol!==''&&!Number.isInteger(corps.volatilite_jours)){M.predErreur='Volatilité : nombre entier de jours, ou vide.';rendreContenuM();return}
    ecrire(async()=>{
      let r;
      try{r=await api('chaine/predicat',corps)}
      catch(err){
        M.predErreur=err.message;tr.querySelector('.rq-refus')?.remove();
        tr.querySelector('.pred-actions').insertAdjacentHTML('beforeend',`<div class="rq-refus" role="alert">Refusé, rien n’a changé : ${esc(err.message)}</div>`);
        return;
      }
      M.pred=null;M.predEdit=null;M.predErreur=null;CH.predicats=null;notify(`Prédicat enregistré : ${r.predicat.libelle} (liste ${r.version}).`);
      await rendreContenuM();
    },pg);
    return;
  }
  const o=e.target.closest('[data-m-onglet]');if(o){M.onglet=o.dataset.mOnglet;rendreOngletsM();quiet(rendreContenuM);return}
  const rt=e.target.closest('[data-retablir]');
  if(rt)enregistrerReglage({seuils:{[rt.dataset.retablir]:null}},rt).then(r=>{if(r){notify('Seuil rétabli à sa valeur par défaut.');rendreContenuM()}});
  if(e.target.id==='pred-remplacer'){
    const fichier=$('#pred-fichier').files[0];
    if(!fichier){notify('Choisissez d’abord un fichier JSON.',true);return}
    if(!confirm('Remplacer toute la liste de prédicats ? L’ancienne est gardée dans predicats.json.precedent.'))return;
    ecrire(async()=>{
      let corps;try{corps=JSON.parse(await fichier.text())}catch{throw Error('Ce fichier n’est pas un JSON valide.')}
      if(Array.isArray(corps))corps={predicats:corps};
      const r=await api('chaine/predicats',{predicats:corps.predicats,version:corps.version});
      M.pred=null;notify(`Liste remplacée : ${r.nombre} prédicats (${r.version}).${(r.avertissements||[]).length?' Avertissements : '+r.avertissements.join(' ; '):''}`);await rendreContenuM();
    },e.target);
  }
});
moteursVue.addEventListener('input',debounce(e=>{if(e.target.id==='seuils-q'){M.seuilQ=e.target.value.trim();rendreContenuM().then(()=>{const q=$('#seuils-q');q.focus();q.setSelectionRange(q.value.length,q.value.length)})}},300));
moteursVue.addEventListener('change',e=>{
  const t=e.target;
  if(t.dataset.moteur)enregistrerReglage({moteurs:{[t.dataset.moteur]:t.value}},t).then(r=>{if(r){notify(`Niveau de moteur enregistré : ${lib('fonction',t.dataset.moteur)} → ${lib('moteur',t.value).toLowerCase()}.`);rendreContenuM()}else ouvrirMoteurs()});
  else if(t.dataset.modele)enregistrerReglage({modeles:{[t.dataset.modele]:t.value.trim()||null}},t).then(r=>{if(r)notify('Modèle enregistré : pris en compte au prochain chargement.');else ouvrirMoteurs()});
  else if(t.dataset.regl){
    const v=t.type==='checkbox'?t.checked:t.value;
    enregistrerReglage({[t.dataset.regl]:v},t).then(r=>{if(r){notify('Réglage enregistré.');rendreContenuM()}else ouvrirMoteurs()});
  }
  else if(t.dataset.autre)enregistrerReglage({autres:{[t.dataset.autre]:t.value.trim()||null}},t).then(r=>{if(r)notify('Réglage enregistré.');else ouvrirMoteurs()});
  else if(t.dataset.seuil){
    const type=t.dataset.type,nom=t.dataset.seuil;
    let v;
    if(type==='bool')v=t.checked;
    else if(type==='nombre'){v=t.value.trim()===''?null:Number(t.value)}
    else if(type==='liste')v=t.value.split(',').map(x=>Number(x.trim()));
    else v=t.value.trim();
    const err=$$('[data-err]').find(x=>x.dataset.err===nom);if(err)err.textContent='';
    enregistrerReglage({seuils:{[nom]:v}},t).then(r=>{
      if(r){notify(`Seuil « ${nomSeuil(nom)} » enregistré.`);rendreContenuM()}
      else{if(err)err.textContent='Refusé : voir le message. La valeur n’a pas été enregistrée.';const s=M.regl.seuils.find(x=>x.nom===nom);if(s&&type!=='bool')t.value=Array.isArray(s.valeur)?s.valeur.join(', '):s.valeur;else if(s)t.checked=!!s.valeur}
    });
  }
});
$('#moteurs-connexions').addEventListener('click',()=>{});
enregistrer('moteurs',{ouvrir:ouvrirMoteurs,actualiser:async()=>{if(surChamp(moteursVue)||M.onglet==='connexions')return;const e=await api('chaine/etat');CH.etat=e;if(M.onglet==='moteurs')await rendreContenuM()}});

/* ---------- Journal : exécutions des étapes et événements (route légère paginée) ---------- */
const J={offset:0,limite:50,q:'',chaine:'1',data:null};
const journalVue=$('#view-activity');
const ROLES_MOTEURS={statuts:'Statuts',gabarits:'Gabarits',reformulation:'Reformulation',paternite:'Paternité',peremption:'Péremption',coreferences:'Coréférences',subjectivite:'Subjectivité',typologie:'Typologie',dates:'Dates',sources:'Sources',propagation:'Propagation',llm:'LLM',classifieur:'Classifieur de pages',regles:'Règles',extraction:'Extraction',verification:'Vérification'};
const fmtMoteur=v=>String(v).replace(/^llm:(\w+):(.*)$/,(m,f,mod)=>'LLM '+lib('fournisseur',f)+(mod?' · '+mod:'')).replace(/\bregles\b/g,'règles').replace(/\blexical\b/,'lexical');
const resumeMoteurs=en=>{
  if(!en||typeof en!=='object')return '';
  const l=Object.entries(en).filter(([k,v])=>k!=='double_passe'&&v&&typeof v!=='object').map(([k,v])=>`${LIB.fonction[k]||ROLES_MOTEURS[k]||lib('fonction',k)} : ${fmtMoteur(v)}`);
  return l.join(' · ');
};
async function ouvrirJournal(){
  journalVue.querySelector('#journal').innerHTML=`<div class="journal-grille"><section class="card padded-s"><div class="bloc-tete"><h2>Exécutions des étapes</h2><span class="aide">15 dernières · moteurs réellement utilisés</span></div><div id="journal-exec"></div></section>
   <section class="card padded-s"><div class="bloc-tete"><h2>Journal des événements</h2><span class="espace"></span><label class="inline"><input type="checkbox" id="journal-chaine"${J.chaine==='1'?' checked':''}> Chaîne seulement</label><input class="recherche" id="journal-q" type="search" placeholder="Rechercher dans le journal" aria-label="Rechercher dans le journal" value="${esc(J.q)}"></div><div id="journal-ev"></div><div id="journal-pager"></div></section></div>`;
  rendreExecutions();await chargerJournal();
}
function rendreExecutions(){
  const z=$('#journal-exec');if(!z)return;
  const l=CH.etat?.executions||[];
  z.innerHTML=l.length?`<ul class="exec-liste">${l.map(x=>`<li><div><strong>${esc(libEtape(x.step))}</strong> ${bd('execution',x.status)}${x.engines?.double_passe?` <span class="bd ${x.engines.double_passe==='forte'?'ok':'warn'}">Double passe ${esc(lib('double_passe',x.engines.double_passe).toLowerCase())}</span>`:''}</div>
    <small>${date(x.started)}${x.local_seconds!=null?' · '+dureeCourte(x.local_seconds):''}${x.items?` · ${nb(x.items)} élément(s)`:''}${x.tokens_in||x.tokens_out?` · ${nb((x.tokens_in||0)+(x.tokens_out||0))} tokens`:''}${x.ibou_units?` · ${nb(x.ibou_units)} u. Ibou`:''}</small>${resumeMoteurs(x.engines)?`<small class="muted">${esc(resumeMoteurs(x.engines))}</small>`:''}${x.note&&x.status!=='terminee'?`<small class="rouge">${esc(humain(courtTexte(x.note,200)))}</small>`:''}</li>`).join('')}</ul>`:videHtml('Aucune exécution','Les étapes lancées apparaîtront ici avec leur coût et les moteurs utilisés.');
}
async function chargerJournal(){
  const d=J.data=await api('chaine/journal?'+qs({offset:J.offset,limite:J.limite,q:J.q,chaine:J.chaine}));
  const z=$('#journal-ev');if(!z)return;
  z.innerHTML=d.evenements.length?`<ul class="journal-liste">${d.evenements.map(x=>`<li><span>${esc(humain(x.message))}</span><small>${date(x.created)}</small></li>`).join('')}</ul>`:videHtml('Aucun événement','Rien ne correspond à ce filtre.');
  $('#journal-pager').innerHTML=pagerHtml('activity',d.total,d.offset,d.limite);
}
journalVue.addEventListener('change',e=>{if(e.target.id==='journal-chaine'){J.chaine=e.target.checked?'1':'';J.offset=0;quiet(chargerJournal)}});
journalVue.addEventListener('input',debounce(e=>{if(e.target.id==='journal-q'){J.q=e.target.value.trim();J.offset=0;quiet(chargerJournal)}},300));
enregistrer('activity',{ouvrir:ouvrirJournal,page:pas=>{J.offset=Math.max(0,J.offset+pas*J.limite);quiet(chargerJournal)},
  actualiser:async()=>{if(surChamp(journalVue))return;rendreExecutions();if(J.offset===0)await chargerJournal()}});
