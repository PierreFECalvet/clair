'use strict';
/* Chaîne M1 à M6 : cœur de l'interface.
   Libellés (table unique), navigation, état léger (pulse), bandeau de traitement, vue d'ensemble et « À faire ».
   Les écrans de données, de revue, de requêtes et de réglages sont dans chaine-*.js. */

const CH={etat:null,pulse:null,reglages:null,vue:'ensemble',rev:'',statutTache:'',occupe:false,dernierEtat:0,enCours:false,params:{},depuisPages:false};

/* ---------- libellés : une seule table de correspondance ---------- */
const LIB={
  tache:{idle:'Prêt',running:'En cours',paused:'Suspendu',completed:'Terminé',error:'Erreur interne',interrupted:'Serveur arrêté'},
  execution:{en_cours:'En cours',terminee:'Terminée',erreur:'Erreur',interrompue:'Interrompue'},
  statut:{sourcee:'Sourcée',a_verifier:'À vérifier',a_verifier_faible:'À vérifier (priorité faible)',non_verifiable:'Non vérifiable (opinion)',dependante:'Dépendante',historique:'Historique',aucun:'Hors périmètre'},
  temporel:{a_jour:'À jour',obsolete_historique:'Obsolète (historique)',remise_en_question:'Remise en question',perimee_par_date:'Périmée par date'},
  classe:{immuable:'Immuable',etat_courant:'État courant',annonce:'Annonce',metadonnee:'Métadonnée',usage:'Usage',inconnu:'Inconnue'},
  ancre:{phrase:'Date écrite dans la phrase',relative:'Date relative résolue',maj:'Date de mise à jour',publication:'Date de publication',inconnue:'Inconnue'},
  sourcing:{sourcee_externe:'Sourcée (externe)',sourcee_interne:'Sourcée (interne)',auto_sourcee:'Auto-sourcée',dependante:'Dépendante',opinion:'Opinion',connaissance_generale:'Connaissance générale',sans_source:'Sans source',aucun:'Hors périmètre'},
  type:{information_source:'Information sourcée',opinion:'Opinion',interpretation:'Interprétation',connaissance_generale:'Connaissance générale',sans_source:'Sans source',aucun:'Non typée'},
  verdict:{confirmee:'Confirmée',corrigee:'Corrigée',completee:'Complétée',insuffisante:'Sources insuffisantes',conflictuelle:'Sources contradictoires'},
  fait_origine:{explicite:'Explicite',inference_contextuelle:'Inférence contextuelle',externe:'Externe (vérification M5)'},
  fait_statut:{extrait:'Extrait',verifie:'Vérifié',corrige:'Corrigé (nouvelle valeur)',complete:'Complété',historique:'Historique (remplacé)',actif:'Actif'},
  hypothese:{connaissance_externe:'Connaissance externe',information_inconnue:'Information inconnue',fait_rejete:'Fait rejeté'},
  relation:{equivalence:'Équivalence',precision:'Précision',remplacement:'Remplacement',conflit:'Conflit',faux_conflit:'Faux conflit',sans_rapport:'Sans rapport'},
  paternite:{sujet_principal:'Sujet principal de la page',nombre_sources:'Nombre de sources',date_publication:'Date de publication',arbitrage_humain:'Arbitrage humain',a_arbitrer:'À arbitrer',aucune:'Sans paternité'},
  source_kind:{lien_interne:'Lien interne',lien_externe:'Lien externe',source_nommee:'Source nommée',note:'Note',verification:'Vérification (M5)'},
  source_scope:{phrase:'Phrase',paragraphe:'Paragraphe',page:'Page'},
  contribution:{migration:'Migration',m2:'M2',heritee:'Héritée (M3)',confirmation:'Confirmation (M5)',correction:'Correction (M5)',implication:'Confirmation',complement:'Complément (M5)',source_ajoutee:'Source ajoutée'},
  lien:{proposee:'Proposé',acceptee:'Accepté',appliquee:'Appliqué',rejetee:'Rejeté'},
  requete_statut:{prete:'Prête',a_revoir:'À revoir',envoyee:'Envoyée',plus_tard:'Plus tard',abandonnee:'Abandonnée'},
  requete_kind:{ouverte:'Question ouverte',controle:'Question de contrôle'},
  appel:{success:'Réussi',cache:'Repris du cache (0 unité)',debit:'Débit limité (429)',solde_epuise:'Solde épuisé',refuse:'Refusé (401/403)',indisponible:'Service indisponible (503)',incertain:'Incertain',uncertain:'Incertain',erreur:'Erreur',refused:'Refusé (ancien parcours)'},
  rejet:{site_audite:'Site audité',quasi_copie_site:'Quasi-copie du site',reprise:'Reprise d’une même dépêche',anterieure_periode:'Antérieure à la période',pertinence_faible:'Pertinence faible',inaccessible:'Page inaccessible'},
  nli:{implication:'Soutient',contradiction:'Contredit',neutre:'Neutre'},
  proposition:{proposee:'Proposée',a_rediger:'À rédiger',acceptee:'Acceptée',appliquee:'Appliquée',modifiee:'Modifiée et appliquée',rejetee:'Rejetée',archive:'Archivée',reportee:'Reportée'},
  motif:{corrigee:'Corrigée',perimee_par_date:'Périmée par date',completee:'Complétée',source_ajoutee:'Source ajoutée',lien_interne:'Lien interne',mention_maj:'Mention de mise à jour'},
  methode:{substitution:'Substitution',reecriture_llm:'Réécriture LLM',humain:'Humain'},
  page_type:{etude:'Étude',enquete:'Enquête',analyse:'Analyse',decision:'Décision',expertise:'Expertise',rapport:'Rapport',recherche:'Recherche',livre_blanc:'Livre blanc',benchmark:'Benchmark',tribune:'Tribune',edito:'Édito',editorial:'Éditorial',opinion:'Opinion',chronique:'Chronique',billet:'Billet',point_de_vue:'Point de vue',actualite:'Actualité',reference:'Référence',autre:'Autre'},
  segment:{titre:'Titre',paragraphe:'Paragraphe',liste:'Liste',citation:'Citation',note:'Note',metadonnee:'Métadonnée',usage:'Usage'},
  moteur:{auto:'Automatique (meilleur disponible)',local:'Modèle local',llm:'LLM (saute les modèles locaux)',regles:'Règles seules',aucun:'Aucun moteur utilisable'},
  fonction:{phrases:'Phrases (segmentation)',analyse:'Analyse linguistique',entites:'Entités nommées',nli:'Implication (NLI)',embeddings:'Embeddings',reordonner:'Reranker',perplexite:'Perplexité (naturel)',classer:'Classifieur de pages',relations:'Relations (faits)'},
  decision:{accepter:'Acceptée',modifier:'Modifiée',rejeter:'Rejetée',archive:'Archivée',reporter:'Reportée'},
  critere:{controle_echoue:'Contrôle échoué',application_impossible:'Application impossible'},
  issue:{terminee:'Terminée',partielle:'Partielle',arretee:'Arrêtée'},
  double_passe:{forte:'Réelle',faible:'Faible',refusee:'Refusée',impossible:'Impossible'},
  fournisseur:{lmstudio:'LM Studio',openai:'OpenAI',openrouter:'OpenRouter'},
  type_llm:{local:'Local',tiers:'Tiers (API payante)'},
  origine_preuve:{verification:'Source de la vérification',preuve:'Preuve Ibou'},
  origine_cadre:{type:'Type d’affirmation (M2)',faits:'Faits (M1)',fait:'Faits (M1)',predicat:'Prédicat',groupe:'Groupe',unite:'Texte de l’unité',reformulation:'Reformulation (M5)'}
};
const lib=(groupe,valeur)=>{
  if(valeur==null||valeur==='')return '—';
  const v=String(valeur);
  if(LIB[groupe]&&LIB[groupe][v]!=null)return LIB[groupe][v];
  return v.replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());
};
const libExtracteur=v=>!v?'—':v==='migration'?'Migrée':v.startsWith('A:')?'Règles ('+v.slice(2)+')':v.startsWith('C:')?'LLM local ('+v.slice(2)+')':v.startsWith('B:')?'Propositionizer':v;
const libGabarit=v=>{
  if(!v)return '—';
  if(v.startsWith('etat_courant:'))return 'État courant'+(v.endsWith(':generique')?'':' · '+v.slice(13).replace(/_/g,' '));
  if(v.startsWith('annonce:futur'))return 'Annonce (futur)'+(v.split(':')[2]?' · '+v.split(':')[2].replace(/_/g,' '):'');
  if(v.startsWith('annonce:passe'))return 'Annonce échue (passé)'+(v.split(':')[2]?' · '+v.split(':')[2].replace(/_/g,' '):'');
  if(v==='connaissance_generale')return 'Connaissance générale';
  if(v==='etat_courant')return 'État courant';
  if(v==='annonce')return 'Annonce';
  if(v.startsWith('controle:'))return 'Contrôle · '+v.slice(9).replace(/_/g,' ');
  if(v.startsWith('reformulation:'))return 'Reformulation M5 · '+v.slice(14).replace(/_/g,' ');
  if(v.endsWith('unite:fermee'))return 'Repli (jamais envoyé par défaut)';
  if(v==='ancien-pipeline')return 'Ancienne requête (jamais envoyée)';
  return v.replace(/[_:]/g,' ');
};
const TONS={
  ok:['sourcee','a_jour','confirmee','terminee','completed','success','appliquee','acceptee','modifiee','prete','implication','retenue','verifie'],
  warn:['a_verifier','a_verifier_faible','a_rediger','proposee','reportee','paused','plus_tard','a_revoir','incertain','uncertain','completee','conflictuelle','insuffisante','a_arbitrer','en_cours','running','remise_en_question','debit','archive'],
  bad:['perimee_par_date','corrigee','erreur','error','refuse','refused','solde_epuise','contradiction','interrompue','interrupted','rejetee','abandonnee','indisponible'],
  info:['cache','envoyee','dependante','annonce','etat_courant','mention_maj','lien_interne','source_ajoutee']
};
const ton=v=>Object.keys(TONS).find(k=>TONS[k].includes(String(v)))||'mute';
const bd=(groupe,valeur,extra='')=>`<span class="bd ${ton(valeur)} ${extra}">${esc(lib(groupe,valeur))}</span>`;
const pct=(a,b)=>b?Math.round(100*a/b)+' %':'—';
const nb=n=>n==null?'—':number(n);
const jour=iso=>{if(!iso)return '—';const d=new Date(String(iso).length===10?iso+'T12:00:00':iso);return isNaN(d)?esc(iso):d.toLocaleDateString('fr-FR',{day:'numeric',month:'long',year:'numeric'})};
const courtTexte=(t,n=140)=>{t=String(t??'');return t.length>n?t.slice(0,n-1)+'…':t};
const dureeCourte=s=>s==null?'—':s<1?'< 1 s':s<90?Math.round(s)+' s':s<5400?Math.round(s/60)+' min':(s/3600).toFixed(1).replace('.',',')+' h';
const debounce=(fn,ms=300)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)}};

