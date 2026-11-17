'use strict';
/* Écrans de données : Pages, Unités (avec volet de détail), Groupes et paternité. */

const qs=o=>{const p=new URLSearchParams();for(const[k,v]of Object.entries(o))if(v!==''&&v!=null)p.set(k,v);return p.toString()};
const selectF=(nom,libelle,options,valeur)=>`<label class="filtre">${libelle}<select data-filtre="${nom}">${options.map(([v,t])=>`<option value="${esc(v)}"${String(v)===String(valeur??'')?' selected':''}>${esc(t)}</option>`).join('')}</select></label>`;
const optionsDe=(groupe,valeurs,tous='Tous')=>[['',tous],...valeurs.map(v=>[v,lib(groupe,v)])];
const pagerHtml=(vue,total,offset,limite)=>total?`<div class="pager"><span>${nb(offset+1)} à ${nb(Math.min(offset+limite,total))} sur ${nb(total)}</span><span><button type="button" class="button" data-pager="${vue}" data-pas="-1"${offset?'':' disabled'}>← Précédent</button> <button type="button" class="button" data-pager="${vue}" data-pas="1"${offset+limite<total?'':' disabled'}>Suivant →</button></span></div>`:'';
const videHtml=(titre,detail)=>`<div class="vide"><strong>${esc(titre)}</strong><p>${esc(detail||'')}</p></div>`;
const lisible=obj=>{
  if(obj==null||obj==='')return '—';
  if(typeof obj==='string'){try{const j=JSON.parse(obj);if(j&&typeof j==='object')return lisible(j)}catch{}return esc(obj)}
  if(typeof obj!=='object')return esc(obj);
  const lignes=Object.entries(obj).filter(([,v])=>v!==null&&v!=='').map(([k,v])=>`<span class="kv"><i>${esc(k.replace(/_/g,' '))}</i> ${typeof v==='object'?esc(JSON.stringify(v)):esc(v)}</span>`);
  return lignes.join('')||'—';
};

/* Critère décisif de la paternité, en phrases (les identifiants d'unités ne sont pas affichés) */
function expliquerPaternite(d,critere){
  if(typeof d==='string'){try{d=JSON.parse(d)}catch{return esc(d)}}
  if(!d||typeof d!=='object')return '';
  const l=[],n=v=>Array.isArray(v)?v.length:v&&typeof v==='object'?Object.keys(v).length:null;
  if(d.membres!=null)l.push(`${nb(d.membres)} unité(s) sur ${nb(d.pages)} page(s)`);
  if(d.trivial)l.push(d.trivial==='membre_unique'?'groupe d’un seul membre : paternité triviale':esc(String(d.trivial).replace(/_/g,' ')));
  if(d.ecart_egalite!=null)l.push(`écart de centralité entre candidats : ${String(d.ecart_egalite).replace('.',',')} (égalité : le critère par centralité ne tranche pas)`);
  const inconnues=n(d.dates_inconnues);
  if(inconnues)l.push(`date de publication inconnue pour ${nb(inconnues)} candidat(s) : le critère par date n’a pas pu trancher`);
  const sources=d.sources&&typeof d.sources==='object'?Object.values(d.sources):null;
  if(sources&&critere==='a_arbitrer'&&new Set(sources).size<=1)l.push('nombre de sources identique pour tous les candidats');
  for(const [k,v] of Object.entries(d))if(!['membres','pages','trivial','ecart_egalite','dates_inconnues','dates','sources','candidats','centralite'].includes(k)&&v!=null&&typeof v!=='object')l.push(`${esc(k.replace(/_/g,' '))} : ${esc(v)}`);
  return l.map(x=>`<span class="kv">${x}</span>`).join('')||'';
}
/* Cadre d'un groupe ou d'une requête : puces lisibles (prédicat par son libellé). */
const libPredicat=n=>!n?'':(CH.predicats&&CH.predicats[n])||String(n).replace(/_/g,' ');
function cadreHtml(c){
  if(typeof c==='string'){try{c=JSON.parse(c)}catch{return esc(c)}}
  if(!c||typeof c!=='object')return '';
  const p=[];
  if(c.sujet)p.push(['Sujet',c.sujet]);if(c.predicat)p.push(['Prédicat',libPredicat(c.predicat)]);if(c.lieu)p.push(['Lieu',c.lieu]);
  const per=c.periode&&typeof c.periode==='object'?c.periode.libelle:c.periode;if(per)p.push(['Période',per]);
  if(c.portee)p.push(['Portée',c.portee]);if(c.valeur)p.push(['Valeur',c.valeur]);
  if(c.classe)p.push(['Classe',LIB.classe[c.classe]??LIB.type[c.classe]??lib('classe',c.classe)]);if(c.famille)p.push(['Famille',String(c.famille).replace(/_/g,' ')]);
  if((c.inconnues||[]).length)p.push(['Inconnues',c.inconnues.map(x=>typeof x==='object'?(x.libelle||x.nom||JSON.stringify(x)):x).join(', ')]);
  if(c.perimee)p.push(['Annonce','échue']);
  return p.map(([k,v])=>`<span class="chip"><i>${esc(k)}</i> ${esc(courtTexte(v,48))}</span>`).join('')||'<span class="muted">Cadre vide (pas encore de faits)</span>';
}
const chargerChamp=(racine,sel,fn)=>racine.querySelector(sel)?.addEventListener('input',debounce(fn,300));

/* ---------- paragraphe de l'unité (fourni par /api/chaine/unite/<id>) ---------- */
function paragrapheSurligne(par){
  if(!par||!par.texte)return {html:null};
  const t=par.texte,a=par.phrase_debut,b=par.phrase_fin;
  if(!par.trouve||a==null||b==null||b<=a||b>t.length)return {html:null};
  return {html:esc(t.slice(0,a))+'<mark>'+esc(t.slice(a,b))+'</mark>'+esc(t.slice(b)),tronque:par.tronque};
}
/* Qui a écrit une valeur de l'unité : exécution (étape, date) ou décision de revue. */
const origineHtml=o=>!o?'':o.etape==='decision'?'<small class="origine">décidé en revue</small>':`<small class="origine">${esc(libEtape(o.etape))}, ${date(o.debut)}${o.double_passe==='faible'?' · double passe faible':''}</small>`;