/* étapes de la chaîne et libellés de job.stage (API §2 et §6) */
const ETAPES={migration:'Migration de l’ancien parcours','m1.page':'M1 · segmentation, contexte, ancrage, classes','m1.unites':'M1 · unités d’information','m1.faits':'M1 · faits atomiques','m1.sujets':'M1 · embeddings, sujets, dédoublonnage',m2:'M2 · sources et typologie',m3:'M3 · voisins, paternité, propagation, statuts temporels',m4:'M4 · statuts finaux et requêtes',m5:'M5 · criticité et vérification Ibou',m6:'M6 · propositions de correction',tout:'Chaîne complète','m1.peremption':'M1 · annonces périmées','m1.dedup':'M1 · dédoublonnage seul','m3.paternite':'M3 · paternité et statuts (sans NLI)','m5.criticite':'M5 · criticité seule'};
const STAGES={chaine:'Démarrage de la chaîne',migration:'Migration de l’ancien parcours','m1.segmentation':'Segmentation des pages','m1.contexte':'Coréférences et dates','m1.ancrage':'Ancrage temporel des unités','m1.obsolescence':'Classes d’obsolescence','m1.peremption':'Annonces périmées','m1.unites':'Unités d’information','m1.faits':'Faits atomiques','m1.encodage.pages':'Embeddings des pages','m1.encodage.unites':'Embeddings des unités','m1.sujets':'Sujets (fuzzy c-means)','m1.dedup':'Dédoublonnage des unités','m2.sources':'Sources explicites','m2.typologie':'Typologie des affirmations','m2.manquantes':'Références internes manquantes','m3.voisins':'Recherche des voisins','m3.qualification':'Qualification des paires','m3.paternite':'Centralité et paternité','m3.propagation':'Propagation des sources','m3.statuts':'Statuts temporels','m4.statuts':'Statuts finaux','m4.requetes':'Requêtes de vérification','m5.criticite':'Criticité','m5.verification':'Vérification Ibou','m6.proposer':'Propositions de correction',m2:ETAPES.m2,m3:ETAPES.m3,m4:ETAPES.m4,m5:ETAPES.m5,m6:ETAPES.m6};
Object.assign(stageLabels,STAGES,{chaine:'Chaîne M1 à M6'});
const libEtape=e=>ETAPES[e]||STAGES[e]||e||'';
const REGLAGES_NOMMES={'chaine:llm_tiers_autorise':'« Autoriser un LLM tiers » (Moteurs et réglages)','chaine:nli_par_llm':'« Autoriser la double passe faible » (Moteurs et réglages)'};
const MOTS_ASCII={modele:'modèle',modeles:'modèles',configure:'configuré',configures:'configurés',cle:'clé',installe:'installé',desactives:'désactivés',desactive:'désactivé',etape:'étape',etapes:'étapes',echec:'échec',deja:'déjà',controle:'contrôle',a:'a',centroide:'centroïde'};
/* Texte serveur → phrase lisible : réglages nommés, identifiants d'étape remplacés par leur libellé, accents rétablis. */
function humain(t){
  if(t==null)return '';
  let x=String(t);
  for(const [k,v] of Object.entries(REGLAGES_NOMMES))x=x.split(k).join(v);
  const m=x.match(/^Étape (\S+) interrompue : (.*)$/s);
  if(m){const l=libEtape(m[1]);let reste=m[2];if(reste.startsWith(l+' : '))reste=reste.slice(l.length+3);x=`Arrêt à l’étape « ${l} » : ${reste}`}
  x=x.replace(/\b(m[1-6](?:\.[a-z_]+)*)\b(?![\w-])/g,(id)=>ETAPES[id]||STAGES[id]?'« '+(ETAPES[id]||STAGES[id])+' »':id);
  x=x.replace(/\b(modeles?|configures?|cle|installe|desactives?|echec|deja|centroide)\b/g,w=>MOTS_ASCII[w]||w);
  return x;
}
/* Cause courte d'un prérequis manquant (pour les cartes d'étape). */
function causeCourte(r){
  const t=String(r||'');
  if(/tiers payant/.test(t))return 'LLM tiers non autorisé';
  if(/double passe/.test(t))return 'double passe impossible';
  if(/NLI/.test(t))return 'aucun moteur NLI';
  if(/embeddings/.test(t))return 'aucun moteur d’embeddings';
  if(/extracteur/.test(t))return 'aucun extracteur de faits';
  if(/Ibou/.test(t))return 'clé Ibou manquante';
  if(/budget/.test(t))return 'aucun plafond Ibou';
  return courtTexte(humain(t),60);
}

const MSG_OCCUPE='Un traitement est en cours : le serveur refuse toute écriture. Suspendez-le ou attendez sa fin.';

/* ---------- navigation ---------- */
const VUES={};
const TITRES={ensemble:'Vue d’ensemble',pages:'Pages',unites:'Unités',groupes:'Groupes et paternité',requetes:'Requêtes Ibou',revue:'Revue',evaluation:'Évaluation',history:'Historique des appels',activity:'Journal',moteurs:'Moteurs et réglages',overview:'Archives · aperçu ancien',contents:'Archives · contenus',claims:'Archives · affirmations',budget:'Archives · budget',corrections:'Archives · corrections'};
const ANCIENNES=['overview','contents','claims','budget','corrections'];
CH.ancienOuvert=false;
const refreshAncien=refresh;
function enregistrer(nom,def){VUES[nom]=def}
go=function(nom,params={}){
  if(nom==='settings'){nom='moteurs';params={onglet:'connexions'}}
  if(nom==='contents'&&CH.depuisPages){CH.depuisPages=false;nom='pages'}
  if(!TITRES[nom])return;
  CH.depuisPages=false;
  CH.vue=nom;CH.params=params;
  $$('.view').forEach(x=>x.hidden=x.id!=='view-'+nom);
  $$('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===nom));
  if(ANCIENNES.includes(nom))$('#nav-archives').open=true;
  $('#breadcrumb').textContent=TITRES[nom];
  try{history.replaceState(null,'','#'+nom)}catch{}
  window.scrollTo(0,0);
  if(ANCIENNES.includes(nom))afficherChargeArchives();
  const v=VUES[nom];if(v&&v.ouvrir)quiet(async()=>{await v.ouvrir(params)});
  if(nom==='history')quiet(loadHistory);
};
refresh=async function(){
  if(CH.ancienOuvert){await refreshAncien();rendreApresAncien()}
  await rafraichir(true);
};
/* Archives : l'ancien état (/api/state, plusieurs Mo) n'est chargé que sur demande explicite. */
function afficherChargeArchives(){
  for(const v of ANCIENNES){
    const vue=$('#view-'+v);if(!vue)continue;
    let bloc=vue.querySelector('.archive-charge');
    if(!bloc){bloc=el('div','archive-charge');vue.prepend(bloc)}
    bloc.innerHTML=CH.ancienOuvert?'':`<span><b>Ancien parcours, en lecture seule.</b> Ces écrans relisent tout l’ancien état (plusieurs mégaoctets) : il n’est chargé que si vous le demandez.</span><button type="button" class="button" data-charger-archives>Charger l’ancien parcours</button>`;
    bloc.hidden=CH.ancienOuvert;
    vue.classList.toggle('archive-vide',!CH.ancienOuvert);
  }
}
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-charger-archives]');if(!b)return;
  b.disabled=true;b.textContent='Chargement…';
  quiet(async()=>{await refreshAncien();CH.ancienOuvert=true;afficherChargeArchives();rendreApresAncien()}).finally(()=>{b.disabled=false});
});
/* L'ancien rendu réécrit des éléments communs (menu, bandeau, connexion) : on les remet à jour après lui. */
function rendreApresAncien(){if(CH.pulse){rendreBandeau(CH.pulse.job);rendreCompteurs(CH.pulse);rendreConnexion(CH.pulse.config);appliquerOccupe()}}
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-vue]');
  if(b){e.preventDefault();let p={};try{p=b.dataset.params?JSON.parse(b.dataset.params):{}}catch{}go(b.dataset.vue,p)}
});
document.addEventListener('click',e=>{const b=e.target.closest('[data-pager]');if(b&&VUES[b.dataset.pager]?.page)VUES[b.dataset.pager].page(Number(b.dataset.pas))});