/* ---------- volet : détail d'une unité ---------- */
const volet=el('dialog','volet');volet.id='volet-unite';document.body.append(volet);
volet.addEventListener('click',e=>{
  if(e.target===volet||e.target.closest('.close-dialog')||e.target.closest('[data-vue]'))volet.close();
  const p=e.target.closest('[data-ouvrir-revue]');if(p){volet.close();go('revue',{id:p.dataset.ouvrirRevue})}
  const u=e.target.closest('[data-unite]');if(u)ouvrirUnite(u.dataset.unite);
});
async function ouvrirUnite(id){
  volet.innerHTML='<div class="volet-tete"><h2>Chargement…</h2><button class="icon-button close-dialog" aria-label="Fermer">×</button></div>';
  if(!volet.open)volet.showModal();
  try{
    const d=await api('chaine/unite/'+encodeURIComponent(id));
    volet.innerHTML=voletUnite(d);
    volet.querySelector('.volet-corps').scrollTop=0;
  }catch(e){volet.close();notify(e.message,true)}
}
const section=(titre,corps,{n=null,ouvert=false}={})=>`<details class="vs"${ouvert?' open':''}><summary>${esc(titre)}${n!=null?` <span class="vs-n">${nb(n)}</span>`:''}</summary><div class="vs-corps">${corps}</div></details>`;
function voletUnite(d){
  const u=d.unite,pg=d.page,e=CH.etat||{},o=d.origines||{};
  const para=paragrapheSurligne(d.paragraphe);
  const preuveDe=id=>(d.preuves||[]).find(p=>p.id===id);
  const preuveCourte=id=>{const p=preuveDe(id);return p?`preuve : ${esc(p.domain||p.title||'—')}${p.page_date?' ('+esc(p.page_date)+')':''}`:'preuve de vérification'};
  const faits=d.faits||[];
  const attente=!faits.length&&u.status==='a_verifier';
  const raisonFaits=e.prerequis?.['m1.faits'];
  const regle=(l,v,r,orig)=>`<tr><th>${l}</th><td>${v}${orig||''}</td><td class="regle">${r?`règle : <code>${esc(r)}</code>`:''}</td></tr>`;
  const ancrage=`<table class="kv-table"><tbody>
    <tr><th>Date d’ancrage</th><td>${u.anchor_date?jour(u.anchor_date):'<span class="muted">Aucune : date inconnue</span>'}${origineHtml(o.anchor_run)}</td><td class="regle">origine : ${esc(lib('ancre',u.anchor_kind))}</td></tr>
    <tr><th>Échéance</th><td>${u.expires?jour(u.expires):'—'}</td><td class="regle">${u.temporal_status==='perimee_par_date'?'échue : annonce périmée':''}</td></tr>
    ${regle('Classe d’obsolescence',esc(lib('classe',u.obs_class))+(u.obs_class==='inconnu'||!u.obs_class?'<small>Aucune règle n’a tranché : l’unité est traitée comme vérifiable.</small>':''),u.obs_rule,origineHtml(o.obs_run))}
    ${regle('Statut temporel',bd('temporel',u.temporal_status),'',origineHtml(o.temporal_run))}
    ${regle('Sourcing',esc(lib('sourcing',u.sourcing))+(u.sourcing_conf!=null?` <small>(confiance ${u.sourcing_conf})</small>`:''),u.sourcing_rule)}
    ${regle('Type d’affirmation',esc(lib('type',d.type?.type))+(d.type?.confidence!=null?` <small>(confiance ${d.type.confidence})</small>`:''),d.type?.rule)}
    ${regle('Statut final (M4)',bd('statut',u.status||'aucun')+(u.status_suivi?` <small>suit le statut le plus grave des unités dont elle dépend : ${esc(lib('statut',u.status_suivi))}</small>`:''),'',origineHtml(o.status_run))}
    ${u.verdict?regle('Verdict (M5)',bd('verdict',u.verdict)+(u.verified_at?` <small>vérifiée le ${esc(jour(u.verified_at))}</small>`:''),'',origineHtml(o.verdict_run)):''}
    ${u.critical!=null?regle('Criticité',(u.critical?'<span class="bd bad">Critique</span>':'<span class="bd mute">Plus tard</span>')+(u.critical_score!=null?` <small>score ${Number(u.critical_score).toFixed(2)}</small>`:''),'',origineHtml(o.critical_run)):''}
    ${u.source_manquante?'<tr><th>Référence</th><td colspan="2"><span class="bd warn">Référence interne manquante</span></td></tr>':''}</tbody></table>`;
  const tFaits=faits.length?`<table class="mini"><thead><tr><th>Sujet</th><th>Prédicat</th><th>Objet</th><th>Origine</th><th>Statut</th></tr></thead><tbody>${faits.map(f=>`<tr><td>${esc(f.subject)}</td><td>${esc(libPredicat(f.predicate))}${f.period?`<small>${esc(f.period)}</small>`:''}</td><td>${esc(f.object)}${f.object_type?`<small>${esc(f.object_type)}${f.scope?' · '+esc(f.scope):''}</small>`:''}</td><td>${esc(lib('fait_origine',f.origin))}${f.evidence_id?`<small>${preuveCourte(f.evidence_id)}</small>`:''}</td><td>${bd('fait_statut',f.status)}${f.replaced_by?'<small>remplacé</small>':''}</td></tr>`).join('')}</tbody></table>`
    :`<div class="explication"><strong>${attente?'En attente de décomposition':'Aucun fait'}</strong><p>${attente?'Cette unité n’a pas encore de faits : M1 faits n’a pas tourné avec un LLM (ou GLiREL). C’est l’état normal d’une unité migrée ; ce n’est pas une erreur.':'Aucun fait n’a été extrait pour cette unité.'} Sans faits, pas de cadre, donc pas de requête Ibou.${raisonFaits?`<br>Ce qui manque : ${esc(humain(raisonFaits))}`:''}</p></div>`;
  const extrait=h=>{const p=preuveDe(h.evidence_id);return p&&h.span_start!=null&&h.span_end!=null&&h.span_end<=(p.text||'').length?`<blockquote>${esc(p.text.slice(h.span_start,h.span_end))}</blockquote>`:''};
  const hyp=(d.hypotheses||[]).length?`<table class="mini"><tbody>${d.hypotheses.map(h=>`<tr><td>${esc(lib('hypothese',h.kind))}${h.resolved_value!=null?'<br><span class="bd ok">Résolue</span>':''}</td><td>${esc(h.text||[h.subject,h.predicate,h.object].filter(Boolean).join(' · '))}<small>${esc(h.reason||'')}</small>${h.resolved_value!=null?`<small>Valeur trouvée par la vérification : <b>${esc(h.resolved_value)}</b>${h.evidence_id?' · '+preuveCourte(h.evidence_id):''}${h.resolved_fact_id?'':' · prédicat hors liste fermée : reste une hypothèse'}</small>${extrait(h)}`:''}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">Aucune hypothèse.</p>';
  const tSrc=l=>l.length?`<table class="mini"><thead><tr><th>Source</th><th>Nature</th><th>Portée</th><th>Confiance</th><th>Apport</th></tr></thead><tbody>${l.map(s=>`<tr><td>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.domain||s.url)}</a><small>${esc(courtTexte(s.url,80))}</small>`:esc(s.cited_entity||'—')}${s.passage?`<small>${esc(courtTexte(s.passage,120))}</small>`:''}</td><td>${esc(lib('source_kind',s.kind))}</td><td>${esc(lib('source_scope',s.scope))}</td><td>${s.confidence??'—'}</td><td>${esc(lib('contribution',s.contribution))}${s.source_date?`<small>${esc(s.source_date)}</small>`:''}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">Aucune.</p>';
  const groupes=(d.groupes||[]).map(g=>`<div class="bloc-g"><strong>${esc(courtTexte(g.label,110))}</strong><small>${nb(g.nombre_membres)} unité(s)${g.paternite?` · parent : ${g.paternite.parent_unit_id===u.id?'<b>cette unité</b>':`<button type="button" class="lien" data-unite="${esc(g.paternite.parent_unit_id)}">une autre unité</button>`} · critère : ${esc(lib('paternite',g.paternite.criterion))}`:' · pas de paternité'}</small>
    <div class="kvs">${cadreHtml(g.frame)}</div>${g.paternite?`<div class="kvs">${expliquerPaternite(g.paternite.detail,g.paternite.criterion)}</div>`:''}</div>`).join('')||'<p class="muted">Aucun groupe.</p>';
  const rel=(d.relations||[]).length?`<table class="mini"><tbody>${d.relations.map(r=>`<tr><td>${bd('relation',r.relation)}</td><td>${esc(courtTexte(r.autre_texte,160))}<small>${esc(r.reason||'')}${r.engine?' · '+esc(r.engine):''}</small></td></tr>`).join('')}</tbody></table>`:'';
  const req=(d.requetes||[]).length?`<table class="mini"><tbody>${d.requetes.map(q=>`<tr><td><b>${esc(q.text)}</b><small>${esc(libGabarit(q.template))}${q.cadre?.sujet?' · sujet : '+esc(q.cadre.sujet):''}</small></td><td>${bd('requete_statut',q.status)}${q.critical?' <span class="bd bad">Critique</span>':''}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">Aucune requête (voir « en attente de décomposition »).</p>';
  const appels=(d.appels_ibou||[]).length?`<table class="mini"><tbody>${d.appels_ibou.map(a=>`<tr><td>${esc(date(a.created))}<small>${esc(a.endpoint)}</small></td><td>${esc(courtTexte(a.query,120))}</td><td>${bd('appel',a.status)}<small>${a.units??0} u.</small></td></tr>`).join('')}</tbody></table>`:'';
  const preuves=(d.preuves||[]).length?`<div class="preuves">${d.preuves.map(p=>`<div class="preuve ${p.kept?'retenue':'ecartee'}"><div class="preuve-t"><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.title||p.domain||p.url)}</a> ${p.kept?'<span class="bd ok">Retenue</span>':'<span class="bd bad">Écartée</span>'}${p.kept||!p.reject_reason?'':` <small>${esc(lib('rejet',p.reject_reason))}</small>`} ${p.nli?bd('nli',p.nli):''}</div><small>${esc(p.domain||'')}${p.page_date?' · '+esc(p.page_date):''}${p.relevance!=null?' · pertinence '+Number(p.relevance).toFixed(2):''}${p.nli_score!=null?' · NLI '+Number(p.nli_score).toFixed(2):''}</small><blockquote>${esc(courtTexte(p.text,600))}</blockquote></div>`).join('')}</div>`:'<p class="muted">Aucune preuve collectée.</p>';
  const props=(d.propositions||[]).length?d.propositions.map(p=>`<div class="bloc-g"><strong>${esc(lib('motif',p.motif?.code))}</strong> ${bd('proposition',p.statut)}<small>criticité ${p.criticite??'—'} · ${esc(courtTexte(p.avant_apres?.avant,90))}</small><button type="button" class="button" data-ouvrir-revue="${esc(p.id)}">Ouvrir dans la revue</button></div>`).join(''):'<p class="muted">Aucune proposition.</p>';
  const liens=(d.liens_recommandes||[]).length?`<table class="mini"><tbody>${d.liens_recommandes.map(l=>`<tr><td>${esc(l.anchor||'—')}</td><td>${esc(courtTexte(l.reason||'',120))}</td><td>${bd('lien',l.status)}</td></tr>`).join('')}</tbody></table>`:'<p class="muted">Aucun lien recommandé.</p>';
  return `<div class="volet-tete"><div><div class="eyebrow">UNITÉ D’INFORMATION</div><h2>${esc(pg.titre||pg.url)}</h2><small class="mono">${esc(pg.url)}</small></div><button class="icon-button close-dialog" aria-label="Fermer">×</button></div>
  <div class="volet-badges">${bd('statut',u.status||'aucun')} ${bd('temporel',u.temporal_status)} ${u.obs_class?bd('classe',u.obs_class):''} ${u.verdict?bd('verdict',u.verdict):''} ${u.critical?'<span class="bd bad">Critique</span>':''} ${u.excluded?'<span class="bd mute">Exclue</span>':''} <span class="bd mute">${esc(libExtracteur(u.extractor))}</span>${u.status_suivi?' '+bd('statut',u.status_suivi,'suivi'):''}</div>
  <div class="volet-corps">
   <section class="phrase"><h3>Phrase source dans son paragraphe</h3>${para.html!=null?`<p class="para">${para.html}</p>${para.tronque?'<small class="muted">Paragraphe long : coupé autour de la phrase.</small>':''}`:`<p class="para"><mark>${esc(u.text)}</mark></p><small class="muted">Phrase introuvable dans la version de page (texte modifié depuis) : texte de l’unité affiché.</small>`}</section>
   ${section('Ancrage, types et règles',ancrage,{ouvert:true})}
   ${section('Faits (triplets)',tFaits,{n:faits.length,ouvert:true})}
   ${section('Hypothèses',hyp,{n:(d.hypotheses||[]).length})}
   ${section('Sources propres',tSrc(d.sources?.propres||[]),{n:(d.sources?.propres||[]).length,ouvert:true})}
   ${section('Sources héritées',tSrc(d.sources?.heritees||[]),{n:(d.sources?.heritees||[]).length})}
   ${section('Groupe et paternité',groupes+rel,{n:(d.groupes||[]).length,ouvert:true})}
   ${section('Requêtes Ibou',req+appels,{n:(d.requetes||[]).length})}
   ${section('Preuves',preuves,{n:(d.preuves||[]).length})}
   ${section('Propositions de correction',props,{n:(d.propositions||[]).length,ouvert:(d.propositions||[]).length>0})}
   ${section('Liens internes recommandés',liens,{n:(d.liens_recommandes||[]).length})}
  </div>`;
}
volet.addEventListener('close',()=>{});

/* ---------- Pages ---------- */
const P={offset:0,limite:50,q:'',dernieres:'1',filtre:'',data:null,suite:null,charge:false};
const pagesVue=$('#view-pages');
function alertePages(){
  const e=CH.etat,r=CH.reglages;if(!e)return '';
  const c=e.compteurs.pages,sansDate=c.pages_sans_date_publication??0;
  const l=[];
  if(sansDate>0)l.push(`<li><b>${nb(sansDate)} page(s) sur ${nb(c.pages)}</b> n’ont pas de date de publication${sansDate===c.pages?' (aucune n’en a)':''}. Sans elle, l’ancrage relatif et le critère de paternité par date sont impossibles.</li>`);
  if(r&&!(r.domaines_effectifs||[]).length)l.push('<li><b>Aucun domaine de site n’est réglé.</b> Les liens ne peuvent pas être classés en internes ou externes.</li>');
  return l.length?`<div class="alerte"><strong>Action prioritaire</strong><ul>${l.join('')}</ul><p>Renseignez les dates ci-dessous (enregistrées au fur et à mesure), réglez le domaine, puis relancez <b>M1 page</b> et <b>M3</b> pour recalculer ancrages et paternités.</p></div>`:'';
}
function squeletteDomaines(){
  const r=CH.reglages||{};
  return `<form id="domaines-form" class="domaines"><label>Domaines du site (séparés par des virgules)<input id="domaines" value="${esc((r.domaines||[]).join(', '))}" placeholder="exemple.fr, www.exemple.fr" aria-describedby="domaines-aide"></label>${btnE('Enregistrer','type="submit"')}<small id="domaines-aide">${(r.domaines_effectifs||[]).length?'Domaines internes effectifs : '+esc(r.domaines_effectifs.join(', ')):'Aucun domaine effectif. Les URL http des pages en fournissent quand elles existent.'}</small></form>`;
}
async function ouvrirPages(params={}){
  if(params.filtre!==undefined)P.filtre=params.filtre;
  if(!CH.reglages)try{await chargerReglages()}catch{}
  pagesVue.innerHTML=`${avisOccupe()}<div id="pages-alerte">${alertePages()}</div>
  <div class="barre"><input class="recherche" id="pages-q" type="search" placeholder="Rechercher une URL ou un titre" aria-label="Rechercher une page" value="${esc(P.q)}">
   <label class="inline"><input type="checkbox" id="pages-dernieres" ${P.dernieres==='1'?'checked':''}> Dernières versions seulement</label>
   ${selectF('filtre','Afficher',[['','Toutes les pages'],['sans_date','Sans date de publication'],['sans_maj','Sans date de mise à jour'],['propositions','Avec propositions en attente']],P.filtre)}
   <span class="espace"></span>${btnE('＋ Ajouter une page','data-import="1"')}${btnE('⇧ Importer un lot','data-import="lot"')}</div>
  <div id="pages-suite"></div>
  <div class="card padded-s">${squeletteDomaines()}</div>
  <section class="card"><div id="pages-liste"></div></section><div id="pages-pager"></div>`;
  rendreSuitePages();
  await chargerPages();
  if(params.focus==='domaines')$('#domaines')?.focus();
}
async function chargerPages(){
  const d=P.data=await api('chaine/pages?'+qs({offset:P.offset,limite:P.limite,q:P.q,dernieres:P.dernieres==='1'?'1':'',filtre:P.filtre}));
  rendrePages();
}
function rendrePages(){
  const d=P.data;if(!d||!$('#pages-liste'))return;
  const l=d.pages;
  $('#pages-liste').innerHTML=l.length?`<div class="table-wrap"><table class="dense"><thead><tr><th>Page</th><th>Publication</th><th>Mise à jour</th><th>Type</th><th>Sujets</th><th class="num">Unités</th><th title="Unités obsolètes (corrigées, ou périmées, remises en question, obsolètes sans confirmation) ÷ unités vérifiables (hors immuables, métadonnées, usages et archivées). Même calcul que l’export Excel.">Obsolescence</th><th class="num">Propositions</th></tr></thead><tbody>${l.map(p=>{
    const t=p.taux_obsolescence;
    return `<tr data-version="${esc(p.version_id)}"><td><strong>${esc(p.title)}</strong><small><span class="mono">${esc(courtTexte(p.url,60))}</span> · version du ${date(p.created)}${p.derniere?'':' · ancienne version'}${p.segmentee?'':' · non segmentée'}</small></td>
    <td><input type="date" class="date" data-champ="published" value="${esc(p.published||'')}" data-ecrit aria-label="Date de publication de ${esc(p.title)}"${CH.occupe?' disabled':''}></td>
    <td><input type="date" class="date" data-champ="updated" value="${esc(p.updated||'')}" data-ecrit aria-label="Date de mise à jour de ${esc(p.title)}"${CH.occupe?' disabled':''}></td>
    <td>${p.page_type?esc(lib('page_type',p.page_type)):'<span class="muted">Non typée</span>'}</td>
    <td class="sujets">${(p.sujets||[]).length?p.sujets.map(x=>`<span class="chip" title="appartenance ${String(x.degre).replace('.',',')}">${esc(courtTexte(x.libelle,28))}</span>`).join(''):'<span class="muted">—</span>'}</td>
    <td class="num">${p.unites?`<button type="button" class="lien" data-vue="unites" data-params='${esc(JSON.stringify({page:p.version_id}))}'>${nb(p.unites)}</button>`:'0'}</td>
    <td>${t?`<div class="obs"><progress max="1" value="${Number(t.taux).toFixed(3)}"></progress><b>${Math.round(100*t.taux)} %</b></div><small>${nb(t.obsoletes)} obsolète(s) sur ${nb(t.verifiables)} vérifiable(s)</small>`:p.unites?'<span class="muted">Aucune unité vérifiable</span>':'<span class="muted">Pas encore d’unités</span>'}</td>
    <td class="num">${p.propositions_en_attente?`<button type="button" class="lien" data-vue="revue" data-params='${esc(JSON.stringify({statut:'en_attente',page:p.version_id,titrePage:p.title}))}'>${nb(p.propositions_en_attente)}</button>`:'0'}</td></tr>`}).join('')}</tbody></table></div>`:videHtml('Aucune page dans cette vue','Changez le filtre ou ajoutez une page.');
  $('#pages-pager').innerHTML=pagerHtml('pages',d.total,d.offset,d.limite);
  appliquerOccupe();
}
function rendreSuitePages(){
  const s=P.suite,z=$('#pages-suite');if(!z)return;
  z.innerHTML=s?`<div class="notice suite"><span>Dates enregistrées (${nb(s.pages)} page(s)) : <b>${nb(s.unites)}</b> unité(s) à réancrer, <b>${nb(s.paternites)}</b> paternité(s) à recalculer. Relancez dans cet ordre pour appliquer.</span><span>${btnE('1. Relancer M1 page','data-lancer-suite="m1.page"')} ${btnE('2. Relancer M3','data-lancer-suite="m3"')}</span></div>`:'';
  appliquerOccupe();
}
pagesVue.addEventListener('change',e=>{
  const d=e.target.closest('input.date');
  if(d){
    const tr=d.closest('tr'),champ=d.dataset.champ;
    ecrire(async()=>{
      const r=await api('chaine/page',{version_id:tr.dataset.version,[champ]:d.value});
      P.suite=P.suite||{pages:0,unites:0,paternites:0};
      P.suite.pages++;P.suite.unites+=r.unites_a_reancrer||0;P.suite.paternites+=r.paternites_a_recalculer||0;
      rendreSuitePages();notify(`Date enregistrée. ${nb(r.unites_a_reancrer)} unité(s) à réancrer ; relancez M1 page puis M3.`);
      await chargerPages();CH.etat=await api('chaine/etat');$('#pages-alerte').innerHTML=alertePages();
    },d).then(r=>{if(r===null)chargerPages()});
    return;
  }
  const f=e.target.closest('[data-filtre]');if(f){P.filtre=f.value;P.offset=0;quiet(chargerPages)}
  if(e.target.id==='pages-dernieres'){P.dernieres=e.target.checked?'1':'0';P.offset=0;quiet(chargerPages)}
});
pagesVue.addEventListener('input',debounce(e=>{if(e.target.id==='pages-q'){P.q=e.target.value.trim();P.offset=0;quiet(chargerPages)}},300));
pagesVue.addEventListener('click',e=>{
  const b=e.target.closest('[data-import]');
  if(b){CH.depuisPages=true;if(b.dataset.import==='lot')bulkButton.click();else $('#import-dialog').showModal();return}
  const s=e.target.closest('[data-lancer-suite]');
  if(s)lancerEtape(s.dataset.lancerSuite).then(r=>{if(s.dataset.lancerSuite==='m3'&&r!==null)P.suite=null,rendreSuitePages()});
});
pagesVue.addEventListener('submit',e=>{
  if(e.target.id!=='domaines-form')return;e.preventDefault();
  ecrire(async()=>{
    const liste=$('#domaines').value.split(/[,\s]+/).map(x=>x.trim().toLowerCase()).filter(Boolean);
    const r=await api('chaine/reglages',{domaines:liste});CH.reglages=r.reglages;
    const c=r.changes?.domaines;
    notify(`Domaines enregistrés.${c?` ${nb(c.vers_lien_interne)} source(s) reclassée(s) en interne, ${nb(c.vers_lien_externe)} en externe.`:''}`);
    $('#pages-alerte').innerHTML=alertePages();$('#domaines-form').outerHTML=squeletteDomaines();appliquerOccupe();
    CH.etat=await api('chaine/etat');
  },e.submitter);
});
enregistrer('pages',{ouvrir:ouvrirPages,page:pas=>{P.offset=Math.max(0,P.offset+pas*P.limite);quiet(chargerPages)},
  actualiser:async()=>{if(surChamp(pagesVue))return;await chargerPages();$('#pages-alerte').innerHTML=alertePages()}});

/* ---------- Unités ---------- */
const U={offset:0,limite:50,f:{statut:'',classe:'',type:'',temporel:'',verdict:'',critique:'',page:'',groupe:'',sans_faits:'',exclues:'0',q:''},data:null,pages:null,groupeLabel:''};
const unitesVue=$('#view-unites');
async function ouvrirUnites(params={}){
  for(const k of Object.keys(params))if(k in U.f)U.f[k]=params[k];
  if(params&&Object.keys(params).length){for(const k of Object.keys(U.f))if(!(k in params)&&!['q','exclues'].includes(k))U.f[k]='';U.offset=0}
  if(!U.pages)try{U.pages=(await api('chaine/pages?limite=200&dernieres=0')).pages}catch{U.pages=[]}
  squeletteUnites();await chargerUnites();
}
function squeletteUnites(){
  const f=U.f,att=CH.etat?.compteurs?.m4?.en_attente_decomposition||0,raison=CH.etat?.prerequis?.['m1.faits'];
  unitesVue.innerHTML=`${att?`<div class="note"><b>${nb(att)} unité(s) à vérifier sont en attente de décomposition.</b> C’est un état normal : elles n’ont pas de faits tant que M1 faits n’a pas tourné avec un LLM (ou GLiREL).${raison?` Ce qui manque : ${esc(humain(raison))}.`:''} <button type="button" class="lien" data-attente="1">Les afficher</button></div>`:''}
  <div class="barre filtres">
   <input class="recherche" id="unites-q" type="search" placeholder="Rechercher dans le texte des unités" aria-label="Rechercher une unité" value="${esc(f.q)}">
   ${selectF('statut','Statut',[['','Tous'],...['a_verifier','a_verifier_faible','sourcee','dependante','non_verifiable','historique','aucun'].map(v=>[v,lib('statut',v)])],f.statut)}
   ${selectF('classe','Obsolescence',[...optionsDe('classe',['immuable','etat_courant','annonce','metadonnee','usage']),['inconnu','Inconnue (traitée comme vérifiable)']],f.classe)}
   ${selectF('type','Type d’affirmation',optionsDe('type',['information_source','interpretation','connaissance_generale','sans_source','opinion','aucun']),f.type)}
   ${selectF('temporel','Statut temporel',optionsDe('temporel',['a_jour','perimee_par_date','remise_en_question','obsolete_historique']),f.temporel)}
   ${selectF('verdict','Verdict',optionsDe('verdict',['confirmee','corrigee','completee','insuffisante','conflictuelle']),f.verdict)}
   ${selectF('critique','Criticité',[['','Toutes'],['1','Critiques'],['0','Plus tard']],f.critique)}
   ${selectF('page','Page',[['','Toutes les pages'],...(U.pages||[]).map(p=>[p.version_id,courtTexte(p.title,48)+(p.derniere?'':' (ancienne version)')])],f.page)}
   <label class="inline"><input type="checkbox" data-filtre="sans_faits" ${f.sans_faits==='1'?'checked':''}> Sans faits</label>
   ${f.groupe?`<span class="puce">Groupe : ${esc(courtTexte(U.groupeLabel||f.groupe,40))} <button type="button" class="lien" data-retirer="groupe" aria-label="Retirer le filtre de groupe">×</button></span>`:''}
   <button type="button" class="button subtle" data-raz="1">Réinitialiser</button></div>
  <section class="card"><div id="unites-liste"></div></section><div id="unites-pager"></div>`;
}
async function chargerUnites(){
  const f=U.f;
  const d=U.data=await api('chaine/unites?'+qs({offset:U.offset,limite:U.limite,statut:f.statut,classe:f.classe,type:f.type,temporel:f.temporel,verdict:f.verdict,critique:f.critique,page:f.page,groupe:f.groupe,sans_faits:f.sans_faits,exclues:f.exclues==='0'?'':f.exclues,q:f.q}));
  rendreUnites();
}
function rendreUnites(){
  const d=U.data;if(!d||!$('#unites-liste'))return;
  $('#unites-liste').innerHTML=d.unites.length?`<div class="table-wrap"><table class="dense lignes"><thead><tr><th>Unité</th><th>Statut</th><th>Obsolescence</th><th>Type</th><th>Ancrage</th><th class="num">Faits</th><th class="num">Sources</th><th class="num">Prop.</th></tr></thead><tbody>${d.unites.map(u=>{
    const attente=!u.faits&&(u.status==='a_verifier'||u.status==='a_verifier_faible');
    return `<tr tabindex="0" data-unite="${esc(u.id)}" role="button" aria-label="Ouvrir l’unité"><td class="texte"><span>${esc(courtTexte(u.text,230))}</span><small>${esc(courtTexte(u.title,70))}${u.critical?' · <b class="rouge">critique</b>':''}${u.excluded?' · exclue':''}</small></td>
    <td>${bd('statut',u.status||'aucun')}${u.verdict?'<br>'+bd('verdict',u.verdict):''}</td>
    <td>${bd('classe',u.obs_class||'inconnu')}${u.temporal_status&&u.temporal_status!=='a_jour'?'<br>'+bd('temporel',u.temporal_status):''}</td>
    <td>${esc(lib('type',u.type||'aucun'))}<small>${esc(lib('sourcing',u.sourcing||'aucun'))}</small></td>
    <td>${u.anchor_date?esc(u.anchor_date):'—'}<small>${esc(lib('ancre',u.anchor_kind))}</small></td>
    <td class="num">${u.faits?nb(u.faits):attente?'<span class="bd mute" title="Pas de faits tant que M1 faits n’a pas tourné avec un LLM : état normal">en attente</span>':'—'}</td>
    <td class="num">${nb(u.sources)}</td><td class="num">${u.propositions?nb(u.propositions):'—'}</td></tr>`}).join('')}</tbody></table></div>`:videHtml('Aucune unité ne correspond','Changez les filtres ou réinitialisez-les.');
  $('#unites-pager').innerHTML=pagerHtml('unites',d.total,d.offset,d.limite);
}
unitesVue.addEventListener('change',e=>{
  const f=e.target.closest('[data-filtre]');if(!f)return;
  U.f[f.dataset.filtre]=f.type==='checkbox'?(f.checked?'1':''):f.value;U.offset=0;quiet(chargerUnites);
});
unitesVue.addEventListener('input',debounce(e=>{if(e.target.id==='unites-q'){U.f.q=e.target.value.trim();U.offset=0;quiet(chargerUnites)}},300));
unitesVue.addEventListener('click',e=>{
  if(e.target.closest('[data-raz]')){for(const k of Object.keys(U.f))U.f[k]=k==='exclues'?'0':'';U.offset=0;squeletteUnites();quiet(chargerUnites);return}
  if(e.target.closest('[data-attente]')){U.f.statut='a_verifier';U.f.sans_faits='1';U.offset=0;squeletteUnites();quiet(chargerUnites);return}
  const r=e.target.closest('[data-retirer]');if(r){U.f[r.dataset.retirer]='';U.offset=0;squeletteUnites();quiet(chargerUnites);return}
  const t=e.target.closest('tr[data-unite]');if(t)ouvrirUnite(t.dataset.unite);
});
unitesVue.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches('tr[data-unite]'))ouvrirUnite(e.target.dataset.unite)});
enregistrer('unites',{ouvrir:ouvrirUnites,page:pas=>{U.offset=Math.max(0,U.offset+pas*U.limite);quiet(chargerUnites)},
  actualiser:async()=>{if(surChamp(unitesVue)||volet.open)return;await chargerUnites()}});

/* ---------- Groupes et paternité ---------- */
const G={onglet:'a_arbitrer',offset:0,limite:40,f:{relation:'',paternite:'',q:'',page:''},data:null,sel:null,detail:null};
const groupesVue=$('#view-groupes');
const ONGLETS_G=[['a_arbitrer','À arbitrer'],['multi','Multi-pages'],['tous','Tous les groupes'],['liens','Liens internes recommandés']];
const nbLiens=()=>{const n=CH.etat?.compteurs?.m3?.liens_recommandes||{};return n.proposee||0};
async function ouvrirGroupes(params={}){
  if(params.onglet)G.onglet=params.onglet;
  if(params.onglet){G.offset=0;G.sel=null}
  squeletteGroupes();
  if(G.onglet==='liens'){await rendreLiens();return}
  await chargerGroupes();
}
function squeletteGroupes(){
  const n=CH.etat?.compteurs?.m3?.paternite?.a_arbitrer||0;
  groupesVue.innerHTML=`${avisOccupe()}
  <div class="tabs onglets" role="tablist">${ONGLETS_G.map(([k,t])=>`<button type="button" role="tab" aria-selected="${k===G.onglet}" class="${k===G.onglet?'selected':''}" data-onglet-g="${k}">${t}${k==='a_arbitrer'?` <span class="vs-n">${nb(n)}</span>`:k==='liens'?` <span class="vs-n">${nb(nbLiens())}</span>`:''}</button>`).join('')}</div>
  <div id="groupes-corps">${G.onglet==='liens'?'':`<div class="barre filtres">
   <input class="recherche" id="groupes-q" type="search" placeholder="Rechercher dans le libellé" aria-label="Rechercher un groupe" value="${esc(G.f.q)}">
   ${G.onglet==='tous'?selectF('paternite','Paternité',[['','Toutes'],...['sujet_principal','nombre_sources','date_publication','arbitrage_humain','a_arbitrer','aucune'].map(v=>[v,lib('paternite',v)])],G.f.paternite):''}
   ${selectF('relation','Relation',optionsDe('relation',['equivalence','precision','remplacement','conflit','faux_conflit','sans_rapport'],'Toutes'),G.f.relation)}</div>
   <div class="maitre-detail"><section class="card maitre"><div id="groupes-liste"></div><div id="groupes-pager"></div></section><section class="card detail" id="groupe-detail" aria-live="polite"></section></div>`}</div>`;
}
function paramsGroupes(){
  const p={offset:G.offset,limite:G.limite,q:G.f.q,relation:G.f.relation};
  if(G.onglet==='a_arbitrer')p.paternite='a_arbitrer';
  else if(G.onglet==='multi'){p.multi='1';p.paternite=G.f.paternite}
  else p.paternite=G.f.paternite;
  return qs(p);
}
async function chargerGroupes(garder=false){
  const d=G.data=await api('chaine/groupes?'+paramsGroupes());
  if(!garder||!d.groupes.some(g=>g.id===G.sel))G.sel=d.groupes[0]?.id||null;
  rendreListeGroupes();
  if(G.sel)await chargerDetailGroupe(G.sel);else $('#groupe-detail').innerHTML=videHtml(G.onglet==='a_arbitrer'?'Aucun groupe à arbitrer':'Aucun groupe','Rien ne correspond à ce filtre.');
}
function rendreListeGroupes(){
  const d=G.data;if(!d||!$('#groupes-liste'))return;
  $('#groupes-liste').innerHTML=d.groupes.length?`<ul class="liste-g">${d.groupes.map(g=>`<li><button type="button" class="${g.id===G.sel?'sel':''}" data-groupe="${esc(g.id)}"><span class="g-label">${esc(courtTexte(g.label,120))}</span><small>${nb(g.membres)} unité(s) · ${nb(g.pages)} page(s) · ${g.paternite?esc(lib('paternite',g.paternite)):'sans paternité'}${Object.keys(g.relations||{}).length?' · '+Object.entries(g.relations).map(([k,n])=>`${n} ${esc(lib('relation',k).toLowerCase())}`).join(', '):''}</small></button></li>`).join('')}</ul>`:'';
  $('#groupes-pager').innerHTML=pagerHtml('groupes',d.total,d.offset,d.limite);
}
async function chargerDetailGroupe(id){
  G.sel=id;rendreListeGroupes();
  const zone=$('#groupe-detail');zone.innerHTML='<div class="vide">Chargement…</div>';
  try{
    const m=await api('chaine/unites?'+qs({groupe:id,limite:50}));
    let pat=null,rel=[];
    if(m.unites[0]){const u=await api('chaine/unite/'+m.unites[0].id);const g=(u.groupes||[]).find(x=>x.id===id);pat=g?.paternite||null;rel=u.relations||[]}
    G.detail={m,pat,rel,id};
    if(G.sel===id)rendreDetailGroupe();
  }catch(e){zone.innerHTML=videHtml('Chargement impossible',e.message)}
}
function rendreDetailGroupe(){
  const {m,pat,rel,id}=G.detail,g=(G.data?.groupes||[]).find(x=>x.id===id)||{},zone=$('#groupe-detail');
  const arbitrer=!pat||pat.criterion==='a_arbitrer';
  zone.innerHTML=`<div class="detail-tete"><h3>${esc(courtTexte(g.label||'Groupe',200))}</h3>
   <div class="kvs">${cadreHtml(g.frame)}</div>
   <p class="paternite-etat">${arbitrer?'<span class="bd warn">À arbitrer</span> Aucun critère automatique ne désigne l’unité parente.':`<span class="bd ok">${esc(lib('paternite',pat.criterion))}</span> Parent actuel : ${pat.human?'choisi par vous':'désigné automatiquement'}.`}</p>
   ${pat?.detail?`<div class="kvs"><span class="kv-t">Pourquoi</span>${expliquerPaternite(pat.detail,pat.criterion)}</div>`:''}</div>
   <div class="table-wrap"><table class="dense"><thead><tr><th>Unité candidate</th><th>Page</th><th>Ancrage</th><th class="num">Centralité</th><th class="num">Sources</th><th></th></tr></thead><tbody>${m.unites.map(u=>{
    const parent=pat&&pat.parent_unit_id===u.id;
    return `<tr class="${parent?'parent':''}"><td class="texte"><button type="button" class="lien texte-b" data-unite="${esc(u.id)}">${esc(courtTexte(u.text,170))}</button></td><td>${esc(courtTexte(u.title,40))}</td><td>${u.anchor_date?esc(u.anchor_date):'—'}<small>${esc(lib('ancre',u.anchor_kind))}</small></td><td class="num">${u.centrality!=null?Number(u.centrality).toFixed(2).replace('.',','):'—'}</td><td class="num">${nb(u.sources)}</td>
    <td class="num">${parent?'<span class="bd ok">Parent</span>':btnE('Choisir comme parent',`data-parent="${esc(u.id)}"`,'button primary petit')}</td></tr>`}).join('')}</tbody></table></div>
   ${m.total>m.unites.length?`<p class="muted">${nb(m.total-m.unites.length)} autre(s) membre(s) non affichés.</p>`:''}
   ${rel.length?`<h4>Relations</h4><ul class="rel">${rel.map(r=>`<li>${bd('relation',r.relation)} ${esc(courtTexte(r.autre_texte,170))}</li>`).join('')}</ul>`:''}
   <p class="aide"><button type="button" class="lien" data-vue="unites" data-params='${esc(JSON.stringify({groupe:id}))}'>Voir toutes les unités du groupe</button></p>`;
  appliquerOccupe();
}
const L={offset:0,limite:40,statut:'proposee',data:null};
const MOTIFS_LIEN={source_manquante:'L’information vient de la page parente du groupe, qui n’est pas citée : un lien la sourcerait.',lien_parent_absent:'Même information que la page parente du groupe, sans lien vers elle.'};
function raisonLien(r){
  let d=r;if(typeof r==='string'){try{d=JSON.parse(r)}catch{return humain(r)}}
  if(!d||typeof d!=='object')return humain(String(r||''));
  return MOTIFS_LIEN[d.motif]||humain(String(d.motif||'').replace(/_/g,' '));
}
async function rendreLiens(){
  $('#groupes-corps').innerHTML=`<div class="barre filtres">${selectF('lien-statut','Statut',[['','Tous'],...['proposee','acceptee','appliquee','rejetee'].map(v=>[v,lib('lien',v)])],L.statut)}
   <span class="aide">Liens recommandés par M3 vers la page parente d’un groupe (source manquante ou reprise d’une information). Chacun est proposé dans la revue (motif « Lien interne ») : c’est là que vous l’acceptez ou le rejetez.</span></div>
   <section class="card"><div id="liens-liste"><div class="vide">Chargement…</div></div></section><div id="liens-pager"></div>`;
  const d=L.data=await api('chaine/liens?'+qs({offset:L.offset,limite:L.limite,statut:L.statut}));
  const ps=d.par_statut||{};
  $('#liens-liste').innerHTML=d.liens.length?`<div class="table-wrap"><table class="dense"><thead><tr><th>Phrase de la page source</th><th>Page source</th><th>Lien vers</th><th>Pourquoi</th><th>Statut</th><th></th></tr></thead><tbody>${d.liens.map(l=>`<tr>
    <td class="texte"><button type="button" class="lien texte-b" data-unite="${esc(l.unit_id)}">${esc(courtTexte(l.texte_unite||l.anchor,170))}</button>${l.anchor&&l.anchor!==l.texte_unite?`<small>ancre : « ${esc(courtTexte(l.anchor,80))} »</small>`:''}</td>
    <td>${esc(courtTexte(l.source_titre||l.source_url||'—',40))}</td><td><b>${esc(courtTexte(l.cible_titre||l.cible_url||'—',40))}</b></td>
    <td><small>${esc(raisonLien(l.reason))}</small></td>
    <td>${bd('lien',l.status)}${l.proposition_statut&&!['proposee'].includes(l.proposition_statut)?`<small>proposition : ${esc(lib('proposition',l.proposition_statut).toLowerCase())}</small>`:''}</td>
    <td>${l.proposition_id?`<button type="button" class="button petit" data-vue="revue" data-params='${esc(JSON.stringify({id:l.proposition_id}))}'>Décider dans la revue</button>`:''}</td></tr>`).join('')}</tbody></table></div>`
    :videHtml('Aucun lien recommandé dans cette vue',Object.keys(ps).length?'Changez le filtre de statut.':'M3 n’a encore recommandé aucun lien interne.');
  $('#liens-pager').innerHTML=pagerHtml('groupes',d.total,d.offset,d.limite);
}
groupesVue.addEventListener('click',e=>{
  const o=e.target.closest('[data-onglet-g]');if(o){G.onglet=o.dataset.ongletG;G.offset=0;G.sel=null;ouvrirGroupes({});return}
  const g=e.target.closest('[data-groupe]');if(g){chargerDetailGroupe(g.dataset.groupe);return}
  const p=e.target.closest('[data-parent]');
  if(p)ecrire(async()=>{
    const r=await api('chaine/paternite',{group_id:G.sel,unit_id:p.dataset.parent});
    notify('Parent enregistré (arbitrage humain). Les sources sont repropagées.');
    CH.etat=await api('chaine/etat');rendreCompteurs(CH.pulse);
    squeletteGroupes();await chargerGroupes(G.onglet!=='a_arbitrer');
  },p);
});
groupesVue.addEventListener('change',e=>{
  const f=e.target.closest('[data-filtre]');if(!f)return;
  if(f.dataset.filtre==='lien-statut'){L.statut=f.value;L.offset=0;quiet(rendreLiens);return}
  G.f[f.dataset.filtre]=f.value;G.offset=0;G.sel=null;quiet(()=>chargerGroupes());
});
groupesVue.addEventListener('input',debounce(e=>{if(e.target.id==='groupes-q'){G.f.q=e.target.value.trim();G.offset=0;G.sel=null;quiet(()=>chargerGroupes())}},300));
groupesVue.addEventListener('click',e=>{const u=e.target.closest('[data-unite]');if(u&&!u.closest('#volet-unite')&&u.tagName==='BUTTON')ouvrirUnite(u.dataset.unite)});
enregistrer('groupes',{ouvrir:ouvrirGroupes,page:pas=>{if(G.onglet==='liens'){L.offset=Math.max(0,L.offset+pas*L.limite);quiet(rendreLiens);return}G.offset=Math.max(0,G.offset+pas*G.limite);G.sel=null;quiet(()=>chargerGroupes())},
  actualiser:async()=>{if(G.onglet==='liens'||surChamp(groupesVue))return;await chargerGroupes(true)}});