/* ---------- écritures protégées ---------- */
async function ecrire(fn,bouton){
  if(CH.occupe){notify(MSG_OCCUPE,true);return null}
  if(bouton)bouton.disabled=true;
  try{return await fn()}
  catch(e){notify(e.message,true);return null}
  finally{if(bouton)bouton.disabled=CH.occupe&&bouton.hasAttribute('data-ecrit')}
}
const avisOccupe=()=>`<div class="avis-occupe" role="status"><span>Traitement en cours (<b class="occupe-etape"></b>) : toute modification est désactivée jusqu’à sa fin ou sa suspension (bouton « Suspendre » ci-dessus).</span></div>`;
function appliquerOccupe(){
  document.body.classList.toggle('occupe',CH.occupe);
  const j=CH.pulse?.job||{};
  $$('.occupe-etape').forEach(x=>x.textContent=libEtape(j.etape_chaine||j.stage)||'étape en cours');
  $$('[data-ecrit]').forEach(b=>{
    const bloque=CH.occupe||b.dataset.bloque==='1';
    b.disabled=bloque;
    if(CH.occupe)b.title='Indisponible pendant un traitement : suspendez-le d’abord.';
    else if(b.title.startsWith('Indisponible pendant'))b.removeAttribute('title');
  });
}
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-stop]');
  if(b)quiet(async()=>{await api('stop',{});notify('Suspension demandée : elle prend effet après l’opération en cours.');await rafraichir(true)});
});
const btnE=(texte,attrs='',classe='button')=>`<button type="button" class="${classe}" data-ecrit ${attrs}${CH.occupe?' disabled title="Indisponible pendant un traitement : suspendez-le d’abord."':''}><span>${texte}</span></button>`;

/* ---------- état : pulse léger, état complet de la chaîne ---------- */
async function chargerReglages(force=false){
  if(CH.reglages&&!force)return CH.reglages;
  CH.reglages=await api('chaine/reglages');return CH.reglages;
}
async function rafraichir(force=false){
  const maintenant=Date.now();
  const p=CH.pulse=await api('pulse');
  CH.occupe=p.job.status==='running';
  const surveille=p.rev!==CH.rev||p.job.status!==CH.statutTache;
  const termine=CH.statutTache==='running'&&p.job.status!=='running';
  CH.rev=p.rev;CH.statutTache=p.job.status;
  rendreBandeau(p.job);rendreCompteurs(p);rendreConnexion(p.config);appliquerOccupe();
  // pendant un traitement, l'état complet est relu au plus toutes les 10 s ; les listes ouvertes au plus toutes les 30 s
  const delai=CH.occupe?10000:1500;
  if(force||!CH.etat||termine||(surveille&&maintenant-CH.dernierEtat>delai)){
    CH.etat=await api('chaine/etat');CH.dernierEtat=Date.now();
    if(!CH.predicats)try{CH.predicats=Object.fromEntries((await api('chaine/predicats')).predicats.map(x=>[x.nom,x.libelle]))}catch{CH.predicats={}}
    rendreCompteurs(p);
    if(CH.vue==='ensemble'||!VUES[CH.vue])await rendreEnsemble();
    const v=VUES[CH.vue];
    if(v&&v.actualiser&&(force||termine||(maintenant-(v.dernier||0)>(CH.occupe?30000:5000)))){v.dernier=Date.now();await v.actualiser()}
  }
}
const surChamp=racine=>{const a=document.activeElement;return !!(a&&racine&&racine.contains(a)&&a.matches('input,textarea,select'))};
let minuterie=null;
function planifier(delai){clearTimeout(minuterie);minuterie=setTimeout(sonde,delai)}
async function sonde(){
  if(CH.enCours)return;CH.enCours=true;
  try{
    const session=await api('session');
    if(token&&token!==session.token)notify('Serveur redémarré : progression conservée. Renseignez de nouveau les connexions.',true);
    token=session.token;
    await rafraichir(false);
  }catch(e){notify(e.message,true)}
  finally{CH.enCours=false;planifier(CH.occupe?1500:5000)}
}
pollState=function(){planifier(0)};

function rendreCompteurs(p){
  const c=CH.etat?.compteurs,ch=p.chaine||{};
  if(c){$('#nav-pages').textContent=nb(c.pages.pages);$('#nav-pages').title=`${nb(c.pages.pages)} page(s) (URL distinctes), ${nb(c.pages.versions)} version(s)`;$('#nav-unites').textContent=nb(c.m1.unites);$('#nav-groupes').textContent=nb(c.m3.paternite?.a_arbitrer||0)}
  $('#nav-requetes').textContent=nb(ch.requetes_pretes||0);
  $('#nav-revue').textContent=nb(ch.propositions_en_attente||0);
}
function rendreConnexion(cfg){
  const llm=cfg.model?`LLM ${cfg.provider==='lmstudio'?'LM Studio (local)':cfg.provider==='openrouter'?'OpenRouter (tiers)':'OpenAI (tiers)'} · ${cfg.model}`:'Aucun LLM configuré';
  $('#connection-label').textContent=`${llm} · ${cfg.ibou_key_set?'Ibou prêt':'Clé Ibou manquante'}`;
  // formulaire des connexions : rempli depuis le suivi léger (l'ancien état n'est plus chargé)
  if(!settingsLoaded&&$('#provider')){
    $('#provider').value=cfg.provider||'openai';$('#model').value=cfg.model||'';$('#embedding-model').value=cfg.embedding_model||'';
    $('#lm-url').value=cfg.lm_url||'http://127.0.0.1:1234/v1';$('#embeddings').checked=!!cfg.embeddings;settingsLoaded=true;toggleProvider();
  }
  $('#openai-key-state').textContent=cfg.openai_key_set?'Une clé est présente en mémoire. Laissez vide pour la conserver.':'Aucune clé configurée.';
  $('#openrouter-key-state').textContent=cfg.openrouter_key_set?'Une clé est présente en mémoire. Laissez vide pour la conserver.':'Aucune clé configurée.';
  $('#ibou-key-state').textContent=cfg.ibou_key_set?'Une clé est présente en mémoire. Laissez vide pour la conserver.':'Aucune clé configurée.';
}

/* ---------- bandeau de traitement ---------- */
const boutonReprise=el('button','button primary','Reprendre');boutonReprise.id='job-reprise';boutonReprise.type='button';boutonReprise.hidden=true;
const lienMoteurs=el('button','button','Moteurs et réglages');lienMoteurs.type='button';lienMoteurs.dataset.vue='moteurs';lienMoteurs.hidden=true;
$('#stop').before(lienMoteurs,boutonReprise);
const jobBar=el('div','job-track','<progress id="job-bar" max="1" value="0"></progress><div id="job-facts" class="job-facts"></div><div id="job-trail" class="job-trail"></div>');
$('#job-progress').after(jobBar);
function rendreBandeau(job){
  const running=job.status==='running',arret=['paused','error'].includes(job.status);
  $('#job-banner').hidden=!(running||arret);
  $('#job-banner').classList.toggle('stopped',arret);
  const etape=libEtape(job.stage)||libEtape(job.etape_chaine)||'Traitement';
  $('#job-message').textContent=humain(job.message||'');
  $('#job-progress').textContent=`${etape} — ${running?'en cours':arret?(job.status==='paused'?'suspendu, données conservées':'erreur'):'terminé'}`;
  $('#stop').hidden=!running;
  boutonReprise.hidden=!arret||job.kind!=='chaine';
  lienMoteurs.hidden=!arret;
  const bar=$('#job-bar');bar.hidden=!job.total;if(job.total){bar.max=job.total;bar.value=Math.min(job.done||0,job.total)}
  const f=[];
  if(job.total)f.push(`<b>${number(job.done||0)} / ${number(job.total)}</b> (${Math.floor(100*(job.done||0)/job.total)} %)`);
  if(running&&job.rate_per_hour)f.push(`${number(Math.round(job.rate_per_hour))} par heure`);
  if(running&&job.eta_seconds!=null)f.push(`reste environ <b>${fmtDuration(job.eta_seconds)}</b>`);
  else if(running&&job.total)f.push('durée restante calculée après 30 s');
  if(job.started)f.push(`${running?'lancé':'dernier lancement'} ${date(job.started)}${running?` · ${fmtDuration(since(job.started))} écoulées`:''}`);
  $('#job-facts').innerHTML=f.join(' · ');
  const trail=(job.trail||[]).filter(t=>t.stage!==job.kind||(job.trail||[]).length===1);
  $('#job-trail').innerHTML=trail.length>1?trail.map(t=>`<span class="${t.ended?'past':'now'}">${esc(libEtape(t.stage))}${t.ended?` · ${fmtDuration((new Date(t.ended)-new Date(t.started))/1000)}`:''}</span>`).join('<i>›</i>'):'';
  if(CH.vue==='revue'&&typeof ajusterCarte==='function')ajusterCarte();
}
boutonReprise.onclick=()=>ecrire(async()=>{
  const e=CH.pulse.job.etape_chaine||'tout';
  await api('chaine/lancer',{etape:e==='m5'?'tout':e});notify('Reprise lancée : ce qui est fait n’est pas refait.');await rafraichir(true)
},boutonReprise);

/* ---------- lancement ---------- */
async function lancerEtape(etape,extra={}){
  return ecrire(async()=>{
    await api('chaine/lancer',Object.assign({etape},extra));
    notify(`Lancé : ${libEtape(etape)}.`);
    await rafraichir(true);
  });
}
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-lancer]');if(!b)return;
  const etape=b.dataset.lancer;
  if(etape==='m5'){go('requetes',{onglet:'budget'});return}
  lancerEtape(etape);
});

/* ---------- vue d'ensemble ---------- */
const CARTES=[
  {id:'m1',titre:'M1',nom:'Pages et unités',ecran:'unites',sous:[['m1.page','Ancrage, classes'],['m1.unites','Unités'],['m1.faits','Faits'],['m1.sujets','Sujets, dédoublonnage']]},
  {id:'m2',titre:'M2',nom:'Sources et typologie',ecran:'unites',sous:[['m2','Sources, types']]},
  {id:'m3',titre:'M3',nom:'Voisins et paternité',ecran:'groupes',sous:[['m3','Voisins, paternité'],['m3.paternite','Paternité seule']]},
  {id:'m4',titre:'M4',nom:'Statuts et requêtes',ecran:'requetes',sous:[['m4','Statuts, requêtes']]},
  {id:'m5',titre:'M5',nom:'Criticité et Ibou',ecran:'requetes',sous:[['m5.criticite','Criticité'],['m5','Vérification Ibou']]},
  {id:'m6',titre:'M6',nom:'Propositions',ecran:'revue',sous:[['m6','Propositions']]}
];
/* Étapes qui tournent en partie sans le moteur manquant (résultat partiel) au lieu de s'arrêter. */
const PARTIELLES_SANS_MOTEUR=['m1.sujets','m3'];
const coutsDe=(prefixe,etat)=>{
  const l=(etat.couts||[]).filter(c=>c.step===prefixe||c.step.startsWith(prefixe+'.'));
  const somme=k=>l.reduce((n,c)=>n+(c[k]||0),0);
  return {s:somme('secondes_locales'),tin:somme('tokens_in'),tout:somme('tokens_out'),u:somme('unites_ibou'),n:somme('executions'),erreurs:somme('erreurs'),lignes:l};
};
function lignesCarte(id,e){
  const c=e.compteurs,m1=c.m1,m2=c.m2,m3=c.m3,m4=c.m4,m5=c.m5,m6=c.m6;
  const sum=o=>Object.values(o||{}).reduce((a,b)=>a+b,0);
  if(id==='m1')return [['Pages',`${nb(c.pages.pages)} <small>(${nb(c.pages.versions)} versions)</small>`],['Unités',nb(m1.unites)],['Avec faits',`${nb(m1.unites_avec_faits)} · ${pct(m1.unites_avec_faits,m1.unites)}`],['Groupes',nb(m1.groupes)]];
  if(id==='m2')return [['Sourcées',nb((m2.par_sourcing.sourcee_externe||0)+(m2.par_sourcing.sourcee_interne||0))],['Sans source',nb(m2.par_sourcing.sans_source)],['Sources manquantes',nb(m2.sources_manquantes)]];
  if(id==='m3')return [['Relations',nb(sum(m3.relations))],['À arbitrer',nb(m3.paternite?.a_arbitrer||0)],['Périmées par date',nb(m3.par_statut_temporel?.perimee_par_date||0)]];
  if(id==='m4')return [['À vérifier',nb((m4.par_statut.a_verifier||0)+(m4.par_statut.a_verifier_faible||0))],['Requêtes prêtes',nb(m4.requetes_par_statut?.prete||0)],['Sans faits (pas de requête)',nb(m4.en_attente_decomposition)]];
  if(id==='m5')return [['Critiques',nb(m5.critiques)],['Appels Ibou',nb(sum(m5.appels_par_statut))],['Verdicts',nb(sum(m5.verdicts))]];
  return [['En attente',nb((m6.par_statut.proposee||0)+(m6.par_statut.a_rediger||0)+(m6.par_statut.reportee||0))],['Appliquées',nb((m6.par_statut.appliquee||0)+(m6.par_statut.modifiee||0))],['À rédiger à la main',nb(m6.par_statut.a_rediger||0)]];
}
/* État d'une sous-étape : en cours, partielle (dernier lancement), bloquée (prérequis), à faire, à jour. */
function etatSousEtape(etape,e,job){
  const reste=e.reste?.[etape],a=reste?reste.a_faire:null,raison=e.prerequis?.[etape],issue=e.issues?.[etape];
  const compte=a==null?'':a===0?'à jour':number(a)+' à faire';
  if(job.status==='running'&&(job.etape_chaine===etape||job.stage===etape))return {etat:'active',texte:'en cours'};
  if(issue&&issue.issue==='partielle')return {etat:'partiel',texte:'partielle',raison:issue.raison,relancer:true};
  if(raison&&PARTIELLES_SANS_MOTEUR.includes(etape))return {etat:'partiel',texte:compte,raison:(etape==='m3'?'sans NLI : étapes 3 à 5 seulement':'sans embeddings : dédoublonnage exact seulement'),detail:raison};
  if(raison)return {etat:'bloque',texte:compte,raison:causeCourte(raison),detail:raison};
  return {etat:a===0?'fait':'afaire',texte:compte};
}
function carteChaine(def,e,job){
  const active=job.status==='running'&&(job.etape_chaine||'').split('.')[0]===def.id;
  const etats=def.sous.map(([etape])=>etatSousEtape(etape,e,job));
  const sous=def.sous.map(([etape,nom],i)=>{
    const x=etats[i];
    const ligne2=x.etat==='partiel'?`<small class="ss-raison" title="${esc(humain(x.detail||x.raison||''))}">${x.relancer?'Partielle : ':''}${esc(courtTexte(humain(x.raison||''),80))}${x.relancer?' — relancer pour continuer':''}</small>`
      :x.etat==='bloque'?`<small class="ss-raison" title="${esc(humain(x.detail))}">Bloquée : ${esc(x.raison)}</small>`:'';
    return `<li class="ss ${x.etat}"><span class="ss-nom">${esc(nom)}</span><span class="ss-reste">${esc(x.texte)}</span>${btnE('▶',`data-lancer="${esc(etape)}" aria-label="${x.relancer?'Relancer':'Lancer'} ${esc(nom)}" title="${x.relancer?'Relancer pour continuer':'Lancer'} : ${esc(libEtape(etape))}"`,'mini')}${ligne2}</li>`;
  }).join('');
  const k=coutsDe(def.id,e);
  const lignes=lignesCarte(def.id,e).map(([a,b])=>`<div><span>${esc(a)}</span><b>${b}</b></div>`).join('');
  const resteTotal=def.sous.reduce((n,[s])=>n+((e.reste?.[s]?.a_faire)||0),0);
  const bloquee=etats.some(x=>x.etat==='bloque'),partielle=etats.some(x=>x.etat==='partiel');
  const etat=active?'active':bloquee?'bloquee':partielle?'partielle':resteTotal?'afaire':'fait';
  const tag=active?'En cours':bloquee?'Bloquée':partielle?'Partielle':resteTotal?'À faire':'À jour';
  return `<article class="etape ${etat}"><div class="etape-tete"><span class="etape-id">${def.titre}</span><h3><button type="button" class="lien titre-etape" data-vue="${def.ecran}">${esc(def.nom)}</button></h3><span class="etape-tag">${tag}</span></div>
    <div class="etape-chiffres">${lignes}</div>
    <ul class="ss-liste">${sous}</ul>
    <div class="etape-cout" title="Temps de calcul local, tokens (envoyés et reçus), unités Ibou"><span>${k.n?dureeCourte(k.s):'jamais lancée'}</span><span>${nb(k.tin+k.tout)} tokens</span><span>${nb(k.u)} u. Ibou</span></div></article>`;
}

/* Diagnostic des moyens : LLM (local ou tiers), double passe, NLI, embeddings, Ibou — chaque point avec l'action à faire. */
function diagnostics(e,cfg){
  const m=e.moteurs||{},llm=m.llm||{},f=m.fonctions||{},pr=e.prerequis||{},out=[];
  const touchees=re=>Object.entries(pr).filter(([,v])=>v&&re.test(v)).map(([k])=>libEtape(k));
  const nomLlm=`${lib('fournisseur',llm.fournisseur)}${llm.modele?' · '+llm.modele:''}`;
  if(!cfg.model)out.push({titre:'Aucun LLM configuré',detail:'Les faits atomiques (M1 faits) demandent un LLM local (LM Studio) ou GLiREL. Sans faits : pas de cadre, donc pas de requête Ibou pour les unités migrées.',etapes:touchees(/extracteur|LLM/),actions:[{t:'Configurer LM Studio',vue:'moteurs',params:{onglet:'connexions'},primaire:true}]});
  else if(llm.type==='tiers'&&!llm.tiers_autorise)out.push({titre:`Le LLM configuré (${nomLlm}) est un service tiers payant`,detail:'Les étapes « LLM local » (unités par LLM, faits, reformulation des requêtes, réécriture des propositions, NLI de repli) refusent de lui envoyer vos textes : elles s’arrêtent ou se replient sur les règles.',etapes:touchees(/tiers payant|LLM/),actions:[{t:'Passer sur LM Studio',vue:'moteurs',params:{onglet:'connexions'},primaire:true},{t:'Autoriser le LLM tiers',vue:'moteurs',params:{onglet:'moteurs',focus:'llm_tiers_autorise'}}]});
  else if(!llm.disponible)out.push({titre:'LLM indisponible',detail:humain(llm.raison),etapes:touchees(/LLM/),actions:[{t:'Ouvrir les connexions',vue:'moteurs',params:{onglet:'connexions'}}]});
  const dp=f.nli?.double_passe||llm.double_passe;
  if(dp==='impossible')out.push({titre:'Aucun moteur NLI',detail:'Le NLI qualifie les relations entre unités (M3), juge les preuves Ibou (M5) et contrôle les sorties du LLM. Sans lui, M3 ne fait que les étapes 3 à 5 et M5 ne peut pas partir.',etapes:touchees(/NLI|double passe/),actions:[{t:'Installer le NLI local : lancer installer_modeles.cmd',info:true},{t:'Voir les moteurs',vue:'moteurs',params:{onglet:'moteurs'}}]});
  else if(dp==='refusee')out.push({titre:'Double passe impossible : le seul NLI disponible est le LLM',detail:'Contrôler une sortie du LLM par ce même LLM est refusé par défaut : unités par LLM et faits s’arrêtent, M5 ne corrige pas automatiquement, M6 laisse la réécriture « à rédiger ».',etapes:touchees(/double passe/),actions:[{t:'Installer le NLI local : lancer installer_modeles.cmd',info:true},{t:'Autoriser la double passe faible',vue:'moteurs',params:{onglet:'moteurs',focus:'nli_par_llm'}}]});
  if((f.embeddings?.actif||'aucun')==='aucun')out.push({titre:'Aucun moteur d’embeddings',detail:'Sans embeddings : ni sujets ni voisins sémantiques ; M1 sujets ne fait que le dédoublonnage exact (résultat partiel).',etapes:touchees(/embeddings/),actions:[{t:'Installer sentence-transformers : lancer installer_modeles.cmd',info:true},{t:'Ou activer les embeddings du fournisseur',vue:'moteurs',params:{onglet:'connexions'}}]});
  if(!m.ibou?.cle)out.push({titre:'Clé Ibou manquante',detail:'M5 (vérification par Ibou) ne peut pas partir. Rien n’est envoyé à Ibou sans plafond explicite.',etapes:touchees(/Ibou/),actions:[{t:'Renseigner la clé Ibou',vue:'moteurs',params:{onglet:'connexions'}}]});
  return out;
}
function aFaire(e,p){
  const bloquants=[],infos=[],c=e.compteurs,pages=c.pages,ch=p.chaine||{},job=p.job,regl=CH.reglages;
  const add=(liste,n,titre,detail,actions,etapes)=>liste.push({n,titre,detail,actions:actions||[],etapes:etapes||[]});
  if(!e.migre)add(bloquants,e.reste.migration?.a_faire,'Migrer l’ancien parcours','Les affirmations, requêtes, appels Ibou et propositions de l’ancien parcours ne sont pas encore repris dans les unités. À faire avant tout le reste ; sans risque, peut être relancée.',[{t:'Lancer la migration',action:'migration',primaire:true}]);
  if(['paused','error'].includes(job.status)&&job.kind==='chaine')add(bloquants,null,job.status==='error'?'Traitement en erreur':'Traitement arrêté',humain(job.message),[]);
  for(const [etape,issue] of Object.entries(e.issues||{}))
    if(issue.issue==='partielle')add(bloquants,null,`${libEtape(etape)} : résultat partiel`,humain(issue.raison),[{t:'Relancer pour continuer',lancer:etape,primaire:true}]);
  const sansDate=pages.pages_sans_date_publication??0;
  if(sansDate>0)add(bloquants,sansDate,`page(s) sur ${nb(pages.pages)} sans date de publication`,'Sans elle, ni ancrage relatif ni critère de paternité par date. Renseignez-la dans Pages, puis relancez M1 page et M3.',[{t:'Renseigner les dates',vue:'pages',params:{filtre:'sans_date'}}]);
  if(regl&&!(regl.domaines_effectifs||[]).length)add(bloquants,null,'Aucun domaine de site réglé','Les liens ne peuvent pas être classés en internes ou externes, et le site audité n’est pas exclu des preuves Ibou.',[{t:'Régler le domaine',vue:'pages',params:{focus:'domaines'}}]);
  if(ch.propositions_en_attente)add(bloquants,ch.propositions_en_attente,'proposition(s) de correction à décider','Triées par criticité puis par page. Rien n’est appliqué sans votre décision ; le site d’origine n’est jamais modifié.',[{t:'Ouvrir la revue',vue:'revue',params:{statut:'en_attente'},primaire:true}]);
  const arb=c.m3.paternite?.a_arbitrer||0;
  if(arb)add(bloquants,arb,'groupe(s) à arbitrer pour la paternité','Aucun critère automatique ne désigne l’unité parente : choisissez-la en un clic.',[{t:'Arbitrer',vue:'groupes',params:{onglet:'a_arbitrer'}}]);
  const arv=c.m4.requetes_par_statut?.a_revoir||0;
  if(arv)add(bloquants,arv,'requête(s) Ibou à corriger','Les contrôles ont refusé leur texte : elles ne partiront pas chez Ibou tant que vous ne les avez pas corrigées.',[{t:'Corriger',vue:'requetes',params:{statut:'a_revoir'}}]);
  if(e.reste?.m5?.a_faire)add(bloquants,e.reste.m5.a_faire,'requête(s) critique(s) prête(s) pour Ibou','M5 les envoie d’abord, dans le plafond que vous fixez. Relisez-les avant d’engager des unités.',[{t:'Relire et lancer',vue:'requetes',params:{statut:'prete'}}]);
  const per=c.m3.par_statut_temporel?.perimee_par_date||0;
  if(per)add(infos,per,'unité(s) périmée(s) par date','Annonces dont la date est échue : une correction ou une mention est proposée dans la revue.',[{t:'Voir les unités',vue:'unites',params:{temporel:'perimee_par_date'}}]);
  const att=c.m4.en_attente_decomposition||0;
  if(att)add(infos,att,'unité(s) à vérifier sans faits','État normal tant que M1 faits n’a pas tourné (LLM local ou GLiREL) : sans faits, pas de cadre ni de requête Ibou.',[{t:'Voir ces unités',vue:'unites',params:{statut:'a_verifier',sans_faits:'1'}}]);
  const inc=c.m1.par_classe?.inconnu||0;
  if(inc)add(infos,inc,`unité(s) de classe d’obsolescence inconnue (${pct(inc,c.m1.unites)})`,'Aucune règle n’a pu trancher : elles sont traitées comme vérifiables (comptées dans le taux d’obsolescence et vérifiées si elles ont un cadre).',[{t:'Voir ces unités',vue:'unites',params:{classe:'inconnu'}}]);
  const pt=e.reste?.m5?.plus_tard;
  if(pt)add(infos,pt,'requête(s) non critique(s) en file « plus tard »','Envoyées seulement si vous cochez « inclure la file plus tard » au lancement de M5.',[{t:'Voir',vue:'requetes',params:{statut:'plus_tard'}}]);
  const llm=e.moteurs?.llm||{};
  if(llm.double_passe==='faible')add(infos,null,'Double passe faible autorisée','Le LLM contrôle ses propres sorties faute de NLI local : les exécutions concernées sont marquées « double passe faible ».',[{t:'Moteurs et réglages',vue:'moteurs',params:{onglet:'moteurs',focus:'nli_par_llm'}}]);
  if(llm.type==='tiers'&&llm.tiers_autorise&&p.config?.model)add(infos,null,'LLM tiers autorisé pour les étapes « LLM local »','Vos textes partent chez le fournisseur tiers et chaque appel est facturé en tokens.',[{t:'Moteurs et réglages',vue:'moteurs',params:{onglet:'moteurs',focus:'llm_tiers_autorise'}}]);
  return {bloquants,infos,diags:diagnostics(e,p.config||{})};
}
/* Moyens manquants : une carte compacte par moyen, avec l'effet et l'action à faire. */
function carteDiag(d){
  const i=aFaireItems.push(d)-1;
  const actions=d.actions.map((a,j)=>a.info?`<span class="todo-info">${esc(a.t)}</span>`:btnE(esc(a.t)+' →',`data-todo="${i}" data-act="${j}"`,a.primaire?'button primary':'button')).join('');
  return `<article class="diag"><strong>${esc(d.titre)}</strong><p>${esc(d.detail)}</p>${d.etapes.length?`<small class="todo-etapes" title="${esc(d.etapes.join(' · '))}">Bloque : ${esc(d.etapes.map(x=>x.split(' · ')[0]+' '+(x.split(' · ')[1]||'').split(',')[0]).join(', '))}</small>`:''}<div class="diag-actions">${actions}</div></article>`;
}
let aFaireItems=[];
function ligneAFaire(x){
  const i=aFaireItems.push(x)-1;
  const actions=x.actions.map((a,j)=>a.info?`<span class="todo-info">${esc(a.t)}</span>`:btnE(esc(a.t)+' →',`data-todo="${i}" data-act="${j}"`,a.primaire?'button primary':'button')).join('');
  return `<li class="todo-l"><span class="todo-n">${x.n!=null?number(x.n):'!'}</span><div><strong>${esc(x.titre)}</strong><p>${esc(x.detail)}</p>${x.etapes.length?`<small class="todo-etapes">Étapes concernées : ${x.etapes.map(esc).join(' · ')}</small>`:''}</div><div class="todo-actions">${actions}</div></li>`;
}
async function rendreEnsemble(){
  const racine=$('#view-ensemble');if(!racine||racine.hidden&&CH.vue!=='ensemble')return;
  const e=CH.etat,p=CH.pulse;if(!e||!p)return;
  if(!CH.reglages)try{await chargerReglages()}catch{}
  if(surChamp(racine))return;
  const job=p.job,{bloquants,infos,diags}=aFaire(e,p);aFaireItems=[];
  const dr=e.dernier_resultat;
  const total=(e.couts||[]).reduce((a,c)=>({s:a.s+(c.secondes_locales||0),tin:a.tin+(c.tokens_in||0),tout:a.tout+(c.tokens_out||0),u:a.u+(c.unites_ibou||0)}),{s:0,tin:0,tout:0,u:0});
  const budget=$('#tout-budget')?.value||'0';
  const ouvert=$('#view-ensemble .todo-reste')?.open;
  racine.innerHTML=`${avisOccupe()}
  <section class="chaine-bloc"><div class="bloc-tete"><h2>Chaîne de vérification</h2>
   <div class="chaine-pied"><span title="Temps de calcul local cumulé de toutes les étapes">Coût : <b>${dureeCourte(total.s)}</b> de calcul</span><span><b>${nb(total.tin)}</b> tokens envoyés · <b>${nb(total.tout)}</b> reçus</span><span><b>${nb(total.u)}</b> unités Ibou</span>${dr?`<span class="dernier ${dr.ok?'ok':'ko'}" title="${esc(humain(dr.raison||''))}">Dernier lancement (${esc(libEtape(dr.etape))}, ${date(dr.date)}) : ${dr.ok?'terminé':(dr.partiel?'partiel à ':'arrêté à ')+esc(libEtape(dr.arret||dr.etape))}</span>`:''}</div>
   <div class="bloc-actions"><label class="inline" title="Plafond 0 : rien ne part chez Ibou (M5 sautée).">Plafond Ibou<input type="number" id="tout-budget" min="0" max="1000000" value="${esc(budget)}" aria-describedby="tout-aide"></label>
   ${btnE('▶ Tout lancer','data-lancer-tout title="Toutes les étapes dans l’ordre, sans refaire ce qui est fait ; arrêt motivé à la première étape impossible."',' button primary')}<span id="tout-aide" class="sr">Toutes les étapes dans l’ordre, sans refaire ce qui est fait ; arrêt motivé à la première étape impossible. Plafond 0 : rien ne part chez Ibou.</span></div></div>
   <div class="chaine-ligne">${CARTES.map((d,i)=>carteChaine(d,e,job)+(i<CARTES.length-1?'<span class="fleche" aria-hidden="true">›</span>':'')).join('')}</div></section>
  <section class="chaine-bloc"><div class="bloc-tete"><h2>À faire</h2><span class="aide">${bloquants.length+diags.length?`${diags.length?`${diags.length} moyen(s) manquant(s), `:''}${bloquants.length} point(s) : ce qui bloque la chaîne d’abord, puis ce qui attend votre décision`:'Rien n’attend d’action de votre part.'}</span></div>
   ${diags.length?`<div class="diag-grille">${diags.map(carteDiag).join('')}</div>`:''}
   <ol class="todo-liste">${bloquants.map(ligneAFaire).join('')||(diags.length?'':'<li class="todo-l"><span class="todo-n">✓</span><div><strong>Tout est à jour</strong><p>Importez de nouvelles pages ou exportez les résultats.</p></div></li>')}</ol>
   ${infos.length?`<details class="todo-reste"${ouvert?' open':''}><summary>Pour information · ${infos.length} point(s)</summary><ol class="todo-liste">${infos.map(ligneAFaire).join('')}</ol></details>`:''}</section>`;
  appliquerOccupe();
}
document.addEventListener('click',e=>{
  const t=e.target.closest('[data-todo]');
  if(t){
    const x=aFaireItems[Number(t.dataset.todo)],a=x?.actions[Number(t.dataset.act||0)];if(!a)return;
    if(a.action==='migration'){ecrire(async()=>{await api('chaine/migrer',{});notify('Migration lancée.');await rafraichir(true)},t);return}
    if(a.lancer){lancerEtape(a.lancer);return}
    if(a.vue)go(a.vue,a.params||{});return;
  }
  if(e.target.closest('[data-lancer-tout]')){
    const budget=Number($('#tout-budget')?.value||0);
    lancerEtape('tout',budget>0?{budget}:{});
  }
});
enregistrer('ensemble',{ouvrir:async()=>{if(CH.etat)await rendreEnsemble()},actualiser:rendreEnsemble});

/* ---------- démarrage ---------- */
(function demarrer(){
  const depart=(location.hash||'').replace('#','');
  go(TITRES[depart]?depart:'ensemble');
  planifier(0);
})();

/* ---------- connexions : champs selon le fournisseur, clé OpenRouter ---------- */
toggleProvider=function(){
  const p=$('#provider').value;
  $$('.lm-fields').forEach(x=>x.hidden=p!=='lmstudio');
  $$('.openai-fields').forEach(x=>x.hidden=p==='openrouter');
  $$('.openrouter-fields').forEach(x=>x.hidden=p!=='openrouter');
};
const saveBeforeOpenrouter=saveSettings;
saveSettings=async function(){
  const key=$('#openrouter-key').value.trim();
  if(key)await api('config',{openrouter_key:key});
  $('#openrouter-key').value='';
  await saveBeforeOpenrouter();
};
$('#provider').onchange=toggleProvider;toggleProvider();
